from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shlex
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Annotated, Literal

import paramiko
import yaml
from fastapi import Depends, FastAPI, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from local_agent_bridge import (
    LOCAL_AGENT_ENABLED,
    hub as local_agent_hub,
    run_agent_socket,
    token_configured as local_agent_token_configured,
)

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
HTTP_FETCH_TIMEOUT = float(os.getenv("HTTP_FETCH_TIMEOUT", "8"))
HTTP_FETCH_MAX_BYTES = int(os.getenv("HTTP_FETCH_MAX_BYTES", "65536"))
WEATHER_TIMEOUT = float(os.getenv("WEATHER_TIMEOUT", "8"))

# Strict host allowlist for outbound public-info HTTPS GET. No secrets required.
ALLOWED_HTTP_HOSTS = frozenset(
    {
        "api.open-meteo.com",
        "geocoding-api.open-meteo.com",
        "wttr.in",
    }
)

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

WMO_WEATHER_CODES = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "slight rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "slight snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


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
    # pod = Tailscale SOCKS from the cloud tool service.
    # local = Windows local-agent companion (LAN-transparent SSH).
    route: Literal["pod", "local"] = "pod"

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
    route: Literal["pod", "local"] = "pod"
    available: bool = True


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


class WeatherRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(
        min_length=1,
        max_length=120,
        description="City, region, or place name to look up (Open-Meteo geocoding).",
    )
    units: Literal["fahrenheit", "celsius"] = Field(
        default="fahrenheit",
        description="Temperature units for the summary.",
    )


class WeatherResponse(BaseModel):
    location: str
    latitude: float
    longitude: float
    timezone: str
    units: str
    temperature: float
    feels_like: float | None = None
    humidity_pct: float | None = None
    wind_speed: float | None = None
    wind_units: str | None = None
    conditions: str
    weather_code: int | None = None
    observed_at: str | None = None
    source: str = "open-meteo"


class HttpGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=8,
        max_length=2000,
        description=(
            "HTTPS URL on an allowlisted public-info host "
            "(api.open-meteo.com, geocoding-api.open-meteo.com, wttr.in)."
        ),
    )

    @model_validator(mode="after")
    def validate_allowlisted_url(self) -> HttpGetRequest:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "https":
            raise ValueError("only https URLs are allowed")
        if parsed.username or parsed.password:
            raise ValueError("URLs must not contain credentials")
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_HTTP_HOSTS:
            raise ValueError(
                "host is not on the public-info allowlist: "
                + ", ".join(sorted(ALLOWED_HTTP_HOSTS))
            )
        if parsed.port not in (None, 443):
            raise ValueError("only default HTTPS port 443 is allowed")
        return self


class HttpGetResponse(BaseModel):
    url: str
    status_code: int
    content_type: str | None = None
    body: str
    truncated: bool
    bytes_read: int


class LocalPingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not SAFE_HOST_RE.fullmatch(value):
            raise ValueError("must be a DNS name or unbracketed IP address")
        return value


class LocalResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not SAFE_HOST_RE.fullmatch(value):
            raise ValueError("must be a DNS name or unbracketed IP address")
        return value


class LocalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(
        min_length=1,
        max_length=500,
        description="Allowlisted local companion command (requires confirmation).",
    )


class CommandTimedOut(Exception):
    pass


class ExternalFetchError(Exception):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    title="Jarvis Allowlisted Tools",
    version="1.2.0",
    description=(
        "Allowlisted SSH on named Tailnet or Windows local-agent targets, plus "
        "public-info HTTPS helpers (Open-Meteo weather and a strict host "
        "allowlist). Arbitrary hosts are never accepted. Weather/public-info "
        "tools do not require user confirmation; SSH and local_run still should."
    ),
    lifespan=lifespan,
)


