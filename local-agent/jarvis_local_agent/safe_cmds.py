from __future__ import annotations

import platform
import re
import shutil
import socket
import subprocess
import time
from typing import Any


SAFE_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:%-]{0,254}$")


def ping_host(host: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    if not SAFE_HOST_RE.fullmatch(host):
        raise ValueError("invalid host")

    started = time.monotonic()
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_seconds))), host]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
            check=False,
        )
        ok = proc.returncode == 0
        return {
            "host": host,
            "ok": ok,
            "method": "icmp",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "detail": (proc.stdout or proc.stderr or "").strip()[:500],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Fall back to TCP/22 reachability if ICMP is blocked.
        try:
            with socket.create_connection((host, 22), timeout=timeout_seconds):
                return {
                    "host": host,
                    "ok": True,
                    "method": "tcp/22",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "detail": f"icmp failed ({type(exc).__name__}); tcp/22 open",
                }
        except OSError as tcp_exc:
            return {
                "host": host,
                "ok": False,
                "method": "tcp/22",
                "duration_ms": round((time.monotonic() - started) * 1000),
                "detail": f"icmp/tcp failed: {type(tcp_exc).__name__}",
            }


def resolve_name(name: str) -> dict[str, Any]:
    if not SAFE_HOST_RE.fullmatch(name):
        raise ValueError("invalid name")
    started = time.monotonic()
    infos = socket.getaddrinfo(name, None)
    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    return {
        "name": name,
        "addresses": addresses,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def run_local_command(command: str, patterns: list[str], timeout: int = 30) -> dict[str, Any]:
    if not patterns:
        raise PermissionError("local_commands allowlist is empty")
    if command != command.strip() or "\n" in command or "\r" in command:
        raise PermissionError("invalid command formatting")
    if not any(re.fullmatch(pattern, command) for pattern in patterns):
        raise PermissionError("command does not match local_commands allowlist")

    started = time.monotonic()
    system = platform.system().lower()
    if system == "windows":
        # Prefer PowerShell for Windows-native commands; fall back to cmd.
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell:
            argv = [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
        else:
            argv = ["cmd", "/c", command]
    else:
        argv = ["/bin/sh", "-c", command]

    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = (proc.stdout or "")[:65536]
    stderr = (proc.stderr or "")[:65536]
    return {
        "exit_status": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "truncated": len(proc.stdout or "") > 65536 or len(proc.stderr or "") > 65536,
    }
