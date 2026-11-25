# InnerVoice repository map

Start with the [project README](README.md) or the [deployment and integration guide](docs/private-gateway.md).

| Area | Files |
| --- | --- |
| Documentation | [Gateway and desktop guide](docs/private-gateway.md), [install guide](INSTALL.md), [documentation archive](docs/archive/README.md), [changelog](CHANGELOG.md), [license](LICENSE). |
| Operator and deployment | [CLI](bin/innervoice), [base Compose](docker-compose.yml), [private overlay](compose.private.yml), [environment example](.env.example). |
| Gateway | [Listeners and network allowlist](gateway/server.py), [job API and scheduler](gateway/jobs.py), [session API](gateway/sessions.py), [legacy bot compatibility](gateway/legacy.py), [telemetry](gateway/telemetry.py), [image](gateway/Dockerfile), [dependencies](gateway/requirements.txt). |
| Clients | [Agent provider](clients/provider.py), [direct smoke](clients/smoke.py), [Ubuntu recorder](clients/desktop.py), [desktop launcher](bin/innervoice-desktop), [local storage controls](clients/recording_storage.py), [desktop image](clients/Dockerfile.desktop). |
| Telegram bot | [Application](bot/bot.py), [image](bot/Dockerfile), [dependencies](bot/requirements-bot.txt). |
| Whisper worker | [Inference service](whisper/whisper_server.py), [ROCm image](whisper/Dockerfile), [CPU image](whisper/Dockerfile.cpu), [optional Faster Whisper image](whisper/Dockerfile.faster), [dependencies](whisper/requirements-service.txt). |
| Historical helpers | Root [bot](bot.py), [deployment script](deploy.sh), [Makefile](Makefile), [Dockerfile](Dockerfile) and [dependencies](requirements.txt) are retained for historical context. Use the operator CLI and the current guide for the private Gateway. |
