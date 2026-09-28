"""The OpenAI transcription endpoint: OpenAI itself and every compatible server.

``POST {TRANSCRIPTION_URL}/v1/audio/transcriptions`` asks for
``verbose_json`` with word and segment timings. A server that refuses that
shape with a 400 (OpenAI's gpt-4o-transcribe family accepts only ``json``;
Mistral refuses timings together with a language) is asked once more for
plain ``json``, and the rest of the run asks it for ``json`` straight away.
"""

from typing import Any, BinaryIO

from . import bearer


class OpenAIProvider:
    name = "openai"
    default_model = "whisper-1"
    diarizes = False

    def auth_headers(self, key: str) -> dict[str, str]:
        return bearer(key)

    async def transcribe(
        self, client: Any, upload: BinaryIO, filename: str, *, model: str, prompt: str | None
    ) -> dict[str, Any]:
        from ..transcription import TranscriptionError, result_columns

        try:
            payload = await client.transcribe(upload, filename, model=model, prompt=prompt, verbose=client.verbose)
        except TranscriptionError as e:
            if e.status != 400 or not client.verbose:
                raise
            upload.seek(0)
            payload = await client.transcribe(upload, filename, model=model, prompt=prompt, verbose=False)
            client.verbose = False
        return result_columns(payload, model=model)


PROVIDER = OpenAIProvider()
