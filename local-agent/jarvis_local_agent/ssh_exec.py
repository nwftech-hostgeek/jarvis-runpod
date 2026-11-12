from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import paramiko


SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\r\n]")
MUTATING_COMMAND_RE = re.compile(
    r"(^|\s)(sudo|su|rm|mv|cp|dd|mkfs(?:\.\w+)?|mount|umount|reboot|"
    r"shutdown|poweroff|halt|kill|pkill|killall|chmod|chown|useradd|"
    r"userdel|usermod|passwd|apt|apt-get|dnf|yum|apk|pip|npm|curl|wget|"
    r"iptables|nft|firewall-cmd)(\s|$)|"
    r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask)\b",
    re.IGNORECASE,
)
MAX_OUTPUT_BYTES = 262144


def append_limited(buffer: bytearray, data: bytes, limit: int) -> bool:
    remaining = max(0, limit - len(buffer))
    buffer.extend(data[:remaining])
    return len(data) > remaining


def command_matches(patterns: list[str], command: str) -> bool:
    return any(re.fullmatch(pattern, command) for pattern in patterns)


def ensure_command_allowed(target: dict[str, Any], command: str) -> None:
    if (
        command != command.strip()
        or "\x00" in command
        or "\r" in command
        or "\n" in command
    ):
        raise PermissionError("invalid command formatting")

    allowed = list(target.get("allowed_commands") or [])
    mutating = list(target.get("mutating_commands") or [])
    if not allowed and not mutating:
        raise PermissionError("target has no allowed commands")

    if command_matches(mutating, command):
        if not target.get("allow_mutating"):
            raise PermissionError("mutating commands are disabled for this target")
        return

    if not command_matches(allowed, command):
        raise PermissionError("command does not match allowlist")

    if SHELL_METACHAR_RE.search(command) or MUTATING_COMMAND_RE.search(command):
        raise PermissionError(
            "read-only allowlist matched a command that looks mutating"
        )


def looks_mutating(command: str, target: dict[str, Any] | None = None) -> bool:
    if target and command_matches(list(target.get("mutating_commands") or []), command):
        return True
    return bool(MUTATING_COMMAND_RE.search(command) or SHELL_METACHAR_RE.search(command))


def execute_ssh(
    *,
    host: str,
    user: str,
    port: int,
    command: str,
    timeout_seconds: int,
    connect_timeout: int,
    identity_file: str,
    known_hosts: str,
) -> dict[str, Any]:
    key_path = Path(identity_file)
    if not key_path.is_file():
        raise FileNotFoundError(f"SSH identity file missing: {key_path}")

    client = paramiko.SSHClient()
    hosts_path = Path(known_hosts)
    if hosts_path.is_file():
        client.load_host_keys(str(hosts_path))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    started = time.monotonic()
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            key_filename=str(key_path),
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            allow_agent=True,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH transport did not become active")

        channel = transport.open_session(timeout=connect_timeout)
        channel.exec_command(command)

        stdout = bytearray()
        stderr = bytearray()
        truncated = False
        deadline = time.monotonic() + timeout_seconds
        while True:
            while channel.recv_ready():
                truncated |= append_limited(
                    stdout, channel.recv(32768), MAX_OUTPUT_BYTES
                )
            while channel.recv_stderr_ready():
                truncated |= append_limited(
                    stderr, channel.recv_stderr(32768), MAX_OUTPUT_BYTES
                )
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            if time.monotonic() >= deadline:
                channel.close()
                raise TimeoutError(
                    f"Command exceeded the {timeout_seconds}-second timeout"
                )
            time.sleep(0.05)

        exit_status = channel.recv_exit_status()
        channel.close()
        return {
            "exit_status": exit_status,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "output_truncated": truncated,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    finally:
        client.close()
