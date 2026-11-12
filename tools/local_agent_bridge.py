"""Outbound local-agent WebSocket registry and RPC helpers for jarvis-tools.

The Windows companion dials in; the Pod never dials the LAN. Secrets are never
logged. At most one agent session is active.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState


logger = logging.getLogger("jarvis-tools.local-agent")

LOCAL_AGENT_TOKEN = os.getenv("JARVIS_LOCAL_AGENT_TOKEN", "") or os.getenv(
    "LOCAL_AGENT_TOKEN", ""
)
LOCAL_AGENT_ENABLED = (
    os.getenv("LOCAL_AGENT_ENABLED", "true").lower() in ("1", "true", "yes")
)
RPC_DEFAULT_TIMEOUT = float(os.getenv("LOCAL_AGENT_RPC_TIMEOUT", "90"))


def token_configured() -> bool:
    return len(LOCAL_AGENT_TOKEN) >= 20 and not LOCAL_AGENT_TOKEN.startswith(
        "change-me"
    )


def verify_agent_token(candidate: str | None) -> bool:
    if not token_configured() or not candidate:
        return False
    return hmac.compare_digest(candidate, LOCAL_AGENT_TOKEN)


@dataclass
class AgentSession:
    websocket: WebSocket
    agent_id: str
    connected_at: float
    capabilities: list[str] = field(default_factory=list)
    advertised_targets: list[dict[str, Any]] = field(default_factory=list)
    version: str = ""
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LocalAgentHub:
    def __init__(self) -> None:
        self._session: AgentSession | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        session = self._session
        if session is None:
            return False
        return session.websocket.client_state == WebSocketState.CONNECTED

    def status(self) -> dict[str, Any]:
        session = self._session
        if not self.connected or session is None:
            return {
                "enabled": LOCAL_AGENT_ENABLED,
                "token_configured": token_configured(),
                "connected": False,
                "agent_id": None,
                "capabilities": [],
                "advertised_target_count": 0,
                "version": None,
                "connected_at": None,
            }
        return {
            "enabled": LOCAL_AGENT_ENABLED,
            "token_configured": token_configured(),
            "connected": True,
            "agent_id": session.agent_id,
            "capabilities": list(session.capabilities),
            "advertised_target_count": len(session.advertised_targets),
            "advertised_targets": [
                {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "examples": t.get("examples", []),
                    "mutating_commands_enabled": bool(
                        t.get("mutating_commands_enabled", False)
                    ),
                }
                for t in session.advertised_targets
                if isinstance(t, dict) and t.get("name")
            ],
            "version": session.version or None,
            "connected_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(session.connected_at)
            ),
        }

    def advertised_targets(self) -> list[dict[str, Any]]:
        session = self._session
        if not self.connected or session is None:
            return []
        return list(session.advertised_targets)

    async def attach(self, websocket: WebSocket, hello: dict[str, Any]) -> AgentSession:
        agent_id = str(hello.get("agent_id") or uuid.uuid4())
        capabilities = hello.get("capabilities") or []
        if not isinstance(capabilities, list):
            capabilities = []
        targets = hello.get("targets") or []
        if not isinstance(targets, list):
            targets = []
        version = str(hello.get("version") or "")

        session = AgentSession(
            websocket=websocket,
            agent_id=agent_id[:64],
            connected_at=time.time(),
            capabilities=[str(c)[:64] for c in capabilities[:32]],
            advertised_targets=[t for t in targets if isinstance(t, dict)][:100],
            version=version[:32],
        )

        async with self._lock:
            old = self._session
            self._session = session
            if old is not None and old.websocket is not websocket:
                await self._fail_pending(old, "replaced by a newer local agent session")
                try:
                    await old.websocket.close(code=4000)
                except Exception:
                    pass

        logger.info(
            "local_agent_connected agent_id=%s capabilities=%s targets=%s",
            session.agent_id,
            ",".join(session.capabilities) or "-",
            len(session.advertised_targets),
        )
        return session

    async def detach(self, session: AgentSession) -> None:
        async with self._lock:
            if self._session is session:
                self._session = None
        await self._fail_pending(session, "local agent disconnected")
        logger.info("local_agent_disconnected agent_id=%s", session.agent_id)

    async def _fail_pending(self, session: AgentSession, reason: str) -> None:
        for fut in list(session.pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        session.pending.clear()

    async def handle_incoming(self, session: AgentSession, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "pong":
            return
        if msg_type == "ping":
            await session.websocket.send_json({"type": "pong"})
            return
        if msg_type == "hello":
            targets = message.get("targets") or []
            if isinstance(targets, list):
                session.advertised_targets = [
                    t for t in targets if isinstance(t, dict)
                ][:100]
            caps = message.get("capabilities") or []
            if isinstance(caps, list):
                session.capabilities = [str(c)[:64] for c in caps[:32]]
            return
        if msg_type == "rpc_result":
            rpc_id = str(message.get("id") or "")
            fut = session.pending.pop(rpc_id, None)
            if fut is not None and not fut.done():
                fut.set_result(message)
            return
        logger.warning("local_agent_unknown_message type=%s", msg_type)

    async def rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session = self._session
        if session is None or not self.connected:
            raise RuntimeError("No local agent is connected.")

        rpc_id = str(uuid.uuid4())
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        session.pending[rpc_id] = fut
        payload = {
            "type": "rpc",
            "id": rpc_id,
            "method": method,
            "params": params,
        }
        try:
            async with session.lock:
                await session.websocket.send_json(payload)
            result = await asyncio.wait_for(
                fut, timeout=timeout or RPC_DEFAULT_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            session.pending.pop(rpc_id, None)
            raise RuntimeError(
                f"Local agent RPC timed out for method={method}"
            ) from exc
        except Exception:
            session.pending.pop(rpc_id, None)
            raise

        if not result.get("ok", False):
            raise RuntimeError(str(result.get("error") or "local agent RPC failed"))
        body = result.get("result")
        if not isinstance(body, dict):
            raise RuntimeError("local agent returned an unexpected result shape")
        return body


hub = LocalAgentHub()


async def run_agent_socket(websocket: WebSocket) -> None:
    """Authenticate, register, and pump messages for one local-agent connection."""
    if not LOCAL_AGENT_ENABLED:
        await websocket.close(code=4403)
        return
    if not token_configured():
        await websocket.close(code=4403)
        return

    token = websocket.headers.get("x-jarvis-local-agent-token")
    if not token:
        auth = websocket.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        token = websocket.query_params.get("token")

    if not verify_agent_token(token):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    session: AgentSession | None = None
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if not isinstance(raw, dict) or raw.get("type") != "hello":
            await websocket.close(code=4400)
            return
        session = await hub.attach(websocket, raw)
        await websocket.send_json(
            {
                "type": "welcome",
                "agent_id": session.agent_id,
                "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                await hub.handle_incoming(session, message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(
            "local_agent_socket_error error_type=%s",
            type(exc).__name__,
        )
    finally:
        if session is not None:
            await hub.detach(session)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
