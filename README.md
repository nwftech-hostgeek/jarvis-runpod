# Jarvis

A self-hosted personal AI assistant stack built around CUDA llama.cpp, Open
WebUI, local speech, and tightly controlled SSH access over Tailscale.

The default model is Qwen3-Coder-30B-A3B-Instruct Q4_K_M. It is optimized for
coding and web application work while remaining practical on a 24 GB GPU.

## Important RunPod constraints

RunPod GPU Pods do **not** support Docker Compose or Docker-in-Docker. A Pod is
one managed container. This repository therefore has two equivalent launch
paths:

- `docker-compose.yml` is the canonical portable stack for a local NVIDIA host
  or any GPU VM where Docker Compose is supported.
- `docker/runpod/Dockerfile` packages the services into one supervised
  container, which is the supported deployment shape for a RunPod GPU Pod.

RunPod Network Volumes are currently available only in **Secure Cloud**. A
Community Cloud Pod cannot simultaneously satisfy the Network Volume
requirement. For the lowest GPU rate, Community Cloud can use a volume disk at
`/workspace`; it survives stop/start but is deleted with the Pod and is more
expensive while stopped. For portable persistent data, use Secure Cloud plus a
Network Volume.

## Architecture

Only Open WebUI is published.

1. Open WebUI serves the browser UI on port 3000 and stores users, chats, RAG
   data, and settings persistently.
2. llama.cpp serves an authenticated OpenAI-compatible API internally on port
   8080.
3. Speaches serves authenticated faster-whisper STT and Kokoro/Piper TTS
   internally on port 8000.
4. Tailscale runs in userspace mode and exposes an internal SOCKS5 proxy on
   port 1055.
5. The Jarvis tool service accepts only named targets and full-command
   allowlists. It reaches TCP/22 through the Tailscale SOCKS5 proxy, pins SSH
   host keys, and never accepts an arbitrary hostname.

In Compose, these are separate containers on one private bridge network. In
the RunPod image, they are separate supervised processes bound to loopback,
except Open WebUI on port 3000.

## Security defaults

- No llama, voice, tool, or Tailscale port is published.
- llama, voice, and tool APIs each require a different bearer token.
- SSH defaults to no targets and no allowed commands.
- SSH host-key checking is mandatory.
- Mutating SSH commands require a `mutating_commands` match, a per-target
  opt-in, and a global opt-in.
- The included Open WebUI native tool asks for interactive confirmation before
  every SSH execution.
- Application processes run non-root where practical. The Compose tool
  bootstrap receives only the capabilities needed to copy a host-owned SSH key
  into an in-memory directory, then drops to its unprivileged UID.
- Secrets are read from `.env`, RunPod secret environment variables, or
  read-only files. No credentials are embedded in an image.

The RunPod HTTP proxy is public internet infrastructure. Keep Open WebUI
authentication enabled and signup disabled. Open WebUI 0.13.3 still permits
the first account bootstrap; that account becomes the administrator.

## Repository layout

```text
docker-compose.yml        Portable multi-container stack
compose.24gb.yml          Conservative 24 GB override
compose.48gb.yml          Higher-throughput 48 GB override
docker/llama/             Reproducible CUDA llama.cpp build and downloader
docker/runpod/            Supported all-in-one RunPod image
docker/tools/             Allowlisted SSH API image
tools/                    SSH API and host-key scanner
openwebui-tools/          Native Open WebUI confirmation wrapper
config/                   SSH target allowlist and pinned host keys
prompts/                  Jarvis system prompt
docs/MODELS.md            Model choices, sizes, and switching instructions
scripts/                  Initialization and smoke-test helpers
```

## Prerequisites

For the Compose path:

- Linux or Docker Desktop with Docker Compose v2
- An NVIDIA driver compatible with CUDA 12.8 (CUDA 12.9 for the Compose
  48 GB GPU-voice image)
- NVIDIA Container Toolkit configured for Docker
- Approximately 40 GB free for images and 25–100 GB persistent model/data
  storage
- A Tailscale tailnet and a tagged auth key
- A dedicated SSH key and restricted account on each approved target

Verify GPU passthrough before building:

```bash
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

## Quick start: Docker Compose

Run these from the repository root after reviewing `.env.example`:

```bash
cp .env.example .env
# Edit .env. For local use, set JARVIS_DATA_ROOT=./data.
# Generate each API secret independently:
openssl rand -hex 32

mkdir -p secrets
cp /secure/path/to/dedicated_id_ed25519 secrets/id_ed25519
chmod 700 secrets
chmod 400 secrets/id_ed25519

