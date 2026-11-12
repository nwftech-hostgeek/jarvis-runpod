from __future__ import annotations

import hmac
import logging
import os
import re
import shlex
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Annotated

import paramiko
import yaml
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("jarvis-tools")

CONFIG_PATH = Path(os.getenv("SSH_TARGETS_FILE", "/config/ssh-targets.yaml"))
KNOWN_HOSTS_PATH = Path(
    os.getenv("SSH_KNOWN_HOSTS_FILE", "/config/ssh_known_hosts")
)
DEFAULT_KEY_PATH = Path(
    os.getenv("SSH_PRIVATE_KEY_PATH", "/run/secrets/ssh/id_ed25519")
)
SOCKS_HOST = os.getenv("SSH_SOCKS5_HOST", "tailscale")
SOCKS_PORT = int(os.getenv("SSH_SOCKS5_PORT", "1055"))
DEFAULT_TIMEOUT = int(os.getenv("SSH_DEFAULT_TIMEOUT", "30"))
MAX_OUTPUT_BYTES = int(os.getenv("SSH_MAX_OUTPUT_BYTES", "262144"))
ENABLE_MUTATING_COMMANDS = (
    os.getenv("SSH_ENABLE_MUTATING_COMMANDS", "false").lower() == "true"
)
TOOL_API_KEY = os.getenv("TOOL_API_KEY", "")

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
SAFE_USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,63}$")
SAFE_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:%-]{0,254}$")
SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\r\n]")
MUTATING_COMMAND_RE = re.compile(
    r"(^|\s)(sudo|su|rm|mv|cp|dd|mkfs(?:\.\w+)?|mount|umount|reboot|"
    r"shutdown|poweroff|halt|kill|pkill|killall|chmod|chown|useradd|"
    r"userdel|usermod|passwd|apt|apt-get|dnf|yum|apk|pip|npm|curl|wget|"
    r"iptables|nft|firewall-cmd)(\s|$)|"
    r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask)\b",
    re.IGNORECASE,
)


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=64)
    port: int = Field(default=22, ge=1, le=65535)
    description: str = Field(default="", max_length=300)
    identity_file: str | None = None
    allowed_commands: list[str] = Field(default_factory=list, max_length=100)
    mutating_commands: list[str] = Field(default_factory=list, max_length=100)
    examples: list[str] = Field(default_factory=list, max_length=20)
    connect_timeout: int = Field(default=15, ge=1, le=60)
    command_timeout: int = Field(default=30, ge=1, le=300)
    allow_mutating: bool = False

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not SAFE_HOST_RE.fullmatch(value):
            raise ValueError("must be a DNS name or unbracketed IP address")
        return value

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        if not SAFE_USER_RE.fullmatch(value):
            raise ValueError("contains unsupported characters")
        return value

    @field_validator("allowed_commands", "mutating_commands")
    @classmethod
    def validate_patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > 1000:
                raise ValueError("command regular expression is too long")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid command regular expression: {exc}") from exc
        return values


class TargetsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: dict[str, TargetConfig] = Field(default_factory=dict)

    @field_validator("targets")
    @classmethod
    def validate_target_names(
        cls, targets: dict[str, TargetConfig]
    ) -> dict[str, TargetConfig]:
        invalid = [name for name in targets if not SAFE_NAME_RE.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid target name(s): {', '.join(invalid)}")
        return targets


class TargetSummary(BaseModel):
    name: str
    description: str
    examples: list[str]
    mutating_commands_enabled: bool


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        min_length=1,
        max_length=64,
        description="Allowlisted target alias returned by list_ssh_targets.",
    )
    command: str = Field(
        min_length=1,
        max_length=2000,
        description="A complete command that must match the target allowlist.",
    )
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)


class ExecuteResponse(BaseModel):
    execution_id: str
    target: str
    exit_status: int
    stdout: str
    stderr: str
    duration_ms: int
    output_truncated: bool


