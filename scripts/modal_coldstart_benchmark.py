"""
Modal Cold-Start Benchmark — Bridge GPU Pipeline
=================================================
Deploys the full Bridge inference stack (Whisper small + Opus-MT + Piper TTS)
to Modal's T4 GPU and measures:

  1. Cold-start wall time     — what a user actually waits for on first request
  2. Model load time          — Whisper + Opus-MT + Piper disk→VRAM (inside container)
  3. Infra overhead           — container spin-up + Python + CUDA init (derived)
  4. Warm per-stage latency   — STT / MT / TTS across all 5 CS clips

All models are pre-baked into the Modal image, so cold-start = load from disk to
VRAM, NOT a network download. This gives the best-case cold-start number.

GPU: T4 ($0.59/hr) — target production cost tier.
     Change gpu="T4" to "A10" below to compare a higher tier.

Usage:
    modal run scripts/modal_coldstart_benchmark.py

Output: runs/modal-coldstart-<timestamp>.json
"""
import io
import json
import statistics
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import modal

# ── Modal image ────────────────────────────────────────────────────────────────
# Mirrors server/Dockerfile, adapted for Modal's image builder.
# Key constraint: no GPU during image build → download model weights on CPU,
# load to VRAM at container start via @modal.enter().
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04",
        add_python="3.11",
    )
    .apt_install("ffmpeg", "libsndfile1", "libcudnn8")  # libcudnn8 needed by ctranslate2 4.x (built against cuDNN 8)
    .pip_install(
        "faster-whisper==1.0.3",
        "ctranslate2==4.3.1",
        "transformers==4.41.0",
        "sentencepiece==0.2.0",
        "sacremoses==0.1.1",
        "torch",
        "torchaudio",
        "soundfile==0.12.1",
        "numpy==1.26.4",
    )
    # piper-tts bundles CPU onnxruntime; uninstall it, replace with GPU version
    .run_commands(
        "pip install piper-tts==1.2.0"
        " && pip uninstall -y onnxruntime"
        " && pip install onnxruntime-gpu==1.26.0"
    )
    # Pre-bake model weights — no network download at cold-start
    .run_commands(
        "python -c \""
        "import torch; "
        "torch.hub.load('snakers4/silero-vad', 'silero_vad', force_reload=True, trust_repo=True)"
        "\""
    )
    .run_commands(
        # CPU + int8 just to trigger the weight download; CUDA + float16 used at runtime
        "python -c \""
        "from faster_whisper import WhisperModel; "
        "WhisperModel('small', device='cpu', compute_type='int8')"
        "\""
    )
    .run_commands(
        "python -c \""
        "from transformers import MarianMTModel, MarianTokenizer; "
        "MarianTokenizer.from_pretrained('Helsinki-NLP/opus-mt-es-en'); "
        "MarianMTModel.from_pretrained('Helsinki-NLP/opus-mt-es-en')"
        "\""
    )
    .run_commands(
        "mkdir -p /models/tts && python -c \""
        "import urllib.request; "
        "base = 'https://huggingface.co/rhasspy/piper-voices/resolve/main"
        "/en/en_US/lessac/medium'; "
        "urllib.request.urlretrieve("
        "  base + '/en_US-lessac-medium.onnx',"
        "  '/models/tts/en_US-lessac-medium.onnx'); "
        "urllib.request.urlretrieve("
        "  base + '/en_US-lessac-medium.onnx.json',"
        "  '/models/tts/en_US-lessac-medium.onnx.json')"
        "\""
    )
)

app = modal.App("bridge-coldstart-benchmark", image=image)


# ── Remote pipeline class ──────────────────────────────────────────────────────