# Edit config/ssh-targets.yaml before enabling an SSH target.
sudo bash scripts/init-data.sh
bash scripts/check-env.sh

docker compose -f docker-compose.yml -f compose.24gb.yml build
docker compose -f docker-compose.yml -f compose.24gb.yml up -d
docker compose logs -f llama voice open-webui
```

First startup downloads the GGUF and voice models. On a new volume, it can take
several minutes before Open WebUI starts because Compose waits for healthy
inference and voice services.

Open `http://localhost:3000` and create the first administrator. Signup is
already disabled for subsequent users. Then run:

```bash
bash scripts/smoke-test.sh
```

Use `compose.48gb.yml` instead of `compose.24gb.yml` on a 48 GB card.

PowerShell equivalents for the preparation/validation steps:

```powershell
Copy-Item .env.example .env
# Edit .env and set JARVIS_DATA_ROOT=.\data.
New-Item -ItemType Directory -Force secrets | Out-Null
Copy-Item C:\secure\id_ed25519 secrets\id_ed25519
.\scripts\init-data.ps1
.\scripts\check-env.ps1
docker compose -f docker-compose.yml -f compose.24gb.yml up -d --build
```

## Primary deployment: RunPod

### Automated deployment from Windows

When Docker is unavailable locally, the included PowerShell automation creates
a private GitHub repository, publishes the image with GitHub Actions, stores a
write-only GHCR pull credential in RunPod, creates the RunPod template and
Network Volume, and starts the Pod.

Keep these values only in the ignored `.env`:

```dotenv
GITHUB_USERNAME=<github-login>
GITHUB_TOKEN=<classic-PAT-with-repo-workflow-read:packages>
GITHUB_REPOSITORY=jarvis-runpod
```

Then run:

```powershell
.\scripts\publish-github.ps1
# Wait for the "Build RunPod image" workflow to succeed.
.\scripts\deploy-runpod.ps1
```

The deploy script defaults to a 100 GB standard Network Volume in `EU-SE-1`
and tries an A40, then an RTX A6000. Both provide 48 GB VRAM and were selected
because that location supports Network Volumes. Override the location or size
only after checking current capacity:

```powershell
.\scripts\deploy-runpod.ps1 -DataCenter EU-SE-1 -VolumeSizeGb 100
```

Use `-TemplateOnly` to create no billable Pod or Network Volume. The script
never uploads `.env`, never injects `RUNPOD_API_KEY` or `GITHUB_TOKEN` into the
Pod, and records only non-secret resource IDs under `.deployment/`.

### 1. Build and publish the single-container image

RunPod cannot build or run this Compose project inside a GPU Pod. Build the
RunPod image on a Docker-capable workstation or CI runner and push it to a
registry RunPod can read:

```bash
export IMAGE=ghcr.io/YOUR_ACCOUNT/jarvis-runpod:0.1.0

docker build --pull \
  --platform linux/amd64 \
  --file docker/runpod/Dockerfile \
  --build-arg CUDA_VERSION=12.8.1 \
  --build-arg LLAMA_CPP_REF=b12316 \
  --tag "${IMAGE}" .

docker push "${IMAGE}"
```

The image is intentionally large: it contains Open WebUI, CUDA llama.cpp, and
isolated speech dependencies. Use a registry cache in CI for subsequent builds.
If the registry is private, add its pull credential in RunPod.

### 2. Create persistent storage

Recommended:

1. In RunPod Storage, create a 100–150 GB Network Volume in the desired data
   center.
2. Deploy a **Secure Cloud** GPU in that same location.
3. Attach the Network Volume during Pod creation. It mounts at `/workspace`
   and cannot be attached later.

Do not run two Jarvis Pods concurrently against the same writable data root;
that would duplicate the Tailscale identity and can corrupt the single-instance
Open WebUI database.

Cost-first alternative:

1. Choose Community Cloud.
2. Allocate a volume disk mounted at `/workspace`.
3. Back it up before terminating the Pod; volume-disk data is tied to that Pod.

### 3. Create a RunPod template

Use the image pushed above and configure:

- Container image: your `jarvis-runpod` tag
- Container disk: at least 40 GB
- Volume mount: `/workspace`
- Exposed port: `3000/http`
- CUDA compatibility filter: 12.8-compatible hosts
- Start command: leave blank so the image entrypoint runs
- GPU: 24 GB for the default profile; 48 GB for larger context/concurrency

Do not expose 8080, 8000, 8090, 1055, or TCP/22.

