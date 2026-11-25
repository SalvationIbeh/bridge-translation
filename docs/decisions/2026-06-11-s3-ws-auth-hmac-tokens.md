# Council Review: S3 WS-Endpoint Auth — Token-Broker vs HMAC Signed Tokens vs Static Token

**Date:** 2026-06-11

## Question

For the Stage 2 pilot (founder + wife consented calls only, single RunPod pod behind RunPod's
TLS-terminating proxy), what replaces the static `AUTH_TOKEN` on `/ws/translate`:
(A) keep the static token, (B) stateless short-lived HMAC-signed tokens with a local mint
script and no new service, or (C) the full TRD v0.2 §8 token-broker service (OIDC, Redis,
5-min JWTs)? Per-connection frame-size cap + rate limits are in scope for the same change.

---

## Advisor Views

**🔴 Skeptic**

**Core assessment:** Option B is the right-sized choice, but the team is treating "auth token type" as the interesting problem when the actual security wins for this pilot are the rate/frame caps and proving the static token isn't already leaked. Option C is scope-violating gold-plating for a two-person consented pilot.

**Key concerns:**
- Threat model is unstated. Behind RunPod's TLS proxy with founder+wife-only calls, a static token already defeats "random scanner hits the public wss URL." HMAC's marginal gain (blast-radius cap via expiry) is real but small — hygiene, not a security upgrade.
- The static token may already be compromised — it's a manually-set env var whose value has traveled through configs and chat. Rotating to any new scheme is worthless if the leak vector persists.
- Option B's nonce-reuse check needs state: either a DB (the thing B avoids) or an in-memory set that dies on restart and doesn't survive horizontal scaling. B is replay-protected only if that's acknowledged.
- Clock skew + key management: expiry assumes synced clocks; the key now lives in two places (pod env + mint script) — two leak surfaces, rotation story must be defined.
- The real ask is being buried: frame-size cap and rate limits are absent today — that's the actual exposure (memory-exhaustion DoS on a single GPU pod). Ship regardless of A/B/C.

**Recommendation:** Condition. Frame-cap + rate-limit first. Adopt B (signature + expiry; nonce-tracking honesty required). Reject C until real customer audio. Audit whether the current static token already leaked.

**🟢 Product**

**Core assessment:** For a two-person consented pilot, Option B buys real risk reduction (expiry + revocability via key rotation) at near-zero delivery cost and zero runtime cost, while Option C is enterprise machinery the product won't need until paying CS teams and the security gates are actually on the table. Option A leaves a non-expiring secret that grants standing access to a live audio pipeline if leaked.

**Key concerns:**
- Customer value at this stage is "don't embarrass yourself," not enterprise SSO. C delivers zero pilot value and burns days + a new always-on service — against spike discipline.
- Latency and reliability are the actual product: B adds one sub-millisecond HMAC check at init; C inserts a token-fetch round trip and a second service whose downtime takes the pipeline offline.
- Static token (A) is the finding an enterprise buyer's security review flags first; B gives a clean expiry+rotation story later.
- The frame-size cap and rate limits matter more than the token scheme — an unthrottled WS endpoint on a GPU pod is a cost-and-availability hole.
- Reversibility: B is throwaway-cheap; C's broker later slots in behind the same WS contract with no client rewrite.

**Recommendation:** Support B, conditioned on shipping the frame-size cap and rate limits in the same change. Reject C as premature infrastructure.

**🔵 Security**

**Core assessment:** At founder+wife pilot scale, Option B is the right security delta — it kills the standing-credential and replay problems of Option A without the unjustified attack surface and operational weight of Option C, which buys nothing real until there are multiple untrusted clients and an identity story.

**Key concerns:**
- Option A is a standing bearer secret with no expiry, rotation, or revocation. Once leaked, the public endpoint is a free GPU/transcription oracle and DoS target indefinitely. Unacceptable past today.
- HMAC key-distribution is a real but bounded weakness: mint script holds the same signing key as the server — a compromised founder laptop = full forgery. Accept at this scale; treat the key as crown-jewel (env only, never committed, rotatable).
- Replay within the validity window: signature+expiry alone lets a captured token be reused until expiry. Keep TTL ≤5 min AND enforce single-use nonce tracking — in-memory is enough at one-client scale. Not optional.
- The broker (C) doesn't change the trust model with one trusted human; it adds attack surface (service, Redis, OIDC, another secret store) plus a new outage dependency on the call path.
- TLS terminates at RunPod's proxy — the proxy→pod hop is plaintext inside RunPod's network and the endpoint is publicly reachable. Residual trust assumption to note before any real PII/PCI audio.

**Recommendation:** Support B, conditioned on: mandatory nonce single-use + ≤5-min expiry, signing key env-only and rotatable, and frame-size cap + per-connection rate limit in the same change. Reject A (effective now) and C (premature).

**🟡 Optimizer**

**Core assessment:** Option B is the right-sized choice — it kills the static-token exposure for ~100 lines and zero new infrastructure, exactly matching spike discipline. Option C is over-build for a two-person pilot; Option A is a latent credential leak.

**Key concerns:**
- Latency is a non-issue for all three: auth is once-per-handshake, off the per-frame hot path. Don't let "real-time" sway the choice.
- Static token never rotates and travels through every config/log/shell-history surface. No expiry = no blast-radius cap. Reject.
- C's operational trap: a second deployed service + Redis + OIDC is a new uptime dependency, attack surface, and patch burden — to authenticate two people. Pure waste.
- B scales fine through the pilot and early multi-tester phase without a DB.
- Bundle frame-size cap + rate limits regardless — higher-value DoS controls, independent of auth choice.

**Recommendation:** Support B, conditioned. Signing key env-only, TTL ≤5 min, reject expired/replayed tokens, and write validation as a clean middleware boundary so a future broker can mint the same token shape — enterprise migration becomes a mint-source swap, not a protocol rewrite.

---

## Synthesis

Unanimous on the shape: B now, C later (if ever), A retired. All four advisors independently
elevated the frame-size cap + rate limits to co-equal or higher priority than the token scheme
itself — they ship in the same change. The one genuine disagreement is nonce tracking: the
Skeptic calls it state-in-disguise, Security calls it mandatory. Resolved for B-as-built:
an in-memory single-use nonce set (trivial at one-pod scale, explicitly documented as
single-pod state that resets on restart — acceptable because TTL bounds the replay window
anyway). Skeptic's audit point stands: the old static token value appeared in chat/docs
history, so the signing key must be a fresh secret, never reused from AUTH_TOKEN.

## Dissent

Skeptic, preserved: HMAC-vs-static is hygiene, not a meaningful security upgrade at this
scale — the caps are the real fix, and clock skew + two-copy key management are new failure
modes A didn't have. Accepted; mitigations: ±30s skew tolerance, key rotation = change one
env var.

## Decision

**PROCEED WITH B (CONDITIONS)**

Implement stateless HMAC-SHA256 signed tokens: `v1.<expiry>.<nonce>.<sig>`, TTL default
300s, signing key `AUTH_SIGNING_KEY` env-only (fresh secret — never the old AUTH_TOKEN),
in-memory single-use nonce set, ±30s clock-skew tolerance, mint via local CLI script.
Same change must include: max WS frame-size cap and per-connection message-rate limit.
Validation isolated in one module so a future broker mints the same token shape.
Legacy static-token mode remains only when `AUTH_SIGNING_KEY` is unset (local dev), with
a loud startup warning.
