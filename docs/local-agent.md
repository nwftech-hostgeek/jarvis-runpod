# Windows local agent (LAN bridge)

Jarvis on RunPod cannot see your home LAN. The **local agent** runs on your
Windows PC, dials **out** to `jarvis-tools` over an authenticated WebSocket, and
executes LAN SSH / ping / resolve on behalf of the cloud stack. Open WebUI tools
stay the same (`list_ssh_targets`, `run_ssh_command`); routing is transparent.

```text
Open WebUI / orb  →  jarvis-tools (Pod)
                         │
                         │  WebSocket RPC (agent dials in)
                         ▼
              Windows local agent (this PC)
                         │
                         ▼
                   LAN hosts / DNS / ping
```

## Prerequisites

- Python **3.11+** on the PC ([python.org](https://www.python.org/downloads/windows/), enable PATH)
- Jarvis Pod with `jarvis-tools` reachable at an HTTPS URL (RunPod proxy for
  port **8090**, or Tailscale to the tools listen address)
- Matching pairing token on Pod and PC (`JARVIS_LOCAL_AGENT_TOKEN`)

## One-time Pod setup

1. Set environment on the template / Pod (secret-aware UI):

   ```dotenv
   LOCAL_AGENT_ENABLED=true
   JARVIS_LOCAL_AGENT_TOKEN=<openssl rand -hex 32>
   ```

2. Expose HTTP port **8090** on the RunPod template (in addition to 3000). The
   agent connects to `https://<POD_ID>-8090.proxy.runpod.net`. Keep
   `TOOL_API_KEY` and the pairing token long and unique. Do not log them.

3. Redeploy / hard-refresh the Pod so tools pick up the new code and env.
   Tools must listen on `0.0.0.0:8090` when the proxy should reach them
   (`LOCAL_AGENT_LISTEN=0.0.0.0` in the RunPod image — see supervisord).

4. Paste the updated system prompt from `prompts/jarvis-system.md` into the
   Jarvis model (Workspace → Models). Refresh Open WebUI tools if you use the
   native wrappers under `openwebui-tools/`.

## One-time PC pairing

From the repo root:

```powershell
.\scripts\start-local-agent.ps1 -Pair -ToolsUrl "https://<POD_ID>-8090.proxy.runpod.net" -Token "<same-as-pod>"
```

This writes `%APPDATA%\jarvis-local-agent\pair.env` (gitignored by virtue of
living outside the repo). Optionally edit
`%APPDATA%\jarvis-local-agent\config.yaml` (created from
`local-agent/config.example.yaml`) to advertise LAN SSH targets and an optional
host allowlist.

You can also set env vars without the pair file:

```powershell
$env:JARVIS_TOOLS_URL = "https://<POD_ID>-8090.proxy.runpod.net"
$env:JARVIS_LOCAL_AGENT_TOKEN = "<token>"
```

## Start the agent

```powershell
.\scripts\start-local-agent.ps1
```

Leave the console open — destructive / SSH confirmations prompt there (`y`/`N`).
Open WebUI still shows its own confirmation dialog before SSH execute.

### Optional: Task Scheduler

1. Action: `powershell.exe`
2. Arguments:
   `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\<you>\jarvis\scripts\start-local-agent.ps1"`
3. Run whether user is logged on: **No** (need interactive confirm UI), or use
   NSSM only for always-on read-only workflows with an empty mutating allowlist.

### Optional: NSSM

```text
nssm install JarvisLocalAgent "C:\Users\<you>\jarvis\local-agent\.venv\Scripts\python.exe" -m jarvis_local_agent
nssm set JarvisLocalAgent AppDirectory C:\Users\<you>\jarvis\local-agent
nssm set JarvisLocalAgent AppEnvironmentExtra JARVIS_TOOLS_URL=https://... JARVIS_LOCAL_AGENT_TOKEN=...
```

Console confirmation will not work as a Windows service unless you also approve
via Open WebUI only and keep mutating commands disabled.

## Configure LAN SSH targets

### Option A — cloud config (`route: local`)

In `config/ssh-targets.yaml` (or Pod `/workspace/jarvis-config/ssh-targets.yaml`):

```yaml
targets:
  home-nas:
    host: 192.168.1.50
    user: jarvis
    port: 22
    route: local
    description: Home NAS via Windows local agent
    examples:
      - uptime
    allowed_commands:
      - '^(hostname|uptime|whoami)$'
```

### Option B — advertise from the PC

Add targets under `%APPDATA%\jarvis-local-agent\config.yaml`. When the agent is
online, they appear in `list_ssh_targets` automatically.

Pin host keys in the PC `known_hosts` file (default `%USERPROFILE%\.ssh\known_hosts`).
The agent uses `RejectPolicy` — unknown keys fail closed.

## How Jarvis uses it

| Tool | Behavior |
| --- | --- |
| `list_ssh_targets` | Pod + local routes; `available=false` if agent offline for `route: local` |
| `run_ssh_command` | Same confirmation UX; executes on Pod (Tailscale) or local agent |
| `local_agent_status` | Connected?, capabilities (no secrets) |
| `local_ping` / `local_resolve` | PC network path |
| `local_run` | Narrow PC allowlist + console confirm |

When the agent is online, Jarvis must **not** claim it lacks LAN access. When
offline, say the companion is disconnected — not that LAN is impossible forever.

## Hardening

- Pairing token ≥ 20 chars; never commit `pair.env` / `.env`
- Optional `host_allowlist` on the PC (hosts or CIDRs)
- No arbitrary RCE: `local_run` requires allowlist + confirm; SSH uses the same
  full-match regex model as cloud tools
- TLS to the RunPod HTTPS proxy (`wss://`)
- Logs never print tokens, keys, or full Authorization headers

## Verify

1. Start the agent; logs should show `paired with jarvis-tools`.
2. `GET /v1/local-agent/status` with `TOOL_API_KEY` → `"connected": true`.
3. Ask Jarvis to list SSH targets or ping a LAN host.