def _require_local_agent() -> None:
    if not LOCAL_AGENT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent support is disabled on this deployment.",
        )
    if not local_agent_token_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JARVIS_LOCAL_AGENT_TOKEN is not configured.",
        )
    if not local_agent_hub.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent is not connected.",
        )


async def execute_via_local_agent(
    *,
    target_name: str,
    target: TargetConfig,
    command: str,
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    result = await local_agent_hub.rpc(
        "ssh_execute",
        {
            "target": target_name,
            "host": target.host,
            "user": target.user,
            "port": target.port,
            "command": command,
            "timeout_seconds": timeout_seconds,
            "connect_timeout": target.connect_timeout,
            "require_confirm": bool(
                ENABLE_MUTATING_COMMANDS and target.allow_mutating
            ),
        },
        timeout=float(timeout_seconds + target.connect_timeout + 30),
    )
    return (
        int(result.get("exit_status", 1)),
        str(result.get("stdout", "")),
        str(result.get("stderr", "")),
        bool(result.get("output_truncated", False)),
    )


def target_from_agent_advertisement(name: str) -> TargetConfig | None:
    for item in local_agent_hub.advertised_targets():
        if item.get("name") != name:
            continue
        try:
            return TargetConfig.model_validate(
                {
                    "host": item.get("host"),
                    "user": item.get("user", "jarvis"),
                    "port": item.get("port", 22),
                    "description": item.get("description", ""),
                    "allowed_commands": item.get("allowed_commands") or [],
                    "mutating_commands": item.get("mutating_commands") or [],
                    "examples": item.get("examples") or [],
                    "connect_timeout": item.get("connect_timeout", 15),
                    "command_timeout": item.get("command_timeout", 30),
                    "allow_mutating": bool(item.get("allow_mutating", False)),
                    "route": "local",
                    "identity_file": None,
                }
            )
        except ValidationError:
            logger.warning("local_agent_bad_target name=%s", name)
            return None
    return None


def _http_get_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    accept: str = "application/json,text/plain,*/*;q=0.1",
) -> tuple[int, str | None, bytes, bool]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ExternalFetchError("only https URLs are allowed", status_code=400)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HTTP_HOSTS:
        raise ExternalFetchError("host is not allowlisted", status_code=403)
    if parsed.username or parsed.password:
        raise ExternalFetchError("URLs must not contain credentials", status_code=400)
    if parsed.port not in (None, 443):
        raise ExternalFetchError("only HTTPS port 443 is allowed", status_code=400)

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": accept,
            "User-Agent": "jarvis-tools/1.1 (+allowlisted-public-info)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(response.status)
            content_type = response.headers.get("Content-Type")
            chunks = bytearray()
            truncated = False
            while True:
                piece = response.read(4096)
                if not piece:
                    break
                remaining = max_bytes - len(chunks)
                if remaining <= 0:
                    truncated = True
                    break
                if len(piece) > remaining:
                    chunks.extend(piece[:remaining])
                    truncated = True
                    break
                chunks.extend(piece)
            return status_code, content_type, bytes(chunks), truncated
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes)
        return int(exc.code), exc.headers.get("Content-Type"), body, False
    except TimeoutError as exc:
        raise ExternalFetchError("upstream request timed out", status_code=504) from exc
    except urllib.error.URLError as exc:
        raise ExternalFetchError(
            f"upstream request failed: {exc.reason}",
            status_code=502,
        ) from exc


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _load_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_decode_body(raw))
    except json.JSONDecodeError as exc:
        raise ExternalFetchError(f"{label} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ExternalFetchError(f"{label} returned unexpected JSON shape")
    return parsed


def fetch_weather(location: str, units: str) -> WeatherResponse:
    query = urllib.parse.urlencode(
        {
            "name": location.strip(),
            "count": 1,
            "language": "en",
            "format": "json",
        }
    )
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?{query}"
    geo_status, _, geo_raw, _ = _http_get_bytes(
        geo_url,
        timeout=WEATHER_TIMEOUT,
        max_bytes=HTTP_FETCH_MAX_BYTES,
    )
    if geo_status >= 400:
        raise ExternalFetchError(
            f"geocoding failed with HTTP {geo_status}",
            status_code=502,
        )
    geo = _load_json_object(geo_raw, label="geocoding")
    results = geo.get("results") or []
    if not results:
        raise ExternalFetchError(
            f"no geocoding match for location: {location.strip()}",
            status_code=404,
        )
    place = results[0]
    latitude = float(place["latitude"])
    longitude = float(place["longitude"])
    place_name = str(place.get("name") or location.strip())
    admin = place.get("admin1")
    country = place.get("country_code") or place.get("country")
    bits = [place_name]
    if admin:
        bits.append(str(admin))
    if country:
        bits.append(str(country))
    display = ", ".join(bits)

    temp_unit = "fahrenheit" if units == "fahrenheit" else "celsius"
    wind_unit = "mph" if units == "fahrenheit" else "kmh"
    forecast_query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:.5f}",
            "longitude": f"{longitude:.5f}",
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            "temperature_unit": temp_unit,
            "wind_speed_unit": wind_unit,
            "timezone": "auto",
        }
    )
    forecast_url = f"https://api.open-meteo.com/v1/forecast?{forecast_query}"
    forecast_status, _, forecast_raw, _ = _http_get_bytes(
        forecast_url,
        timeout=WEATHER_TIMEOUT,
        max_bytes=HTTP_FETCH_MAX_BYTES,
    )
    if forecast_status >= 400:
        raise ExternalFetchError(
            f"forecast failed with HTTP {forecast_status}",
            status_code=502,
        )
    forecast = _load_json_object(forecast_raw, label="forecast")
    current = forecast.get("current") or {}
    if not isinstance(current, dict) or "temperature_2m" not in current:
        raise ExternalFetchError("forecast response missing current conditions")

    code = current.get("weather_code")
    try:
        weather_code = int(code) if code is not None else None
    except (TypeError, ValueError):
        weather_code = None
    conditions = (
        WMO_WEATHER_CODES.get(weather_code, f"code {weather_code}")
        if weather_code is not None
        else "unknown"
    )

    def _maybe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return WeatherResponse(
        location=display,
        latitude=latitude,
        longitude=longitude,
        timezone=str(forecast.get("timezone") or place.get("timezone") or "UTC"),
        units=temp_unit,
        temperature=float(current["temperature_2m"]),
        feels_like=_maybe_float(current.get("apparent_temperature")),
        humidity_pct=_maybe_float(current.get("relative_humidity_2m")),
        wind_speed=_maybe_float(current.get("wind_speed_10m")),
        wind_units=wind_unit,
        conditions=conditions,
        weather_code=weather_code,
        observed_at=str(current.get("time")) if current.get("time") else None,
    )