class CommandTimedOut(Exception):
    pass


def load_targets() -> dict[str, TargetConfig]:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"SSH target configuration is missing: {CONFIG_PATH}")

    try:
        raw: Any = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        document = TargetsDocument.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RuntimeError(f"Invalid SSH target configuration: {exc}") from exc

    return document.targets


def target_key_path(target: TargetConfig) -> Path:
    path = Path(target.identity_file) if target.identity_file else DEFAULT_KEY_PATH
    resolved = path.resolve()
    secrets_root = Path("/run/secrets/ssh").resolve()
    try:
        resolved.relative_to(secrets_root)
    except ValueError as exc:
        raise RuntimeError("SSH identity files must be under /run/secrets/ssh") from exc
    return resolved


def authorize(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(auto_error=False)),
    ],
) -> None:
    if not TOOL_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tool API authentication is not configured.",
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, TOOL_API_KEY)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tool API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def ensure_command_allowed(target: TargetConfig, command: str) -> None:
    if (
        command != command.strip()
        or "\x00" in command
        or "\r" in command
        or "\n" in command
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Commands may not contain leading/trailing whitespace or control characters.",
        )

    if not target.allowed_commands and not target.mutating_commands:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This target has no allowed commands.",
        )

    read_only_match = any(
        re.fullmatch(pattern, command) for pattern in target.allowed_commands
    )
    mutating_match = any(
        re.fullmatch(pattern, command) for pattern in target.mutating_commands
    )

    if mutating_match:
        if not (ENABLE_MUTATING_COMMANDS and target.allow_mutating):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Mutating commands are disabled.",
            )
        return

    if not read_only_match:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Command does not match this target's allowlist.",
        )

    if (
        SHELL_METACHAR_RE.search(command) or MUTATING_COMMAND_RE.search(command)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "A read-only allowlist matched a command that appears mutating; "
                "classify it under mutating_commands instead."
            ),
        )


def append_limited(buffer: bytearray, data: bytes, limit: int) -> bool:
    remaining = max(0, limit - len(buffer))
    buffer.extend(data[:remaining])
    return len(data) > remaining


def execute_ssh(
    target_name: str,
    target: TargetConfig,
    command: str,
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    if not KNOWN_HOSTS_PATH.is_file():
        raise RuntimeError(f"Known-hosts file is missing: {KNOWN_HOSTS_PATH}")

    key_path = target_key_path(target)
    if not key_path.is_file():
        raise RuntimeError(f"SSH private key is missing for target {target_name}")

    proxy_address = f"{SOCKS_HOST}:{SOCKS_PORT}"
    proxy_command = " ".join(
        [
            "nc",
            "-w",
            str(target.connect_timeout),
            "-X",
            "5",
            "-x",
            shlex.quote(proxy_address),
            shlex.quote(target.host),
            str(target.port),
        ]
    )

    proxy = paramiko.ProxyCommand(proxy_command)
    client = paramiko.SSHClient()
    client.load_host_keys(str(KNOWN_HOSTS_PATH))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.user,
            key_filename=str(key_path),
            sock=proxy,
            timeout=target.connect_timeout,
            banner_timeout=target.connect_timeout,
            auth_timeout=target.connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH transport did not become active")

        channel = transport.open_session(timeout=target.connect_timeout)
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
                raise CommandTimedOut(
                    f"Command exceeded the {timeout_seconds}-second timeout"
                )
            time.sleep(0.05)

        exit_status = channel.recv_exit_status()
        channel.close()
        return (
            exit_status,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            truncated,
        )
    finally:
        client.close()
        proxy.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(TOOL_API_KEY) < 20 or TOOL_API_KEY.startswith("change-me"):
        raise RuntimeError("TOOL_API_KEY must contain at least 20 characters")
    load_targets()
    yield


app = FastAPI(
    title="Jarvis Allowlisted SSH Tools",
    version="1.0.0",
    description=(
        "Executes administrator-approved SSH commands on named Tailnet targets. "
        "Targets, host keys, identities, and full-command regular expressions "
        "are configured by the operator; arbitrary hosts are never accepted."
    ),
    lifespan=lifespan,
)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, Any]:
    checks: dict[str, bool] = {
        "api_key": len(TOOL_API_KEY) >= 20
        and not TOOL_API_KEY.startswith("change-me"),
        "config": False,
        "known_hosts": KNOWN_HOSTS_PATH.is_file(),
        "private_key": DEFAULT_KEY_PATH.is_file(),
        "tailscale_proxy": False,
    }
    target_count = 0
    try:
        target_count = len(load_targets())
        checks["config"] = True
    except RuntimeError:
        pass

    try:
        with socket.create_connection((SOCKS_HOST, SOCKS_PORT), timeout=1):
            checks["tailscale_proxy"] = True
    except OSError:
        pass

    required = checks["api_key"] and checks["config"] and checks["known_hosts"]
    return {
        "status": "ok" if required else "degraded",
        "checks": checks,
        "target_count": target_count,
    }


