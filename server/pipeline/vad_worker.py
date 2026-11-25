"""
VAD Worker — per-session voice activity detection.
Each session gets its own VADWorker instance. Lightweight enough to run
on CPU inside the asyncio event loop without blocking.

Silero VAD fires an utterance_ready callback when speech ends, which
enqueues a Job into the STT queue.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Callable, Awaitable

import numpy as np
import torch

import config
from pipeline.job import Job

logger = logging.getLogger(__name__)

# Load Silero VAD once at module level — shared across all VAD workers.
# It's CPU-bound and tiny; no CUDA needed.
_vad_model = None
_vad_utils = None

def _load_vad():
    global _vad_model, _vad_utils
    if _vad_model is None:
        logger.info("Loading Silero VAD model...")
        _vad_model, _vad_utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        logger.info("Silero VAD loaded.")
    return _vad_model, _vad_utils


# Silero VAD v4 requires exactly 512 samples per inference call at 16kHz (32ms).
VAD_WINDOW_SAMPLES = 512
VAD_WINDOW_BYTES   = VAD_WINDOW_SAMPLES * 2   # 16-bit PCM


class VADWorker:
    """
    Maintains audio buffer for a single session.
    Accepts arbitrary-sized PCM chunks; buffers internally and runs VAD
    inference in 512-sample windows as required by Silero VAD v4.
    When speech ends, fires the on_utterance callback with a Job.
    """

    def __init__(
        self,
        session_id: str,
        source_lang: str,
        target_lang: str,
        on_utterance: Callable[[Job], Awaitable[None]],
    ):
        self.session_id   = session_id
        self.source_lang  = source_lang
        self.target_lang  = target_lang
        self.on_utterance = on_utterance

        self._model, utils = _load_vad()
        self._get_speech_timestamps = utils[0]

        self._utterance_buf: list[bytes] = []   # Accumulates raw PCM for the utterance
        self._vad_remainder: bytes = b""        # Partial window waiting for more bytes
        self._speech_started  = False
        self._silence_windows = 0

        silence_ms          = config.VAD_SILENCE_MS
        min_speech_ms       = config.VAD_MIN_SPEECH_MS
        window_ms           = VAD_WINDOW_SAMPLES * 1000 // config.AUDIO_SAMPLE_RATE   # 32ms
        self._silence_threshold   = max(1, silence_ms    // window_ms)
        self._min_speech_windows  = max(1, min_speech_ms // window_ms)
        self._max_utterance_bytes = config.VAD_MAX_BUFFER_S * config.AUDIO_SAMPLE_RATE * 2

    def feed(self, pcm_bytes: bytes) -> bool:
        """
        Feed any number of raw PCM bytes. Returns True when an utterance has
        ended and flush() should be called. May process multiple VAD windows
        per call if a large chunk is provided.
        """
        self._vad_remainder += pcm_bytes
        self._utterance_buf.append(pcm_bytes)

        # Cap utterance buffer to prevent unbounded growth
        total = sum(len(b) for b in self._utterance_buf)
        if total > self._max_utterance_bytes:
            overflow = total - self._max_utterance_bytes
            while self._utterance_buf and overflow > 0:
                overflow -= len(self._utterance_buf[0])
                self._utterance_buf.pop(0)

        # Process as many complete 512-sample windows as available
        utterance_ready = False
        while len(self._vad_remainder) >= VAD_WINDOW_BYTES:
            window_bytes           = self._vad_remainder[:VAD_WINDOW_BYTES]
            self._vad_remainder    = self._vad_remainder[VAD_WINDOW_BYTES:]

            samples     = np.frombuffer(window_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            tensor      = torch.from_numpy(samples)
            speech_prob = self._model(tensor, config.AUDIO_SAMPLE_RATE).item()
            is_speech   = speech_prob >= config.VAD_THRESHOLD

            if is_speech:
                self._speech_started  = True
                self._silence_windows = 0
            elif self._speech_started:
                self._silence_windows += 1
                if self._silence_windows >= self._silence_threshold:
                    speech_windows = (
                        sum(len(b) for b in self._utterance_buf) // VAD_WINDOW_BYTES
                        - self._silence_windows
                    )
                    if speech_windows >= self._min_speech_windows:
                        utterance_ready = True
                    else:
                        self._reset()

        return utterance_ready

    def flush(self) -> Job:
        """
        Return a Job containing the buffered utterance audio and reset state.
        Call immediately after feed() returns True.
        """
        audio = b"".join(self._utterance_buf)
        self._reset()
        return Job(
            session_id=self.session_id,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
            audio=audio,
            enqueued_at=time.monotonic(),
        )

    @property
    def has_audio(self) -> bool:
        """True if any PCM bytes have been buffered since last flush."""
        return bool(self._utterance_buf)

    def discard(self) -> None:
        """Drop all buffered audio WITHOUT producing a Job. Used by redaction mode
        to ensure a flagged card-entry segment is never transcribed (§8.4)."""
        self._reset()

    def _reset(self):
        self._utterance_buf   = []
        self._vad_remainder   = b""
        self._speech_started  = False
        self._silence_windows = 0
