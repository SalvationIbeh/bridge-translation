# Decision: WS auth — server-issued rotating resumption tokens (SME two-wave review)

**Date:** 2026-06-20
**Status:** DECIDED (secondary SME council) — pending founder approval.
**Supersedes:** the auth choice in `docs/decisions/2026-06-20-stage2-client-b-e.md` (which the
original council split 2–2 and resolved as "static per-partner token + pre-GA vend gate").
**Context:** Stage-2 client Phase B2 (reconnect). Founder requested a deeper review of the 2–2 auth
split via a secondary SME panel + appellate adjudication.

## Question

How should the Vocivio desktop client authenticate to `/ws/translate` for its first design-partner
pilot, given that signed tokens are single-use per connection (server nonce cache, TTL ≤300s) and
the client can't self-mint — so reconnect needs a fresh token?

## Process

Two waves. **Wave 1 — SME panel (independent):** real-time/WS auth architect; identity/token
security (OWASP ASVS 5.0 / Top 10:2025 A02/A07/A09, ASI03); PCI/compliance-for-pilots; pragmatic
pilot-shipping CISO. **Wave 2 — appellate pair:** senior security architect (rigor) + founder-CTO
(opportunity cost). Grounded in the `owasp-security` skill.

## Options considered

- **(A) Conditioned static-now:** per-partner static token (keychain, reused on reconnect) + enforced
  controls + a written pre-GA vend gate. (Original council's resolution.)
- **(B) Vend-now:** build a token-vend endpoint (short-lived per-agent tokens) before the first call.
- **(C) Server-issued resumption token (surfaced by the WS-auth SME):** on `init` success the server
  mints a single-use HMAC token (TTL ≤300s) and pushes it in the `ready` frame; the client caches it
  (keychain) and re-presents it on reconnect; the server validates + rotates. No new service, reuses
  the existing HMAC signer, same `auth` field, per-connection attribution, blast radius ≤300s.

## Findings

- **WS-auth architect → C.** Single-use + reconnect is a *self-inflicted* conflict; C resolves it at
  ≈ the effort of wiring the static path, and is a clean stepping-stone to (B) — not a dead-end.
- **Identity/ASVS, Compliance, Pragmatic-CISO → "conditioned static-now,"** but all three insisted
  the pre-GA gate be **executable, not prose** (server refuses static when `ENV=prod`, CI check,
  dated tripwire; delete the legacy generic `AUTH_TOKEN`), plus per-partner uniqueness, ≥128-bit
  entropy, keychain, WSS-only, rate-limit + audit log, kill-switch. All noted the real pilot blockers
  are data-handling (DPA/consent/retention), not token minting.
- **Both appellate adjudicators → C, decisively.** Once A's conditions are honestly priced, C costs
  about the same but (1) makes the insecure mode *structurally absent* rather than policed; (2) gives
  attribution + bounded blast radius natively (the very things A's conditions simulate); (3) kills the
  second migration; (4) frees scarce founder hours for the actual critical path (DPA/consent). B is
  premature for n=1.

## Decision

**Adopt Option C — server-issued rotating resumption tokens.**

1. **First connect:** client presents a per-partner **bootstrap** credential (keychain, revocable
   per-partner). This is the only standing per-partner secret, used once per session to bootstrap.
2. **On `init` success:** server mints a single-use HMAC resumption token (TTL ≤300s) via the
   existing `auth.mint_token` and adds it to the `ready` frame (additive field).
3. **Reconnect:** client re-presents the cached token in the `init` `auth` field; server validates
   via the existing nonce path, then rotates (mints + pushes the next token in the new `ready`).
4. **Non-negotiable controls:** WSS/TLS only; expiry on **server time only** (±30s skew); single-use
   nonce enforced; rate-limit + lockout on reconnect/auth-fail; per-connection audit log; one-command
   kill-switch/rotation; **delete the legacy generic `AUTH_TOKEN` path from the prod build** (a server
   flag that refuses to boot static-mode when `ENV=prod`, + a CI test asserting it).
5. **Pre-GA gate (retained):** a token-vend endpoint issuing per-agent bootstrap credentials replaces
   the manual per-partner bootstrap before partner #2 / production. With C, this is "move the mint to
   a dedicated endpoint," not a redesign.
6. **Non-auth pilot blockers (the real critical path):** signed pilot DPA + two-party consent +
   retention/revocation runbook before any real-customer-audio call; one-page partner security brief.

## Consequences

- Phase B2 gains a small **server** change (mint-on-init + push-in-`ready` + validate-rotate-on-
  reconnect; delete legacy static path) on top of the client cache/re-present — more than static, far
  less than a vend service, nothing thrown away.
- The `ready` frame gets one additive field (`resumption_token`); covered by this review, no separate
  protocol council needed.
- Attribution + bounded blast radius are now structural, so the heavy static-token control list (A)
  is no longer needed.