Add environment variables through RunPod's secret-aware UI. At minimum:

```dotenv
RUNPOD_DATA_ROOT=/workspace/jarvis-data
RUNPOD_UI_PORT=3000

WEBUI_SECRET_KEY=<independent-64-hex-character-value>
WEBUI_AUTH=true
ENABLE_SIGNUP=false
DEFAULT_USER_ROLE=pending
LLAMA_API_KEY=<independent-random-value>
VOICE_API_KEY=<independent-random-value>
TOOL_API_KEY=<independent-random-value>

HF_TOKEN=<optional-read-only-Hugging-Face-token>
HF_REPO_ID=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF
HF_FILENAME=Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
MODEL_PATH=/models/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
MODEL_ALIAS=qwen3-coder-30b-a3b
LLAMA_CTX_SIZE=16384
LLAMA_PARALLEL=1
LLAMA_N_GPU_LAYERS=99

STT_MODEL=Systran/faster-whisper-small
TTS_MODEL=speaches-ai/Kokoro-82M-v1.0-ONNX
TTS_VOICE=af_heart
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8

TS_AUTHKEY=<tagged-Tailscale-auth-key>
TS_HOSTNAME=jarvis-runpod
TS_ACCEPT_DNS=false
TS_EXTRA_ARGS=--accept-routes
```

`RUNPOD_API_KEY` is not required by the application and should not be injected
into the Pod. It is only for `runpodctl` or external lifecycle automation.

For SSH, either place configuration in `/workspace/jarvis-config` from a secure
RunPod basic web terminal, or provide these as secret base64 values. The image
is application-only and intentionally does not run sshd or Jupyter:

```dotenv
SSH_PRIVATE_KEY_B64=<base64-of-dedicated-private-key>
SSH_TARGETS_YAML_B64=<base64-of-config/ssh-targets.yaml>
SSH_KNOWN_HOSTS_B64=<base64-of-reviewed-known-hosts-file>
```

Base64 target/known-host values are copied into `/workspace/jarvis-config` on
startup. Remove those two environment variables after bootstrap or update them
when the files change; otherwise an old value will overwrite later edits on the
next restart. The private key remains ephemeral under `/run/secrets`.

Create one-line base64 values on Linux:

```bash
base64 -w0 secrets/id_ed25519
base64 -w0 config/ssh-targets.yaml
base64 -w0 config/ssh_known_hosts
```

On PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("secrets\id_ed25519"))
```

### 4. Deploy and initialize Open WebUI

Deploy the Pod, follow its logs until llama reports `status: ok`, then open:

```text
https://<POD_ID>-3000.proxy.runpod.net
```

Create the first administrator account. Open WebUI permits this initial
bootstrap while keeping subsequent signup disabled.

Persistent data is under:

```text
/workspace/jarvis-data/models
/workspace/jarvis-data/open-webui
/workspace/jarvis-data/voice-cache
/workspace/jarvis-data/tailscale
/workspace/jarvis-config
```

## Tailscale and onsite SSH

### Tailnet policy

Create a dedicated tag such as `tag:jarvis` and use a preauthorized, one-off
auth key that applies only that tag; persistent state preserves the enrolled
node across stops. In the Tailscale admin console, grant that tag TCP/22 access
only to the intended onsite hosts or subnet-router destinations. Do not grant
broad tailnet access merely to simplify initial testing.

After the node has enrolled and `/workspace/jarvis-data/tailscale` is
persisted, remove the consumed one-off `TS_AUTHKEY` from the Pod/template. The
saved node state reconnects without retaining an enrollment credential.

The stack uses userspace networking, so applications do not receive a TUN
interface. The SSH service deliberately sends connections through Tailscale's
SOCKS5 proxy. A normal `ping` or direct `ssh` from a sibling Compose container
is therefore not a valid test.

Check the node:

```bash
docker compose exec tailscale \
  tailscale --socket=/var/run/tailscale/tailscaled.sock status
