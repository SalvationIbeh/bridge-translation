# Bridge — Real-Time Call Translator for Customer Service Teams

**Document type:** Product Requirements Document (PRD)
**Version:** 0.1 (draft, for engineering handoff)
**Working product name:** Bridge (placeholder — see §13)
**Last updated:** May 17, 2026
**Owner:** Salvation

---

## 1. TL;DR

Bridge is a cross-platform desktop application (macOS + Windows) that performs real-time, simultaneous voice translation between Spanish and English during customer-service phone calls. It installs a virtual audio device that sits between the agent's headset and their softphone (Zendesk Talk, Five9, RingCentral, etc.), translating audio in both directions without requiring any integration on the softphone side or any installation on the caller's side.

**v1 promise to the agent:** "Wear your headset, open your normal softphone, take the call. Hear English from the caller, speak English back — the caller hears Spanish."

---

## 2. Problem

Customer-service teams in the US increasingly handle Spanish-speaking callers, but most agents are monolingual. Bilingual agents become bottlenecks: they get routed every Spanish call while their monolingual peers lose call volume, miss commissions, and become harder to promote. Companies respond by paying a 10–20% wage premium for bilingual hires, accepting longer queues for Spanish-speaking customers, or routing to third-party human interpreters that cost $1–3/minute and add multi-second connection delays.

The trigger insight for this product came from a real case: a US-based CS agent was being sidelined at her job because she couldn't take Spanish calls, despite being a strong performer otherwise. She didn't need to *learn* Spanish — she needed to *handle* Spanish calls.

Existing options don't solve this for a working CS agent:

- **Apple Live Translation / Google Interpreter Mode:** Locked to native phone or FaceTime, not softphones; consecutive only.
- **Human interpreter services (LanguageLine et al.):** Expensive, slow to connect, three-way-call friction.
- **General translation apps (Google Translate, etc.):** Designed for in-person use; can't be routed through a call.

---

## 3. Goals & Non-Goals

### Goals (v1)
- Enable a monolingual English-speaking CS agent to competently handle Spanish-language inbound and outbound calls.
- End-to-end translation latency under 800ms per utterance (p50).
- Zero integration required with the agent's softphone — install Bridge, route audio through it, done.
- Bidirectional simultaneous translation: both parties can talk over each other; each hears the translation in their own language.
- Agent stays in control: toggle translation on/off, switch direction, mute, all mid-call.

### Non-Goals (v1)
- Any language pair beyond English ↔ Spanish.
- Native integrations with CS platforms (Zendesk, Five9, RingCentral, etc.).
- Mobile apps (iOS/Android).
- Cloned/personalized voice for translated output.
- Sub-300ms "true simultaneous" interpretation.
- Translating chat, email, SMS, or any written channel.
- Caller-side app or modification to the caller's experience beyond audio.

---

## 4. Target Users

**Primary persona — "Maria, the monolingual CS agent."**
Inbound queue at a US contact center. Tools: headset, a softphone (browser or desktop), a CRM. Measured on Average Handle Time (AHT), First-Call Resolution (FCR), and CSAT. Wants to take more calls without being penalized for not speaking Spanish.

**Secondary persona — "David, the CS team lead / operations manager."**
The buyer. Cares about per-seat economics, AHT and CSAT deltas, compliance posture (recording, retention), and rollout speed across 50–500 seats.

**Out of audience for v1**
- Consumers making international personal calls.
- Field agents on mobile.
- Professional human interpreters.

---

## 5. Success Metrics

### Product metrics (per agent, per call)
- Median end-to-end latency: ≤ 800ms; p95 ≤ 1200ms.
- Human-rated translation accuracy on a held-out CS-call eval set: ≥ 90%.
- Translation uptime during active call: ≥ 99.5%.

### Business metrics (per customer)
- AHT on Spanish calls vs. human-interpreter baseline: target −30% or better.
- CSAT on translated calls: within 5 points of same-language baseline.
- Agent utilization on the Spanish queue: ≥ 2× within 30 days of rollout.

### Activation metrics
- Time from installer download to first translated call: < 15 minutes.
- Pilot-to-paid conversion: ≥ 25%.

---

## 6. Core User Stories

1. **Install & onboard.** As an agent, I download Bridge, run the installer, grant the audio-driver permission once, run a 30-second mic/speaker test, and Bridge is ready before my next call.
2. **Pick up a translated call.** My softphone rings. I answer normally. I hear the caller's Spanish translated to English in my headset. I reply in English; the caller hears Spanish in their handset.
3. **Mid-call control.** From a small always-on-top window I can mute, pause translation, swap direction, or escalate to a human interpreter.
4. **Post-call summary.** Optionally, I see a one-screen summary in English with key terms and action items. Opt-in only, no audio retained.
5. **Admin oversight.** As an ops manager, I see a dashboard with aggregate latency, low-confidence flags, and per-seat usage. No transcripts by default.

