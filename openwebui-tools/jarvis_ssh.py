"""
title: Jarvis SSH (confirmed, allowlisted)
author: Jarvis Stack
version: 1.0.0
description: Calls the stack's allowlisted SSH service after an interactive confirmation.
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
        request_timeout_seconds: int = Field(default=120, ge=5, le=300)
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
            return json.dumps({"error": "Jarvis SSH tool API key is not configured."})

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
                    "error": "SSH tool request was rejected.",
                    "status": exc.code,
                    "detail": body,
                }
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            detail = exc.reason if hasattr(exc, "reason") else exc
            return json.dumps(
                {
                    "error": "SSH tool service is unavailable.",
                    "detail": str(detail),
                }
            )

    async def list_ssh_targets(self) -> str:
        """
        List administrator-approved SSH target aliases and safe command examples.
        """
        return await asyncio.to_thread(self._request, "GET", "/v1/ssh/targets")

    async def run_ssh_command(
        self,
        target: str,
        command: str,
        __event_call__: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """
        Ask the user to confirm, then run one allowlisted command over SSH.

        :param target: Exact target alias returned by list_ssh_targets.
        :param command: Complete command matching the administrator allowlist.
        """
        if __event_call__ is None:
            return json.dumps(
                {"status": "refused", "reason": "Interactive confirmation is unavailable."}
            )

        try:
            confirmed = await asyncio.wait_for(
                __event_call__(
                    {
                        "type": "confirmation",
                        "data": {
                            "title": f"Confirm SSH command on {target}",
                            "message": f"Run exactly this command?\n\n{command}",
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
            "/v1/ssh/execute",
            {"target": target, "command": command},
        )
