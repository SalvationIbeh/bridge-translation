# Decision — Reliability & Availability Path for the Always-Warm Node

**Date:** 2026-06-06
**Method:** Council review (Skeptic / Product / Security / Optimizer, independent + parallel)
**Status:** CONDITION — document the path, with refinements (DNS not a router; pre-warm is the centerpiece)
**Feeds:** `docs/STAGE2_DESIGN.md` §4.4

---

## Question

Should Bridge commit to (and document) this reliability architecture as its path?
- **NOW (1–4 users):** single RunPod always-warm pod + bulletproof pre-warm.
- **NEXT:** a small always-on router/proxy VPS (stable hostname + TLS + health-check) in front of swappable GPU backends.
- **LATER (gated by a real outage/SLA):** multi-region/multi-provider cloud backends behind the router.
- Home GPU only as an offline synthetic-only dev box (decided 2026-06-06).

---

## Council Report

### Advisor Views

**🔴 Skeptic** — *Condition.* The instinct to write down a path is healthy, but the plan over-specifies the
wrong layer. A custom router as the committed NEXT step is premature: it **moves the SPOF** to a $5 unmanaged
VPS in the live PCI/PII path, adds a **latency hop** on every WSS frame in a ~900ms budget, and solves a
problem **DNS already solves** (low-TTL CNAME/A record you repoint in seconds, zero hop, zero box). A
documented 3-stage HA plan at 1 user is an "astrology" gravity well — future-you "implements the doc" instead
of re-deciding against then-current managed options. Commit to: bulletproof pre-warm NOW; client stability via
**low-TTL DNS, not a router**; failover/proxy only when a logged outage or SLA forces it. Write the *principle*
("GPU decoupled from client address"), not the box.

**🔵 Security** — *Condition (support with a non-negotiable).* Safe to document **only if the doc hard-codes
that the router NEVER terminates TLS for audio.** A TLS-terminating proxy on a cheap shared-tenancy VPS puts
spoken card numbers in **plaintext** in that VPS's RAM → full PCI-DSS scope it cannot satisfy, plus a second
sub-processor in the audio path. Mandate **TLS passthrough (SNI/L4)** so the GPU is the sole plaintext point;
if smart routing ever forces termination, require **router→backend mTLS re-encryption** so plaintext never
rests on the VPS, and treat the VPS as in-scope if it ever does. Key custody on the GPU alone. (Note: the DNS
approach sidesteps this entirely — client connects directly to the GPU, no middle box sees the audio.)

**🟡 Optimizer** — *Condition.* The goal (stable hostname decoupled from a swappable GPU) is real, but a
dedicated router VPS is over-engineered for NEXT. **DNS-level indirection buys ~90% of the decoupling at
near-zero cost and zero added latency hop.** An L7 proxy adds a TLS handshake + RTT on a path fought to ~900ms;
a low-TTL CNAME / managed DNS-failover (Route53/NS1 health checks) repoints to a new GPU with the client
connecting **straight to the GPU**. For a live stateful WSS a router can only route **new** connections anyway
— matching "failover protects the next call, not the in-flight one" — so you need a fast way to change where
the *next* connection lands = DNS's job, not a proxy's. DNS TTL (30–60s) is the reconnect window, acceptable
when a call already tolerates a 29s cold start. Defer any proxy to LATER; if adopted, same-region **L4
passthrough** only.

**🟢 Product** — *Support, conditioned.* Documenting a now/next/later path is the right amount of planning
**if the doc's center of gravity is the pre-warm guarantee** — the ONE thing that moves customer-perceived
reliability at 1–4 users (29s cold = dropped call = lost pilot). Pre-warm + a **logged warm-success metric**
deserves ~70% of the page; the router is a one-paragraph "future swap point, do not build"; multi-region a
gated footnote; the HA diagram labelled "illustrative, not scheduled." Make deferral **active**: each NEXT/LATER
item needs an explicit **trigger gate** ("only when X"), not a timeline, so "documented" doesn't become
"to-do." The sellable uptime artifact for a future customer is "last 30 days: zero cold-starts," not a diagram —
so commit to logging that metric now.

### Synthesis

Unanimous endorsement of the *goal* (stable client address decoupled from a swappable GPU; failover deferred
until real need) — but a unanimous **refinement of the mechanism**: replace the NEXT-stage "router VPS" with
**low-TTL DNS / managed DNS-failover** (Skeptic + Optimizer, independently). DNS is cheaper, adds **zero latency
hop**, introduces **no new SPOF**, keeps the client connecting **directly to the GPU** (TLS terminates on the
GPU → no second provider in the plaintext audio path), and matches the live-WSS reality that only the *next*
connection can be redirected. Security adds the hard rule for *any* future proxy: **never terminate audio TLS on
the proxy** (L4/SNI passthrough or mTLS re-encrypt). Product reframes the doc so **bulletproof pre-warm + a
logged warm-success metric is the NOW centerpiece**, with everything else gated by explicit triggers so the doc
discourages premature building.

### Dissent

None on substance — all four are "condition/support." No advisor defended the dedicated router VPS as the NEXT
step; it is demoted to a gated LATER escape-hatch with the TLS-passthrough constraint.

### Decision

**CONDITION — document the path, refined:**
1. **NOW (centerpiece):** bulletproof pre-warm (health-check + auto-restart) + **log a warm-success/cold-start
   metric**. Single RunPod always-warm pod; no redundancy.
2. **NEXT (cheap decoupling):** **low-TTL DNS / managed DNS-failover** for a stable hostname → repoint to swap
   GPU backends. **Not** a router VPS. Client connects directly to the GPU.
3. **LATER (gated by a real logged outage or signed SLA):** multi-region/multi-provider backends via DNS-failover.
   A proxy only if smart routing is truly needed, and **only** same-region L4/SNI passthrough or mTLS
   re-encryption — **never** terminate audio TLS on the proxy.
4. **Never:** a home GPU in the customer-audio path (offline synthetic dev box only).
5. Keep deferral **active**: explicit trigger gates; label diagrams "illustrative, not scheduled."

**Most important reason:** DNS delivers the same address-decoupling goal with zero latency, zero new SPOF, and
zero extra provider in the PII/PCI audio path — so the only reliability work worth doing now is making pre-warm
provably bulletproof and logging it.
