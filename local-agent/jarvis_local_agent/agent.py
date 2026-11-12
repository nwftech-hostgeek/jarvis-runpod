from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from websockets.exceptions import ConnectionClosed

from . import __version__
from .config import advertised_targets, dump_status_blob, host_allowed, load_config
from .confirm import confirm_console
from .safe_cmds import ping_host, resolve_name, run_local_command
from .ssh_exec import ensure_command_allowed, execute_ssh, looks_mutating


logger = logging.getLogger("jarvis-local-agent")


def tools_url_to_ws(tools_url: str) -> str:
    parsed = urlparse(tools_url)
    if parsed.scheme in ("http", "https"):
        scheme = "wss" if parsed.scheme == "https" else "ws"
    elif parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    else:
        raise ValueError("tools_url must be http(s) or ws(s)")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/local-agent/ws"):
        path = f"{path}/v1/local-agent/ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class LocalAgent:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self._targets_by_name = {
            t["name"]: t for t in advertised_targets(self.config)
        }

    def _confirm(self, title: str, message: str) -> bool:
        timeout = int(self.config.get("confirm_timeout_seconds") or 120)
        return confirm_console(title, message, timeout_seconds=timeout)

    def handle_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            host = str(params.get("host") or "")
            if not host_allowed(host, self.config.get("host_allowlist") or []):
                raise PermissionError("host is not on the local allowlist")
            return ping_host(host)

        if method == "resolve":
            name = str(params.get("name") or "")
            if not host_allowed(name, self.config.get("host_allowlist") or []):
                raise PermissionError("name is not on the local allowlist")
            return resolve_name(name)

        if method == "local_run":
            command = str(params.get("command") or "")
            if params.get("require_confirm", True):
                if not self._confirm(
                    "Local command",
                    f"Run this command on the Windows PC?\n\n{command}",
                ):
                    raise PermissionError("user denied local_run")
            return run_local_command(
                command,
                list(self.config.get("local_commands") or []),
            )

        if method == "ssh_execute":
            return self._ssh_execute(params)

        if method == "confirm":
            ok = self._confirm(
                str(params.get("title") or "Confirm"),
                str(params.get("message") or ""),
            )
            return {"confirmed": ok}

        raise ValueError(f"unknown method: {method}")

    def _ssh_execute(self, params: dict[str, Any]) -> dict[str, Any]:
        target_name = str(params.get("target") or "")
        host = str(params.get("host") or "")
        user = str(params.get("user") or "")
        port = int(params.get("port") or 22)
        command = str(params.get("command") or "")
        timeout_seconds = int(params.get("timeout_seconds") or 30)
        connect_timeout = int(params.get("connect_timeout") or 15)

        if not host_allowed(host, self.config.get("host_allowlist") or []):
            raise PermissionError("host is not on the local allowlist")

        local_target = self._targets_by_name.get(target_name)
        if local_target is not None:
            ensure_command_allowed(local_target, command)
            mutating = looks_mutating(command, local_target)
        else:
            # Cloud already enforced allowlist; still gate destructive-looking cmds.
            mutating = looks_mutating(command, None) or bool(
                params.get("require_confirm")
            )

        if mutating or params.get("require_confirm"):
            if not self._confirm(
                f"SSH on {target_name or host}",
                f"user={user} host={host} port={port}\n\n{command}",
            ):
                raise PermissionError("user denied SSH command")

        return execute_ssh(
            host=host,
            user=user,
            port=port,
            command=command,
            timeout_seconds=timeout_seconds,
            connect_timeout=connect_timeout,
            identity_file=str(self.config.get("identity_file") or ""),
            known_hosts=str(self.config.get("known_hosts") or ""),
        )

    async def run_forever(self) -> None:
        tools_url = str(self.config.get("tools_url") or "")
        token = str(self.config.get("token") or "")
        if not tools_url:
            raise SystemExit(
                "tools_url / JARVIS_TOOLS_URL is not set. See docs/local-agent.md"
            )
        if len(token) < 20:
            raise SystemExit(
                "JARVIS_LOCAL_AGENT_TOKEN is missing or too short (need >= 20 chars)."
            )

        ws_url = tools_url_to_ws(tools_url)
        logger.info("connecting to %s as %s", ws_url, self.config.get("agent_id"))
        logger.info("config summary: %s", dump_status_blob(self.config))

        ssl_context = ssl.create_default_context()
        backoff = 1.0
        while True:
            try:
                await self._session(ws_url, token, ssl_context)
                backoff = 1.0
            except ConnectionClosed as exc:
                logger.warning("websocket closed: code=%s", exc.code)
            except Exception as exc:
                logger.warning(
                    "connection error type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(
        self,
        ws_url: str,
        token: str,
        ssl_context: ssl.SSLContext,
    ) -> None:
        headers = {"X-Jarvis-Local-Agent-Token": token}
        connect_kwargs: dict[str, Any] = {
            "additional_headers": headers,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 2_000_000,
        }
        if ws_url.startswith("wss://"):
            connect_kwargs["ssl"] = ssl_context

        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            hello = {
                "type": "hello",
                "agent_id": self.config.get("agent_id"),
                "version": __version__,
                "capabilities": ["ssh", "ping", "resolve", "local_run", "confirm"],
                "targets": advertised_targets(self.config),
            }
            await ws.send(json.dumps(hello))
            welcome_raw = await asyncio.wait_for(ws.recv(), timeout=20)
            welcome = json.loads(welcome_raw)
            if welcome.get("type") != "welcome":
                raise RuntimeError("expected welcome from jarvis-tools")
            logger.info(
                "paired with jarvis-tools agent_id=%s",
                welcome.get("agent_id"),
            )

            while True:
                raw = await ws.recv()
                message = json.loads(raw)
                msg_type = message.get("type")
                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                    continue
                if msg_type == "pong":
                    continue
                if msg_type != "rpc":
                    logger.warning("ignored message type=%s", msg_type)
                    continue

                rpc_id = str(message.get("id") or "")
                method = str(message.get("method") or "")
                params = message.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                try:
                    # SSH/paramiko and console confirm are blocking — offload.
                    result = await asyncio.to_thread(
                        self.handle_rpc, method, params
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "rpc_result",
                                "id": rpc_id,
                                "ok": True,
                                "result": result,
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "rpc_failed method=%s error_type=%s",
                        method,
                        type(exc).__name__,
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "rpc_result",
                                "id": rpc_id,
                                "ok": False,
                                "error": str(exc),
                            }
                        )
                    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Never attach token/secret filters that print env; keep handlers quiet.
    agent = LocalAgent()
    asyncio.run(agent.run_forever())
