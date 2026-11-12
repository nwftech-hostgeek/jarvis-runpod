from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml


SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
SAFE_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:%-]{0,254}$")


def default_config_dir() -> Path:
    override = os.getenv("JARVIS_LOCAL_AGENT_CONFIG_DIR")
    if override:
        return Path(override)
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "jarvis-local-agent"
    return Path.home() / ".config" / "jarvis-local-agent"


def resolve_config_path() -> Path:
    explicit = os.getenv("JARVIS_LOCAL_AGENT_CONFIG")
    if explicit:
        return Path(explicit)
    return default_config_dir() / "config.yaml"


def load_config() -> dict[str, Any]:
    path = resolve_config_path()
    data: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Config must be a mapping: {path}")
        data = raw

    token = (
        os.getenv("JARVIS_LOCAL_AGENT_TOKEN")
        or os.getenv("LOCAL_AGENT_TOKEN")
        or str(data.get("token") or "")
    ).strip()
    tools_url = (
        os.getenv("JARVIS_TOOLS_URL")
        or os.getenv("JARVIS_LOCAL_AGENT_TOOLS_URL")
        or str(data.get("tools_url") or "")
    ).strip().rstrip("/")

    identity = (
        os.getenv("JARVIS_LOCAL_AGENT_IDENTITY")
        or str(data.get("identity_file") or "")
        or str(Path.home() / ".ssh" / "id_ed25519")
    )
    known_hosts = (
        os.getenv("JARVIS_LOCAL_AGENT_KNOWN_HOSTS")
        or str(data.get("known_hosts") or "")
        or str(Path.home() / ".ssh" / "known_hosts")
    )

    return {
        "config_path": str(path),
        "config_dir": str(default_config_dir()),
        "tools_url": tools_url,
        "token": token,
        "agent_id": str(
            os.getenv("JARVIS_LOCAL_AGENT_ID")
            or data.get("agent_id")
            or "windows-pc"
        )[:64],
        "host_allowlist": list(data.get("host_allowlist") or []),
        "identity_file": identity,
        "known_hosts": known_hosts,
        "targets": data.get("targets") or {},
        "local_commands": list(data.get("local_commands") or []),
        "confirm_timeout_seconds": int(data.get("confirm_timeout_seconds") or 120),
    }


def save_pair_env(tools_url: str, token: str) -> Path:
    """Write a non-committed pair.env for the start script."""
    directory = default_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pair.env"
    path.write_text(
        f"JARVIS_TOOLS_URL={tools_url.rstrip('/')}\n"
        f"JARVIS_LOCAL_AGENT_TOKEN={token}\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def host_allowed(host: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    host = host.strip().lower()
    for entry in allowlist:
        entry = str(entry).strip().lower()
        if not entry:
            continue
        if entry == host:
            return True
        if "/" in entry:
            try:
                network = ipaddress.ip_network(entry, strict=False)
                address = ipaddress.ip_address(host)
                if address in network:
                    return True
            except ValueError:
                continue
    return False


def advertised_targets(config: dict[str, Any]) -> list[dict[str, Any]]:
    targets = config.get("targets") or {}
    if not isinstance(targets, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, raw in targets.items():
        if not SAFE_NAME_RE.fullmatch(str(name)) or not isinstance(raw, dict):
            continue
        host = str(raw.get("host") or "")
        if not SAFE_HOST_RE.fullmatch(host):
            continue
        out.append(
            {
                "name": name,
                "host": host,
                "user": str(raw.get("user") or "jarvis"),
                "port": int(raw.get("port") or 22),
                "description": str(raw.get("description") or ""),
                "examples": list(raw.get("examples") or [])[:20],
                "allowed_commands": list(raw.get("allowed_commands") or []),
                "mutating_commands": list(raw.get("mutating_commands") or []),
                "allow_mutating": bool(raw.get("allow_mutating", False)),
                "mutating_commands_enabled": bool(raw.get("allow_mutating", False)),
                "connect_timeout": int(raw.get("connect_timeout") or 15),
                "command_timeout": int(raw.get("command_timeout") or 30),
            }
        )
    return out


def write_example_if_missing() -> Path | None:
    directory = default_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    if path.exists():
        return None
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    if example.is_file():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        path.write_text(
            "tools_url: https://REPLACE_POD_ID-8090.proxy.runpod.net\ntargets: {}\n",
            encoding="utf-8",
        )
    return path


def redact_for_log(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:2] + "***" + value[-2:]


def dump_status_blob(config: dict[str, Any]) -> str:
    safe = {
        "config_path": config.get("config_path"),
        "tools_url": config.get("tools_url"),
        "agent_id": config.get("agent_id"),
        "token_set": bool(config.get("token")),
        "host_allowlist_count": len(config.get("host_allowlist") or []),
        "target_count": len(config.get("targets") or {}),
    }
    return json.dumps(safe, indent=2)