---

## 7. Functional Requirements

### 7.1 Audio routing
- Bridge installs a virtual audio device (input + output) at the OS level.
- The agent sets the virtual device as their softphone's microphone and speaker.
- Bridge taps both streams: inbound (caller → softphone → Bridge input) and outbound (agent mic → Bridge → softphone output).
- Translated audio is injected into the appropriate stream so the agent hears English and the caller hears Spanish.
- The agent's headset is the *physical* I/O; the virtual device is the *logical* path the softphone sees.

### 7.2 Translation pipeline
- Continuous streaming STT on both audio streams concurrently.
- Voice Activity Detection (VAD) per stream.
- Partial-result translation: begin translating before the speaker finishes the sentence; finalize on detected sentence boundary.
- TTS playback synchronized to the appropriate stream with jitter buffering.

### 7.3 Control surface
- Always-on-top mini-window (~250×80px) with: translation toggle, direction swap, mute, status indicator (latency, connection health), and an emergency "escalate to human interpreter" button (link out for v1; integrated dialer for v2).
- System tray / menu bar icon and global hotkeys for the same controls.

### 7.4 Settings
- Default language pair (en ↔ es; future pairs disabled in v1 UI).
- TTS voice selection (2–3 neutral voices per language).
- Audio device picker (for agents who switch headsets).
- Network diagnostics screen.

### 7.5 Telemetry & logging
- Per-call: latency p50/p95, translation confidence scores, error events.
- No raw audio retained.
- Transcripts are opt-in only, encrypted at rest, with a default 30-day retention.

---

## 8. Technical Architecture

### High-level data flow

```
Caller (Spanish) ──► Softphone ──► Bridge virtual mic ──► STT(es) ──► Translate(es→en) ──► TTS(en) ──► Agent headphones
Agent (English)  ──► Headset mic ──► Bridge virtual speaker ──► STT(en) ──► Translate(en→es) ──► TTS(es) ──► Softphone ──► Caller
```

Both directions run concurrently and independently. The headset isolates the agent's mic from their speakers, so simultaneous bidirectional translation does not collide acoustically.

### Components

1. **Audio driver (native).** Virtual audio device. macOS: CoreAudio HAL plugin (user-space; BlackHole-style). Windows: WASAPI virtual cable driver.
2. **Audio engine (Rust core).** Low-level capture, VAD, jitter buffer, sample-rate conversion, echo control.
3. **Pipeline orchestrator (Rust).** Manages STT / translate / TTS streams concurrently per direction; handles partial vs. final results and stream lifecycle.
4. **Cloud bridge.** WebSocket connections to provider APIs (one per service per direction at minimum). Authenticated with short-lived tokens issued by the Bridge backend.
5. **Desktop UI (Tauri).** Mini control window, settings, onboarding flows. Renders the React frontend in a system webview; calls into the Rust core.
6. **Backend (Python + FastAPI).** Auth, per-seat licensing, usage metering, key rotation for provider APIs, admin dashboard.

### Latency budget (target end-to-end ≤ 800ms p50)

| Stage                            | Budget   |
|----------------------------------|----------|
| Audio capture + VAD              | ≤ 100ms  |
| Streaming STT first partial      | ≤ 250ms  |
| Translation (streaming)          | ≤ 200ms  |
| TTS first audio chunk            | ≤ 200ms  |
| Network + jitter buffer overhead | ≤ 50ms   |

Engineering should treat these as initial targets; the first-week spike (§13) will validate or revise them under real network conditions.

---

## 9. Tech Stack (v1 recommendations)

These are starting recommendations. Engineering should validate during a one-week spike before committing.

- **STT:** Deepgram Streaming (Nova-2 or newer) — low latency, strong Spanish, streaming partials. Fallback: AssemblyAI Universal-Streaming.
- **Translation:** GPT-4o-mini for context-aware translation, with DeepL as fallback for cost optimization on short utterances.
- **TTS:** ElevenLabs Streaming (Flash v2.5) for naturalness; Azure Neural TTS as a cheaper fallback.
- **Desktop shell:** Tauri (smaller binary, Rust core) preferred over Electron.
- **Audio core:** Rust with cpal where viable; native CoreAudio / WASAPI where lower-level control is needed. Hot path stays out of JS.
- **Backend:** Python + FastAPI. Postgres for accounts and usage. Redis for session state.
- **Deployment:** Provider APIs run in their own clouds; Bridge backend on a single region (us-east) for v1.