def _probe_http(url: str, timeout: float = 1.0) -> str:
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as response:
            return "ok" if 200 <= int(response.status) < 300 else "down"
    except Exception:
        return "down"


def collect_host_stats() -> dict[str, Any]:
    """Sanitized host telemetry for the orb HUD. Never includes secrets."""
    mem: dict[str, Any] = {"total_mb": 0, "used_mb": 0, "used_pct": 0.0}
    try:
        info = {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if ":" in line
        }
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = max(total - avail, 0)
        if total:
            mem = {
                "total_mb": round(total / 1024),
                "used_mb": round(used / 1024),
                "used_pct": round((used * 100) / total, 1),
            }
    except OSError:
        pass

    cpu: dict[str, Any] = {"load1": 0.0, "cpus": 1, "approx_pct": 0.0}
    try:
        load1 = float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
        cpus = os.cpu_count() or 1
        cpu = {
            "load1": load1,
            "cpus": cpus,
            "approx_pct": round(min(100.0, (load1 / cpus) * 100), 1),
        }
    except OSError:
        pass

    gpu: dict[str, Any] = {"available": False}
    try:
        import subprocess

        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        line = (proc.stdout or "").strip().splitlines()[:1]
        if line:
            parts = [p.strip() for p in line[0].split(",")]
            if len(parts) >= 5:
                gpu = {
                    "available": True,
                    "util_pct": float(parts[0] or 0),
                    "mem_util_pct": float(parts[1] or 0),
                    "vram_used_mb": float(parts[2] or 0),
                    "vram_total_mb": float(parts[3] or 0),
                    "temp_c": float(parts[4] or 0),
                }
    except Exception:
        pass

    uptime_sec = 0
    try:
        uptime_sec = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except OSError:
        pass

    ui_port = os.getenv("RUNPOD_UI_PORT", "3000")
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_sec": uptime_sec,
        "services": {
            "open_webui": _probe_http(f"http://127.0.0.1:{ui_port}/health"),
            "llama": _probe_http("http://127.0.0.1:8080/health"),
            "voice": _probe_http("http://127.0.0.1:8000/health"),
            "tools": "ok",
        },
        "cpu": cpu,
        "memory": mem,
        "gpu": gpu,
    }


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

    checks["local_agent_token"] = local_agent_token_configured()
    checks["local_agent_connected"] = local_agent_hub.connected
    required = checks["api_key"] and checks["config"] and checks["known_hosts"]
    return {
        "status": "ok" if required else "degraded",
        "checks": checks,
        "target_count": target_count,
        "local_agent": local_agent_hub.status(),
    }


