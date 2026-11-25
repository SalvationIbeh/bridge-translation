from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

import miniaudio
import websocket

from .config import ProviderConfig

# Kokoro is optional — only imported if available
try:
    import numpy as np
    import soundfile as sf
    from kokoro_onnx import Kokoro as _KokoroModel
    _KOKORO_AVAILABLE = True
except ImportError:
    _KOKORO_AVAILABLE = False


class ProviderError(RuntimeError):
    pass


class SpeechToTextProvider(ABC):
    @abstractmethod
    def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError


class TranslationProvider(ABC):
    @abstractmethod
    def translate(self, text: str, source_language: str, target_language: str) -> str:
        raise NotImplementedError

    def translate_stream(
        self, text: str, source_language: str, target_language: str
    ) -> Iterator[str]:
        yield self.translate(text, source_language, target_language)


class TextToSpeechProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str, output_path: Path, language_code: str) -> Path:
        raise NotImplementedError


class MockSpeechToText(SpeechToTextProvider):
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    def transcribe(self, audio_path: Path) -> str:
        return self.transcript


class MockTranslator(TranslationProvider):
    PHRASES = {
        "quiero bloquear mi tarjeta porque vi un cargo extraño": (
            "I want to block my card because I saw a strange charge."
        ),
        "quiero bloquear mi tarjeta porque vi un cargo extrano": (
            "I want to block my card because I saw a strange charge."
        ),
        "necesito revisar el saldo de mi cuenta": "I need to check my account balance.",
        "no reconozco esta transaccion": "I do not recognize this transaction.",
    }

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        normalized = text.lower().strip().rstrip(".?!")
        return self.PHRASES.get(normalized, f"[mock {source_language}->{target_language}] {text}")


class MockTextToSpeech(TextToSpeechProvider):
    def synthesize(self, text: str, output_path: Path, language_code: str) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        return output_path