```

In a RunPod terminal:

```bash
tailscale --socket=/run/tailscale/tailscaled.sock status
```

### Configure targets

Edit `config/ssh-targets.yaml`. Keep aliases, destinations, users, and complete
command regular expressions under operator control. The API never takes a host
from a model request.

Use a dedicated, preferably unprivileged remote account. Paramiko cannot prompt
for an encrypted key passphrase in this service, so use a dedicated unencrypted
automation key protected by:

- a narrowly scoped Tailscale grant;
- a restricted remote account and sudoers policy;
- the command allowlist in this repository; and
- strict local secret-file permissions.

### Pin host keys

Start the stack after adding the target, then fetch one key through Tailscale:

```bash
bash scripts/scan-host-key.sh core-router
```

The script prints a SHA256 fingerprint. Verify it through an independent
channel (onsite console, existing management system, or a trusted local
administrator) before accepting it.

For the RunPod image:

```bash
scan-host-key-runpod core-router
```

Append the reviewed stdout line to
`/workspace/jarvis-config/ssh_known_hosts`. The tool reads the file on every
request; no restart is needed.

Test a configured read-only command in Compose:

```bash
bash scripts/test-ssh-tool.sh core-router uptime
```

## Enable SSH tools in Open WebUI

### Preferred: native confirmation tool

The included `openwebui-tools/jarvis_ssh.py` presents an interactive
confirmation dialog before every command:

1. In Open WebUI, go to Workspace / Tools and create a tool.
2. Paste the contents of `openwebui-tools/jarvis_ssh.py`.
3. In the tool's Valves, set:
   - Compose URL: `http://jarvis-tools:8090`
   - RunPod URL: `http://127.0.0.1:8090`
   - API key: the value of `TOOL_API_KEY`
4. Grant the tool only to trusted users/groups.
5. Enable it for the Jarvis model or explicitly select it in a chat.
6. Under the model's Advanced Params, keep Function Calling set to `Native`.

The model can list target aliases without a prompt. Execution pauses until the
browser user confirms the exact target and command.

### Alternative: external OpenAPI server

Open WebUI can import the API directly:

1. Settings → Admin → Integrations → External Tool Servers → Add Connection
2. Type: OpenAPI
3. URL: `http://jarvis-tools:8090` (Compose) or
   `http://127.0.0.1:8090` (RunPod)
4. OpenAPI path: `/openapi.json`
5. Auth: Bearer
6. Key: `TOOL_API_KEY`

OpenAPI tool servers do not provide interactive confirmation callbacks. Use
this mode only for read-only commands with `SSH_ENABLE_MUTATING_COMMANDS=false`.
For write actions, use the native confirmation wrapper or a separate audited
automation system.

Mutations remain disabled unless all three settings agree:

1. The exact command matches `mutating_commands`.
2. The target has `allow_mutating: true`.
3. `.env` has `SSH_ENABLE_MUTATING_COMMANDS=true`.

## Open WebUI setup

The first account is the administrator. To onboard additional users, enable
signup temporarily under Settings → Admin → Authentication; new accounts use
the `pending` role until an administrator approves them. Disable signup again
after onboarding.

### System prompt

Create or edit the model in Workspace / Models and paste
`prompts/jarvis-system.md` into its system prompt. Keep the model's context
setting at or below the per-slot llama.cpp context.

### Voice

Open WebUI is preconfigured to use:

- STT: `Systran/faster-whisper-small`
- TTS: `speaches-ai/Kokoro-82M-v1.0-ONNX`
- Voice: `af_heart`

RunPod's HTTPS proxy provides the secure browser context required for microphone
permission. On local HTTP, browsers generally permit microphone access on
`localhost`; remote plain HTTP may be blocked.

After first login, verify Settings → Admin → Audio. Values persisted in the
Open WebUI database can override changed environment defaults. In user Audio
settings, select the server default. Browser speech APIs remain available as a
fallback.

The 24 GB profile keeps Whisper and Kokoro's ONNX provider on CPU to protect
LLM VRAM. The 48 GB override enables CUDA voice inference. Change `STT_MODEL`
to a larger faster-whisper model if CPU/RAM and startup time permit.

## Models and GPU profiles

See [docs/MODELS.md](docs/MODELS.md) for exact download commands, current Qwen
recommendations, sizing, and model switching.

The conservative profile:

```bash
docker compose -f docker-compose.yml -f compose.24gb.yml up -d
```

The 48 GB profile:

```bash
docker compose -f docker-compose.yml -f compose.48gb.yml up -d
```

Tune the selected profile with `LLAMA_CTX_SIZE_24GB` /
`LLAMA_PARALLEL_24GB` or `LLAMA_CTX_SIZE_48GB` /
`LLAMA_PARALLEL_48GB` in `.env`. Generic `LLAMA_CTX_SIZE` and
`LLAMA_PARALLEL` apply to the base Compose file and the RunPod image.

`LLAMA_CTX_SIZE` is shared by parallel slots. The 48 GB default uses 65,536
total tokens and two slots, or approximately 32K per concurrent request.

## Operations

Inspect health and logs:

