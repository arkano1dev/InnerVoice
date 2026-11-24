# InnerVoice

InnerVoice runs speech recognition locally with Whisper. Its private HTTPS Gateway accepts audio from an agent, processes it in recoverable jobs, and returns final English text. The standalone Telegram bot uses an internal compatibility listener for its transcription and translation modes. The optional Ubuntu desktop client records with a hotkey, recovers interrupted captures, and lets you edit the transcript before inserting or copying it.

This refactor is available on `refactor/private-voice-gateway`. Production acceptance is pending. Validate the Gateway directly before connecting an agent, then validate the desktop client separately in a graphical Ubuntu session.

## Install and start

1. Prepare a Linux host with Python 3.11+, Docker Engine with Compose v2, the selected inference runtime, a private VLAN IPv4 address, and a TLS certificate that covers that address. Provision a verified local multilingual model and a worker image with a recorded digest or verified local image ID. See the [server provisioning guide](docs/private-gateway.md#provision-the-server) for the build, certificate, model checksum and Telegram bot token.
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

   Use the [direct smoke guide](docs/private-gateway.md#direct-smoke-first) to prepare the protected client TOML, trust certificate, bearer token, synthetic audio, and expected English text.

## Use the clients

| Client | Setup and behavior |
| --- | --- |
| Agent with a Telegram connector | Give the agent its own Gateway identity and configure [the speech provider](docs/private-gateway.md#agent-connection) in the installed framework. The agent handles the Telegram event and receives one final UTF-8 prompt. Verify its silence, retry, timeout and external fallback settings. |
| InnerVoice Telegram bot | Configure the bot token during server provisioning. Send `/start` and a voice note, then verify the bot's fast and full modes, preferences and timestamps. |
| Ubuntu desktop | Follow [desktop setup](docs/private-gateway.md#desktop-role) to provision a separate identity and start the recorder under your X11 session. Press the hotkey to start and again to stop. If the original target cannot be verified, the edited text stays available in the editor. |

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

1. Record the current commit with `git rev-parse HEAD`. Keep the prior checkout or commit, verified image IDs, active configuration and Compose project name. Preserve the existing volumes and protected state. If updating from the original bot/worker deployment, use the [migration baseline](docs/private-gateway.md#provision-the-server) to inventory ports, callers, images and volumes before cutover.
2. Update the existing branch with a fast-forward. Review the changes, build or provision the new worker image when its source or dependencies change, and update the worker image and model identifiers using `config set`. Provision the worker before calling `restart`, which builds the bot and Gateway.

   ```bash
   git status --short --branch
   git switch refactor/private-voice-gateway
   git pull --ff-only origin refactor/private-voice-gateway
   ./bin/innervoice config validate
   ./bin/innervoice restart
   ./bin/innervoice status
   ```

3. Run direct smoke, check the bot, then verify the installed agent connector. Update each client checkout separately. For Ubuntu, stop the recorder before updating and restart it from the same user session afterward. If the drain times out, let accepted work finish before retrying. If activation or verification fails, use the saved image, checkout and active configuration with the [maintenance and rollback guide](docs/private-gateway.md#maintenance-and-acceptance).

## Project support

InnerVoice is maintained by [Arkano21](https://t.me/arkano21). Report issues or propose changes through [GitHub](https://github.com/arkano1dev/InnerVoice/issues). Use synthetic examples in public reports.

- Bitcoin — `bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn`
- Lightning — `buffswan6@primal.net`
- Nostr — `npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5`