class DeepgramPrerecordedSTT(SpeechToTextProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def transcribe(self, audio_path: Path) -> str:
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        query = urllib.parse.urlencode(
            {
                "model": self.config.deepgram_model,
                "language": "es",
                "punctuate": "true",
                "smart_format": "true",
                "utterances": "true",
            }
        )
        url = f"https://api.deepgram.com/v1/listen?{query}"
        content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
        request = urllib.request.Request(
            url,
            data=audio_path.read_bytes(),
            method="POST",
            headers={
                "Authorization": f"Token {self.config.deepgram_api_key}",
                "Content-Type": content_type,
            },
        )
        payload = _json_request(request, provider="Deepgram")
        try:
            transcript = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected Deepgram response shape: {payload}") from exc
        if not transcript:
            raise ProviderError("Deepgram returned an empty transcript.")
        return transcript


class DeepgramStreamingSTT(SpeechToTextProvider):
    """Streams audio to Deepgram WebSocket API at real-time pace, simulating live mic input.

    Sends 20ms PCM chunks decoded from the source MP3. Captures the transcript on the first
    is_final=true message, which fires ~endpointing_ms after speech ends.
    """

    SAMPLE_RATE = 16000
    CHANNELS = 1
    CHUNK_MS = 20  # send 20ms of audio per frame
    ENDPOINTING_MS = 300

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def transcribe(self, audio_path: Path) -> str:
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        # Decode MP3 to 16kHz mono signed-16 PCM
        decoded = miniaudio.decode_file(
            str(audio_path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=self.CHANNELS,
            sample_rate=self.SAMPLE_RATE,
        )
        pcm: bytes = bytes(decoded.samples)
        bytes_per_chunk = int(self.SAMPLE_RATE * self.CHANNELS * 2 * self.CHUNK_MS / 1000)
        chunk_sleep = self.CHUNK_MS / 1000

        query = urllib.parse.urlencode(
            {
                "model": self.config.deepgram_model,
                "language": "es",
                "punctuate": "true",
                "smart_format": "true",
                "encoding": "linear16",
                "sample_rate": self.SAMPLE_RATE,
                "channels": self.CHANNELS,
                "endpointing": self.ENDPOINTING_MS,
            }
        )
        ws_url = f"wss://api.deepgram.com/v1/listen?{query}"

        transcript_parts: list[str] = []
        errors: list[str] = []
        finished = threading.Event()

        def on_open(ws: websocket.WebSocketApp) -> None:
            # Stream PCM at real-time pace from the on_open callback — guarantees connection is live
            offset = 0
            while offset < len(pcm):
                chunk = pcm[offset : offset + bytes_per_chunk]
                try:
                    ws.send(chunk, websocket.ABNF.OPCODE_BINARY)
                except Exception:
                    return
                offset += bytes_per_chunk
                time.sleep(chunk_sleep)
            try:
                ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

        def on_message(ws: websocket.WebSocketApp, message: str) -> None:
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
            # SpeechFinal fires when Deepgram detects end of utterance
            if data.get("type") == "SpeechFinal":
                finished.set()
            # Metadata signals stream is fully closed
            if data.get("type") == "Metadata":
                finished.set()

        def on_error(ws: websocket.WebSocketApp, error: object) -> None:
            errors.append(str(error))
            finished.set()

        ws_app = websocket.WebSocketApp(
            ws_url,
            header={"Authorization": f"Token {self.config.deepgram_api_key}"},
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
        )

        thread = threading.Thread(target=ws_app.run_forever, daemon=True)
        thread.start()

        # Wait for SpeechFinal/Metadata or timeout (audio duration + 3s buffer)
        audio_duration_s = len(pcm) / (self.SAMPLE_RATE * self.CHANNELS * 2)
        finished.wait(timeout=audio_duration_s + 3.0)
        ws_app.close()
        thread.join(timeout=2.0)

        if errors and not transcript_parts:
            raise ProviderError(f"Deepgram streaming error: {errors[0]}")
        if not transcript_parts:
            raise ProviderError("Deepgram streaming returned no transcript.")

        return " ".join(transcript_parts)


class OpenAITranslator(TranslationProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        body = {
            "model": self.config.openai_model,
            "store": False,
            "instructions": (
                "You translate short customer-service banking utterances. "
                "Preserve account, card, fraud, and support terminology. "
                "Return only the translated sentence, with no commentary."
            ),
            "input": (
                f"Translate from {source_language} to {target_language}.\n\n"
                f"Utterance:\n{text}"
            ),
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        payload = _json_request(request, provider="OpenAI")
        translated = payload.get("output_text") or _first_text(payload)
        if not translated:
            raise ProviderError(f"OpenAI response did not contain text: {payload}")
        return translated.strip()

    def translate_stream(
        self, text: str, source_language: str, target_language: str
    ) -> Iterator[str]:
        body = {
            "model": self.config.openai_model,
            "stream": True,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate short customer-service banking utterances. "
                        "Preserve account, card, fraud, and support terminology. "
                        "Return only the translated sentence, with no commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Translate from {source_language} to {target_language}.\n\n"
                        f"Utterance:\n{text}"
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if not line.startswith("data: "):
                        continue
                    payload_str = line[6:]
                    if payload_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_str)
                        content = chunk["choices"][0]["delta"].get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if content:
                        yield content
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            payload = _parse_json(err_body)
            if _is_openai_insufficient_quota(payload):
                raise ProviderError(
                    "OpenAI translation could not run: the API key has insufficient quota."
                ) from exc
            raise ProviderError(f"OpenAI HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc


class OpenAITextToSpeech(TextToSpeechProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def synthesize(self, text: str, output_path: Path, language_code: str) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "model": self.config.openai_tts_model,
            "voice": self.config.openai_tts_voice,
            "input": text,
            "instructions": (
                "Speak clearly and neutrally for a customer-service call. "
                "Keep the pacing natural and easy to understand."
            ),
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                output_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            payload = _parse_json(body)
            if _is_openai_insufficient_quota(payload):
                raise ProviderError(
                    "OpenAI TTS could not run: the API key has insufficient quota. "
                    "Add API billing/credits for the API project behind OPENAI_API_KEY "
                    "and retry."
                ) from exc
            raise ProviderError(f"OpenAI TTS HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenAI TTS request failed: {exc}") from exc
        return output_path

    def synthesize_first_byte(self, text: str, output_path: Path) -> tuple[Path, float]:
        """Like synthesize(), but returns (path, t_first_audio_byte) where t_first_audio_byte
        is time.perf_counter() captured on the first non-empty response chunk."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "model": self.config.openai_tts_model,
            "voice": self.config.openai_tts_voice,
            "input": text,
            "instructions": (
                "Speak clearly and neutrally for a customer-service call. "
                "Keep the pacing natural and easy to understand."
            ),
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            chunks: list[bytes] = []
            t_first_byte: float | None = None
            with urllib.request.urlopen(request, timeout=60) as response:
                while True:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    if t_first_byte is None:
                        t_first_byte = time.perf_counter()
                    chunks.append(chunk)
            output_path.write_bytes(b"".join(chunks))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            payload = _parse_json(err_body)
            if _is_openai_insufficient_quota(payload):
                raise ProviderError(
                    "OpenAI TTS could not run: the API key has insufficient quota. "
                    "Add API billing/credits for the API project behind OPENAI_API_KEY "
                    "and retry."
                ) from exc
            raise ProviderError(f"OpenAI TTS HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenAI TTS request failed: {exc}") from exc

        if t_first_byte is None:
            raise ProviderError("OpenAI TTS returned an empty response.")
        return output_path, t_first_byte


class ElevenLabsStreamingTTS(TextToSpeechProvider):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    def synthesize(self, text: str, output_path: Path, language_code: str) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        query = urllib.parse.urlencode({"output_format": "mp3_44100_128"})
        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{urllib.parse.quote(self.config.elevenlabs_voice_id)}/stream?{query}"
        )
        body = {
            "text": text,
            "model_id": self.config.elevenlabs_model,
            "language_code": language_code,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "xi-api-key": self.config.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                output_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"ElevenLabs HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"ElevenLabs request failed: {exc}") from exc
        return output_path

    def synthesize_first_byte(self, text: str, output_path: Path) -> tuple[Path, float]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        query = urllib.parse.urlencode({"output_format": "mp3_44100_128"})
        url = (
            "https://api.elevenlabs.io/v1/text-to-speech/"
            f"{urllib.parse.quote(self.config.elevenlabs_voice_id)}/stream?{query}"
        )
        body = {
            "text": text,
            "model_id": self.config.elevenlabs_model,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "xi-api-key": self.config.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            chunks: list[bytes] = []
            t_first_byte: float | None = None
            with urllib.request.urlopen(request, timeout=60) as response:
                while True:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    if t_first_byte is None:
                        t_first_byte = time.perf_counter()
                    chunks.append(chunk)
            output_path.write_bytes(b"".join(chunks))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"ElevenLabs HTTP {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"ElevenLabs request failed: {exc}") from exc

        if t_first_byte is None:
            raise ProviderError("ElevenLabs TTS returned an empty response.")
        return output_path, t_first_byte


class KokoroTTS(TextToSpeechProvider):
    """Local TTS using Kokoro ONNX. Model is loaded and warmed on init so first
    real synthesis is fast (~500ms–1.7s depending on sentence length)."""

    WARMUP_TEXT = "Hola."  # short warm-up to JIT-compile ONNX graph

    def __init__(self, model_path: Path, voices_path: Path, voice: str = "ef_dora") -> None:
        if not _KOKORO_AVAILABLE:
            raise ProviderError(
                "kokoro-onnx and soundfile are required for local TTS. "
                "Run: pip install kokoro-onnx soundfile"
            )
        self.voice = voice
        self._model = _KokoroModel(str(model_path), str(voices_path))
        # Warm up ONNX graph — eliminates cold-start penalty on first real utterance
        self._model.create(self.WARMUP_TEXT, voice=self.voice, lang="es")

    def synthesize(self, text: str, output_path: Path, language_code: str) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lang = language_code[:2] if language_code else "es"
        samples, sample_rate = self._model.create(text, voice=self.voice, lang=lang)
        sf.write(str(output_path), samples, sample_rate, format="WAV")
        return output_path

    def synthesize_first_byte(self, text: str, output_path: Path) -> tuple[Path, float]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Kokoro runs synchronously — synthesis completes before returning.
        # t_first_byte is captured immediately after create() returns since
        # all audio is available at that moment (no network round-trip).
        samples, sample_rate = self._model.create(text, voice=self.voice, lang="es")
        t_first_byte = time.perf_counter()
        sf.write(str(output_path.with_suffix(".wav")), samples, sample_rate, format="WAV")
        output_path = output_path.with_suffix(".wav")
        return output_path, t_first_byte


def _json_request(request: urllib.request.Request, provider: str) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        payload = _parse_json(body)
        if provider == "OpenAI" and _is_openai_insufficient_quota(payload):
            raise ProviderError(
                "OpenAI translation could not run: the API key has insufficient quota. "
                "Add API billing/credits for the API project behind OPENAI_API_KEY, "
                "wait a couple of minutes if billing was just updated, and retry."
            ) from exc
        raise ProviderError(f"{provider} HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"{provider} request failed: {exc}") from exc


def _first_text(value: object) -> str:
    if isinstance(value, dict):
        for key in ("text", "content"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        for candidate in value.values():
            found = _first_text(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_text(item)
            if found:
                return found
    return ""


def _parse_json(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _is_openai_insufficient_quota(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return isinstance(error, dict) and error.get("code") == "insufficient_quota"
