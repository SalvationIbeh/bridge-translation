# Decision: Stage-2 client Phases B–E — resequenced & trimmed (Council)

**Date:** 2026-06-20
**Status:** DECIDED (Council) — pending founder approval per the plan→council→approve workflow.
**AUTH SUPERSEDED 2026-06-20:** the auth choice below (static per-partner token + pre-GA vend gate)
was re-reviewed by a secondary SME panel and replaced with **server-issued rotating resumption
tokens (Option C)** — see `docs/decisions/2026-06-20-ws-auth-resumption-token.md`. The rest of this
B–E decision stands.
**Context:** Stage-2 desktop client v1, pilot-ready on an off-the-shelf virtual cable
(`.claude/plans/` Stage 2 Client v1 plan; routing decision and Phase A already done).

## Question

Is the planned scope/sequence for Phases B (reliability: reconnect + fail-open), C (routing +
device picker), D (redaction enforcement + minimal trust UX), and E (AEC + packaging) right for a
first pilot-ready v1?

## Council Report

**🔴 Skeptic — Condition.** Fail-open passthrough is a silent-failure machine — a monolingual agent
can't tell the call is dead; needs an unmissable signal + defined recovery, and re-introduces the
echo loop on speakers. Static `AUTH_TOKEN` is permanent debt; reconnect/single-use friction shows
connection-auth and session-auth are conflated → vend endpoint now. Conditional AEC punts a known
requirement on an unrun measurement. Redaction-at-source belongs earlier (active trust violation).

**🟢 Product — Condition.** B and D are the only true pilot blockers; C partial; E mostly
deferrable. The redaction lie is a credibility landmine — fix before any live call. Fail-open is the
top adoption item but the agent must know it degraded (B and D coupled). "Headset required, AEC
later" is fine for a chaperoned pilot — biggest time save. Trust UX right-sized; keep captions. Use
the static token.

**🔵 Security — Condition.** Static token is the trap (no per-agent revoke/expiry,
leak-once-compromised-everywhere); gate real-customer audio on a vend endpoint. Fail-open fine for
availability but assert zero-persistence holds in the degraded path + surface "UNTRANSLATED".
Client-side redaction frame-drop mandatory (PCI §8.4); watch toggle-edge races + buffered lead-in;
fail-closed on ambiguity. Keychain correct (Windows DPAPI). CSP null is a real hole — fix before
pilot.

**🟡 Optimizer — Support, conditioned.** Over-built. Cut the app heartbeat (transport ping/pong
already exists). Reconnect = dumb retry-with-backoff loop, re-read token each attempt. Static token,
full stop — vend endpoint is tax; not a dead-end if the later swap changes only the token source;
keep in keychain. Fail-open gotcha: copy input→output only works if sample rates + channels match —
else resample-or-mute. Mandate headsets, write zero AEC code. Device picker = two dropdowns, no
hot-swap.

### Synthesis
Unanimous: (1) fail-open must surface an unmissable degraded state + resample-or-mute + preserve
zero-persistence; (2) real redaction-at-source is the true gate, move it earliest; (3) mandate
headsets and cut AEC code from v1; (4) trim the over-build (no app heartbeat, dumb reconnect loop,
two-dropdown picker, one-page routing guide, strict CSP before pilot). Split on auth: static now
(Product, Optimizer) vs vend endpoint now (Skeptic, Security).

### Dissent (preserved)
- **Auth (2–2):** resolved to **static per-partner token, keychain-stored, WSS-only, with a hard
  pre-GA gate** — vend endpoint required before partner #2 / production; swap kept to "token source
  only." Honors speed + not-a-dead-end while binding "must not ship to production."
- **Skeptic** wants the pod AEC/echo measurement before locking scope; majority: headset mandate
  removes the dependency → measurement is informational/post-pilot.

## Decision

**PROCEED with B–E, resequenced and trimmed.**

**Revised build order (value/effort):**
1. **Redaction-at-source** (was D, moved first): `Arc<AtomicBool>`-gated mic pump; drop at capture;
   flush any in-flight ring buffer on enable; fail-closed on ambiguous state; toggle + hotkey.
2. **Reconnect + fail-open** (B, simplified): dumb `loop { connect; pump; on-error backoff }`
   (0.5→8s cap), **no app-level heartbeat** (transport ping/pong suffices); re-read the token each
   attempt; slower retry cadence on `NOT_READY`/503. Fail-open passthrough conditions: **format
   guard** (resample-or-mute if input/output rate or channels mismatch — never chipmunk), an
   **unmissable visible "UNTRANSLATED" degraded state**, a defined recovery handshake (clean source
   switch, no double-play/clip), and a **code-level assertion that zero-persistence holds in the
   passthrough path** (no debug buffer / recording).
3. **Minimal trust UX** (coupled with #2): status pill
   (connecting / ready / degraded-passthrough / reconnecting / error), kill switch (force
   passthrough + stop sending), **keep captions** (agent self-verification).
4. **Device picker** (C, minimal): two dropdowns from cpal enumeration; pick once; restart stream
   on change; device-disappearance → treat as disconnect → fail-open path. **No hot-swap.**
   One-page routing setup note (not a polished guide).
5. **Packaging + auth storage** (E minus AEC): **static per-partner token in the OS keychain**
   (`keyring`); strict **CSP** (currently null) + authored capabilities allowlist; Tauri bundle
   (unsigned/self-signed OK for pilot); settings persistence (URL/langs/devices). **Hard pre-GA
   gate: the vend endpoint replaces the static token before partner #2 / production — static must
   not ship to real multi-tenant use.**
6. **AEC — CUT from v1.** Mandate headsets (one-line onboarding requirement); ship an
   echo-detected warning pill; write zero AEC code. Run the pod VAD/echo measurement after the
   pilot (informational).

## Consequences

- D's redaction fix is now the first thing built, not the last — closes an active trust/PCI gap
  before any live audio.
- Fail-open gains three binding safety conditions (visible state, format guard, zero-persistence
  assert) it didn't have in the original plan.
- AEC code is removed from v1 scope entirely; the dependency on the (unrun, pod-stopped)
  measurement is removed by mandating headsets.
- Auth ships pragmatically static for the single-partner pilot but cannot silently become the
  production posture — the vend endpoint is a written pre-GA gate.
