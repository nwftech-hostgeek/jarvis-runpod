from __future__ import annotations

import argparse
import base64
import hashlib
import shlex
import sys

import paramiko

from app import SOCKS_HOST, SOCKS_PORT, load_targets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch one SSH host key through the Tailscale SOCKS5 proxy."
    )
    parser.add_argument("target", help="Target alias from ssh-targets.yaml")
    args = parser.parse_args()

    targets = load_targets()
    target = targets.get(args.target)
    if target is None:
        parser.error(f"unknown target alias: {args.target}")

    proxy_address = f"{SOCKS_HOST}:{SOCKS_PORT}"
    command = " ".join(
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
    proxy = paramiko.ProxyCommand(command)
    transport = paramiko.Transport(proxy)

    try:
        transport.start_client(timeout=target.connect_timeout)
        key = transport.get_remote_server_key()
    finally:
        transport.close()
        proxy.close()

    host = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
    known_hosts_line = f"{host} {key.get_name()} {key.get_base64()}"
    fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode()

    print(
        f"Review fingerprint for {args.target}: SHA256:{fingerprint}",
        file=sys.stderr,
    )
    print(known_hosts_line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
