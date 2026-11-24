# InnerVoice

**Turn speech into text for the tools you already use.** InnerVoice is an open-source, self-hosted project powered by Whisper. Send a voice note to your agent or record on Ubuntu, then use the English text in your workflow. For example, you can speak an instruction in Spanish and have your agent receive it as an English prompt.

## Choose how to use it

| I want to… | Use |
| --- | --- |
| Speak to my agent through Telegram | The agent's Telegram connector passes the note to the private Voice Gateway. The agent receives one final English result and handles the reply. See the [agent provider guide](docs/private-gateway.md#agent-connection) and [reference adapter](clients/provider.py). |
| Get a transcript or translation in Telegram | The standalone InnerVoice bot sends the result back in your Telegram chat. See the [bot guide](DOCUMENTATION.md) and [current bot source](bot/bot.py). |
| Dictate into a desktop app | The optional Ubuntu X11 client lets you start and stop with a shortcut, edit text as it arrives, and copy it or insert it when the original text field can still be verified. See the [desktop guide](docs/private-gateway.md#desktop-role) and [desktop client](clients/desktop.py). |

The **Voice Gateway** accepts audio over private HTTPS, uses a shared Whisper worker and keeps track of work so accepted jobs can resume after an interruption. The standalone bot connects through a separate internal endpoint. The desktop client saves fragments locally so a recording can be recovered after an interruption.

## Get started

1. **Set up the server.** You'll need Linux, Docker Compose, a private network endpoint and a local multilingual Whisper model. The setup wizard can create the TLS certificate. Follow [server provisioning](docs/private-gateway.md#provision-the-server).
2. **Verify the Gateway directly.** Run `./bin/innervoice start --setup` to configure and start it. Follow the [CLI quickstart](docs/private-gateway.md#gateway-quickstart). Run the [direct smoke check](docs/private-gateway.md#direct-smoke-first) with a short synthetic audio fixture.
3. **Connect one client.** Follow the setup guide for your [agent](docs/private-gateway.md#agent-connection), [standalone bot](DOCUMENTATION.md) or [Ubuntu desktop](docs/private-gateway.md#desktop-role).

The private Gateway and full-file agent integration are **Release A**. The desktop recorder and incremental session API are **Release B**. Deployment acceptance is pending. Release B also requires checks in a graphical Ubuntu session after Release A is accepted.

## Documentation and source

| Resource | What you'll find |
| --- | --- |
| [Private Gateway and desktop guide](docs/private-gateway.md) | Provisioning, TLS, test fixtures, agent setup, desktop recovery and acceptance. |
| [CLI reference](docs/private-gateway.md#cli-reference) and [updates and rollback](docs/private-gateway.md#update-an-existing-gateway-deployment) | Setup, lifecycle, diagnostics, engine switching, maintenance and recovery. The entry point is [bin/innervoice](bin/innervoice). |
| [Telegram bot documentation](DOCUMENTATION.md) | Bot commands and historical deployment notes. Follow the private Gateway guide for current deployment. |
| [Refactor baseline](docs/refactor-baseline.md) and [changelog](CHANGELOG.md) | The previous architecture, migration context and version history. |
| [Repository map](FILES.md) | Active source, build files and historical helpers. |
| [Compose services](docker-compose.yml), [private deployment overlay](compose.private.yml) and [environment example](.env.example) | Service boundaries and deployment configuration. |
| [Gateway job API](gateway/jobs.py), [recording session API](gateway/sessions.py) and [direct smoke client](clients/smoke.py) | Reference code for jobs, incremental recording and end-to-end verification. |
| [Whisper worker](whisper/whisper_server.py) | Local inference and model handling. |

## License

InnerVoice's source code is released under the [MIT License](LICENSE). Third-party libraries and model weights are governed by their own licenses.

## Project support

InnerVoice is maintained by [Arkano21](https://t.me/arkano21). Report issues or propose changes through [GitHub](https://github.com/arkano1dev/InnerVoice/issues). Use synthetic examples in public reports.

- Bitcoin — `bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn`
- Lightning — `buffswan6@primal.net`
- Nostr — `npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5`
