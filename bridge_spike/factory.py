from __future__ import annotations

from .config import ProviderConfig, load_dotenv
from .latency import LatencyLog
from .pipeline import Stage1Pipeline, StreamingPipeline
from .providers import (
    DeepgramPrerecordedSTT,
    DeepgramStreamingSTT,
    ElevenLabsStreamingTTS,
    KokoroTTS,
    MockSpeechToText,
    MockTextToSpeech,
    MockTranslator,
    OpenAITextToSpeech,
    OpenAITranslator,
)


DEFAULT_MOCK_TRANSCRIPT = "Quiero bloquear mi tarjeta porque vi un cargo extrano."


def build_stage1_pipeline(
    *,
    mode: str,
    tts_provider: str,
    latency_log: LatencyLog,
    mock_transcript: str = DEFAULT_MOCK_TRANSCRIPT,
) -> Stage1Pipeline:
    if mode == "mock":
        return Stage1Pipeline(
            stt=MockSpeechToText(mock_transcript),
            translator=MockTranslator(),
            tts=MockTextToSpeech(),
            latency_log=latency_log,
        )

    load_dotenv()
    config = ProviderConfig.from_env()
    config.require_real_mode(tts_provider)
    tts = (
        ElevenLabsStreamingTTS(config)
        if tts_provider == "elevenlabs"
        else OpenAITextToSpeech(config)
    )
    return Stage1Pipeline(
        stt=DeepgramPrerecordedSTT(config),
        translator=OpenAITranslator(config),
        tts=tts,
        latency_log=latency_log,
    )


def build_streaming_pipeline(
    *,
    mode: str,
    tts_provider: str = "openai",
    stt_provider: str = "prerecorded",
    latency_log: LatencyLog,
    mock_transcript: str = DEFAULT_MOCK_TRANSCRIPT,
) -> StreamingPipeline:
    if mode == "mock":
        return StreamingPipeline(
            stt=MockSpeechToText(mock_transcript),
            translator=MockTranslator(),
            tts=MockTextToSpeech(),  # type: ignore[arg-type]
            latency_log=latency_log,
        )

    load_dotenv()
    config = ProviderConfig.from_env()
    config.require_real_mode(tts_provider)
    stt = (
        DeepgramStreamingSTT(config)
        if stt_provider == "streaming"
        else DeepgramPrerecordedSTT(config)
    )
    if tts_provider == "kokoro":
        tts = KokoroTTS(
            model_path=Path(config.kokoro_model_path),
            voices_path=Path(config.kokoro_voices_path),
            voice=config.kokoro_voice,
        )
    elif tts_provider == "elevenlabs":
        tts = ElevenLabsStreamingTTS(config)
    else:
        tts = OpenAITextToSpeech(config)

    return StreamingPipeline(
        stt=stt,
        translator=OpenAITranslator(config),
        tts=tts,
        latency_log=latency_log,
    )