@app.get(
    "/v1/stats",
    operation_id="host_stats",
    dependencies=[Depends(authorize)],
    summary="Sanitized host telemetry for Jarvis HUD",
)
def host_stats() -> dict[str, Any]:
    """Return CPU/GPU/memory/service health without secrets or identities."""
    payload = collect_host_stats()
    payload["local_agent"] = {
        "connected": local_agent_hub.connected,
        "agent_id": local_agent_hub.status().get("agent_id"),
    }
    return payload


@app.get(
    "/v1/local-agent/status",
    operation_id="local_agent_status",
    dependencies=[Depends(authorize)],
    summary="Windows local-agent connection status",
)
def local_agent_status() -> dict[str, Any]:
    """Report whether the LAN companion is online (no secrets)."""
    return local_agent_hub.status()


@app.websocket("/v1/local-agent/ws")
async def local_agent_ws(websocket: WebSocket) -> None:
    """Local companion dial-in. Auth via JARVIS_LOCAL_AGENT_TOKEN."""
    await run_agent_socket(websocket)


@app.post(
    "/v1/weather",
    response_model=WeatherResponse,
    operation_id="get_weather",
    dependencies=[Depends(authorize)],
    summary="Current weather for a place (Open-Meteo, no confirmation)",
)
async def get_weather(request: WeatherRequest) -> WeatherResponse:
    """Geocode a place and return current conditions. Safe public-info tool."""
    try:
        return await run_in_threadpool(fetch_weather, request.location, request.units)
    except ExternalFetchError as exc:
        logger.warning("weather_failed location=%r error=%s", request.location, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post(
    "/v1/http/get",
    response_model=HttpGetResponse,
    operation_id="http_get_allowlisted",
    dependencies=[Depends(authorize)],
    summary="HTTPS GET on an allowlisted public-info host (no confirmation)",
)
async def http_get_allowlisted(request: HttpGetRequest) -> HttpGetResponse:
    """Fetch a small HTTPS response from a hard-coded public-info host allowlist."""
    try:
        status_code, content_type, raw, truncated = await run_in_threadpool(
            _http_get_bytes,
            request.url,
            timeout=HTTP_FETCH_TIMEOUT,
            max_bytes=HTTP_FETCH_MAX_BYTES,
        )
    except ExternalFetchError as exc:
        logger.warning("http_get_failed error=%s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    body = _decode_body(raw)
    logger.info(
        "http_get_complete host=%s status=%s bytes=%s truncated=%s",
        urllib.parse.urlparse(request.url).hostname,
        status_code,
        len(raw),
        truncated,
    )
    return HttpGetResponse(
        url=request.url,
        status_code=status_code,
        content_type=content_type,
        body=body,
        truncated=truncated,
        bytes_read=len(raw),
    )


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
    agent_online = local_agent_hub.connected
    summaries: list[TargetSummary] = []
    for name, target in sorted(targets.items()):
        available = True
        if target.route == "local" and not agent_online:
            available = False
        summaries.append(
            TargetSummary(
                name=name,
                description=target.description,
                examples=target.examples,
                mutating_commands_enabled=(
                    ENABLE_MUTATING_COMMANDS and target.allow_mutating
                ),
                route=target.route,
                available=available,
            )
        )

    seen = {item.name for item in summaries}
    for item in local_agent_hub.advertised_targets():
        name = str(item.get("name") or "")
        if not name or name in seen or not SAFE_NAME_RE.fullmatch(name):
            continue
        summaries.append(
            TargetSummary(
                name=name,
                description=str(item.get("description") or "Local-agent target"),
                examples=[str(e) for e in (item.get("examples") or [])[:20]],
                mutating_commands_enabled=bool(
                    item.get("mutating_commands_enabled", False)
                ),
                route="local",
                available=agent_online,
            )
        )
        seen.add(name)
    return summaries


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
        target = target_from_agent_advertisement(request.target)
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
        if target.route == "local":
            _require_local_agent()
            exit_status, stdout, stderr, truncated = await execute_via_local_agent(
                target_name=request.target,
                target=target,
                command=request.command,
                timeout_seconds=timeout_seconds,
            )
        else:
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
    except HTTPException:
        raise
    except (paramiko.SSHException, OSError, RuntimeError) as exc:
        logger.error(
            "ssh_connection_failed execution_id=%s target=%s error_type=%s",
            execution_id,
            request.target,
            type(exc).__name__,
        )
        detail = (
            str(exc)
            if target.route == "local"
            else "SSH connection or execution failed; inspect tool-service logs."
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc

    duration_ms = round((time.monotonic() - started) * 1000)
    logger.info(
        "ssh_complete execution_id=%s target=%s route=%s exit_status=%s duration_ms=%s truncated=%s",
        execution_id,
        request.target,
        target.route,
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


@app.post(
    "/v1/local/ping",
    operation_id="local_ping",
    dependencies=[Depends(authorize)],
    summary="ICMP/TCP reachability check via the Windows local agent",
)
async def local_ping(request: LocalPingRequest) -> dict[str, Any]:
    """Ping a host from the PC's network. Requires a connected local agent."""
    _require_local_agent()
    try:
        return await local_agent_hub.rpc(
            "ping",
            {"host": request.host},
            timeout=20,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post(
    "/v1/local/resolve",
    operation_id="local_resolve",
    dependencies=[Depends(authorize)],
    summary="DNS resolve via the Windows local agent",
)
async def local_resolve(request: LocalResolveRequest) -> dict[str, Any]:
    """Resolve a name using the PC's DNS. Requires a connected local agent."""
    _require_local_agent()
    try:
        return await local_agent_hub.rpc(
            "resolve",
            {"name": request.name},
            timeout=15,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post(
    "/v1/local/run",
    operation_id="local_run",
    dependencies=[Depends(authorize)],
    summary="Run an allowlisted local companion command (confirmation required)",
)
async def local_run(request: LocalRunRequest) -> dict[str, Any]:
    """Execute a narrowly allowlisted local command on the PC after confirm."""
    _require_local_agent()
    try:
        return await local_agent_hub.rpc(
            "local_run",
            {"command": request.command, "require_confirm": True},
            timeout=120,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
