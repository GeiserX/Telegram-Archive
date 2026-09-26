"""AssemblyAI, asynchronous on its side and waited for inside one call.

Three requests with ``authorization: <key>`` (no Bearer): ``POST /v2/upload``
with the raw audio returns an ``upload_url``; ``POST /v2/transcript`` with
that URL returns a transcript id; ``GET /v2/transcript/{id}`` is polled
until its status is ``completed`` or ``error``. ``TRANSCRIPTION_URL`` is
``https://api.assemblyai.com`` (``https://api.eu.assemblyai.com`` for the EU).

No webhook: the archive already treats polling as the path that never
loses a result. A transcript not finished within the synchronous path's
wait counts as a request the server took and never answered: a failed row,
and the run ends. Times arrive in milliseconds and are stored in seconds.
"""

import asyncio
import time
import urllib.parse
from typing import Any, BinaryIO


class AssemblyAIProvider:
    name = "assemblyai"
    default_model = ""  # AssemblyAI picks its own speech models unless told
    diarizes = True

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": key}

    async def transcribe(
        self, client: Any, upload: BinaryIO, filename: str, *, model: str, prompt: str | None
    ) -> dict[str, Any]:
        from ..transcription import TranscriptionError

        uploaded = await client.request_json(
            "POST",
            "/v2/upload",
            timeout=client.sync_timeout(),
            body=upload,
            headers={"Content-Type": "application/octet-stream"},
        )
        audio_url = uploaded.get("upload_url")
        if not isinstance(audio_url, str) or not audio_url:
            raise TranscriptionError("invalid_json")
        request: dict[str, Any] = {"audio_url": audio_url, "speaker_labels": client.diarize}
        if client.language:
            request["language_code"] = client.language
        else:
            request["language_detection"] = True
        if model:
            request["speech_models"] = [model]
        job = await client.request_json("POST", "/v2/transcript", timeout=client.poll_timeout(), json=request)
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise TranscriptionError("invalid_json")
        path = f"/v2/transcript/{urllib.parse.quote(job_id, safe='')}"
        deadline = time.monotonic() + client.SYNC_RESPONSE_TIMEOUT_SECONDS
        while job.get("status") not in ("completed", "error"):
            if time.monotonic() >= deadline:
                raise TranscriptionError("timeout", transient=True, stalled=True)
            await asyncio.sleep(client.poll_interval)
            job = await client.request_json("GET", path, timeout=client.poll_timeout())
        if job["status"] == "error":
            # The message is server text about the audio: neither stored nor logged.
            raise TranscriptionError("failed")
        return _columns(job, model=model)


def _seconds(value: Any) -> float | None:
    from ..transcription_contract import _number

    ms = _number(value)
    return None if ms is None else ms / 1000


def _columns(job: dict[str, Any], *, model: str) -> dict[str, Any]:
    from ..transcription_contract import _dicts, _number, language_tag

    words = [
        {
            "w": w.get("text"),
            "s": _seconds(w.get("start")),
            "e": _seconds(w.get("end")),
            "c": _number(w.get("confidence")),
        }
        for w in _dicts(job.get("words"))
    ]
    segments = [
        {
            "s": _seconds(u.get("start")),
            "e": _seconds(u.get("end")),
            "text": u.get("text") if isinstance(u.get("text"), str) else "",
            "speaker": u.get("speaker") if isinstance(u.get("speaker"), str) else None,
        }
        for u in _dicts(job.get("utterances"))
    ]
    language = job.get("language_code")
    text = job.get("text")
    return {
        "text": text if isinstance(text, str) else "",
        # AssemblyAI writes regional codes with an underscore ("en_us").
        "language": language_tag(language.replace("_", "-") if isinstance(language, str) else None),
        "language_confidence": _number(job.get("language_confidence")),
        "confidence": _number(job.get("confidence")),
        "duration_s": _number(job.get("audio_duration")),
        "words": words,
        "segments": segments,
        "models": [model] if model else [],
    }


PROVIDER = AssemblyAIProvider()
