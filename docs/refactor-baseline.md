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

## Stage 1 increment: internal compatibility listener

Compose now routes the existing bot to `http://gateway:9000`. The Gateway serves the four legacy routes on the Compose network. Only the Gateway and Whisper share InferenceNet. Neither service publishes a host port in this increment. The Gateway accepts one legacy inference request at a time with a 128 MiB HTTP body ceiling and returns the legacy busy shape at saturation. The worker independently rejects concurrent inference and shares its slot with idle unloading. The health path can respond through separate request threads.

The model environment, cache mount, segmentation and bot source are unchanged. InferenceNet is a dedicated bridge with egress because the incumbent worker can still download missing weights. Offline weight provisioning, authenticated VLAN HTTPS, the two-lane scheduler and versioned jobs are pending. This increment is a reviewable source checkpoint, not a Release A cutover.

Pending operator checks for this increment, when explicitly evaluating it on the target host:

```bash
docker compose config --quiet
docker compose config --services
docker compose exec -T bot python -c 'from urllib.request import urlopen; print(urlopen("http://gateway:9000/health", timeout=5).read().decode())'
docker compose port whisper 9000
docker compose port gateway 9000
```

With the candidate running, expect services `bot`, `gateway` and `whisper`, a healthy response naming the configured model, and no published port from the last two commands. During a long note, query health and check busy handling from a second caller. Repeat the baseline bot modes and segment-boundary fixture. Release A validation starts with the direct HTTPS smoke test once that listener and job API exist.

Before any rollout, record the baseline commit and retain the prior image, Compose project name, volume paths and host-port ownership. To revert this increment if it was started, run `docker compose stop gateway` from the candidate checkout. Return to the recorded baseline checkout, preserving its environment and Compose project name, and run `docker compose up -d --force-recreate whisper bot` using the recorded prior images. Confirm the previous host binding and bot health. Preserve all named volumes.
