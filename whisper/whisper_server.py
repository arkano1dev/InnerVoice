"""Whisper API server - Flask-based transcription service with ROCm/GPU support."""
import os
import gc
import time
import tempfile
import subprocess
import logging
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

model_size = os.getenv("WHISPER_MODEL", "medium")
VRAM_THRESHOLD_FREE_MB = int(os.getenv("VRAM_THRESHOLD_FREE_MB", "2048"))
MODEL_PRELOAD = os.getenv("MODEL_PRELOAD", "false").lower() in ("1", "true", "yes")
USE_FP16 = os.getenv("WHISPER_FP16", "true").lower() in ("1", "true", "yes")  # fp16 reduces VRAM for medium
USE_TORCH_COMPILE = os.getenv("WHISPER_TORCH_COMPILE", "true").lower() in ("1", "true", "yes")  # PyTorch 2+ compile for ROCm speedup
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))  # 1=greedy (fast), 5=default (slower, better accuracy)
CONDITION_ON_PREVIOUS = os.getenv("WHISPER_CONDITION_ON_PREVIOUS_TEXT", "false").lower() in ("1", "true", "yes")

# Idle-unload tuning: unload Whisper from GPU after this many seconds without use
IDLE_UNLOAD_SECONDS = int(os.getenv("WHISPER_IDLE_UNLOAD_SECONDS", str(15 * 60)))
IDLE_CHECK_INTERVAL_SECONDS = int(os.getenv("WHISPER_IDLE_CHECK_INTERVAL_SECONDS", "60"))

# Lazy-load model on first request so the server can bind to port 9000 before any GPU load (avoids startup segfault on some ROCm setups)
_model = None
_model_load_error = None
_last_used_monotonic = None
_model_lock = threading.Lock()


def _probe_gpu():
    """Minimal GPU probe - call before model load to diagnose ROCm/HIP issues."""
    try:
        import torch
        available = torch.cuda.is_available()
        device_count = torch.cuda.device_count() if available else 0
        device_name = torch.cuda.get_device_name(0) if device_count else None
        return {"available": available, "count": device_count, "name": device_name}
    except Exception as e:
        logger.exception("GPU probe failed")
        return {"available": False, "error": str(e)}


def get_model():
    global _model, _model_load_error, _last_used_monotonic
    # Fast path without lock if model is already loaded
    if _model is not None:
        _last_used_monotonic = time.monotonic()
        return _model
    if _model_load_error is not None:
        raise RuntimeError(_model_load_error) from _model_load_error

    with _model_lock:
        # Re-check inside lock to avoid double-load
        if _model is not None:
            _last_used_monotonic = time.monotonic()
            return _model
        if _model_load_error is not None:
            raise RuntimeError(_model_load_error) from _model_load_error

        import whisper
        logger.info("Loading Whisper model %s (first request or after idle unload)...", model_size)
        try:
            gpu = _probe_gpu()
            if not gpu.get("available"):
                err = gpu.get("error", "GPU not available")
                raise RuntimeError(f"Cannot load model: {err}. Check ROCm/iGPU setup.")
            _model = whisper.load_model(model_size, device="cuda")
            if USE_TORCH_COMPILE:
                try:
                    import torch
                    _model = torch.compile(_model, mode="reduce-overhead")
                    logger.info("Applied torch.compile to Whisper model")
                except Exception as e:
                    logger.warning("torch.compile failed, using eager: %s", e)
            logger.info("Loaded Whisper model: %s", model_size)
            _last_used_monotonic = time.monotonic()
        except Exception as e:
            _model_load_error = e
            logger.exception("Failed to load Whisper model: %s", e)
            raise
    return _model


def get_vram_stats():
    """Get VRAM usage. Returns (used_mb, total_mb) or (None, None)."""
    # Try PyTorch first (works with ROCm via HIP)
    try:
        import torch
        if torch.cuda.is_available():
            used = torch.cuda.memory_allocated() // (1024 * 1024)
            total = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
            return int(used), int(total)
    except Exception as e:
        logger.debug(f"PyTorch VRAM check: {e}")

    # Fallback: rocm-smi
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmemuse"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            import re
            # Parse "GPU[0]  Memory: 1234 MiB / 8192 MiB" or similar
            for line in result.stdout.split("\n"):
                match = re.search(r"(\d+)\s*MiB\s*/\s*(\d+)\s*MiB", line, re.I)
                if match:
                    used = int(match.group(1))
                    total = int(match.group(2))
                    return used, total
    except Exception as e:
        logger.debug(f"rocm-smi VRAM check: {e}")
    return None, None


def check_vram_available():
    """Return True if enough VRAM is free (threshold from VRAM_THRESHOLD_FREE_MB)."""
    used_mb, total_mb = get_vram_stats()
    if used_mb is None or total_mb is None:
        return True  # Unknown, allow attempt
    free_mb = total_mb - used_mb
    return free_mb >= VRAM_THRESHOLD_FREE_MB


