"""Live microphone latency probe.

Captures speech from the mic, streams to Deepgram, translates, and synthesizes.
Reports t_first_audio_byte from the moment Deepgram fires is_final.

Usage:
    python scripts/live_latency_probe.py
    python scripts/live_latency_probe.py --source-lang en --target-lang es
    python scripts/live_latency_probe.py --tts-provider elevenlabs --runs 3
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import sounddevice as sd
import websocket

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge_spike.config import ProviderConfig, load_dotenv
from bridge_spike.providers import (
    ElevenLabsStreamingTTS,
    KokoroTTS,
    OpenAITextToSpeech,
    OpenAITranslator,
    ProviderError,
)

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 20
BYTES_PER_CHUNK = int(SAMPLE_RATE * CHANNELS * 2 * CHUNK_MS / 1000)  # 16-bit = 2 bytes
MIN_SENTENCE_CHARS = 20
SENTENCE_BOUNDARIES = ".!?;"


@dataclass
class ProbeResult:
    transcript: str
    translation: str
    t_stt_final_ms: float    # pipeline start → Deepgram is_final
    t_first_byte_ms: float   # pipeline start → first TTS audio chunk
    sentence_count: int
    output_path: Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live mic latency probe for Bridge Stage 1.")
    p.add_argument("--source-lang", default="en", help="Spoken language (default: en)")
    p.add_argument("--target-lang", default="es", help="Translation target (default: es)")
    p.add_argument("--tts-provider", choices=["openai", "elevenlabs", "kokoro"], default="openai")
    p.add_argument("--device", type=int, default=None, help="sounddevice input device index")
    p.add_argument("--runs", type=int, default=1, help="Number of utterances to capture")
    p.add_argument("--output-dir", type=Path, default=Path("runs/live-probe"))
    p.add_argument("--endpointing", type=int, default=300, help="Deepgram endpointing ms")
    return p.parse_args()


def _buffer_sentences(token_iter):
    """Yield complete sentences from a token stream."""
    buffer = ""
    for token in token_iter:
        buffer += token
        best_idx = -1
        for b in SENTENCE_BOUNDARIES:
            idx = buffer.rfind(b)
            if idx > best_idx:
                best_idx = idx
        if best_idx >= 0:
            candidate = buffer[: best_idx + 1].strip()
            if len(candidate) >= MIN_SENTENCE_CHARS:
                yield candidate
                buffer = buffer[best_idx + 1 :]
    remainder = buffer.strip()
    if remainder:
        yield remainder


def run_probe(
    run_idx: int,
    args: argparse.Namespace,
    config: ProviderConfig,
    translator: OpenAITranslator,
    tts,
) -> ProbeResult:
    output_dir = args.output_dir / f"run_{run_idx:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Mic capture setup ──────────────────────────────────────────────────
    audio_q: queue.Queue[bytes] = queue.Queue()
    stop_capture = threading.Event()

    def mic_callback(indata, frames, time_info, status):
        if not stop_capture.is_set():
            audio_q.put(bytes(indata))

    # ── Deepgram WebSocket setup ───────────────────────────────────────────
    query = urllib.parse.urlencode({
        "model": config.deepgram_model,
        "language": args.source_lang,
        "punctuate": "true",
        "smart_format": "true",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "endpointing": args.endpointing,
        "interim_results": "true",
        "utterance_end_ms": "1000",
    })
    ws_url = f"wss://api.deepgram.com/v1/listen?{query}"

    transcript_parts: list[str] = []
    stt_final_event = threading.Event()
    t_stt_final: list[float] = []
    t_pipeline_start: list[float] = []
    ws_errors: list[str] = []

    def on_open(ws):
        t_pipeline_start.append(time.perf_counter())
        print("\n  [Listening... speak now. Deepgram will auto-detect when you stop.]\n")

        def stream_mic():
            while not stt_final_event.is_set():
                try:
                    chunk = audio_q.get(timeout=0.1)
                    ws.send(chunk, websocket.ABNF.OPCODE_BINARY)
                except queue.Empty:
                    continue
                except Exception:
                    break
            try:
                ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

        threading.Thread(target=stream_mic, daemon=True).start()

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        if data.get("type") == "Results" and data.get("is_final"):
            text = (
                data.get("channel", {})
                .get("alternatives", [{}])[0]
                .get("transcript", "")
                .strip()
            )
            if text:
                transcript_parts.append(text)
                if not t_stt_final:
                    t_stt_final.append(time.perf_counter())
                    stt_final_event.set()
                    stop_capture.set()
                    print(f"  STT final: \"{text}\"")

        if data.get("type") in ("SpeechFinal", "Metadata") and not stt_final_event.is_set():
            stt_final_event.set()
            stop_capture.set()

    def on_error(ws, error):
        ws_errors.append(str(error))
        stt_final_event.set()
        stop_capture.set()

    ws_app = websocket.WebSocketApp(
        ws_url,
        header={"Authorization": f"Token {config.deepgram_api_key}"},
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )

    # ── Start mic and WebSocket ────────────────────────────────────────────
    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=BYTES_PER_CHUNK // 2,  # frames (samples), not bytes
        device=args.device,
        callback=mic_callback,
    )

    ws_thread = threading.Thread(target=ws_app.run_forever, daemon=True)
    ws_thread.start()

    with stream:
        stt_final_event.wait(timeout=30.0)

    ws_app.close()
    ws_thread.join(timeout=2.0)

    if ws_errors and not transcript_parts:
        raise ProviderError(f"Deepgram error: {ws_errors[0]}")
    if not transcript_parts:
        raise ProviderError("No transcript received — did you speak into the mic?")

    transcript = " ".join(transcript_parts)
    t_start = t_pipeline_start[0] if t_pipeline_start else time.perf_counter()
    t_stt = t_stt_final[0] if t_stt_final else time.perf_counter()
    t_stt_final_ms = (t_stt - t_start) * 1000

    # ── MT streaming → TTS ────────────────────────────────────────────────
    print(f"  Translating: {args.source_lang} → {args.target_lang}")
    token_iter = translator.translate_stream(transcript, args.source_lang, args.target_lang)

    sentence_paths: list[Path] = []
    translation_parts: list[str] = []
    t_first_byte: float | None = None

    for i, sentence in enumerate(_buffer_sentences(token_iter)):
        translation_parts.append(sentence)
        sentence_path = output_dir / f"sentence_{i:02d}.mp3"

        if i == 0 and hasattr(tts, "synthesize_first_byte"):
            _, t_fb = tts.synthesize_first_byte(sentence, sentence_path)
            t_first_byte = t_fb
            fb_ms = (t_fb - t_start) * 1000
            print(f"  TTS first byte for sentence 0: {fb_ms:.0f}ms from pipeline start")
        else:
            tts.synthesize(sentence, sentence_path, args.target_lang[:2])
            if i == 0:
                t_first_byte = time.perf_counter()

        sentence_paths.append(sentence_path)
        print(f"  Sentence {i}: \"{sentence[:60]}{'...' if len(sentence)>60 else ''}\"")

    translation = " ".join(translation_parts)

    # Concatenate sentence audio
    output_path = output_dir / "output.mp3"
    output_path.write_bytes(b"".join(p.read_bytes() for p in sentence_paths))

    t_first_byte_ms = (t_first_byte - t_start) * 1000 if t_first_byte else 0.0

    return ProbeResult(
        transcript=transcript,
        translation=translation,
        t_stt_final_ms=round(t_stt_final_ms, 2),
        t_first_byte_ms=round(t_first_byte_ms, 2),
        sentence_count=len(sentence_paths),
        output_path=output_path,
    )


def main() -> None:
    args = parse_args()
    load_dotenv()
    config = ProviderConfig.from_env()
    config.require_real_mode(args.tts_provider)

    translator = OpenAITranslator(config)
    if args.tts_provider == "kokoro":
        print("  Loading Kokoro model (warm-up ~4s on first run)...")
        tts = KokoroTTS(
            model_path=Path(config.kokoro_model_path),
            voices_path=Path(config.kokoro_voices_path),
            voice=config.kokoro_voice,
        )
        print("  Kokoro ready.")
    elif args.tts_provider == "elevenlabs":
        tts = ElevenLabsStreamingTTS(config)
    else:
        tts = OpenAITextToSpeech(config)

    print(f"Bridge Live Latency Probe")
    print(f"  source lang : {args.source_lang}")
    print(f"  target lang : {args.target_lang}")
    print(f"  tts         : {args.tts_provider}")
    print(f"  endpointing : {args.endpointing}ms")
    print(f"  runs        : {args.runs}")
    print()

    results: list[ProbeResult] = []
    for i in range(args.runs):
        if args.runs > 1:
            print(f"--- Run {i + 1}/{args.runs} ---")
        input("  Press ENTER to start recording...")

        try:
            result = run_probe(i, args, config, translator, tts)
        except ProviderError as exc:
            print(f"  ERROR: {exc}")
            continue

        target_met = result.t_first_byte_ms < 2000
        marker = "[PASS]" if target_met else "[FAIL]"
        results.append(result)

        print()
        print(f"  transcript  : {result.transcript}")
        print(f"  translation : {result.translation}")
        print(f"  STT final   : {result.t_stt_final_ms:.0f}ms")
        print(f"  first byte  : {result.t_first_byte_ms:.0f}ms  {marker} (<2000ms target)")
        print(f"  sentences   : {result.sentence_count}")
        print(f"  output      : {result.output_path}")

        # Save JSON record
        record = {
            "run": i,
            "source_lang": args.source_lang,
            "target_lang": args.target_lang,
            "tts_provider": args.tts_provider,
            "transcript": result.transcript,
            "translation": result.translation,
            "t_stt_final_ms": result.t_stt_final_ms,
            "t_first_byte_ms": result.t_first_byte_ms,
            "sentence_count": result.sentence_count,
        }
        record_path = args.output_dir / f"run_{i:02d}" / "result.json"
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

        if i < args.runs - 1:
            print()

    if len(results) > 1:
        import statistics
        fbs = [r.t_first_byte_ms for r in results]
        print()
        print("--- Summary ---")
        print(f"  runs        : {len(results)}")
        print(f"  p50 first-byte: {statistics.median(fbs):.0f}ms")
        print(f"  min first-byte: {min(fbs):.0f}ms")
        print(f"  max first-byte: {max(fbs):.0f}ms")


if __name__ == "__main__":
    main()