@app.cls(
    gpu="T4",                   # change to "A10" to test a higher tier
    scaledown_window=15,  # container dies 15s after last request → enables repeated cold tests
)
class BridgePipeline:

    @modal.enter()
    def load_models(self) -> None:
        """
        Runs once per container start. Times each model's disk→VRAM load.
        These timings are returned with every inference call so cold vs warm
        can be distinguished from outside.
        """
        import torch
        from faster_whisper import WhisperModel
        from piper import PiperVoice
        from transformers import MarianMTModel, MarianTokenizer

        t0 = time.perf_counter()

        t = time.perf_counter()
        self.vad_model, _ = torch.hub.load(
            "snakers4/silero-vad", "silero_vad", trust_repo=True
        )
        self.vad_model = self.vad_model.to("cuda")
        self._t_vad_ms = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        self.whisper = WhisperModel(
            "small", device="cuda", device_index=0, compute_type="float16"
        )
        self._t_stt_ms = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        self.tokenizer = MarianTokenizer.from_pretrained("Helsinki-NLP/opus-mt-es-en")
        self.mt_model = (
            MarianMTModel.from_pretrained("Helsinki-NLP/opus-mt-es-en")
            .to("cuda")
            .eval()
        )
        self._t_mt_ms = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        self.tts_voice = PiperVoice.load(
            "/models/tts/en_US-lessac-medium.onnx", use_cuda=True
        )
        self._t_tts_ms = (time.perf_counter() - t) * 1000

        self._total_load_ms = (time.perf_counter() - t0) * 1000
        print(
            f"[load] vad={self._t_vad_ms:.0f}ms  stt={self._t_stt_ms:.0f}ms  "
            f"mt={self._t_mt_ms:.0f}ms  tts={self._t_tts_ms:.0f}ms  "
            f"total={self._total_load_ms:.0f}ms"
        )

    @modal.method()
    def run_inference(self, pcm_bytes: bytes, sample_rate: int = 16_000) -> dict:
        """
        Full pipeline: raw PCM (16-bit mono) → Spanish transcript → English → WAV.
        Returns per-stage timings and model-load snapshot from this container.
        """
        import io
        import wave

        import numpy as np
        import soundfile as sf
        import torch

        # Wrap raw PCM in a WAV container — mirrors server/pipeline/stt_worker.py
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        wav_buf.seek(0)
        audio_np, _ = sf.read(wav_buf, dtype="float32")

        # STT
        t = time.perf_counter()
        segments, _ = self.whisper.transcribe(
            audio_np, language="es", beam_size=5, vad_filter=False
        )
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        stt_ms = (time.perf_counter() - t) * 1000

        if not transcript:
            return {"error": "empty transcript"}

        # MT
        t = time.perf_counter()
        inputs = self.tokenizer(
            [transcript], return_tensors="pt", padding=True
        ).to("cuda")
        with torch.no_grad():
            out = self.mt_model.generate(**inputs)
        translation = self.tokenizer.decode(out[0], skip_special_tokens=True)
        mt_ms = (time.perf_counter() - t) * 1000

        # TTS
        t = time.perf_counter()
        tts_buf = io.BytesIO()
        with wave.open(tts_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.tts_voice.config.sample_rate)
            self.tts_voice.synthesize(translation, wf)
        tts_ms = (time.perf_counter() - t) * 1000

        return {
            "transcript":   transcript,
            "translation":  translation,
            "stt_ms":       round(stt_ms, 1),
            "mt_ms":        round(mt_ms, 1),
            "tts_ms":       round(tts_ms, 1),
            "inference_ms": round(stt_ms + mt_ms + tts_ms, 1),
            "model_load_ms": {
                "vad":   round(self._t_vad_ms, 1),
                "stt":   round(self._t_stt_ms, 1),
                "mt":    round(self._t_mt_ms, 1),
                "tts":   round(self._t_tts_ms, 1),
                "total": round(self._total_load_ms, 1),
            },
        }


# ── Local entrypoint ───────────────────────────────────────────────────────────

