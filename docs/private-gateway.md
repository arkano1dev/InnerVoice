# Private Voice Gateway and Ubuntu desktop

Candidate source handoff. All target-host, installed-provider, performance and desktop acceptance checks are pending. Keep local settings and operator evidence outside the checkout. The baseline and rollback inventory is in [refactor-baseline.md](refactor-baseline.md).

## Provision the server

Use Python 3.11+, Docker Engine with Compose v2, FFmpeg and the supported compute runtime. On Python 3.10 install tomli. Run commands from the repository root. For a fresh install clone the repository and switch to the candidate branch. For an existing installation inventory the running project, existing port and callers, named volumes, image IDs, weights and protected environment first. Keep the same Compose project name. Preserve the prior checkout and tag the actual previous bot and worker images locally before recreating containers. Protect or exclude spool, SQLite including WAL/SHM, client state, audio, secrets and private operator evidence from backups.

Provision a trusted local multilingual checkpoint before service startup. Use the incumbent model and verify its upstream checksum separately. The following variables are operator inputs: ROCM_BASE is the immutable runtime image reference, WEIGHTS the verified checkpoint file, SECRET_DIR the protected directory, and VLAN_IP the actual listener IP. The runtime override, if needed, belongs in the existing private Compose environment.

```bash
umask 077
docker build --build-arg ROCM_BASE="$ROCM_BASE" -t innervoice-worker:candidate whisper
docker image inspect --format '{{.Id}}' innervoice-worker:candidate
sha256sum "$WEIGHTS"
install -d -m 700 "$SECRET_DIR"
openssl req -x509 -newkey rsa:3072 -nodes -days 90 -subj '/CN=InnerVoice' \
  -addext "subjectAltName=IP:$VLAN_IP" -keyout "$SECRET_DIR/tls.key" -out "$SECRET_DIR/tls.crt"
./bin/innervoice config setup
./bin/innervoice credential --identity operator-smoke
./bin/innervoice credential --identity telegram-agent
./bin/innervoice config validate
./bin/innervoice config show
./bin/innervoice start
./bin/innervoice status
```

Use a new empty secret directory for certificate generation, or install an existing certificate and key there. Distribute only the trust certificate and a distinct random bearer secret per integration over a protected channel. Enter each bearer token at its masked credential prompt. The server stores its digest mapped to a stable integration label. Repeat the command with the same label to rotate. Ensure TLS files and clients.json are readable by the operator UID. Provision the bot token via the existing protected .env. The wizard asks for a real private bind IP and port, image tag plus image ID (or digest reference), local weight path, checksum and both engine profiles. Optional profile image and weights can remain empty until activation. Start with admission disabled. Set `deployment.allowed_networks` to the authorized private client IPv4 addresses or CIDRs, separated by commas. The HTTPS listener rejects peers outside that list before processing bearer credentials. It uses the actual socket peer address. Apply the same restriction at the host/VLAN boundary and check effective Docker forwarding, including that the authorized source arrives with its original address. Check that a connection from outside the list is denied. Docker publishes only the specified IP.

On a prepared host, `start --setup` combines the configuration wizard with startup. Plain start and restart are unattended. First activation replaces the old port mapping; schedule it only after recording the baseline. The worker has no published host port. Legacy routes are available inside Compose on gateway:9000. The private HTTPS port serves only the versioned API.

## Check ROCm compatibility with the previous deployment

A GPU reported by `/gpu-check` can still fail when PyTorch first allocates or
runs on it. If Gunicorn replaces a worker with exit status 139, reproduce the
failure with a small tensor before testing audio. If the previous deployment
used a graphics target override, supply its effective
`HSA_OVERRIDE_GFX_VERSION` in the operator shell or protected `.env`. If
Docker cannot resolve the `render` group in the worker image, set
`WHISPER_RENDER_GROUP` to the owning numeric GID of the intended
`/dev/dri/renderD*` device on the host.

For a Docker-only bot smoke test with the previous runtime settings, add
`compose.rocm-compat.yml` explicitly. This overlay disables seccomp filtering for
Whisper and shares the host IPC namespace. Use it only on a trusted host for
compatibility testing. The private HTTPS CLI uses the standard worker
configuration.

```bash
docker compose -f docker-compose.yml -f compose.rocm-compat.yml up -d --build
docker compose -f docker-compose.yml -f compose.rocm-compat.yml exec -T whisper \
  python -c 'import torch; print(torch.ones(8, device="cuda").tolist())'
```

