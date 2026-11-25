# Private Voice Gateway and Ubuntu desktop

Candidate source handoff. All target-host, installed-provider, performance and desktop acceptance checks are pending. Keep local settings and operator evidence outside the checkout. The baseline and rollback inventory is in [refactor-baseline.md](archive/refactor-baseline.md).

## Provision the server

Use Python 3.11+, Docker Engine with Compose v2, FFmpeg and the supported compute runtime. On Python 3.10 install tomli. Run commands from the repository root. For a fresh install clone the repository and switch to the candidate branch. For an existing installation inventory the running project, existing port and callers, named volumes, image IDs, weights and protected environment first. Keep the same Compose project name. Preserve the prior checkout and tag the actual previous bot and worker images locally before recreating containers. Protect or exclude spool, SQLite including WAL/SHM, client state, audio, secrets and private operator evidence from backups.

Provision a trusted local multilingual checkpoint and a compatible worker image first. Reuse the known working worker image if it supports the Gateway profile checks, or build the candidate worker with your validated ROCm runtime:

```bash
docker build --build-arg ROCM_BASE="$ROCM_BASE" -t innervoice-worker:candidate whisper
```

Set `ROCM_BASE` to the approved base image reference by digest. Keep any host-specific runtime overrides and standalone bot credentials in the protected repository-root `.env`. See [ROCm compatibility checks](#check-rocm-compatibility-with-the-previous-deployment) and [server audio and Telegram](#server-audio-and-telegram).

Run the guided setup from the repository root:

```bash
./bin/innervoice start --setup
```

Enter the server's IPv4 address on its private LAN, VLAN or virtual network, HTTPS port, allowed client IPs, CIDRs or Nord names, local worker image, multilingual model name and path to its weights. Setup reads the image ID from Docker and computes the checkpoint SHA-256. Compare the displayed digest with your trusted source before accepting a new or changed checkpoint. Use `config setup` to save the settings without starting services, and `config edit` or `config set` for advanced settings. An existing running Gateway takes saved changes on `restart`.

Setup reuses the saved certificate and key if they match the chosen IP. If neither exists, it uses OpenSSL to create a self-signed certificate valid for 90 days. You can also install your own `tls.crt` and `tls.key` in the protected secrets directory before setup. For an existing CA-issued certificate, put its trust chain in `ca.crt` in the same directory if setup is creating a smoke-test client.

On first setup, the wizard creates a local smoke-test client if the server has no credential map. Its bearer file, trust certificate and `client.toml` are under `~/.config/innervoice/clients/operator-smoke` by default. From another authorized machine, copy the public trust certificate and bearer file over a protected channel and adapt the paths in the client TOML. For the Telegram agent, provision a separate credential with `./bin/innervoice credential --identity telegram-agent --scope jobs`.

The setup accepts RFC 1918 private ranges and the RFC 6598 shared range (`100.64.0.0/10`) for both the server IP and the client list. On a shared-address virtual network, use the IP assigned to the server and list each authorized client's IPv4 or full Nord name. See [direct Meshnet access](../INSTALL.md#direct-meshnet-access) for resolution and troubleshooting.

The HTTPS listener rejects peers outside the configured private IPv4 addresses or CIDRs before processing bearer credentials. Apply the same restriction at the host/VLAN boundary and verify that Docker forwards the authorized source address. The worker has no published host port. Legacy routes are available inside Compose on `gateway:9000`. The private HTTPS port serves only the versioned API.

First activation replaces the old port mapping. Schedule it after recording the baseline. Fresh setup leaves API job admission disabled until the direct smoke check.

## Daily operation and network boundaries

| Goal | Command or setting |
| --- | --- |
| Try a different model | `./bin/innervoice config worker`, then `./bin/innervoice restart`. The worker wizard reuses the saved endpoint, allowlist and credentials. |
| Change decoding only | For example, `./bin/innervoice config set openai-whisper.beam=3`, then restart. |
| Leave the server available | Start once. Compose uses `unless-stopped` for the gateway, bot and worker. Enable Docker at boot on the server. |
| Switch the workstation on or off | `./bin/innervoice-desktop power`. For a shell shortcut, define `alias ivpower='"/absolute/path/to/InnerVoice/bin/innervoice-desktop" power'`. Native installations use `./bin/innervoice desktop power`. |
| Check the connection on demand | `./bin/innervoice-desktop check` verifies TLS, token and API from the running container. `status` shows process state. |
| Show the current editor | `./bin/innervoice-desktop show` or **Show editor** in the indicator menu. |

**Mesh access.** Run Meshnet on the Gateway host and enable incoming access for the linked agent or workstation. Use the Gateway's Meshnet endpoint and keep the peer's traffic routing and LAN access disabled. Restrict HTTPS at the Docker-aware ingress and retain the Gateway's source allowlist and scoped bearer. See [direct Meshnet access](../INSTALL.md#direct-meshnet-access).

**Listener exposure.** The default publishes HTTPS on the selected private host address. For a multihomed gateway, `config set deployment.listen_all=true` publishes that port on **every IPv4 interface**, including any public interface. Set the Docker-aware ingress policy first. Keep `deployment.bind` as the actual private address clients verify against the certificate. Extra client-facing addresses need their own matching certificate SANs. Set `deployment.listen_all=false` and restart to restore single-address publishing. Never publish the worker or the bot compatibility port. Docker's forwarding rules can bypass ordinary UFW input rules. Verify reachability from an allowed peer and denial from an unauthorized peer and the public side. See [Docker packet filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/) and the [Meshnet permissions guide](https://support.nordvpn.com/hc/en-us/articles/20278389297041-Guide-for-Meshnet-users).

**Workstation privacy.** The recorder opens the microphone during capture. The panel helper reads a state label and sends local commands through a user-owned socket. The container has no desktop session bus mount. Successful text goes to the system clipboard, where clipboard managers and remote clipboard sync may retain it. Fragments and editor text are readable by the local account and its administrators. Local recording history retains audio and transcripts across restarts, including after successful transcription. The user owns retention. An hour of mono PCM at 16 kHz uses about 115 MB. Review clipboard history, backups and local disk encryption on sensitive workstations.

## Check ROCm compatibility with the previous deployment

The CLI keeps audio submissions paused while it checks HTTPS and runs a small PyTorch tensor computation in the running Whisper container. Admissions open after both checks pass and the active configuration is saved. If activation fails or is interrupted, retry `start` or `restart` after fixing the reported problem. Send a short recording after startup to check model loading and transcription.

If the tensor test fails or Gunicorn replaces a worker with exit status 139, compare the effective runtime settings with the last working deployment. If that deployment used `HSA_OVERRIDE_GFX_VERSION`, restore its verified value in the server's protected `.env` and restart. Keep the value private and specific to the host. The private launcher resolves the default `render` group to the host render node's numeric GID before starting Whisper.

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

1. Prepare the host, a trusted multilingual checkpoint and a compatible worker image. Follow [server provisioning](#provision-the-server).
2. In the candidate checkout run `./bin/innervoice start --setup`, then `./bin/innervoice status`. Use `./bin/innervoice config setup` if you want to configure before starting.

3. Set `deployment.bind` to the server's private address, `deployment.port` to the agreed listener port, and `deployment.allowed_networks` to the comma-separated private IPv4 addresses, CIDRs or full Nord names for the clients. Include the direct smoke client. Enforce the same restriction at the host/VLAN boundary. Keep deployment settings, weights, credentials, certificates, audio and runtime state outside the checkout. The worker and the bot compatibility listener have no published host ports.
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
| Ubuntu desktop | Follow [desktop setup](#desktop-role) to provision a separate identity and start the recorder from your graphical session. Press the hotkey to start and again to stop. Edit the transcript and copy it into the destination application. |

## CLI reference

Run commands from the repository root. Deployment edits apply on activation. Rotate an integration token by reusing its identity label so that it retains access to its jobs.

| Command | Behavior |
| --- | --- |
| `initialize`, `config setup` | Run the guided setup and save settings after confirmation. |
| `config edit` | Review and edit advanced settings interactively. |
| `config show` | Show saved configuration with local addresses and paths redacted. Keep the output private. |
| `config validate` | Validate saved settings. Startup also checks the worker image, model weights, TLS identity and credential map. |
| `config set SECTION.KEY=VALUE [SECTION.KEY=VALUE ...]` | Save one or more validated edits. Booleans take `true` or `false`. |
| `credential --identity LABEL` | Register or rotate a token at a masked prompt. Use a distinct random token for each client. |
| `start [--setup] [--engine ENGINE]` | Start the prepared deployment. Optionally run setup first or select an engine. Use restart when changing an active deployment. |
| `restart [--engine ENGINE]` | Validate and prepare the candidate, drain work, and activate it. Engine names are `openai-whisper` and `faster-whisper`. A drain timeout aborts the cutover. |
| `stop` | Drain and stop the server's Compose project. Stop any remaining Gateway, Whisper or bot containers labeled for that project and checkout. |
| `status` | Show Compose service state, the saved and active engine settings, and pending edits. |
| `audit` | Use a locally installed Grype to scan the immutable image IDs of the running Gateway, worker and optional bot. High or critical findings and scanner errors return failure. |
| `dashboard [--watch] [--job ID]` | Show local job and service metadata, optionally refreshing continuously or filtering by job ID. |
| `desktop setup`, `desktop start`, `desktop status`, `desktop stop` | Configure, start, inspect or stop the native desktop user service. Use `./bin/innervoice-desktop stop` for the Docker recorder and its one-off setup containers. |

If InnerVoice containers remain after shutdown, inspect their Compose project and working directory. Compose normally derives the project from the checkout directory. An earlier checkout or project override can leave another stack running on the same host.

```sh
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}'
```

For a separate deployment, run the shutdown command from its checkout with the project settings used to start it. If that checkout is unavailable, confirm the leftover container IDs and wait for any active transcriptions to finish before passing those IDs to `docker stop`. Check `docker ps` again. Stopping containers preserves their files and volumes.
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

Install requests on the client (and tomli on Python 3.10). Use the generated local smoke-test TOML, or create this TOML outside the repository. Replace all placeholders with the client machine’s values. Use a unique restricted state directory per integration.

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

On the agent host, install Python and `requests` (also `tomli` for Python 3.10). Run the wizard as the identity that executes the provider.

```bash
./bin/innervoice agent setup
```

The wizard verifies access to the jobs API, then saves the endpoint, CA reference and owner-only bearer under `~/.config/innervoice-agent/`. Use the absolute path to the saved `settings.json` in the provider command below. Existing TOML configurations remain supported. Issue separate tokens on the gateway with `credential --identity telegram-agent --scope jobs --peer "$AGENT_MESH_IP"`. Use the exact source the gateway sees. The desktop equivalent is `--scope sessions`. After updating the running gateway, re-register existing identities with the desired scope and press Enter at the hidden prompt to keep their token. Earlier unscoped tokens retain both APIs until re-registered. To revoke access remove that identity from the private credential map while the operator CLI is idle. New requests read the map again.

Set up the installed agent's command STT provider only after direct smoke. Example mapping (confirm syntax and timeout in the installed framework):

```yaml
stt:
  provider: innervoice
  providers:
    innervoice:
      type: command
      command: python3 /absolute/path/to/clients/provider.py --config /absolute/private/settings.json {input_path} {output_path}
      format: txt
      normalize: false
```

Disable external fallback in the actual agent. The adapter opens its input read-only and atomically writes final UTF-8 output. Supply INNERVOICE_EVENT_ID for stable event replay. Without a host event identity the provider requires an output path unique to the invocation and stable across its retries. A zero-byte output represents silence; verify that the host suppresses the turn. An ambiguous publication exits with an error and requires the host to reconcile delivery. Record the installed agent version and enforce a provider timeout longer than the adapter deadline. Verify exactly one turn and retention of host-owned input separately.

## Desktop role

### Run the desktop client in Docker

On the machine with the keyboard and microphone, open a graphical terminal in a local Wayland session. Use Docker Engine with Compose and PulseAudio or PipeWire-Pulse. The container uses the Wayland display socket and the session audio socket. It captures locally and sends audio over HTTPS to Whisper on the server. Use **Copy** in the editor to paste the result into another app.

On the Gateway server, register a client with `./bin/innervoice credential --identity desktop-hotkey --scope sessions`, allow the workstation's source IP, and enable API jobs. Keep the token you entered on the server. For a self-signed Gateway, copy its public `tls.crt` from the default server path `~/.config/innervoice/secrets/tls.crt` to `~/.config/innervoice-desktop/ca.crt` on the workstation. If the Gateway uses a private CA, copy that CA certificate instead. Use `system` at the trust prompt for a certificate trusted by the container's system trust store.

Run these commands from the workstation's checkout:

```bash
./bin/innervoice-desktop install
./bin/innervoice-desktop shortcut
```

Enter the Gateway's full HTTPS URL and accept the suggested certificate path. Paste the registered token at the hidden prompt or reuse a local bearer file. The wizard checks TLS, the token, and support for desktop recording before saving. It uses the default microphone and retains advanced settings in an existing configuration. The wizard does not schedule an audio job; the first short recording verifies admission and inference. The `shortcut` command prints the full command to register in your desktop settings. In Ubuntu GNOME, open **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**. Add **InnerVoice**, paste the printed command and assign **Super+Shift+F8** or another free combination. The compositor calls `toggle` in the running container.

For daily use, launch **InnerVoice** from Applications or run `./bin/innervoice-desktop open`. It reuses the desktop image and your saved settings. After pulling changes to the desktop application or its dependencies, run `./bin/innervoice-desktop update` to rebuild and start it.

Press the shortcut once to record and again to stop. Existing shortcuts keep their binding until you change it in Settings. Check for conflicts in your compositor. Run `./bin/innervoice-desktop show` and click **Record** to test before assigning a shortcut. Use `./bin/innervoice-desktop status`, `logs` or `stop` to manage the client. Settings and saved recordings survive container restarts.

**Recorder and editor.** Click the microphone or use the shortcut to record. Pause releases the microphone. Play lets you listen to the saved take while paused. Stop playback and resume to append to the same take, or click the square to finish. Correct names directly in the plain-text editor during dictation. Long transcripts scroll inside the window and the recorder controls stay below the text. Use the window manager to move, resize, maximize or minimize the editor. Copy uses the current edited text.

**Local history.** The clock lists the ten most recent recordings with locally available audio, ordered by capture date. Select an entry to review, play or retry transcription. Audio is written in small batches to the desktop state directory during capture, independently of the Gateway. The existing limit is 60 minutes of captured audio per take. Stop the client before managing files manually. To archive a take, copy its entire directory from `~/.local/state/innervoice-desktop/`. The numbered files are consecutive signed 16-bit mono PCM, 16 kHz. Older versions may have already removed audio for completed transcripts.

**Storage controls.** Run `./bin/innervoice-desktop storage` on the workstation to see the host folder, audio usage and limit. The default audio budget is 2048 MiB. At the limit the recorder stops capture and retains the samples already written. Existing files are retained even if the limit is lowered below usage. Stop the recorder before changing the limit or cleaning. The following commands show a 4096 MiB budget and an optional cleanup:

```bash
./bin/innervoice-desktop storage limit 4096
./bin/innervoice-desktop storage clean --keep 10
./bin/innervoice-desktop storage clean --keep 10 --apply
```

The preview lists eligible folders. Applying removes only numbered audio fragments from completed takes outside the newest ten audio recordings. Transcripts and pending takes are retained. The menu limit itself never deletes audio.


The Docker recorder uses Qt 6 on Wolfi. It captures and plays audio through the mounted Pulse socket using PortAudio and the ALSA Pulse plugin. CI exercises the window on a headless Wayland compositor and tests both directions against a synthetic audio device.

For acceptance, with another Wayland app focused, record a five-second note with the shortcut and stop it with the same shortcut. Verify that text appears in the editor and can be copied into the other app. Repeat on your normal microphone and private network. Run the lightweight regression tests with `python3 -m unittest discover -s tests -v` from the checkout with `requests` and PyQt5 installed so the editor test runs.

#### Mesh VPN works on the workstation but times out in the container

First run the HTTPS capabilities check from the workstation itself. Use the same Gateway URL and public certificate as setup. If that check reaches the Gateway (including an HTTP 401 without a token) while setup times out inside Docker, opt in to host networking for the desktop client on Linux:

```bash
export INNERVOICE_DESKTOP_HOST_NETWORK=1
./bin/innervoice-desktop setup
./bin/innervoice-desktop start
```

Successful setup saves the selected mode. Existing host-network users can rerun setup once with the variable set and accept the saved connection values. Set it to 0 during setup to switch back. The container shares the workstation's network namespace in this mode, including access to its network interfaces and local services. The client continues to verify the Gateway certificate and send its bearer token. No ports are published for the desktop service. The default launcher uses Compose's bridge network until a different mode is saved or explicitly selected.

### Run directly on Ubuntu

On a graphical Ubuntu workstation install Python, Qt, PortAudio and a microphone. The server and bot are independent. Run as the logged-in user:

```bash
sudo apt install python3-venv python3-pyqt5 qtwayland5 libportaudio2
python3 -m venv --system-site-packages "$HOME/.local/share/innervoice-desktop-env"
. "$HOME/.local/share/innervoice-desktop-env/bin/activate"
pip install requests tomli sounddevice pynput
./bin/innervoice desktop setup
./bin/innervoice desktop start
./bin/innervoice desktop status
./bin/innervoice desktop stop
```

Provision a separate desktop identity with the server CLI before setup. Set the private URL, trust root and token path. Leave source IP empty and port zero for OS selection. On Wayland, point a desktop custom shortcut to the virtual environment's Python interpreter followed by the absolute path to `clients/desktop.py` and the argument `toggle`. On X11, use the hotkey selected at setup. Press once to start and again to stop. The editor appends indexed text while preserving edits. On reconnect the client compares durable fragment indexes. After restart the editor opens empty. Open the clock to select a recent saved capture. After recording stops, **Retry sync** resumes a failed transfer from saved audio. If the Gateway session expired or failed permanently, that action starts a replacement session while keeping edited text. Local audio remains in the workstation history after transcription. The same storage limit and cleanup commands apply to native and container installations. On completion the full editor contents go to the clipboard. Paste in the destination with its usual shortcut. Desktop stop affects only the user service. After stopping, archive or remove the user's config, state and virtual environment when decommissioning.

GET /v1/capabilities advertises sessions. POST /v1/sessions with Idempotency-Key, PUT a mono 16 kHz signed 16-bit WAV to /v1/sessions/{id}/fragments/{index}, GET the session and POST /v1/sessions/{id}/finalize with last_index. Missing indexes are returned in a 409 response. A deliberate recover_prefix request may discard stored fragments beyond the contiguous prefix. Finalization returns a pollable job.

## Maintenance and acceptance

Keep the Gateway allowlist and the host firewall aligned. The private overlay isolates the worker network from external routes and the worker loads local weights. The worker starts with Docker's default seccomp profile, its own IPC namespace and access to the configured compute devices. Include these settings in the first host check. Validate the final image digests, operating system packages, GPU runtime and effective port exposure on the target before promotion.

- Use `config set` for validated edits and `restart` to apply them. Config, credentials, data and local weights persist outside containers. Record effective image digest, private runtime overrides, exact installed packages, warm/cold state and host capacity in private evidence.
- For the optional Faster Whisper trial, supply a pinned ROCm base with compatible Torch and the ROCm CTranslate2 4.9.0 wheel already installed. Build with `docker build -f whisper/Dockerfile.faster --build-arg CT2_ROCM_IMAGE="$CT2_BASE" -t innervoice-faster:candidate whisper`. Provision the complete converted model directory. Its manifest digest hashes each sorted relative filename plus NUL and its bytes. Fit the trial to the actual GPU and verify local inference. Set the faster-whisper image, image_id, weights and digest with config set, then run `restart --engine faster-whisper`. Restore with `restart --engine openai-whisper`. Roll back a failed transition with the previous image and saved manifest. If reverting to the pre-Gateway application, stop candidate services, restore the previous Compose project and exact retained images/environment, recreate the original bot and worker, and verify the host binding. Keep all data volumes.
- Trigger restart during a long inference. Verify one ordered result, absence of duplicate segments, prompt progress from a second owner and a short legacy request. Check replays, different bytes under the same key, unknown owner, credential rotation and revocation, expired results, oversized decoded content, silence and names/numbers. Verify bot fast/full, timestamps, defaults/reset and an explicit false override with two distinct users.
- Compare three or more repeats per fixed fixture per condition via `python3 clients/smoke.py "$CLIENT_CONFIG" "$FIXTURE" "$EXPECTED_TEXT" --repeats 3`. Use a separate one-hour deadline for long fixtures. Measure cold/warm, short-note absolute latency, real-time factor, boundary speech, language mixing and noise, memory, disk, busy conditions and cleanup. Require warm end-to-end RTF at most 1 per representative voiced clip; compare English-source word error rate and rubric-scored translation fidelity against the incumbent. Require zero new critical name, number or meaning errors. Check direct one-hour results separately from actual Telegram download limits.
- Validate installed provider outage, no external fallback, deadline and silence behavior. Exercise desktop focus changes, record/stop, offline recovery, retained edits, restart during transfer, short final fragment and clipboard fallback. Record observed results by candidate SHA using synthetic IDs only. Keep the real host, fixture content, runtime measurements and deployment manifest private.

## Server audio and Telegram

The desktop keeps its local recording history. The private server processes uploads, PCM fragments, Whisper temporary files and standalone bot notes in bounded RAM filesystems. Disable swap on the Docker host and run the CLI as a non-root Linux user with a non-root primary group. The deployment disables core files in each service. The Gateway retains transcripts and job metadata in its protected SQLite state according to the existing retention settings. After an unplanned container stop, unprocessed audio may need to be submitted again. Stop recording and drain before an upgrade.

The standalone bot is disabled by default. An agent that already has a Telegram connector does not need it. To enable the standalone bot, set the following two variables in the repository-root, mode-0600 `.env` on the server. Use the actual numeric Telegram user ID and the bot's token. Do not commit this file.

```dotenv
BOT_TOKEN=<bot token>
TELEGRAM_USER_ID=<single authorized positive numeric user ID>
```

```sh
./bin/innervoice config set deployment.telegram=true deployment.enable=true
./bin/innervoice restart
```

The CLI provisions a separate random bot-to-Gateway bearer. Only the bot and Gateway receive it. They communicate through a local Unix socket. The worker is on an internal network. Only the private HTTPS port is published. The Gateway ingress bridge disables IP masquerading. Enforce destination-specific egress at the host or network boundary for explicitly routed networks and for the desktop and Telegram bridge. Permit the workstation to reach the configured Gateway address and TLS port, and permit the standalone bot to reach the Telegram Bot API over HTTPS and the configured DNS resolver. Apply equivalent IPv6 rules if enabled. No bot port is published. To disable the standalone bot, set `deployment.telegram=false` and restart.

For ROCm the default render node is `/dev/dri/renderD128`. Set `IV_RENDER_NODE` to select another render node. The launcher detects its owning group automatically. An explicit numeric `WHISPER_RENDER_GROUP` overrides detection. The service identity must be able to read the weights and access both that node and `/dev/kfd`.

On upgrade, stop and drain the earlier version. Inspect its state directory and previous Compose audio volume (normally named with the project prefix and `audio_temp`). Existing audio there, in backups or in old containers is outside the new RAM spool. Review it before deletion. Leave the jobs database and desktop history in place.

### Enforce bridge egress on Linux

On each Docker host, an administrator can install the supplied nftables policy before starting InnerVoice. It drops new connections from the Gateway ingress bridge, confines desktop connections to the configured private IPv4 endpoint, and allows optional bot HTTPS only to resolved Telegram API addresses. It also covers connections from those bridges to the host. Docker's internal inference network handles the Gateway-to-worker request. Host-network desktop mode bypasses bridge filtering, so use the default bridge mode for this policy.

```bash
# Server, without the standalone bot
python3 deploy/egress-policy | sudo nft -f -
# Server, with the standalone bot
python3 deploy/egress-policy --telegram | sudo nft -f -
# Workstation; substitute the actual endpoint used by its client
python3 deploy/egress-policy --gateway "$GATEWAY_IP" --port "$GATEWAY_PORT" | sudo nft -f -
```

On a combined host pass both sets of options in a single invocation. Load the policy through the host's firewall configuration on boot. Refresh it when the Gateway endpoint or Telegram DNS records change. The bot uses Docker's embedded DNS resolver. A resolver requiring a separate bridge egress allowance must be restricted to the host's trusted resolver. Prefer a literal private IP for the workstation endpoint. Inspect the active policy with `sudo nft list table inet innervoice_egress`. The bridge names are fixed and intended for one deployment per host.