@app.local_entrypoint()
def main() -> None:
    """
    Benchmark sequence
    ------------------
    1. Cold run #1  — container spun fresh, captures full cold-start wall time
    2. Warm runs    — 3 runs × 5 clips, container already hot
    3. Sleep 20s    — outlasts the 15s idle timeout; container dies
    4. Cold run #2  — second cold measurement for consistency check
    5. Summary + JSON written to runs/
    """
    PCM_DIR = Path("samples/realtime-pcm")
    RUNS_DIR = Path("runs")
    RUNS_DIR.mkdir(exist_ok=True)

    pcm_files = sorted(PCM_DIR.glob("*.pcm"))
    if not pcm_files:
        raise SystemExit("No .pcm files found in samples/realtime-pcm/")

    # card_block: cleanest sample (0% WER on both models) — ideal cold-start probe
    cold_probe = PCM_DIR / "card_block.pcm"
    if not cold_probe.exists():
        cold_probe = pcm_files[0]
    probe_bytes = cold_probe.read_bytes()

    pipeline = BridgePipeline()
    cold_runs: list[dict] = []
    warm_clips: list[dict] = []

    # ── 1. Cold run #1 ──────────────────────────────────────────────────────
    print(f"\nBridge Modal Cold-Start Benchmark — T4 GPU")
    print(f"Probe sample : {cold_probe.name}")
    print("=" * 62)
    print("\n[1/4] Cold run #1  (container starting — this takes a moment...)")

    t0 = time.perf_counter()
    r = pipeline.run_inference.remote(probe_bytes)
    cold_wall_ms = (time.perf_counter() - t0) * 1000

    if "error" in r:
        print(f"  ERROR: {r['error']}")
    else:
        load = r["model_load_ms"]
        infra_ms = cold_wall_ms - load["total"] - r["inference_ms"]
        cold_runs.append({
            "run": 1,
            "cold_wall_ms": round(cold_wall_ms, 1),
            "inference_ms": r["inference_ms"],
            "model_load_ms": load,
            "infra_overhead_ms": round(infra_ms, 1),
            "transcript": r.get("transcript"),
            "translation": r.get("translation"),
        })
        print(f"  cold wall       : {cold_wall_ms:>7.0f} ms")
        print(
            f"  model load      : {load['total']:>7.0f} ms"
            f"  (vad={load['vad']:.0f}  stt={load['stt']:.0f}"
            f"  mt={load['mt']:.0f}  tts={load['tts']:.0f})"
        )
        print(f"  inference       : {r['inference_ms']:>7.0f} ms"
              f"  (stt={r['stt_ms']:.0f}  mt={r['mt_ms']:.0f}  tts={r['tts_ms']:.0f})")
        print(f"  infra overhead  : {infra_ms:>7.0f} ms  (container spin-up + Python + CUDA init)")

    # ── 2. Warm runs ─────────────────────────────────────────────────────────
    print(f"\n[2/4] Warm inference — {len(pcm_files)} clips × 3 runs")
    print(f"  {'clip':<24}  stt    mt     tts    total  wall")
    print(f"  {'-'*24}  {'---':>5}  {'--':>5}  {'---':>5}  {'-----':>5}  {'----':>5}")

    for pcm_path in pcm_files:
        pcm_bytes = pcm_path.read_bytes()
        clip_id = pcm_path.stem
        runs: list[dict] = []

        for _ in range(3):
            t0 = time.perf_counter()
            r = pipeline.run_inference.remote(pcm_bytes)
            wall_ms = (time.perf_counter() - t0) * 1000
            if "error" not in r:
                runs.append({
                    "stt_ms":       r["stt_ms"],
                    "mt_ms":        r["mt_ms"],
                    "tts_ms":       r["tts_ms"],
                    "inference_ms": r["inference_ms"],
                    "wall_ms":      round(wall_ms, 1),
                    "transcript":   r.get("transcript"),
                    "translation":  r.get("translation"),
                })

        warm_clips.append({"clip_id": clip_id, "runs": runs})

        if runs:
            med = sorted(runs, key=lambda x: x["inference_ms"])[len(runs) // 2]
            print(
                f"  {clip_id:<24}  "
                f"{med['stt_ms']:>4.0f}ms "
                f"{med['mt_ms']:>4.0f}ms  "
                f"{med['tts_ms']:>4.0f}ms  "
                f"{med['inference_ms']:>5.0f}ms "
                f"{med['wall_ms']:>5.0f}ms"
            )

    # ── 3. Let container die ─────────────────────────────────────────────────
    print(f"\n[3/4] Waiting 20s for container to hit idle timeout...")
    time.sleep(20)

    # ── 4. Cold run #2 ───────────────────────────────────────────────────────
    print("[4/4] Cold run #2  (new container)...")

    t0 = time.perf_counter()
    r = pipeline.run_inference.remote(probe_bytes)
    cold_wall_ms = (time.perf_counter() - t0) * 1000

    if "error" in r:
        print(f"  ERROR: {r['error']}")
    else:
        load = r["model_load_ms"]
        infra_ms = cold_wall_ms - load["total"] - r["inference_ms"]
        cold_runs.append({
            "run": 2,
            "cold_wall_ms": round(cold_wall_ms, 1),
            "inference_ms": r["inference_ms"],
            "model_load_ms": load,
            "infra_overhead_ms": round(infra_ms, 1),
        })
        print(f"  cold wall       : {cold_wall_ms:>7.0f} ms")
        print(f"  model load      : {load['total']:>7.0f} ms")
        print(f"  infra overhead  : {infra_ms:>7.0f} ms")

    # ── Summary ──────────────────────────────────────────────────────────────
    all_inference = [r["inference_ms"] for clip in warm_clips for r in clip["runs"]]
    all_wall      = [r["wall_ms"]      for clip in warm_clips for r in clip["runs"]]

    def pct(values: list[float], p: int) -> float:
        s = sorted(values)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    avg_cold  = statistics.mean(c["cold_wall_ms"]          for c in cold_runs) if cold_runs else None
    avg_load  = statistics.mean(c["model_load_ms"]["total"] for c in cold_runs) if cold_runs else None
    avg_infra = statistics.mean(c["infra_overhead_ms"]      for c in cold_runs) if cold_runs else None

    print("\n" + "=" * 62)
    print("SUMMARY")
    print(f"\n  Cold-start (avg of {len(cold_runs)} runs)")
    print(f"    total wall time   : {avg_cold:>7.0f} ms") if avg_cold else None
    print(f"    model load        : {avg_load:>7.0f} ms") if avg_load else None
    print(f"    infra overhead    : {avg_infra:>7.0f} ms  ← can't be optimised, Modal constant") if avg_infra else None

    if all_inference:
        print(f"\n  Warm inference (P50 / P90 across all clips)")
        print(f"    inference P50 : {pct(all_inference, 50):>7.0f} ms")
        print(f"    inference P90 : {pct(all_inference, 90):>7.0f} ms")
        print(f"    wall P50      : {pct(all_wall, 50):>7.0f} ms  (inference + Modal RPC)")
        print(f"    wall P90      : {pct(all_wall, 90):>7.0f} ms")

    # ── Write JSON ────────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {
        "benchmark":  "modal_coldstart",
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "gpu":        "T4",
        "pipeline":   "Whisper-small/float16 + Opus-MT-es-en + Piper-en_US-lessac-medium",
        "cold_runs":  cold_runs,
        "warm_clips": warm_clips,
        "summary": {
            "cold_wall_avg_ms":    round(avg_cold, 1)  if avg_cold  else None,
            "model_load_avg_ms":   round(avg_load, 1)  if avg_load  else None,
            "infra_overhead_avg_ms": round(avg_infra, 1) if avg_infra else None,
            "warm_inference_p50":  round(pct(all_inference, 50), 1) if all_inference else None,
            "warm_inference_p90":  round(pct(all_inference, 90), 1) if all_inference else None,
            "warm_wall_p50":       round(pct(all_wall, 50), 1)      if all_wall      else None,
            "warm_wall_p90":       round(pct(all_wall, 90), 1)      if all_wall      else None,
        },
    }
    out_path = RUNS_DIR / f"modal-coldstart-{ts}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport → {out_path}")
