# MiniMax Music 3 — web UI + hot model server

```
browser ──> python proxy (music3_webui/server.py, :8321)
               │  validates + queues
               ▼
          audiocpp_server (:8080)  ← model loaded once, kept resident (HOT)
```

- `build.sh` (repo root) — CMake/CUDA build of `audiocpp_server` + `audiocpp_cli`
  (custom model set: `minimax_music3` only). Portable across hosts with the
  same repo layout; idempotent.
- `run.sh` (repo root) — starts the hot server (model loads at startup) and
  the web UI, waits for health, prints URLs. Safe to re-run (port checks).
- `music3_webui/server.py` — dependency-free (stdlib) proxy: serves the page,
  validates jobs, forwards to the hot server's `POST /v1/tasks/run`, stores
  the returned WAV. Loads no model itself.
- `music3_webui/server_config.json` — hot server config: binds `0.0.0.0:8080`,
  `lazy_load: false` (load at startup), `idle_unload_ms: 0` (never unload),
  model id `music3` at `/home/justin/local_llm/huggingface/MiniMax-Music3-GGUF`.

## Run

```bash
./build.sh          # once (and after model/spec changes)
./run.sh            # foreground; Ctrl-C stops what it started
nohup ./run.sh >run.log 2>&1 &   # or keep it running in the background
```

Web UI: `http://<host>:8321` (0.0.0.0). Generated WAVs: `music3_webui/output/`.
Logs: `music3_webui/logs/server.log`, `music3_webui/logs/ui.log`.

## mlx-serve-compatible endpoint

The hot server also exposes `POST /v1/audio/music-generations` (mlx-serve
contract, synchronous): `{prompt, lyrics | instrumental, duration_seconds,
seed}` in, raw WAV bytes out (stereo 16-bit 44.1 kHz). Missing-field errors
mirror mlx-serve's validation text. See `app/server/api.md`.

```bash
curl http://<host>:8080/v1/audio/music-generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"<structured caption>","lyrics":"[verse]\n...","duration_seconds":180,"seed":1}' \
  -o song.wav
```

To point pilgrim-dot-farm at this box, set `hosts.mlx_serve` in
`pilgrim/config.yaml` to `http://<host>:8080` — no code changes needed
(`prompt`/`lyrics`/`duration_seconds`/`seed`/`instrumental` all map 1:1).
Differences from mlx-serve: no 4096-token AR cap (long songs don't hard-stop
mid-note), `duration_seconds` range is 1-600 (not 1-360), default is 60 s as
on mlx-serve, and the tuning pass-throughs `num_inference_steps` /
`guidance_scale` / `ar_guidance_scale` / `top_k` are accepted.

Env overrides:

| Var | Default | Meaning |
|---|---|---|
| `CUDAToolkit_ROOT` | auto (`/usr/local/cuda` or `nvcc` on PATH) | build.sh CUDA toolkit |
| `JOBS` | `nproc` | build.sh parallelism |
| `HOT_SERVER` | `http://127.0.0.1:8080` | server.py hot server URL |
| `MODEL_ID` | `music3` | server.py model id (config file) |
| `PORT` / `HOST` | `8321` / `0.0.0.0` | server.py listen address |
| `UI_PORT` | `8321` | run.sh web UI port |

## Why the hot server (and the mem_saver gotcha)

Each cold `audiocpp_cli` run pays ~10–35s of model load + graph build before
any generation. The hot server loads once at startup and reuses the session,
so steady-state latency is pure compute:

| Path (8s song, 20 steps) | Latency |
|---|---:|
| CLI spawn, cold page cache | ~60s |
| CLI spawn, warm page cache | ~43s |
| hot server + `mem_saver=true` (default!) | ~39s |
| **hot server + `mem_saver=false`** | **~14s** |

`mem_saver=true` releases large stages (LM 5.3GB, flow 1.3GB, …) after each
request and re-uploads them on the next — on a 121GB unified-memory box that
only costs time, so the config sets it to `false`. On a memory-tight host set
`minimax_music3.mem_saver` back to `true` in `server_config.json`.

The hot server runs one request per model at a time; extra submissions queue
(FCFS). Cancel only works before a job is submitted to the hot server.

## Prompting notes (from the
[prompting guide](https://huggingface.co/spaces/multimodalart/minimax-music3-prompting-guide))

- Every generation is driven by exactly two texts: **tagged lyrics** + a
  **structured caption**. Keep them separate.
- Each section tag goes on its **own line** — text on the same line as a
  leading tag is silently dropped (the proxy auto-splits this case).
- The caption should have exactly three headings, ~250–450 words:
  1. **Global Metadata** — genre, bpm, (optional) key/scale, emotional arc,
     application scenarios/imagery, sonics & production profile.
  2. **Vocal Details** — state vocal gender & timbre explicitly (leaving
     vocals unspecified is the #1 cause of unwanted instrumental drift).
     For instrumentals, say so explicitly.
  3. **Arrangement** — primary/secondary instruments and their lifecycle.
- `duration` is an upper bound (AR frame budget); the song ends naturally
  when the lyrics run out. Short lyrics make short songs.
