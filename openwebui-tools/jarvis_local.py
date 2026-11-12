"""
title: Jarvis Local Agent (LAN ping / resolve / status)
author: Jarvis Stack
version: 1.0.0
description: Transparent helpers that call jarvis-tools local-agent routes when the Windows companion is online.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Callable

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        tool_service_url: str = Field(
            default="http://jarvis-tools:8090",
            description=(
                "Backend-reachable Jarvis tool-service URL. Use "
                "http://127.0.0.1:8090 in the RunPod all-in-one image."
            ),
        )
        api_key: str = Field(
            default="",
            description="Bearer token matching TOOL_API_KEY.",
        )
        request_timeout_seconds: int = Field(default=60, ge=5, le=180)
        confirmation_timeout_seconds: int = Field(default=120, ge=10, le=600)

    def __init__(self):
        self.valves = self.Valves()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        if not self.valves.api_key:
            return json.dumps(
                {"error": "Jarvis tool API key is not configured."}
            )

        url = f"{self.valves.tool_service_url.rstrip('/')}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.valves.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.valves.request_timeout_seconds,
            ) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            return json.dumps(
                {
                    "error": "Local-agent tool request was rejected.",
                    "status": exc.code,
                    "detail": body,
                }
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            detail = exc.reason if hasattr(exc, "reason") else exc
            return json.dumps(
                {
                    "error": "SSH/tool service is unavailable.",
                    "detail": str(detail),
                }
            )

    async def local_agent_status(self) -> str:
        """
        Check whether the Windows local companion is connected (LAN bridge).
        """
        return await asyncio.to_thread(
            self._request, "GET", "/v1/local-agent/status"
        )

    async def local_ping(self, host: str) -> str:
        """
        Ping a host from the Windows PC network via the local agent.

        :param host: DNS name or IP on the PC/LAN path.
        """
        return await asyncio.to_thread(
            self._request, "POST", "/v1/local/ping", {"host": host}
        )

    async def local_resolve(self, name: str) -> str:
        """
        Resolve a DNS name using the Windows PC resolver.

        :param name: Hostname to resolve.
        """
        return await asyncio.to_thread(
            self._request, "POST", "/v1/local/resolve", {"name": name}
        )

    async def local_run(
        self,
        command: str,
        __event_call__: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """
        Ask the user to confirm, then run one allowlisted local PC command.

        :param command: Complete command matching the local_commands allowlist.
        """
        if __event_call__ is None:
            return json.dumps(
                {
                    "status": "refused",
                    "reason": "Interactive confirmation is unavailable.",
                }
            )

        try:
            confirmed = await asyncio.wait_for(
                __event_call__(
                    {
                        "type": "confirmation",
                        "data": {
                            "title": "Confirm local PC command",
                            "message": f"Run exactly this command on the PC?\n\n{command}",
                        },
                    }
                ),
                timeout=self.valves.confirmation_timeout_seconds,
            )
        except TimeoutError:
            return json.dumps(
                {"status": "refused", "reason": "Confirmation timed out."}
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return json.dumps(
                {"status": "refused", "reason": "Confirmation failed."}
            )

        if confirmed is not True:
            return json.dumps(
                {"status": "cancelled", "reason": "The user did not confirm."}
            )

        return await asyncio.to_thread(
            self._request,
            "POST",
            "/v1/local/run",
            {"command": command},
        )
