# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        netcat-openbsd \
        util-linux \
    && useradd --create-home --uid 10002 --shell /usr/sbin/nologin jarvis-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY tools/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chown=jarvis-tools:jarvis-tools tools/app.py tools/scan_host_key.py tools/local_agent_bridge.py /app/
COPY --chmod=0755 docker/tools/entrypoint.sh /usr/local/bin/tools-entrypoint.sh
RUN python -m py_compile /app/app.py /app/scan_host_key.py /app/local_agent_bridge.py \
    && rm -rf /app/__pycache__ \
    && sh -n /usr/local/bin/tools-entrypoint.sh \
    && mkdir -p /run/secrets/ssh /run/secrets-src/ssh \
    && chown jarvis-tools:jarvis-tools /run/secrets/ssh
RUN python - <<'PY'
from fastapi import HTTPException
from app import TargetConfig, app, ensure_command_allowed

target = TargetConfig(
    host="example.ts.net",
    user="audit",
    allowed_commands=[r"^uptime$"],
    mutating_commands=[r"^reboot$"],
)
ensure_command_allowed(target, "uptime")
try:
    ensure_command_allowed(target, "reboot")
except HTTPException as exc:
    assert exc.status_code == 403
else:
    raise AssertionError("mutating command was not denied")
operation_ids = {
    operation.get("operationId")
    for path in app.openapi()["paths"].values()
    for operation in path.values()
}
assert {
    "list_ssh_targets",
    "run_ssh_command",
    "get_weather",
    "http_get_allowlisted",
    "local_agent_status",
    "local_ping",
    "local_resolve",
    "local_run",
} <= operation_ids
PY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8090

ENTRYPOINT ["/usr/local/bin/tools-entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
