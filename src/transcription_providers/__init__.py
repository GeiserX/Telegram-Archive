"""Speech-to-text providers of the synchronous path (docs/TRANSCRIPTION.md, "Providers").

``TRANSCRIPTION_PROVIDER`` picks one. ``auto`` and ``akou`` keep akou's job
path in ``src/transcription.py``; everything that is not akou's job path
goes through one of the providers here, and a provider is one module in
this package with a module-level ``PROVIDER`` object:

- ``name``: the ``TRANSCRIPTION_PROVIDER`` value, the module's name and the
  ``source`` and ``engine_name`` of the rows it writes (16 characters at most).
- ``default_model``: sent when ``TRANSCRIPTION_MODEL`` is empty; ``""`` means
  "the provider's own default", and the field is left out.
- ``diarizes``: True when ``TRANSCRIPTION_DIARIZE`` turns on speaker labels.
- ``auth_headers(key)``: the headers that carry ``TRANSCRIPTION_API_KEY``.
- ``async transcribe(client, upload, filename, *, model, prompt)``: send the
  open file and return the row's columns in the shape ``result_columns``
  returns (``text``, ``language``, ``duration_s``, ``words`` as
  ``[{w, s, e, c}]``, ``segments`` as ``[{s, e, text, speaker}]``, ``models``,
  optionally ``language_confidence`` and ``confidence``).

A provider sends through ``client.request_json`` (or ``client.transcribe``
for the OpenAI shape), which already retries, never follows redirects,
logs no URL and raises ``TranscriptionError`` with the HTTP status, so the
drain's rules apply unchanged: an outage or a 401, 402, 403 or 429 keeps
the row queued, a 413 is ``too_large``, any other 4xx is an answer about
the file once the server has transcribed something in the run. Adding one
is a module here and its name in ``TRANSCRIPTION_PROVIDERS`` in
``src/config.py``, the one list both read.

This package imports nothing from ``src.transcription`` at import time,
so that module can import it; the provider modules do, and are loaded
lazily by ``load``. The viewer image copies neither.
"""

import importlib
from typing import Any, BinaryIO, Protocol

from ..config import TRANSCRIPTION_PROVIDERS

# The providers with an adapter of their own: every name the setting takes
# but the three the archive handles itself. ``openai`` is the OpenAI
# transcription endpoint that auto, akou's app mode and every compatible
# server share; it writes ``source = "openai"`` like before this package.
NATIVE_PROVIDERS = TRANSCRIPTION_PROVIDERS - {"auto", "akou", "openai"}


class Provider(Protocol):
    name: str
    default_model: str
    diarizes: bool

    def auth_headers(self, key: str) -> dict[str, str]: ...

    async def transcribe(
        self, client: Any, upload: BinaryIO, filename: str, *, model: str, prompt: str | None
    ) -> dict[str, Any]: ...


def load(name: str) -> Provider:
    """The ``PROVIDER`` of ``src/transcription_providers/<name>.py``; ``openai`` for ``auto`` and ``akou``."""
    module = name if name in NATIVE_PROVIDERS else "openai"
    return importlib.import_module(f"{__name__}.{module}").PROVIDER


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def segments_from_words(words: list[dict[str, Any]], speakers: list[Any]) -> list[dict[str, Any]]:
    """One segment per run of the same speaker; no segments when no word names one.

    ``words`` are already in the row's ``{w, s, e, c}`` shape and
    ``speakers`` holds each word's label, None when the provider gave none.
    Labels are stored as strings, the shape the viewer builds turns from.
    """
    if not any(s is not None for s in speakers):
        return []
    segments: list[dict[str, Any]] = []
    for word, speaker in zip(words, speakers, strict=True):
        label = None if speaker is None else str(speaker)
        text = word["w"] if isinstance(word["w"], str) else ""
        if segments and segments[-1]["speaker"] == label:
            segments[-1]["e"] = word["e"]
            segments[-1]["text"] = f"{segments[-1]['text']} {text}".strip()
        else:
            segments.append({"s": word["s"], "e": word["e"], "text": text, "speaker": label})
    return segments
