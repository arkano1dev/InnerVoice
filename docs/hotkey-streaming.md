# Hotkey streaming on Ubuntu

On the original desktop path, the client uploads each full 30-second fragment during recording. For a shorter clip, it uploads the first fragment on stop. The Gateway schedules the contiguous prefix, the worker returns an entire Whisper response per request, and the desktop polls and appends the confirmed segments. For an 8-second clip, capture shutdown, upload, dispatch, inference and polling all occur after the second press.

Streaming keeps the same private HTTPS session and adds 2-second uploads. The Gateway returns a provisional result for the current window when enough audio arrives. The editor displays a single transcript. Incoming previews replace its unfinished tail. Editing resumes when streaming completes or sync pauses for a retry. The Gateway spaces later previews farther apart as the window grows to reduce repeated decoding. The Gateway reconstructs the original 30-second windows and sends the original tail to Whisper after stop. Only confirmed text is saved as the transcript and copied automatically. No overlap or pause-based slicing is needed to reconstruct the final input. A preview already running on the worker can still delay final inference on the shared GPU.

## Prepare the build

On the server, from a checkout containing this change, run `./bin/innervoice restart` when you are ready to test. This uses the saved deployment profile and rebuilds the Gateway. The existing worker supports both tasks. For separate model-load and inference timings, build a candidate from the same ROCm base and select it while keeping the previous worker image available:

```bash
docker build -f whisper/Dockerfile.faster --build-arg CT2_ROCM_IMAGE="$CT2_BASE" \
  -t innervoice-faster:hotkey-stream whisper
./bin/innervoice config worker --engine faster-whisper
./bin/innervoice restart --engine faster-whisper
```

Set `CT2_BASE` to the same pinned ROCm CTranslate2 base used for your current worker. In the config wizard, select the candidate tag and keep the existing medium model directory. Use `./bin/innervoice status` to confirm the engine, PyAV version and model. Keep FP16 and beam 1 in the saved profile. With the existing worker image, `worker_request_ms` is available and the two worker-specific fields are empty.

On the Ubuntu desktop, from the checkout containing this change:

```bash
./bin/innervoice-desktop stop
./bin/innervoice-desktop update
./bin/innervoice-desktop check
./bin/innervoice-desktop shortcut
./bin/innervoice-desktop show
```

Open the **EN/ES** button and check **Live text while recording** to enable streaming for the next recording. The editor remembers this choice across restarts. Clear it to return to standard mode. The menu choice overrides the streaming flag in settings.json. For the A–D comparison below, set the menu to match each run.

The existing shortcut still toggles capture. The language button in the editor lets you choose **Transcribe Spanish** or **Translate to English** for the next recording. The `task` setting supplies the default when the app restarts. Saved recordings retain their original task and fragment size when retried.

## Compare one variable at a time

In the desktop settings file, set the following values. Restart the desktop between settings changes.

| Run | `streaming` | `poll_seconds` | `ui_poll_ms` | What it measures |
| --- | --- | --- | --- | --- |
| A | `false` | `1` | `700` | Original 30-second capture path and polling interval |
| B | `false` | `0.2` | `700` | HTTP polling interval |
| C | `true` | `0.2` | `700` | Early upload and preview with the same polling intervals |
| D | `true` | `0.2` | `100` | Faster editor polling after stop |

Change the values from the desktop checkout with:

```bash
# Arguments are streaming, HTTP poll interval, editor poll interval and default task.
python3 - true 0.2 100 transcribe <<'PY'
import json
from pathlib import Path
import tempfile
import sys

path = Path.home() / ".config/innervoice-desktop/settings.json"
settings = json.loads(path.read_text())
settings.update(streaming=sys.argv[1] == "true", poll_seconds=float(sys.argv[2]),
                ui_poll_ms=int(sys.argv[3]), task=sys.argv[4])
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
    json.dump(settings, file)
Path(file.name).replace(path)
PY
./bin/innervoice-desktop stop
./bin/innervoice-desktop start
```

Start the following in separate terminals before each batch. Stop the log followers when that batch is done. Keep one log pair per audio fixture so clip lengths are compared separately.

```bash
# Workstation
./bin/innervoice-desktop logs | rg --line-buffered 'IV_METRIC ' > desktop-A.log
# Server
./bin/innervoice logs gateway | rg --line-buffered 'IV_METRIC ' > gateway-A.log
# From the checkout with paired log files available locally
python3 tests/report_hotkey_metrics.py desktop-A.log gateway-A.log desktop-B.log gateway-B.log desktop-C.log gateway-C.log desktop-D.log gateway-D.log
```

The desktop log command includes recent history. Start each batch with new log names and use only entries from the intended test interval when combining results. The reporter joins the two machines by session ID and prints median, minimum, maximum and count. Per-fragment stages are counted per fragment. First visible and final text are counted per session. `voice_to_visible_ms` estimates onset from the first PCM sample above the capture threshold. It is a proxy for the start of speech. Client intervals use a monotonic clock. Gateway queue time uses its local receipt clock. No subtraction across machines is required. Logs identify sessions, timing, task, fragment indexes and completion status. The existing editor and Gateway stores retain their normal audio and result data.

## Test fixtures and completion checks

Record each fixture once and replay the same source into the workstation for all runs. Warm up each task once and exclude that session. Run at least five takes per fixture and variant without simultaneous Telegram load for the latency comparison. Keep the editor visible. Replay at the same volume and press the hotkey the second time as soon as the clip ends.

