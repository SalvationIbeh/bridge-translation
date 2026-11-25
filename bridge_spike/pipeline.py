from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .latency import LatencyLog
from .providers import OpenAITextToSpeech, OpenAITranslator, SpeechToTextProvider, TextToSpeechProvider, TranslationProvider


@dataclass(frozen=True)
class PipelineResult:
    transcript: str
    translation: str
    output_path: Path
    latency_log_path: Path
    total_ms: float
    t_first_byte_ms: float | None = None
    sentence_count: int | None = None


class Stage1Pipeline:
    def __init__(
        self,
        stt: SpeechToTextProvider,
        translator: TranslationProvider,
        tts: TextToSpeechProvider,
        latency_log: LatencyLog,
    ) -> None:
        self.stt = stt
        self.translator = translator
        self.tts = tts
        self.latency_log = latency_log

    def run(self, audio_path: Path, output_path: Path) -> PipelineResult:
        with self.latency_log.measure("speech_to_text", str(audio_path)):
            transcript = self.stt.transcribe(audio_path)

        with self.latency_log.measure("translation", "Spanish to English"):
            translation = self.translator.translate(
                transcript,
                source_language="Spanish",
                target_language="English",
            )

        with self.latency_log.measure("text_to_speech", str(output_path)):
            final_output = self.tts.synthesize(
                translation,
                output_path=output_path,
                language_code="en",
            )

        return PipelineResult(
            transcript=transcript,
            translation=translation,
            output_path=final_output,
            latency_log_path=self.latency_log.log_path,
            total_ms=self.latency_log.total_ms(),
        )


class StreamingPipeline:
    """MT→TTS streaming overlap pipeline.

    Streams translation tokens, buffers into sentences, and fires TTS on each complete
    sentence. Captures t_first_audio_byte — the moment the first TTS audio chunk arrives —
    as the primary latency metric.
    """

    MIN_SENTENCE_CHARS = 20
    SENTENCE_BOUNDARIES = ".!?;"

    def __init__(
        self,
        stt: SpeechToTextProvider,
        translator: OpenAITranslator,
        tts: OpenAITextToSpeech,
        latency_log: LatencyLog,
    ) -> None:
        self.stt = stt
        self.translator = translator
        self.tts = tts
        self.latency_log = latency_log

    def run(self, audio_path: Path, output_path: Path) -> PipelineResult:
        t_start = time.perf_counter()
        self.latency_log.pipeline_start = t_start

        with self.latency_log.measure("speech_to_text", str(audio_path)):
            transcript = self.stt.transcribe(audio_path)

        token_iter = self.translator.translate_stream(transcript, "Spanish", "English")
        sentence_paths: list[Path] = []
        full_translation_parts: list[str] = []
        t_first_byte: float | None = None

        with self.latency_log.measure("translation_tts_overlap", "streaming MT→TTS"):
            for i, sentence in enumerate(self._buffer_sentences(token_iter)):
                full_translation_parts.append(sentence)
                sentence_path = output_path.parent / f"sentence_{i:02d}.mp3"
                sentence_path.parent.mkdir(parents=True, exist_ok=True)

                if i == 0 and hasattr(self.tts, "synthesize_first_byte"):
                    _, t_first_byte = self.tts.synthesize_first_byte(sentence, sentence_path)
                    self.latency_log.record_milestone(
                        "t_first_audio_byte", t_first_byte, f"sentence 0: {sentence[:40]}"
                    )
                else:
                    self.tts.synthesize(sentence, sentence_path, "en")
                    if i == 0:
                        # Mock mode: record milestone at synthesis completion
                        self.latency_log.record_milestone(
                            "t_first_audio_byte", time.perf_counter(), f"sentence 0: {sentence[:40]}"
                        )

                sentence_paths.append(sentence_path)

        # Concatenate sentence MP3s into final output (binary concat is valid for same-codec MP3)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"".join(p.read_bytes() for p in sentence_paths))

        translation = " ".join(full_translation_parts)
        return PipelineResult(
            transcript=transcript,
            translation=translation,
            output_path=output_path,
            latency_log_path=self.latency_log.log_path,
            total_ms=self.latency_log.total_ms(),
            t_first_byte_ms=self.latency_log.first_byte_ms(),
            sentence_count=len(sentence_paths),
        )

    def _buffer_sentences(self, token_iter: Iterator[str]) -> Iterator[str]:
        buffer = ""
        for token in token_iter:
            buffer += token
            # Find the rightmost sentence boundary in the buffer
            best_idx = -1
            for boundary in self.SENTENCE_BOUNDARIES:
                idx = buffer.rfind(boundary)
                if idx > best_idx:
                    best_idx = idx
            if best_idx >= 0:
                candidate = buffer[: best_idx + 1].strip()
                if len(candidate) >= self.MIN_SENTENCE_CHARS:
                    yield candidate
                    buffer = buffer[best_idx + 1 :]
        # Flush any remaining text after the stream ends
        remainder = buffer.strip()
        if remainder:
            yield remainder

