"""Use the production HTTP worker and a deterministic recognizer for CPU-only CI."""
from pathlib import Path
import tempfile
import wave

import whisper_server


class FixtureModel:
    def transcribe(self, audio, **options):
        assert Path(audio).parent == Path(tempfile.gettempdir()) == Path("/tmp")
        with wave.open(audio, "rb") as source:
            assert source.getframerate() == 16000
            assert any(source.readframes(source.getnframes()))
        return {"text": "synthetic recording", "segments": []}


whisper_server.get_model = FixtureModel
whisper_server.app.run(host="0.0.0.0", port=9000)