Use the existing Compose project name when reusing its volumes, and reuse the
previously validated ROCm base image by digest if the floating base tag has
changed. After the tensor check succeeds, send two short notes to the bot and
compare initial and warm response times. If GPU execution still fails, compare
against the previous working image with the same operator settings.

## Gateway quickstart

1. Prepare a Linux host with Python 3.11+, Docker Engine with Compose v2, the selected inference runtime, a private VLAN IPv4 address, and a TLS certificate that covers that address. Provision a verified local multilingual model and a worker image with a recorded digest or verified local image ID. See the [server provisioning guide](#provision-the-server) for the build, certificate, model checksum and Telegram bot token.
2. Clone and configure the refactor branch as the deployment operator.

   ```bash
   git clone --branch refactor/private-voice-gateway https://github.com/arkano1dev/InnerVoice.git
   cd InnerVoice
   ./bin/innervoice config setup
   ./bin/innervoice credential --identity operator-smoke
   ./bin/innervoice config validate
   ./bin/innervoice start
   ./bin/innervoice status
   ```

3. Set `deployment.bind` to the server's private address, `deployment.port` to the agreed listener port, and `deployment.allowed_networks` to the comma-separated private IPv4 addresses or CIDRs for the clients. Include the direct smoke client. Enforce the same restriction at the host/VLAN boundary. Keep deployment settings, weights, credentials, certificates, audio and runtime state outside the checkout. The worker and the bot compatibility listener have no published host ports.
4. Start with admission disabled while checking the private listener and bot. When ready for the first direct smoke test, enable jobs and restart.

   ```bash
   ./bin/innervoice config set deployment.enable=true
   ./bin/innervoice restart
   python3 clients/smoke.py "$CLIENT_CONFIG" "$FIXTURE" "$EXPECTED_TEXT"
   ./bin/innervoice dashboard
   ```

   Use the [direct smoke guide](#direct-smoke-first) to prepare the protected client TOML, trust certificate, bearer token, synthetic audio, and expected English text.

## Use the clients

| Client | Setup and behavior |
| --- | --- |
| Agent with a Telegram connector | Give the agent its own Gateway identity and configure [the speech provider](#agent-connection) in the installed framework. The agent handles the Telegram event and receives one final UTF-8 prompt. Verify its silence, retry, timeout and external fallback settings. |
| InnerVoice Telegram bot | Configure the bot token during server provisioning. Send `/start` and a voice note, then verify the bot's fast and full modes, preferences and timestamps. |
| Ubuntu desktop | Follow [desktop setup](#desktop-role) to provision a separate identity and start the recorder under your X11 session. Press the hotkey to start and again to stop. If the original target cannot be verified, the edited text stays available in the editor. |

## CLI reference

Run commands from the repository root. Deployment edits apply on activation. Rotate an integration token by reusing its identity label so that it retains access to its jobs.

| Command | Behavior |
| --- | --- |
| `initialize`, `config setup`, `config edit` | Run the interactive deployment configuration wizard and save settings after confirmation. |
| `config show` | Show saved configuration with local addresses and paths redacted. Keep the output private. |
| `config validate` | Validate saved settings. Startup also checks the worker image, model weights, TLS identity and credential map. |
| `config set SECTION.KEY=VALUE [SECTION.KEY=VALUE ...]` | Save one or more validated edits. Booleans take `true` or `false`. |
| `credential --identity LABEL` | Register or rotate a token at a masked prompt. Use a distinct random token for each client. |
| `start [--setup] [--engine ENGINE]` | Start the prepared deployment. Optionally run setup first or select an engine. Use restart when changing an active deployment. |
| `restart [--engine ENGINE]` | Validate and prepare the candidate, drain work, and activate it. Engine names are `openai-whisper` and `faster-whisper`. A drain timeout aborts the cutover. |
| `stop` | Drain and stop the service containers. |
| `status` | Show Compose service state, the saved and active engine settings, and pending edits. |
| `dashboard [--watch] [--job ID]` | Show local job and service metadata, optionally refreshing continuously or filtering by job ID. |
| `desktop setup`, `desktop start`, `desktop status`, `desktop stop` | Configure, start, inspect or stop the desktop user service. |
| `desktop run` | Run the configured desktop recorder in the foreground from a supported graphical session. |

## Update an existing Gateway deployment

1. Record the current commit with `git rev-parse HEAD`. Keep the prior checkout or commit, verified image IDs, active configuration and Compose project name. Preserve the existing volumes and protected state. If updating from the original bot/worker deployment, use the [migration baseline](#provision-the-server) to inventory ports, callers, images and volumes before cutover.
2. Update the existing branch with a fast-forward. Review the changes, build or provision the new worker image when its source or dependencies change, and update the worker image and model identifiers using `config set`. Provision the worker before calling `restart`, which builds the bot and Gateway.

   ```bash
   git status --short --branch
   git switch refactor/private-voice-gateway
   git pull --ff-only origin refactor/private-voice-gateway
   ./bin/innervoice config validate
   ./bin/innervoice restart
   ./bin/innervoice status
   ```

3. Run direct smoke, check the bot, then verify the installed agent connector. Update each client checkout separately. For Ubuntu, stop the recorder before updating and restart it from the same user session afterward. If the drain times out, let accepted work finish before retrying. If activation or verification fails, use the saved image, checkout and active configuration with the [maintenance and rollback guide](#maintenance-and-acceptance).

## Direct smoke first

Install requests on the client (and tomli on Python 3.10). Save this TOML outside the repository. Replace all placeholder strings including paths. Use a unique restricted state directory per integration.

```toml
[client]
url = "https://GATEWAY_HOST:PORT"
ca = "/absolute/private/client/ca.crt"
token_file = "/absolute/private/client/bearer"
state_dir = "/absolute/private/client/state"
deadline = 300
source_ip = ""
source_port = 0
```

```bash
./bin/innervoice config set deployment.enable=true
./bin/innervoice restart
python3 clients/smoke.py "$CLIENT_CONFIG" "$FIXTURE" "$EXPECTED_TEXT"
./bin/innervoice dashboard
./bin/innervoice dashboard --watch
./bin/innervoice dashboard --job "$JOB_ID"
```

Supply a synthetic Spanish utterance with a known name and number and the expected English sentence in a separate private file. The smoke command compares normalized text and prints timing and verdict, including no transcript. Review semantically valid alternatives locally. Confirm that health, admission, cleanup and polling survive service recreation. Use the protected direct API command below when testing replay and ownership; never paste credentials or response text into reports.

```bash
python3 - "$CLIENT_CONFIG" "$FIXTURE" <<'PYTHON'
import secrets, sys, time
sys.path.insert(0,"clients")
from provider import Client, tomllib
with open(sys.argv[1],"rb") as f:
    client=Client(tomllib.load(f)["client"])
key=secrets.token_urlsafe(32)
def submit():
    with open(sys.argv[2],"rb") as audio:
        return client.call("POST","/v1/jobs",files={"audio":("fixture",audio)},headers={"Idempotency-Key":key})
job=submit()
assert submit()["job_id"]==job["job_id"]
until=time.monotonic()+3600
while time.monotonic()<until:
    result=client.call("GET",job["status_url"])
    if result["state"] in ("failed","succeeded"):
        print(job["job_id"],result["state"])
        assert result["state"]=="succeeded" and result["result"]["language"]=="en"
        break
    time.sleep(1)
else:
    raise TimeoutError("Operator polling deadline")
PYTHON
```

API requests use Bearer authentication. POST /v1/jobs takes multipart audio plus Idempotency-Key. WAV, Ogg, FLAC, MP3, AAC, MP4 and WebM use bounded local decoding. Success returns 202, job_id, state, status_url. GET the relative URL until succeeded or failed. Retry 429/503 with backoff. Invalid auth is 401, a denied source network is 403, missing or other-owner job is 404, conflicting key is 409, expired delivery is 410, oversized audio is 413, unsupported format is 415, undecodable audio is 422. All clients share the selected model. The compatibility listener preserves the bot's own task and language selection.

## Agent connection

Set up the installed agent's command STT provider only after direct smoke. Example mapping (confirm syntax and timeout in the installed framework):

```yaml
stt:
  provider: innervoice
  providers:
    innervoice:
      type: command
      command: /absolute/path/to/clients/provider.py --config /absolute/private/client.toml {input_path} {output_path}
      format: txt
      normalize: false
```

Disable external fallback in the actual agent. The adapter opens its input read-only and atomically writes final UTF-8 output. Supply INNERVOICE_EVENT_ID for stable event replay. Without a host event identity the provider requires an output path unique to the invocation and stable across its retries. A zero-byte output represents silence; verify that the host suppresses the turn. An ambiguous publication exits with an error and requires the host to reconcile delivery. Record the installed agent version and enforce a provider timeout longer than the adapter deadline. Verify exactly one turn and retention of host-owned input separately.

## Desktop role

On a graphical Ubuntu workstation install Python, Tk, PortAudio, the AT-SPI Python bindings, a microphone, and an X11 session supporting global shortcuts. The server and bot are independent. Run as the logged-in user:

```bash
sudo apt install python3-venv python3-tk python3-pyatspi libportaudio2
python3 -m venv --system-site-packages "$HOME/.local/share/innervoice-desktop-env"
. "$HOME/.local/share/innervoice-desktop-env/bin/activate"
pip install requests tomli sounddevice pynput pystray Pillow
./bin/innervoice desktop setup
./bin/innervoice desktop start
./bin/innervoice desktop status
./bin/innervoice desktop stop
```

Provision a separate desktop identity with the server CLI before setup. Set the private URL, trust root and token path. Leave source IP empty and port zero for OS selection. Press the selected hotkey to start, then press again to stop. The editor appends indexed text while preserving edits. On reconnect the client compares durable fragment indexes. On restart it recovers the saved capture. After recording stops, **Retry sync** resumes a failed transfer from saved audio. If the Gateway session expired or failed permanently, that action starts a replacement session while keeping edited text. Local fragments are removed after the editor has saved the result. Automatic insertion requires the same live editable accessibility object to be focused. Recovered captures have no live destination identity and remain in the editor. An uncertain insertion retains text for inspection and manual copy. Desktop stop affects only the user service. After stopping, archive or remove the user's config, state and virtual environment when decommissioning.

GET /v1/capabilities advertises sessions. POST /v1/sessions with Idempotency-Key, PUT a mono 16 kHz signed 16-bit WAV to /v1/sessions/{id}/fragments/{index}, GET the session and POST /v1/sessions/{id}/finalize with last_index. Missing indexes are returned in a 409 response. A deliberate recover_prefix request may discard stored fragments beyond the contiguous prefix. Finalization returns a pollable job.

## Maintenance and acceptance

Keep the Gateway allowlist and the host firewall aligned. The private overlay isolates the worker network from external routes and the worker loads local weights. The worker starts with Docker's default seccomp profile, its own IPC namespace and access to the configured compute devices. Include these settings in the first host check. Validate the final image digests, operating system packages, GPU runtime and effective port exposure on the target before promotion.

- Use `config set` for validated edits and `restart` to apply them. Config, credentials, data and local weights persist outside containers. Record effective image digest, private runtime overrides, exact installed packages, warm/cold state and host capacity in private evidence.
- For the optional Faster Whisper trial, supply a pinned ROCm base with compatible Torch and the ROCm CTranslate2 4.9.0 wheel already installed. Build with `docker build -f whisper/Dockerfile.faster --build-arg CT2_ROCM_IMAGE="$CT2_BASE" -t innervoice-faster:candidate whisper`. Provision the complete converted model directory. Its manifest digest hashes each sorted relative filename plus NUL and its bytes. Fit the trial to the actual GPU and verify local inference. Set the faster-whisper image, image_id, weights and digest with config set, then run `restart --engine faster-whisper`. Restore with `restart --engine openai-whisper`. Roll back a failed transition with the previous image and saved manifest. If reverting to the pre-Gateway application, stop candidate services, restore the previous Compose project and exact retained images/environment, recreate the original bot and worker, and verify the host binding. Keep all data volumes.
- Trigger restart during a long inference. Verify one ordered result, absence of duplicate segments, prompt progress from a second owner and a short legacy request. Check replays, different bytes under the same key, unknown owner, credential rotation and revocation, expired results, oversized decoded content, silence and names/numbers. Verify bot fast/full, timestamps, defaults/reset and an explicit false override with two distinct users.
- Compare three or more repeats per fixed fixture per condition via `python3 clients/smoke.py "$CLIENT_CONFIG" "$FIXTURE" "$EXPECTED_TEXT" --repeats 3`. Use a separate one-hour deadline for long fixtures. Measure cold/warm, short-note absolute latency, real-time factor, boundary speech, language mixing and noise, memory, disk, busy conditions and cleanup. Require warm end-to-end RTF at most 1 per representative voiced clip; compare English-source word error rate and rubric-scored translation fidelity against the incumbent. Require zero new critical name, number or meaning errors. Check direct one-hour results separately from actual Telegram download limits.
- Validate installed provider outage, no external fallback, deadline and silence behavior. Exercise desktop focus changes, record/stop, offline recovery, retained edits, restart during insertion, short final fragment and clipboard fallback. Record observed results by candidate SHA using synthetic IDs only. Keep the real host, fixture content, runtime measurements and deployment manifest private.
