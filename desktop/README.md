# Vocivio Desktop Client

The desktop app CS agents run alongside their softphone. It captures call audio, streams it to
the GPU translation server (`/ws/translate`), and plays back the translated speech.

## Build sequence (committed stack: **Tauri + Rust** — not disposable)

- **P1a — Rust core loop (this scaffold).** A headless binary (`vocivio-probe`) that does the
  real, hard part: mic capture → 16 kHz PCM frames → WebSocket → server → playback. Runnable
  with `cargo run`, fast feedback, no UI complexity yet. **This crate is permanent** — the Tauri
  UI drives it; nothing here is thrown away.
- **P1b — Tauri UI wrapper (scaffolded).** `src-tauri/` (Tauri app) + `ui/` (web UI) drive
  this core via Tauri commands (`connect`/`disconnect`/`set_redaction`) and events
  (`status`, `transcript`). Audio runs on a dedicated thread (cpal `Stream` is `!Send`).
- **P2 — Softphone audio routing / virtual device** (capture far-end audio, inject translated
  audio). The #1 product risk — why we're in Rust.
- **P3 — Full UX:** reconnect, redaction toggle, consent flow, polish → pilot-ready.

## Run the P1a core probe
```bash
cd desktop
cargo run --bin vocivio-probe -- \
  --server ws://<pod-host>:8000/ws/translate \
  --token  "$(python ../scripts/mint_token.py)" \
  --source es --target en
```
Speak into the mic; translated audio plays back and transcripts print to the console.
Ctrl-C sends `end_of_stream` and exits.

## Protocol mapping (mirrors `docs/STAGE2_DESIGN.md` §5.1)
- Connect WSS → send `{type:"init", auth, source_lang, target_lang}` → await `{type:"ready"}`.
- Send mic audio as **binary** 16 kHz mono 16-bit PCM, 20 ms / 320-sample frames.
- Receive `{type:"transcript", original, translated, latency_ms, stage_ms}` (text) and TTS
  audio (binary).
- `end_of_stream`, `ping`/`pong`, `redact_start`/`redact_end` per protocol.

## ⚠️ Status / known TODOs (scaffold — not yet built or device-tested)
- **Not compiled/run yet** — written against the documented protocol; expect to iterate with
  `cargo build` on a real machine.
- **`TTS_SAMPLE_RATE` in `src/audio.rs` is an assumption (22 050 Hz).** Confirm the server's
  Piper output rate/encoding and set it to match, or playback pitch will be wrong.
- Resampling is a naive linear pass (fine for a voice P1; swap for `rubato` if quality needs it).
- Auth: dev uses a minted HMAC token (`scripts/mint_token.py`); WSS/TLS for real audio (§5.2).
- Only default input/output devices; device selection + hot-swap is P2/P3.