```bash
docker compose ps
docker compose logs -f --tail=200 llama
docker compose logs -f --tail=200 voice
docker compose logs -f --tail=200 tailscale jarvis-tools open-webui
bash scripts/smoke-test.sh
```

The RunPod image sends every supervised service log to Pod stdout/stderr, so
the RunPod Logs view contains the same information. Its Docker health check
requires WebUI, llama, voice, and tools; Tailscale is required only when
`TS_AUTHKEY` is set.

Back up at least:

```text
open-webui/        Users, chats, settings, RAG metadata
models/            Optional to back up; reproducible but large
tailscale/         Node identity; protect as sensitive
jarvis-config/     Target aliases and pinned host keys
```

Stop Open WebUI before copying its database for a consistent backup.

### Updating

1. Back up persistent data.
2. Review release notes and database migrations.
3. Change pinned image tags/build arguments intentionally.
4. Rebuild/pull.
5. Recreate one stack and run the smoke test before deleting the backup.

Do not use `latest` tags in an unattended production update.
After validating a release on the target architecture, pin third-party images
by digest if your change-control policy requires immutable artifacts.

## Cost control

Stopping containers does not stop RunPod billing. Stop the **Pod**:

```bash
runpodctl pod stop <POD_ID>
runpodctl pod start <POD_ID>
```

Compute billing stops with the Pod; Network Volume storage billing continues.
At the current standard rate, 100 GB is approximately $7/month. GPU pricing is
variable, so calculate:

```text
monthly cost = GPU hourly price × running hours + persistent storage
```

For example, 200 hours at $0.50/hour plus a 100 GB Network Volume is about
$107/month. At $0.75/hour, keep usage near 160 hours to remain around
$127/month. These are planning examples; use the current RunPod console price.

Practical controls:

- Stop the Pod after each work session.
- Keep models and WebUI data on persistent storage so startup does not
  redownload them.
- Use one 24 GB GPU and one parallel slot unless concurrency is necessary.
- Set RunPod balance alerts and an external idle-stop automation if desired.
- Never confuse `docker compose down` with stopping a cloud VM/Pod.

## Migration to local NVIDIA hardware

1. Install a compatible NVIDIA driver, Docker, Compose v2, and NVIDIA Container
   Toolkit.
2. Stop the RunPod stack.
3. Copy `/workspace/jarvis-data/open-webui`, required GGUF files, and the
   non-secret configuration to local storage.
4. Set `JARVIS_DATA_ROOT` to that local path.
5. Put the SSH key in `secrets/`, run `scripts/init-data.sh`, then launch the
   matching 24/48 GB Compose override.
6. Verify health before retiring the cloud Pod.

Do not run two hosts from a copied Tailscale state directory. Either move the
state and ensure only one instance runs, or delete the copied local
`tailscale/` directory and enroll the local host as a new Tailnet node.

The CUDA image targets NVIDIA. Moving to Apple Silicon, AMD, or CPU-only
hardware requires replacing the llama build backend and is more than an
environment-only change.

## Troubleshooting

### llama reports out of memory

Reduce, in order:

1. `LLAMA_CTX_SIZE`
2. `LLAMA_BATCH_SIZE`
3. `LLAMA_PARALLEL`
4. KV cache from Q8_0 to Q4_0
5. `LLAMA_N_GPU_LAYERS` to allow CPU offload

### Open WebUI never starts in Compose

It waits for llama and voice health. Inspect:

```bash
docker compose ps
docker compose logs --tail=200 llama voice
```

The common first-boot causes are a wrong Hugging Face filename, a gated model
without `HF_TOKEN`, a bind-mount permission error, or GPU OOM.

### Voice buttons work but no transcript/audio returns

- Confirm microphone permission and HTTPS/localhost browser context.
- Check Settings → Admin → Audio for database-persisted overrides.
- Inspect `docker compose logs voice open-webui`.
- Confirm the model IDs exactly match `.env`.

### Tailscale is online but SSH fails

- Confirm the target is reachable from the Jarvis tag in Tailnet policy.
- Use a MagicDNS FQDN or Tailscale IP in `ssh-targets.yaml`.
- Verify the SOCKS proxy and host key with `scan-host-key.sh`.
- Check the remote account/key and the pinned known-hosts entry.
- Remember that direct sibling-container networking does not traverse a
  userspace Tailscale daemon; the tool service does so explicitly.

### Tool returns “command does not match”

The regular expression must match the entire command. Add a narrowly scoped
pattern and a corresponding example; do not replace the allowlist with `.*`.
