#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
    printf 'Usage: %s TARGET_ALIAS COMMAND...\n' "$0" >&2
    exit 2
fi

target=$1
shift
command=$*

docker compose exec -T --user jarvis-tools jarvis-tools \
    python - "${target}" "${command}" <<'PY'
import json
import os
import sys
import urllib.request

payload = json.dumps({"target": sys.argv[1], "command": sys.argv[2]}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8090/v1/ssh/execute",
    data=payload,
    headers={
        "Authorization": f"Bearer {os.environ['TOOL_API_KEY']}",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=120) as response:
    print(json.dumps(json.load(response), indent=2))
PY
