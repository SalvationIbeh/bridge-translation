# Decision: How to Cut TTS Latency from 2,800ms to Under 500ms

**Date:** 2026-06-01
**Status:** COMPLETE ✅ — GPU TTS confirmed, target met

---

## Question

Piper TTS is 2,800ms P50 — 90% of total pipeline latency. How do we cut it to under 500ms?

## Context

- Stage breakdown: STT=270ms, Translate=83ms, TTS=2,800ms
- Piper TTS v1.2, ONNX runtime, `TTS_DEVICE=cuda:0` set but unverified
- 20 concurrent sessions, RTX PRO 4500 16GB VRAM
- Roadmap: replace Piper with OpenVoice v2 for voice cloning

## Council Decision

**PROCEED in this exact sequence:**

1. **Option 3 (today):** Verify Piper is actually using GPU. Run `nvidia-smi dmon` during TTS call.
   If flat GPU utilization → add `providers=["CUDAExecutionProvider"]` to ONNX session in `tts_worker.py`.
2. **Option 1 (after GPU confirmed):** Sentence-chunked streaming TTS — first audio chunk within ~300ms.
3. **Option 2 (only if needed):** Swap to Kokoro ONNX if GPU Piper + streaming still exceeds 500ms P50.
   NOT StyleTTS2.
4. **OpenVoice v2:** Blocked until voice data handling policy exists + VRAM budget validated at 20-session load.

## Key Findings

- All 4 advisors unanimous: GPU check first — `TTS_DEVICE=cuda:0` env var is a no-op without
  explicit `providers=["CUDAExecutionProvider"]` in ONNX session options
- GPU fix alone may drop TTS from 2,800ms to under 400ms at zero migration cost
- Streaming TTS reduces perceived latency only, not real synthesis load under concurrency
- Security hard block on OpenVoice v2: voice prints are biometric data (GDPR Art.9, BIPA, CCPA)

## Results (2026-06-01)

| Stage | Before | After | Δ |
|---|---|---|---|
| TTS P50 | 2,800ms | **444ms** | 6.3× faster |
| Total P50 (es→en est.) | ~3,221ms | **~827ms** | 3.9× faster |

Root cause: `onnxruntime` (CPU-only) was installed instead of `onnxruntime-gpu`.
Fix: `pip install onnxruntime-gpu==1.26.0` + `PiperVoice.load(use_cuda=True)`.

Also added: same-language passthrough in `translate_worker.py` (source == target → skip model).

## Next Actions

- [x] SSH into pod, run `nvidia-smi dmon` during TTS call, check GPU utilization
- [x] If CPU-bound: patch `tts_worker.py` to pass `use_cuda=True`
- [x] Re-run 5 sample benchmark, record new P50
- [x] Update PROJECT_STATUS.md with results
- [ ] Bake `onnxruntime-gpu==1.26.0` into Dockerfile (replaces CPU onnxruntime)
- [ ] Upgrade Whisper base → small for accuracy (two transcription errors observed)