---

## 10. Non-Functional Requirements

### Performance
- p50 end-to-end latency ≤ 800ms; p95 ≤ 1200ms.
- Bridge desktop CPU usage ≤ 15% on a 2020-era Mac/Windows laptop during an active call.
- Memory footprint ≤ 400MB.

### Reliability
- Graceful degradation if any provider fails: switch to fallback within 2 seconds, surface a warning indicator, never drop the call.
- Auto-reconnect on network blips under 5 seconds without manual intervention.

### Security & privacy
- All audio streams encrypted in transit (TLS 1.3, WSS).
- No call audio stored by default. Opt-in transcripts are encrypted at rest (AES-256).
- Provider API keys never shipped to the client; backend brokers short-lived tokens.
- SOC 2 Type I within 12 months of launch. HIPAA / PCI modes are explicit v3 work.

### Compliance
- Two-party-consent prompt for call recording, jurisdiction-aware.
- GDPR-ready: data export, right to deletion, EU data residency option (post-v1).
- Audit log of all admin actions (settings changes, user provisioning).

### Accessibility
- Mini-window fully keyboard-navigable.
- WCAG 2.1 AA target for all UI.

---

## 11. Out of Scope (v1)

Stated explicitly so it doesn't drift in:
- Any language pair beyond English ↔ Spanish.
- Voice cloning of the speaker.
- Native softphone integrations (Zendesk, Five9, RingCentral, etc.).
- Mobile apps.
- Anything beyond the opt-in post-call summary.
- Real-time translation of chat / SMS / email.
- Sentiment, coaching, or QA features.
- Multi-party (conference call) translation.
- Mid-call language auto-switching.

---

## 12. Roadmap

### v1 (months 0–6) — MVP
- macOS + Windows desktop apps.
- English ↔ Spanish, generic TTS voices.
- Virtual audio device, no softphone integrations.
- Self-serve install + per-seat licensing.
- 5–10 design-partner CS teams as paid pilots.

### v2 (months 6–12) — Expand
- Add French, Portuguese, Mandarin (English-pivot translation initially; direct pairs as performance allows).
- Voice cloning option for outbound translated voice.
- First native integration — Zendesk Talk or Five9, decided by pilot-customer demand.
- Admin dashboard with team-level analytics.

### v3 (months 12–24) — Deepen
- Multi-party / conference-call support.
- HIPAA and PCI compliance modes.
- AI call summaries, action-item extraction.
- Coaching mode (real-time agent prompts based on call context).
- Mobile companion for field agents.

---

## 13. Open Questions

1. **Product name.** "Bridge" is a placeholder. Trademark + domain availability check needed before any marketing work.
2. **Pricing.** Working range $40–80/seat/month based on CS-tool benchmarks. Needs validation with 3–5 ops managers during discovery.
3. **First design partner.** Origin-story company (wife's employer) is candidate #1. Need 4–6 more to avoid a sample-size-of-one bias.
4. **Voice-cloning sequencing.** Some pilot customers may demand cloned voice in v1 for brand consistency. Revisit after the first 10 demo calls.
5. **Real-world latency.** 800ms is a target; needs measurement on residential US broadband plus typical agent VPN setups before locking.
6. **STT accuracy on accented Spanish.** Mexican, Cuban, Argentine, Castilian — Deepgram performance varies. Build a CS-specific eval set during week one.
7. **Driver install UX on macOS.** Kernel-extension permissions are friction; user-space CoreAudio HAL plugins are preferred but need validation on Apple Silicon and recent macOS versions.
8. **What does the caller hear during translation latency?** Silence feels awkward; a "translating…" tone is intrusive. Needs prototyping during week-one spike.
9. **Crosstalk handling.** When agent and caller speak at the same time, which side gets translated first, and what does the other side hear during that window?

---

## 14. Glossary

- **STT** — Speech-to-Text.
- **TTS** — Text-to-Speech.
- **VAD** — Voice Activity Detection.
- **Softphone** — Software application that turns a computer into a telephone (Zendesk Talk, Five9, RingCentral, etc.).
- **Virtual audio device** — Software-defined audio input/output that other applications can route audio through, used here to intercept the softphone's audio streams.
- **AHT** — Average Handle Time; a core CS KPI.
- **FCR** — First-Call Resolution; a core CS KPI.
- **CSAT** — Customer Satisfaction score.
- **Two-party consent** — Legal requirement in some US states (e.g., California) that both parties consent to a call being recorded.
