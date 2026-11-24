# InnerVoice refactor baseline

Stage 0 source inventory. Clean `main` at `0b8edc9bb812cc4e64eefb24bebd00b196c8ba2b`.
All runtime checks are pending operator execution.

| Entry point | Source contract |
| --- | --- |
| Compose bot | `bot/Dockerfile` builds from `./bot` and runs `bot/bot.py`. The bot uses `WHISPER_API_URL` on TelegramNet and keeps pending audio in the mounted audio volume. User preferences are process-local at baseline. |
| Compose Whisper | `whisper/Dockerfile` builds from `./whisper`. Gunicorn serves one process with one request thread. The host publishes port 9000 on all interfaces. |
| Standalone bot | Root Dockerfile and `python bot.py` run the separate in-process bot. It loads its own model and does not call the Compose API. The backup bot scripts are not entry points. |
| CPU API image | `whisper/Dockerfile.cpu` is an alternative build context based on CPU Torch. The server currently insists on CUDA when loading the model, so this is not a verified CPU inference path. |
| Operations | `Makefile` and `deploy.sh` wrap Compose, including service-scoped bot and Whisper targets. |

Compose selects Medium, FP16, beam size 1, no previous-text conditioning, Torch compilation, and a 1536 MiB free VRAM threshold. Loading is lazy unless `MODEL_PRELOAD` is enabled. The Dockerfile uses a floating ROCm/PyTorch tag and unpinned Python dependencies. The cache volume is mounted at `/root/.cache`.

`POST /transcribe` takes multipart `audio`, optional `language`, `task`, and `return_segments`. It returns `text` and optionally `segments`. Missing audio returns 400. Busy and OOM conditions return 503 with `gpu_busy` or `gpu_oom`. `GET /can-transcribe` returns the admission signal used by the bot queue. `GET /health` returns the configured model and available VRAM fields. `GET /gpu-check` returns GPU probe and model-loaded state.

The Compose bot accepts Telegram voice events, downloads the file and uses FFmpeg to normalize to mono 16 kHz signed 16-bit WAV. Above 1 MiB it sends numerically ordered, nominal 30-second FFmpeg segments, including the final file if present. Requests are independent and timestamps are segment-local. Fast mode sends a translation request. Combined mode sends transcription followed by translation. The bot owns progress, output formatting and busy recovery. Record the actual Telegram container and codec with an authorized synthetic note.

The checkout contains no test suite or provider registration. The proposed provider contract is a command that receives input and output paths and supplies a final UTF-8 text file. Confirm the installed framework version, configured timeout, input ownership and output handling after the first direct Gateway smoke test.

## Operator baseline

From the existing production checkout, record the effective service configuration and image ID without exporting secrets.

```bash
git rev-parse HEAD
git status --short --branch
docker compose version
docker compose config --quiet
docker compose config --services
docker compose ps
docker inspect --format '{{.Image}}' "$(docker compose ps -q whisper)"
ss -ltn 'sport = :9000'
```

At the reference checkout the service list is `bot` and `whisper`. On the live host, record the port owner and callers, VLAN address and certificate identity, image digest, live profile, ROCm and Torch versions, disk headroom, model cache and existing volume names. Keep the existing deployment and image available for rollback. Use the same synthetic note in fast and combined modes and record progress, timestamps and busy behavior. Include speech just after 30 seconds in a fixture long enough to cross the 1 MiB split threshold.
