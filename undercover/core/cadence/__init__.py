"""Cadence engines: text + a short clip of a person -> speech with that person's delivery (phase 4).

The engines (Chatterbox by default, Chatterbox-Turbo optional; F5-TTS as an add-on) and Whisper (transcripts) run in a separate worker process
(`worker.py`), started on first use and stopped after [cadence] idle_minutes unused. That keeps their import
stand-ins, crashes and VRAM out of the server: if the worker dies, the next request starts a new one.

This file is the app's side: which engines exist (`ENGINES`, light, no heavy imports) and the client (`WORKER`).
DoubleAgent (transcripts, picking references) and SpeakEasy (reading) both use it; the per-voice data a cadence needs
lives with the voice (models/voices/<name>/tts/ + extra.tts), written by DoubleAgent.
"""
from .client import WORKER, WorkerError, transcribe, speak  # noqa: F401
from .engines import ENGINES, FAMILY, default_engine, engine_ready, engine_info, MODEL_FILES, pack_ready, reading_engine  # noqa: F401
