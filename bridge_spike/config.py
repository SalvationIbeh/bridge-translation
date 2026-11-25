from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class ProviderConfig:
    deepgram_api_key: str
    openai_api_key: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    deepgram_model: str = "nova-3"
    openai_model: str = "gpt-4o-mini"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "coral"
    elevenlabs_model: str = "eleven_multilingual_v2"
    kokoro_model_path: str = "models/kokoro-v1.0.onnx"
    kokoro_voices_path: str = "models/voices-v1.0.bin"
    kokoro_voice: str = "ef_dora"

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        return cls(
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice_id=os.environ.get("ELEVENLABS_VOICE_ID", ""),
            deepgram_model=os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            openai_tts_model=os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            openai_tts_voice=os.environ.get("OPENAI_TTS_VOICE", "coral"),
            elevenlabs_model=os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2"),
            kokoro_model_path=os.environ.get("KOKORO_MODEL_PATH", "models/kokoro-v1.0.onnx"),
            kokoro_voices_path=os.environ.get("KOKORO_VOICES_PATH", "models/voices-v1.0.bin"),
            kokoro_voice=os.environ.get("KOKORO_VOICE", "ef_dora"),
        )

    def require_real_mode(self, tts_provider: str) -> None:
        required = {
            "DEEPGRAM_API_KEY": self.deepgram_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
        }
        if tts_provider == "elevenlabs":
            required.update(
                {
                    "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
                    "ELEVENLABS_VOICE_ID": self.elevenlabs_voice_id,
                }
            )
        if tts_provider == "kokoro":
            if not Path(self.kokoro_model_path).exists():
                raise RuntimeError(
                    f"Kokoro model not found at {self.kokoro_model_path}. "
                    "Download it with: python scripts/download_kokoro.py"
                )

        missing = [name for name, value in required.items() if not value]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Real mode is missing required environment values: {joined}")