def _gpu_cache_clear():
    """Release GPU cache after each transcribe to reduce VRAM fragmentation over many chunks (long audio)."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception as e:
        logger.debug("GPU cache clear: %s", e)


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """Transcribe or translate audio. Accepts audio file + optional params: language, task, return_segments."""
    if "audio" not in request.files:
        return jsonify({"error": "No audio file"}), 400

    # Check VRAM before processing
    if not check_vram_available():
        return (
            jsonify({
                "error": "gpu_busy",
                "message": "GPU/VRAM is busy (e.g. Ollama in use). Try again when free.",
            }),
            503,
        )

    audio_file = request.files["audio"]
    language = request.form.get("language") or request.args.get("language")
    task = request.form.get("task", "transcribe")
    return_segments = request.form.get("return_segments", "false").lower() in ("1", "true", "yes")

    if task not in ("transcribe", "translate"):
        task = "transcribe"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        try:
            audio_file.save(tmp.name)
            result = get_model().transcribe(
                tmp.name,
                task=task,
                fp16=USE_FP16,
                language=language if language else None,
                verbose=False,
                beam_size=BEAM_SIZE,
                condition_on_previous_text=CONDITION_ON_PREVIOUS,
                temperature=(0.0,),  # single temp avoids fallback loops
            )
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            _gpu_cache_clear()
            return jsonify({"error": str(e)}), 500
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            # Reduce VRAM fragmentation / driver stress over many chunks (long audio)
            _gpu_cache_clear()

    out = {"text": result.get("text", "").strip()}
    if return_segments and "segments" in result:
        out["segments"] = result["segments"]
    return jsonify(out)


@app.route("/health", methods=["GET"])
def health():
    """Health check with optional GPU/VRAM stats (only after model loaded to avoid GPU init at startup)."""
    resp = {"status": "healthy", "model": model_size}
    if _model is not None:
        used_mb, total_mb = get_vram_stats()
        if used_mb is not None and total_mb is not None:
            resp["vram_used_mb"] = used_mb
            resp["vram_total_mb"] = total_mb
            resp["vram_free_mb"] = total_mb - used_mb
    return jsonify(resp)


@app.route("/gpu-check", methods=["GET"])
def gpu_check():
    """Diagnostic: minimal GPU probe without loading the full model. Use to verify ROCm/HIP before first transcribe."""
    probe = _probe_gpu()
    return jsonify({"gpu": probe, "model_loaded": _model is not None})


def _unload_model_if_idle():
    """Background loop: unload Whisper model from GPU if idle for too long."""
    global _model, _last_used_monotonic
    if IDLE_UNLOAD_SECONDS <= 0:
        logger.info("WHISPER_IDLE_UNLOAD_SECONDS <= 0: idle unload disabled")
        return

    logger.info(
        "Starting idle-unload watcher: idle=%ss, check_interval=%ss",
        IDLE_UNLOAD_SECONDS,
        IDLE_CHECK_INTERVAL_SECONDS,
    )
    while True:
        try:
            time.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            if _model is None:
                continue

            now = time.monotonic()
            last = _last_used_monotonic
            if last is None:
                _last_used_monotonic = now
                continue

            idle_for = now - last
            if idle_for < IDLE_UNLOAD_SECONDS:
                continue

            with _model_lock:
                if _model is None:
                    continue
                if _last_used_monotonic is not None and (time.monotonic() - _last_used_monotonic) < IDLE_UNLOAD_SECONDS:
                    continue

                logger.info(
                    "Whisper model idle for %.1fs (>= %ss). Unloading from GPU...",
                    idle_for,
                    IDLE_UNLOAD_SECONDS,
                )
                try:
                    model_ref = _model
                    _model = None
                    _last_used_monotonic = None
                    del model_ref
                    gc.collect()
                    _gpu_cache_clear()
                    logger.info("Whisper model successfully unloaded due to idle timeout")
                except Exception as e:
                    logger.warning("Error while unloading idle Whisper model: %s", e)
        except Exception as e:
            logger.warning("Idle-unload watcher loop error: %s", e)


def _preload_model():
    """Optionally load model at startup (MODEL_PRELOAD=true) to surface load errors in logs before serving."""
    if not MODEL_PRELOAD:
        return
    logger.info("MODEL_PRELOAD=true: probing GPU and loading model at startup...")
    probe = _probe_gpu()
    logger.info("GPU probe: %s", probe)
    if not probe.get("available"):
        logger.error("GPU not available. Set MODEL_PRELOAD=false to defer and rely on /gpu-check + first request.")
        return
    get_model()


def _start_idle_unload_thread():
    """Start the background thread that unloads the model after idle periods."""
    t = threading.Thread(target=_unload_model_if_idle, name="whisper-idle-unload", daemon=True)
    t.start()


# Start background housekeeping when the module is imported (works for Gunicorn and Flask dev server)
_start_idle_unload_thread()
_preload_model()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