@app.get(
    "/v1/ssh/targets",
    response_model=list[TargetSummary],
    operation_id="list_ssh_targets",
    dependencies=[Depends(authorize)],
    summary="List approved SSH targets",
)
def list_targets() -> list[TargetSummary]:
    """List aliases and safe example commands available to the assistant."""
    targets = load_targets()
    return [
        TargetSummary(
            name=name,
            description=target.description,
            examples=target.examples,
            mutating_commands_enabled=(
                ENABLE_MUTATING_COMMANDS and target.allow_mutating
            ),
        )
        for name, target in sorted(targets.items())
    ]


@app.post(
    "/v1/ssh/execute",
    response_model=ExecuteResponse,
    operation_id="run_ssh_command",
    dependencies=[Depends(authorize)],
    summary="Run one allowlisted SSH command",
)
async def run_command(request: ExecuteRequest) -> ExecuteResponse:
    """Run a full-match allowlisted command after normal tool confirmation."""
    targets = load_targets()
    target = targets.get(request.target)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown SSH target alias.",
        )

    try:
        ensure_command_allowed(target, request.command)
    except HTTPException:
        logger.warning("ssh_command_denied target=%s", request.target)
        raise
    timeout_seconds = min(
        request.timeout_seconds or DEFAULT_TIMEOUT,
        target.command_timeout,
        300,
    )
    execution_id = str(uuid.uuid4())
    started = time.monotonic()

    try:
        exit_status, stdout, stderr, truncated = await run_in_threadpool(
            execute_ssh,
            request.target,
            target,
            request.command,
            timeout_seconds,
        )
    except CommandTimedOut as exc:
        logger.warning(
            "ssh_timeout execution_id=%s target=%s timeout=%s",
            execution_id,
            request.target,
            timeout_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except paramiko.BadHostKeyException as exc:
        logger.error(
            "ssh_host_key_rejected execution_id=%s target=%s",
            execution_id,
            request.target,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SSH host key did not match the pinned known-hosts entry.",
        ) from exc
    except paramiko.AuthenticationException as exc:
        logger.error(
            "ssh_authentication_failed execution_id=%s target=%s",
            execution_id,
            request.target,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SSH authentication failed.",
        ) from exc
    except (paramiko.SSHException, OSError, RuntimeError) as exc:
        logger.error(
            "ssh_connection_failed execution_id=%s target=%s error_type=%s",
            execution_id,
            request.target,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SSH connection or execution failed; inspect tool-service logs.",
        ) from exc

    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info(
        "ssh_complete execution_id=%s target=%s exit_status=%s duration_ms=%s truncated=%s",
        execution_id,
        request.target,
        exit_status,
        duration_ms,
        truncated,
    )
    return ExecuteResponse(
        execution_id=execution_id,
        target=request.target,
        exit_status=exit_status,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        output_truncated=truncated,
    )
