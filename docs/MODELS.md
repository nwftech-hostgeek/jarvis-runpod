# Model guide

The stack accepts any llama.cpp-compatible GGUF. The default is
Qwen3-Coder-30B-A3B-Instruct Q4_K_M because it is a capable coding and tool-use
model that can fit on a 24 GB card with a restrained context.

> The requested name `Qwen3.8-27B` does not correspond to a canonical Qwen
> release in the current model catalog. The current dense 27B model is
> `Qwen3.5-27B`; it is included below.

## Recommended models

Memory figures are planning estimates, not guarantees. llama.cpp build,
context, batch size, parallel slots, KV-cache type, and GPU architecture all
change actual use.

| Model / quant | GGUF size | Practical GPU | Suggested starting context | Notes |
| --- | ---: | ---: | ---: | --- |
| Qwen3-Coder-30B-A3B-Instruct Q4_K_M | ~17.3 GB | 24 GB | 8K–16K, 1 slot | Default; strong coding and agentic tool use; 30.5B total / 3.3B active |
| Qwen3.5-27B Q4_K_M | ~16.7 GB | 24 GB | 16K, 1 slot | Dense general/coding model; stronger but usually slower than the MoE default |
| Qwen3.5-35B-A3B 4-bit | ~22 GB | 32–48 GB | 16K–32K | Faster MoE option; use 48 GB for headroom and voice on GPU |
| Qwen3-Coder-30B-A3B-Instruct Q8_0 | ~30.3 GB | 48 GB | 32K, 1–2 slots | Higher fidelity; substantially more storage and VRAM |

Do not choose the 480B Qwen3-Coder model for a single 24/48 GB GPU. Its
quantized weights require a multi-GPU or very large unified-memory system.
The stack serves text GGUFs; multimodal Qwen variants also need their matching
projection file and additional llama-server configuration.

Useful upstream references:

- <https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct>
- <https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF>
- <https://huggingface.co/Qwen/Qwen3.5-27B>
- <https://huggingface.co/unsloth/Qwen3.5-27B-GGUF>

## Automatic download

Set these values in `.env`; the llama container downloads a missing file once
and keeps it on the persistent model volume:

```dotenv
HF_TOKEN=
HF_REPO_ID=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF
HF_FILENAME=Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
HF_MODEL_PATTERN=
MODEL_PATH=/models/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf
MODEL_ALIAS=qwen3-coder-30b-a3b
```

`HF_TOKEN` may remain empty for a public repository. For a gated repository,
create a read-only Hugging Face token and supply it through `.env` or the RunPod
secret environment-variable UI.

For a sharded GGUF, download all shards with a pattern and point `MODEL_PATH`
at the first shard:

```dotenv
HF_MODEL_PATTERN=Model-Q4_K_M-*.gguf
MODEL_PATH=/models/Model-Q4_K_M-00001-of-00003.gguf
```

## Manual downloads

From a host with the Hugging Face CLI:

```bash
hf download \
  unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF \
  Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  --local-dir /workspace/jarvis-data/models
```

For Qwen3.5-27B:

```bash
hf download \
  unsloth/Qwen3.5-27B-GGUF \
  Qwen3.5-27B-Q4_K_M.gguf \
  --local-dir /workspace/jarvis-data/models
```

## Switching models

1. Change `HF_REPO_ID`, `HF_FILENAME`, `MODEL_PATH`, and `MODEL_ALIAS`.
2. Adjust `LLAMA_CTX_SIZE` and `LLAMA_PARALLEL` for the base/RunPod image, or
   the corresponding `_24GB` / `_48GB` variables when using a Compose override.
3. Recreate llama-server and Open WebUI:

```bash
docker compose up -d --build --force-recreate llama open-webui
docker compose logs -f llama
```

No existing GGUF is deleted. Remove old files manually only after the new model
passes a smoke test.

## VRAM tuning

- `LLAMA_CTX_SIZE` is shared across parallel slots. With `65536` and two slots,
  budget for approximately 32K tokens per concurrent request.
- Q8_0 KV cache is the default compromise. If a 24 GB card is close to OOM,
  first reduce context or batch size; use Q4_0 KV only if needed.
- Keep `LLAMA_N_GPU_LAYERS=99` for full offload. Reduce it to spill layers to
  system RAM when the model cannot fit, accepting lower throughput.
- CPU-hosted voice is the default on 24 GB so STT/TTS cannot evict LLM layers.
- `--jinja` is enabled by the entrypoint so model-native function calling works.