| Fixture | Spoken content and check |
| --- | --- |
| Around 8 seconds, names and numbers | “Lucía Méndez pidió 42 unidades para el 15 de julio. El pedido no fue cancelado.” Check the name, quantity, date and negation. |
| Around 8 seconds, pause and boundary | “El puerto de … [short pause] … San Antonio no recibió los 320 litros.” Place “San Antonio” across a two-second boundary and stop immediately after “litros.” Check the complete place name and last word. |
| Over 60 seconds | Use a fixed Spanish passage with names, numbers, pauses, and a sentence spanning 30 seconds. Check that the already confirmed text remains stable during the next preview and that final order and meaning match A. |

Run A–D for Spanish transcription and English translation of the same fixtures. Check the editor and clipboard for missing words, repetitions and changes of meaning. Record whether each preview was useful, since a nonempty preview alone does not establish accuracy. Compare `voice_to_visible_ms` against the 2–3 second goal and `stop_to_final_ms` against the 1–2 second goal for 8-second takes. Compare capture close, upload, Gateway queue, inference, publish and UI timings if the targets are missed. Run a separate Telegram Full smoke check using the existing bot and keep those timings separate from the desktop.

## Revert the desktop behavior

Set `streaming` to `false`, `poll_seconds` to `1` and `ui_poll_ms` to `700` using the settings command above, then stop and start the desktop. Select `translate` for the previous English output. The compatible desktop build can still retry recordings captured in streaming mode. To reverse the optional worker rebuild, reselect the previous image in `./bin/innervoice config worker --engine faster-whisper` and run `./bin/innervoice restart --engine faster-whisper`.

## Local capture and GPU fault recovery

Recording writes and syncs microphone samples on the desktop before upload. A disconnected Gateway or paused GPU leaves the take in local history. Select the take in the clock menu, then use **Retry sync** for incomplete work or **Reprocess saved audio** for a completed take. Reprocessing uses the saved task and replaces the transcript only after success. A new recording can start while a previous upload finishes. Only the current take updates the visible editor. Local quota, available disk space and microphone errors still limit capture. The storage command reports local usage.

A dropped worker connection, timeout, inference server error or explicit GPU out-of-memory response pauses inference in the Gateway. Desktop and Telegram share this protection. The pause survives a Gateway restart. The private worker starts with lazy model loading and Docker retries container failure up to three times. This limits repeated dispatch after a fault; the first failing GPU operation can still reset the display or device.

Check the server with `./bin/innervoice status`. After investigating the host fault and restoring a stable worker, run `./bin/innervoice gpu-resume` and retry one saved take. The resume check queries worker readiness and can touch the GPU. Use it only after inspecting the host. Pending Telegram notes remain held until the operator explicitly runs `./bin/innervoice telegram-release-pending`. New notes can still be tested. Work already handed to the bot or worker may still be in flight. After a host reboot, start the deployment as described in INSTALL.md.

GPU inference uses the existing ROCm worker and the operator's private HSA override. The default worker read timeout is 45 seconds per dispatched segment for desktop and Telegram. Set IV_INFERENCE_TIMEOUT_SECONDS in .env to an integer from 5 to 590 for other worker performance profiles. An expired read timeout pauses dispatch even if the native worker is still running. A visible AMD device coredump on the selected render device also trips protection before dispatch. The Gateway checks for the sysfs entry without reading the dump. A missing entry cannot establish that the driver is healthy.

Starting the private stack with a latched worker fault skips the operator's GPU activation probes. Startup while paused checks Gateway TLS only. After host recovery, gpu-resume checks worker readiness and the visible coredump entry before clearing the latch. A timeout does not cancel native GPU work. Local recording remains available throughout.

For a paused deployment with drained jobs and matching saved configuration, use `./bin/innervoice update-gateway` to apply the protection update without restarting the GPU worker or the bot. Then inspect `./bin/innervoice status`.


From the server checkout, run `./bin/innervoice-incident --since 3h` before changing services. The snapshot reports UTC kernel and container events, boot identity, image IDs, worker source hash, Gateway source hashes, effective timeout and render device visibility. The coredump check uses file metadata. Boot offsets such as `--boot=-1` select an earlier kernel journal. A recreated container can no longer supply its predecessor's logs.

Use `./bin/innervoice gpu-pause` to hold new dispatch during inspection. The Gateway marks unfinished server jobs failed during a fault. Desktop PCM remains available for playback and retry. The Telegram bot stores pending audio on tmpfs, so preserve its running container. The worker's own idle unloading and native teardown act independently of the Gateway.

After host recovery, keep the same image, weights, decoder settings and short spoken sample. With the pending queue held and no request in flight, try a new Telegram note alone, then a standard desktop take alone, then the same desktop take with live previews. Finish with one new Telegram note alongside a live take only if each isolated run succeeds. Record task, sample duration, cold/warm model state, time to result, worker requests and kernel events. Stop at the first new fault.

### Desktop priority and background Telegram notes

The Gateway grants one worker request at a time. A Telegram segment in flight can finish. Once the Gateway receives a desktop session, subsequent Telegram segments and queue-readiness polls yield through the gaps between previews and through final processing. An open session that stops uploading releases its reservation after 8 seconds for live uploads or 36 seconds for standard uploads. New fragments restore priority. The Gateway first learns about a recording when the desktop opens its remote session.

Telegram waits between worker requests, including between transcription and translation of a Full segment. If the wait approaches the bot HTTP deadline, an updated bot sleeps asynchronously and retries that same segment and task. The previous bot can return the note to its pending list on such an extended wait. A real worker fault still engages the shared pause and the existing audio retention flow.
