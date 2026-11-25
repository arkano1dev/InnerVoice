import io
import json
from pathlib import Path
import queue
import threading
import tempfile
import unittest
from unittest.mock import patch
import wave

import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "gateway"), str(ROOT / "clients")]
import jobs
import sessions
import desktop


def wav(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as file:
        file.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        file.writeframes(pcm)
    return output.getvalue()


class WorkerResponse:
    status_code = 200

    def __init__(self, headers=None, text=""):
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"text": self.text}


class StreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sessions.routes(jobs.api)

    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(tempfile, "tempdir", tempfile.tempdir))
        self.enterContext(patch.multiple(jobs, ROOT=directory, AUDIO=directory / "audio"))
        self.enterContext(patch.object(jobs.api, "before_request_funcs", {}))
        self.enterContext(patch.object(jobs, "paused", return_value=False))
        self.enterContext(patch.object(jobs, "space_available", return_value=True))
        self.emit = self.enterContext(patch.object(jobs.telemetry, "emit"))
        (directory / "fingerprint.key").write_bytes(b"test-key")
        jobs.initialize()
        self.client = jobs.api.test_client()
        self.worker_calls = []
        self.enterContext(patch.object(jobs.legacy.upstream_client, "get", return_value=WorkerResponse()))
        self.enterContext(patch.object(jobs.legacy.upstream_client, "post", side_effect=self.worker))

    def worker(self, _, *, files, data, headers, **options):
        with wave.open(io.BytesIO(files["audio"][1]), "rb") as file:
            pcm = file.readframes(file.getnframes())
        self.worker_calls.append((pcm, data))
        text = "El pago no llegó." if data["task"] == "transcribe" else "The payment did not arrive."
        return WorkerResponse({**headers, "X-Inference-Ms": "12.5"}, text)

    def call(self, method, path, owner="desktop", **kwargs):
        return self.client.open(path, method=method, environ_overrides={
            "innervoice.owner": owner, "innervoice.scope": "sessions",
        }, **kwargs)

    def create(self, key, **options):
        response = self.call("POST", "/v1/sessions", headers={"Idempotency-Key": key}, **options)
        self.assertEqual(response.status_code, 201)
        return "/v1/sessions/" + response.json["session_id"]

    def upload(self, path, index, pcm):
        return self.call("PUT", f"{path}/fragments/{index}", data=wav(pcm), content_type="audio/wav")

    def progress(self, path):
        return self.call("GET", path).json

    def process(self, preview):
        with jobs.database() as db:
            row, = jobs.runnable(db)
            self.assertEqual(bool(row["is_preview"]), preview)
            db.execute("UPDATE jobs SET state='processing' WHERE id=?", (row["id"],))
        jobs.process(row)

    def test_preview_and_final_match_legacy_worker_input_in_both_tasks(self):
        chunks = [bytes((i, 0)) * (sessions.STREAM_BLOCK // 2) for i in (1, 2, 3)]
        tail = b"\x04\x00" * 23999
        complete = b"".join([*chunks, tail])
        for task in ("translate", "transcribe"):
            with self.subTest(task=task):
                options = {"task": task, "streaming": True}
                streaming = self.create(task, json=options)
                baseline = self.create("baseline-" + task, **(
                    {} if task == "translate" else {"json": {"task": task}}))
                self.assertEqual(self.upload(streaming, 1, chunks[1]).status_code, 201)
                with jobs.database() as db:
                    self.assertEqual(jobs.runnable(db), [])
                self.assertEqual(self.upload(streaming, 0, chunks[0]).status_code, 201)
                self.assertEqual(self.upload(streaming, 0, chunks[0]).status_code, 200)
                self.assertEqual(self.upload(streaming, 0, chunks[1]).status_code, 409)
                self.process(preview=True)
                first = self.progress(streaming)
                self.assertTrue(first["preview"])
                self.assertEqual(first["segments"], [])
                self.assertEqual(self.call("GET", streaming, owner="someone-else").status_code, 404)
                self.assertEqual(self.upload(streaming, 3, tail).status_code, 201)
                self.assertEqual(self.call("POST", streaming + "/finalize", json={"last_index": 3}).status_code, 409)
                self.assertEqual(self.upload(streaming, 2, chunks[2]).status_code, 201)
                for _ in range(2):
                    self.assertEqual(self.call("POST", streaming + "/finalize",
                                               json={"last_index": 3}).status_code, 202)
                self.process(preview=False)
                final = self.progress(streaming)
                self.assertEqual(final["state"], "succeeded")
                self.assertEqual(final["preview"], "")
                self.assertEqual(len(final["segments"]), 1)
                self.assertEqual(self.worker_calls[-1][0], complete)

                self.assertEqual(self.upload(baseline, 0, complete).status_code, 201)
                self.assertEqual(self.call("POST", baseline + "/finalize", json={"last_index": 0}).status_code, 202)
                self.process(preview=False)
                self.assertEqual(self.worker_calls[-2], self.worker_calls[-1])
                self.assertEqual(self.progress(baseline)["segments"], final["segments"])
        self.assertNotIn("payment", str(self.emit.call_args_list))
        self.assertNotIn("pago", str(self.emit.call_args_list))

    def test_confirmed_window_stays_stable_during_next_preview(self):
        path = self.create("long", json={"streaming": True, "task": "transcribe"})
        piece = b"\x01\x00" * (sessions.STREAM_BLOCK // 2)
        for index in range(15):
            self.assertEqual(self.upload(path, index, piece).status_code, 201)
        self.process(preview=False)
        confirmed = self.progress(path)["segments"]
        self.assertEqual(self.upload(path, 15, piece).status_code, 201)
        self.process(preview=True)
        self.assertEqual(self.progress(path)["segments"], confirmed)
        self.assertTrue(self.progress(path)["preview"])
        self.assertEqual(self.call("POST", path + "/finalize", json={"last_index": 15}).status_code, 202)
        self.process(preview=False)
        self.assertEqual([s["index"] for s in self.progress(path)["segments"]], [0, 1])
        self.assertEqual([len(entry[0]) for entry in self.worker_calls], [jobs.BLOCK, len(piece), len(piece)])

    def test_long_capture_limits_repeated_preview_decodes(self):
        path = self.create("bounded", json={"streaming": True, "task": "transcribe"})
        piece = b"\x01\x00" * (sessions.STREAM_BLOCK // 2)
        for index in range(15):
            self.assertEqual(self.upload(path, index, piece).status_code, 201)
            with jobs.database() as db:
                pending = jobs.runnable(db)
            if pending:
                self.process(preview=index < 14)
        sizes = [len(pcm) // len(piece) for pcm, _ in self.worker_calls]
        self.assertEqual(sizes, [1, 3, 6, 12, 15])

    def test_short_piece_only_allowed_at_the_end(self):
        path = self.create("short", json={"streaming": True})
        self.assertEqual(self.upload(path, 0, b"\x01\x00").status_code, 201)
        self.assertEqual(self.upload(path, 1, b"\x01\x00").status_code, 422)
        with jobs.database() as db:
            self.assertEqual(jobs.runnable(db), [])
        self.assertEqual(self.call("POST", path + "/finalize", json={"last_index": 0}).status_code, 202)
        self.process(preview=False)
        self.assertEqual(self.progress(path)["state"], "succeeded")

    def test_desktop_reconnects_after_upload_ack_is_lost_and_flushes_tail(self):
        directory = jobs.ROOT / "recording"
        directory.mkdir()
        (directory / "editor.json").write_text(json.dumps({"streaming": True, "task": "transcribe"}))
        piece = b"\x01\x00" * (sessions.STREAM_BLOCK // 2)
        tail = b"\x02\x00" * 17
        (directory / "000.raw").write_bytes(piece)
        finished = threading.Event()
        events = queue.Queue()
        uploads = []

        def call(method, path, **kwargs):
            if method == "PUT":
                uploads.append(path)
                kwargs["content_type"] = kwargs.pop("headers")["Content-Type"]
            response = self.call(method, path, **kwargs)
            self.assertLess(response.status_code, 400, response.json)
            if method == "PUT" and path.endswith("/0"):
                self.process(preview=True)
                raise requests.ConnectionError("The reply was lost")
            if method == "GET" and response.json.get("preview"):
                (directory / "001.raw").write_bytes(tail)
                finished.set()
            if method == "POST" and path.endswith("/finalize"):
                self.process(preview=False)
            return response.json

        with (patch.object(desktop, "Client") as client,
              patch.object(desktop, "metric"),
              patch.object(desktop.time, "sleep")):
            client.return_value.call.side_effect = call
            desktop.network({"poll_seconds": .1}, directory, "key", events, finished)

        self.assertEqual(len(uploads), 2)
        self.assertEqual(self.worker_calls[-1][0], piece + tail)
        emitted = list(events.queue)
        self.assertIn(("preview", (0, "El pago no llegó.")), emitted)
        self.assertIn(("preview", (1, "")), emitted)
        self.assertEqual(emitted[-1], ("ready", "Ready"))

    def test_worker_disconnect_blocks_further_dispatch_until_manual_resume(self):
        path = self.create("fault", json={"streaming": True})
        piece = b"\x01\x00" * (sessions.STREAM_BLOCK // 2)
        self.upload(path, 0, piece)
        with patch.object(jobs.legacy.upstream_client, "post", side_effect=requests.ConnectionError):
            with self.assertRaises(jobs.legacy.WorkerFault):
                self.process(preview=True)
        self.assertTrue(jobs.legacy.fault_marker().is_file())
        with self.assertRaises(jobs.legacy.WorkerFault):
            with jobs.legacy.guarded_dispatch():
                self.fail("A second request reached the worker")
        response = self.call("POST", "/v1/sessions", headers={"Idempotency-Key": "next"})
        self.assertEqual(response.json["error"]["code"], "worker_protection_active")
        jobs.legacy.fault_marker().unlink()
        self.create("resumed")

    def test_reprocess_keeps_original_audio_and_replaces_text_only_on_success(self):
        directory = jobs.ROOT / "saved"
        directory.mkdir()
        state = {"ready": True, "text": "old", "reprocess_key": "again"}
        metadata = directory / "editor.json"
        metadata.write_text(json.dumps(state))
        audio = directory / "000.raw"
        audio.write_bytes(b"\x01\x00")
        finished = threading.Event()
        finished.set()
        events = queue.Queue()

        def call(method, path, **kwargs):
            if method == "PUT":
                kwargs["content_type"] = kwargs.pop("headers")["Content-Type"]
            response = self.call(method, path, **kwargs)
            self.assertLess(response.status_code, 400)
            if path.endswith("/finalize"):
                self.process(preview=False)
            return response.json

        with patch.object(desktop, "Client") as client, patch.object(desktop, "metric"):
            client.return_value.call.side_effect = call
            desktop.network({"poll_seconds": .1}, directory, "again", events, finished)
        self.assertEqual(audio.read_bytes(), b"\x01\x00")
        self.assertEqual(json.loads(metadata.read_text()), state)
        kinds = [kind for kind, _ in events.queue]
        self.assertNotIn("segments", kinds)
        self.assertEqual(kinds[-3:], ["replacement", "delivered", "ready"])

    def test_read_timeout_latches_without_another_inference(self):
        path = self.create("hung", json={"streaming": True})
        self.upload(path, 0, b"\x01\x00" * (sessions.STREAM_BLOCK // 2))
        with patch.object(jobs.legacy.upstream_client, "post", side_effect=requests.ReadTimeout) as post:
            with self.assertRaises(jobs.legacy.WorkerFault):
                self.process(preview=True)
        self.assertEqual(post.call_args.kwargs["timeout"], (2, jobs.legacy.INFERENCE_TIMEOUT))
        self.assertTrue(jobs.legacy.fault_marker().is_file())

    def test_kernel_dump_blocks_worker_before_probe_or_inference(self):
        path = self.create("dump", json={"streaming": True})
        self.upload(path, 0, b"\x01\x00" * (sessions.STREAM_BLOCK // 2))
        with patch.object(jobs.legacy, "gpu_dump_present", return_value=True):
            with self.assertRaises(jobs.legacy.WorkerFault):
                self.process(preview=True)
        jobs.legacy.upstream_client.get.assert_not_called()
        self.assertFalse(self.worker_calls)
        self.assertTrue(jobs.legacy.fault_marker().is_file())

    def test_all_telegram_endpoints_respect_fault(self):
        legacy = jobs.legacy
        self.enterContext(patch.object(legacy.app, "before_request_funcs", {}))
        self.enterContext(patch.dict(legacy.os.environ, ENABLE_JOBS="1"))
        client = legacy.app.test_client()
        legacy.fault_marker().touch()
        with patch.object(legacy.upstream_client, "request") as call:
            for path in ("/health", "/gpu-check", "/can-transcribe", "/transcribe"):
                response = client.open(path, method="POST" if path == "/transcribe" else "GET")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["error"], "worker_protection_active")
            call.assert_not_called()

    def test_probe_contention_and_pending_hold_do_not_latch(self):
        legacy = jobs.legacy
        self.enterContext(patch.object(legacy.app, "before_request_funcs", {}))
        client = legacy.app.test_client()
        with patch.object(legacy.upstream_client, "request") as call:
            with legacy.inference_lane("jobs"):
                self.assertEqual(client.get("/health").json["error"], "gpu_busy")
            legacy.fault_marker().with_name("telegram-pending-hold").touch()
            self.assertEqual(client.get("/can-transcribe").json["error"], "pending_queue_held")
            call.assert_not_called()
            self.assertFalse(legacy.fault_marker().exists())
            call.side_effect = requests.ReadTimeout
            self.assertEqual(client.get("/health").status_code, 503)
            self.assertTrue(legacy.fault_marker().exists())

    def test_invisible_render_node_prevents_private_dispatch(self):
        legacy = jobs.legacy
        with patch.dict(legacy.os.environ, PRIVATE_API="1", IV_RENDER_NODE="/dev/dri/test-missing"):
            with self.assertRaises(legacy.WorkerFault):
                with legacy.guarded_dispatch():
                    self.fail("unobserved GPU reached")
        self.assertTrue(legacy.fault_marker().exists())

    def test_failed_partial_reprocess_preserves_transcript_and_tail(self):
        directory = jobs.ROOT / "partial"
        directory.mkdir()
        state = {"ready": False, "text": "edited", "reprocess_key": "again"}
        metadata = directory / "editor.json"
        metadata.write_text(json.dumps(state))
        audio = directory / "000.raw"
        audio.write_bytes(b"\x01\x00")
        finished = threading.Event()
        finished.set()
        events = queue.Queue()
        with patch.object(desktop, "Client") as client, patch.object(desktop, "metric"):
            client.return_value.call.side_effect = [
                {"session_id": "retry"},
                {"received": [], "segments": [{"index": 0, "text": "partial"}],
                 "state": "failed", "error": {"code": "worker_protection_active"}},
            ]
            desktop.network({}, directory, "again", events, finished)
        self.assertEqual(json.loads(metadata.read_text()), state)
        self.assertEqual(audio.read_bytes(), b"\x01\x00")
        self.assertEqual(list(events.queue)[-1], ("retry", (desktop.GPU_PAUSED, True)))
        self.assertFalse({"segments", "replacement", "preview"} & {kind for kind, _ in events.queue})

    def test_live_session_reserves_preview_gaps_and_releases_after_final(self):
        path = self.create("priority", json={"streaming": True})
        legacy = jobs.legacy
        self.enterContext(patch.object(legacy.app, "before_request_funcs", {}))
        self.enterContext(patch.dict(legacy.os.environ, ENABLE_JOBS="1"))
        client = legacy.app.test_client()
        self.assertEqual(client.get("/can-transcribe").json["error"], "gpu_busy")

        # Keep a real Telegram request waiting while the hotkey gets two turns.
        checked = threading.Event()
        inspect = legacy.desktop_active

        def check():
            busy = inspect()
            checked.set()
            return busy

        from concurrent.futures import ThreadPoolExecutor
        with patch.object(legacy, "desktop_active", side_effect=check), ThreadPoolExecutor(1) as pool:
            with patch.object(legacy, "forward", return_value={"text": "telegram"}) as forward:
                pending = pool.submit(legacy.app.test_client().post, "/transcribe")
                try:
                    self.assertTrue(checked.wait(2))
                    self.upload(path, 0, b"\x01\x00" * (sessions.STREAM_BLOCK // 2))
                    self.process(preview=True)
                    self.assertFalse(pending.done())
                    self.call("POST", path + "/finalize", json={"last_index": 0})
                    self.assertTrue(inspect())
                    self.process(preview=False)
                    self.assertEqual(pending.result(timeout=3).json, {"text": "telegram"})
                    forward.assert_called_once()
                finally:
                    legacy.fault_marker().touch()

    def test_disconnected_open_stream_expires_and_new_fragment_restores_priority(self):
        path = self.create("abandoned", json={"streaming": True})
        with jobs.database() as db:
            db.execute("UPDATE jobs SET created=0")
        self.assertFalse(jobs.legacy.desktop_active())
        self.upload(path, 0, b"\x01\x00" * (sessions.STREAM_BLOCK // 2))
        self.assertTrue(jobs.legacy.desktop_active())
        with jobs.database() as db:
            db.execute("UPDATE fragment_arrivals SET received=0")
        self.assertFalse(jobs.legacy.desktop_active())

    def test_numeric_fragment_order_survives_three_digit_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in ("999.raw", "1000.raw", "1001.raw", "010.raw"):
                (directory / name).touch()
            self.assertEqual([part.name for part in desktop.audio_fragments(directory)],
                             ["010.raw", "999.raw", "1000.raw", "1001.raw"])
