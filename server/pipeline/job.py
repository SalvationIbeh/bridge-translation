"""
Job — the unit of work flowing through the pipeline.
Each job carries a session_id so results route back to the right WebSocket.
"""
from dataclasses import dataclass, field
from typing import Optional
import time
import uuid


@dataclass
class Job:
    session_id:   str
    source_lang:  str
    target_lang:  str

    # Filled progressively as the job moves through pipeline stages
    audio:        Optional[bytes] = None   # Raw PCM for STT
    transcript:   Optional[str]   = None   # STT output
    translated:   Optional[str]   = None   # Translation output
    tts_audio:    Optional[bytes] = None   # Synthesized audio

    # Timing — set at each stage boundary for latency tracking
    enqueued_at:     float = field(default_factory=time.monotonic)
    stt_start:       Optional[float] = None
    stt_end:         Optional[float] = None
    translate_start: Optional[float] = None
    translate_end:   Optional[float] = None
    tts_start:       Optional[float] = None
    tts_end:         Optional[float] = None

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def total_ms(self) -> Optional[float]:
        if self.tts_end:
            return (self.tts_end - self.enqueued_at) * 1000
        return None
