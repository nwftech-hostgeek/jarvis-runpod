#!/usr/bin/env python3
"""One-shot pairing helper: write %APPDATA%\\jarvis-local-agent\\pair.env."""

from __future__ import annotations

import argparse
import secrets
import sys

from jarvis_local_agent.config import default_config_dir, save_pair_env, write_example_if_missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Pair Windows local agent with Jarvis")
    parser.add_argument(
        "--tools-url",
        required=True,
        help="HTTPS jarvis-tools base URL, e.g. https://POD-8090.proxy.runpod.net",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Existing JARVIS_LOCAL_AGENT_TOKEN (generated if omitted)",
    )
    args = parser.parse_args()
    token = args.token.strip() or secrets.token_hex(32)
    if len(token) < 20:
        print("token must be at least 20 characters", file=sys.stderr)
        return 1

    write_example_if_missing()
    path = save_pair_env(args.tools_url.rstrip("/"), token)
    cfg_dir = default_config_dir()
    print(f"Wrote {path}")
    print(f"Config directory: {cfg_dir}")
    print()
    print("Set the SAME token on the Pod / jarvis-tools environment:")
    print(f"  JARVIS_LOCAL_AGENT_TOKEN={token}")
    print("  LOCAL_AGENT_ENABLED=true")
    print()
    print("Also expose RunPod HTTP port 8090 (tools) so the agent can dial in.")
    print("Then run: .\\scripts\\start-local-agent.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
