# Install InnerVoice

Start with the [server](#1-set-up-the-gateway). Then connect an [Ubuntu desktop](#2-install-the-ubuntu-desktop), a [Telegram agent](docs/private-gateway.md#agent-connection) or the [standalone Telegram bot](docs/private-gateway.md#server-audio-and-telegram). Run commands from a checkout of the version you are installing.

## 1. Set up the Gateway

On a Linux host, install Docker Engine with Compose v2, Python 3.11+, FFmpeg and OpenSSL. Use a non-root operator account, disable host swap for the private audio deployment, and provision a trusted local multilingual checkpoint and a compatible Whisper worker image. Follow the [server provisioning guide](docs/private-gateway.md#provision-the-server) for worker preparation and updates from an existing installation.

```bash
./bin/innervoice start --setup
./bin/innervoice status
```

The wizard asks for the server's private IPv4 address, HTTPS port, allowed **client** addresses or Nord names, worker image and model weights. It creates a certificate for the server IP if you have supplied none. Keep the TLS private key, bearer files and model weights on their designated hosts, outside the checkout. Run the [direct smoke check](docs/private-gateway.md#direct-smoke-first) and enable job admission before the first desktop recording.

### Start the server on Ubuntu boot

For a system-wide Docker Engine installation, enable the Docker service after completing the first InnerVoice start:

```bash
sudo systemctl enable --now docker.service
systemctl is-enabled docker.service
./bin/innervoice status
```

The Gateway and optional Telegram bot use `unless-stopped`. The private Whisper worker uses `on-failure:3` to bound container crash loops. After a host or Docker daemon restart, run `./bin/innervoice start` from the server checkout to bring the worker online. Check `./bin/innervoice status` for the GPU dispatch state. A worker fault requires explicit `./bin/innervoice gpu-resume` after recovery.

For Faster Whisper, use `./bin/innervoice config worker --engine faster-whisper` to save the built worker image and its local model directory. The Faster Whisper Dockerfile pins PyAV 18.1.0 for compatibility with Faster Whisper 1.2.1. Keep the saved image and model available on the server for future starts.

If the Gateway publishes HTTPS on a Meshnet address, Docker needs that address to be present when it binds the listener. After the first reboot, check `./bin/innervoice status` and send a short test from each client you use. If the Gateway cannot bind its address, check `journalctl -b -u docker.service` and run `./bin/innervoice start` once Meshnet is ready.

### Direct Meshnet access

Run Meshnet on the server that publishes the Gateway. On that server, allow incoming Meshnet traffic from the workstation. Leave traffic routing and access to LAN devices through a peer disabled. Use the server's Meshnet IPv4 as the bind address and as the HTTPS host if the setup wizard generated the certificate. If you use a Nord name as the HTTPS host, the certificate needs a matching DNS SAN.

In **Allowed clients**, enter the workstation's full Nord name ending in `.nord` or its Meshnet IPv4. You can enter several peers separated by commas. At start or restart the operator CLI resolves each name on the Gateway host to one Meshnet IPv4 and passes an exact `/32` to the listener. Invalid, missing or ambiguous answers abort activation before an existing service is drained. After a peer IP changes, check the host ingress rule and any credential with a fixed `--peer` address, then restart InnerVoice to refresh the application allowlist. Preserve each client's scoped bearer.

A working SSH session confirms direct access to the SSH port. To check InnerVoice separately, try this from the workstation with traffic routing disabled. Replace the example address with the server's actual Meshnet IPv4.

```bash
curl --connect-timeout 5 --max-time 10 --cacert ~/.config/innervoice-desktop/ca.crt \
  -sS -o /dev/null -w '%{http_code}\n' https://100.64.0.10:9000/v1/capabilities
```

| Result | What to check |
| --- | --- |
| HTTP 401 without a bearer | TLS reached the Gateway. Finish setup and verify with the registered token. |
| HTTP 403 | Compare the source seen by the Gateway with the allowed peer and any IP restriction on the credential. |
| Timeout or refused connection | Check the Meshnet address and listen port, Docker port publication and host forwarding rules. Compare a SYN and its response at the published port. |
| Certificate verification error | Check the public certificate and the IP or DNS name in its SAN. |
| Host check works, Docker desktop setup times out | Try the [workstation host-network profile](docs/private-gateway.md#mesh-vpn-works-on-the-workstation-but-times-out-in-the-container). This profile does not make the server a traffic router. |

Verify a permitted peer can reach HTTPS and a denied peer and non-Meshnet interface cannot. A Docker-published port requires a [Docker-aware ingress policy](https://docs.docker.com/engine/network/packet-filtering-firewalls/). See [network boundaries](docs/private-gateway.md#daily-operation-and-network-boundaries) for the listener defaults.

## 2. Install the Ubuntu desktop

On your workstation, open a terminal in an Ubuntu Wayland session. The Docker client uses Compose v2 and the PulseAudio or PipeWire-Pulse session socket. Install the optional tray dependencies for your desktop. On Ubuntu GNOME, use `python3-pyqt5`, `qtwayland5` and an enabled AppIndicator extension.

1. On the Gateway, run `./bin/innervoice credential --identity desktop-hotkey --scope sessions` and keep the chosen token.
2. On the workstation, put the Gateway's public certificate (or its trust CA) at `~/.config/innervoice-desktop/ca.crt`. The default Gateway certificate is `~/.config/innervoice/secrets/tls.crt` on the server. With a system-trusted CA, choose `system` in setup.
3. Run `./bin/innervoice-desktop install`. The wizard checks the HTTPS URL, certificate, token and recording API before saving. Existing settings are reused. The install starts the recorder and registers InnerVoice in the application menu. Keep the checkout in place, or rerun install from its new location.
4. Open **InnerVoice** from Applications and test a short recording, playback and copying text. Run `./bin/innervoice-desktop shortcut` for the command to register as a GNOME custom shortcut. **Super+Shift+F8** is an example binding.

Use `./bin/innervoice-desktop check` to verify the running container can authenticate, `status` for the container state, and `logs` for its output. After updating the checkout, run `./bin/innervoice-desktop update` to rebuild the local image. See the [desktop guide](docs/private-gateway.md#desktop-role) for storage, the tray, and recording controls.

See the [hotkey streaming guide](docs/hotkey-streaming.md) for early text, Spanish transcription, latency measurement and rollback.

To remove the application menu entry, stop the recorder and remove `applications/io.github.arkano1dev.InnerVoice.desktop` and `icons/hicolor/128x128/apps/io.github.arkano1dev.InnerVoice.png` under `${XDG_DATA_HOME:-~/.local/share}`. Remove the optional shortcut in the desktop settings.
