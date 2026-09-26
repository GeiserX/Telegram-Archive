"""ElevenLabs Scribe, synchronous.

``POST {TRANSCRIPTION_URL}/v1/speech-to-text``, multipart with ``model_id``
and ``file``, and the key in the ``xi-api-key`` header.
``TRANSCRIPTION_URL`` is ``https://api.elevenlabs.io``. The answer's
``words`` mix words, spacing and audio events; only the words are stored as
words, the text is stored as given.
"""

from typing import Any, BinaryIO

from . import segments_from_words


class ElevenLabsProvider:
    name = "elevenlabs"
    default_model = "scribe_v2"
    diarizes = True

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"xi-api-key": key}

    async def transcribe(
        self, client: Any, upload: BinaryIO, filename: str, *, model: str, prompt: str | None
    ) -> dict[str, Any]:
        from ..transcription_contract import _dicts, _number, language_tag

        data = {
            "model_id": model,
            "timestamps_granularity": "word",
            # Always stated, like the job path: the archive's setting wins.
            "diarize": "true" if client.diarize else "false",
        }
        if client.language:
            data["language_code"] = client.language
        payload = await client.request_json(
            "POST",
            "/v1/speech-to-text",
            timeout=client.sync_timeout(),
            data=data,
            files={"file": (filename, upload, "application/octet-stream")},
        )
        raw_words = [w for w in _dicts(payload.get("words")) if w.get("type", "word") == "word"]
        words = [
            {"w": w.get("text"), "s": _number(w.get("start")), "e": _number(w.get("end")), "c": None} for w in raw_words
        ]
        text = payload.get("text")
        return {
            "text": text if isinstance(text, str) else "",
            "language": language_tag(payload.get("language_code")),
            "language_confidence": _number(payload.get("language_probability")),
            "duration_s": _number(payload.get("audio_duration_secs")),
            "words": words,
            "segments": segments_from_words(words, [w.get("speaker_id") for w in raw_words]),
            "models": [model],
        }


PROVIDER = ElevenLabsProvider()
