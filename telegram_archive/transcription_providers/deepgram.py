"""Deepgram pre-recorded transcription, synchronous.

``POST {TRANSCRIPTION_URL}/v1/listen?model=...`` with the raw audio as the
body and ``Authorization: Token <key>``; the options travel in the query.
``TRANSCRIPTION_URL`` is ``https://api.deepgram.com`` (or a self-hosted
Deepgram). The answer's first channel's first alternative is the
transcript; its words carry timings, confidence and, with diarization,
a speaker number.
"""

import mimetypes
from typing import Any, BinaryIO

from . import segments_from_words


class DeepgramProvider:
    name = "deepgram"
    default_model = "nova-3"
    diarizes = True

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Token {key}"}

    async def transcribe(
        self, client: Any, upload: BinaryIO, filename: str, *, model: str, prompt: str | None
    ) -> dict[str, Any]:
        from ..transcription_contract import _dicts, _number, language_tag

        params: dict[str, str] = {"model": model, "smart_format": "true"}
        if client.language:
            params["language"] = client.language
        else:
            params["detect_language"] = "true"
        if client.diarize:
            params["diarize_model"] = "latest"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        payload = await client.request_json(
            "POST",
            "/v1/listen",
            timeout=client.sync_timeout(),
            params=params,
            body=upload,
            headers={"Content-Type": content_type},
        )
        results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
        channels = _dicts(results.get("channels"))
        channel = channels[0] if channels else {}
        alternatives = _dicts(channel.get("alternatives"))
        best = alternatives[0] if alternatives else {}
        raw_words = _dicts(best.get("words"))
        words = [
            {
                "w": w.get("punctuated_word") if isinstance(w.get("punctuated_word"), str) else w.get("word"),
                "s": _number(w.get("start")),
                "e": _number(w.get("end")),
                "c": _number(w.get("confidence")),
            }
            for w in raw_words
        ]
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        text = best.get("transcript")
        return {
            "text": text if isinstance(text, str) else "",
            "language": language_tag(channel.get("detected_language")) or language_tag(client.language or None),
            "language_confidence": _number(channel.get("language_confidence")),
            "confidence": _number(best.get("confidence")),
            "duration_s": _number(metadata.get("duration")),
            "words": words,
            "segments": segments_from_words(words, [w.get("speaker") for w in raw_words]),
            "models": [model],
        }


PROVIDER = DeepgramProvider()
