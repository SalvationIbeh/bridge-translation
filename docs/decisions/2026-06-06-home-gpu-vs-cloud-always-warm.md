# Decision — Home GPU vs Cloud for the Always-Warm Entry Point

**Date:** 2026-06-06
**Method:** Council review (4 independent advisors: Skeptic / Product / Security / Optimizer)
**Status:** REJECT (home GPU as primary) — constructive alternative attached

---

## Question

For Bridge's always-warm production entry point, does it make sense to **buy one physical GPU**
(≈RTX 4000 Ada, ~$1,000–1,500 capex) to run **at home 24/7 as the PRIMARY always-warm node**,
with RunPod as failover — to mitigate single-cloud-provider downtime?

**Trigger:** the founder saw a cloud GPU provider's status page showing downtime and wants
reliability for the always-warm node.

**Context:** self-hosted GPU stack (VAD→Whisper-small→Opus-MT→Piper), S1 telephony gate just
passed on a RunPod RTX 4000 Ada @ $0.26/hr ≈ $187/mo always-warm; pre-warm mandatory (29s cold
= dropped call); server is in the live audio path; customer audio carries PII + likely PCI;
solo bootstrapped builder, 1 pilot user then 3 more.

---

## Council Report

### Advisor Views

**🔴 Skeptic**

*Core:* Reliability theater, not a reliability upgrade. A solo founder's home is strictly
lower-availability than a Tier-III datacenter, and the failover story doesn't survive contact
with how live calls actually break.

- Home is a downgrade: residential ISP (no SLA, dynamic IP, maintenance windows), single power
  feed, consumer cooling, a box that reboots for updates ≈ 98–99% — *worse* than RunPod's ~99.9%.
- The on-call SRE is asleep. Tripped breaker / ISP outage / hung GPU at 3am has no auto-remediation
  and no human; a 2-week vacation is a 2-week SPOF.
- Failover can't save a call in progress — the server is in the live audio path; node death = dead
  call. And RunPod's 29s cold start means the *next* call drops too, violating the pre-warm rule.
- PII/PCI at home is compliance debt you don't need yet (kills future SOC2/PCI conversations).
- Infra LARP at 1–4 users. The trigger was a status page, not an outage that hurt a customer.

*Recommendation:* **Reject for now.** Stay single-RunPod; if a real outage bites, the in-architecture
answer is a second RunPod region/provider, not a basement. Buy the GPU only as a dev/inference box
(~8 months of warm-node spend) — never in the production audio path until there's a paying customer
and a second human on call.

**🟢 Product**

*Core:* Buying a home GPU at 1–4 users solves a problem the product doesn't have yet while starving
the problems it does. Perceived reliability here comes from the audio path + pre-warm, not provider
redundancy — and a single home box is *less* reliable than RunPod.

- Trigger is fear, not data — zero recorded outages affecting a real call.
- A home box lowers the SLA, then you build failover to cover the failover.
- Opportunity cost is the whole ballgame: hours on a 24/7 node = hours not on barge-in, the desktop
  client, or finding pilot #2 (the actual moat). Solo builders die from doing too many things.
- $187/mo cloud already covers 4 users; $1,500 capex + labor buys zero customer-perceived value today.
- Live audio on a home network invites a compliance conversation you don't want yet.

*Recommendation:* **Reject for now.** Stay on the single RunPod always-warm pod; make pre-warm
bulletproof. If real outage data ever justifies redundancy, do **cloud multi-region** (RunPod + a
second managed provider). Revisit no earlier than Stage 4–5, gated by a logged outage, not a status page.

**🔵 Security**

*Core:* Moving the internet-facing audio entry point into a residence converts a contained datacenter
risk into an unbounded personal-and-customer risk; for spoken card data it's the single worst place to
terminate the stream.

- **PCI-DSS scope explosion** — spoken PANs in VRAM/buffers put the home box, its LAN, and the founder
  personally into PCI scope a solo founder cannot satisfy; "card data transited my home PC" is a
  pilot-security-questionnaire deal-killer.
- **Blast radius = personal life** — a port-forward onto a flat home LAN means compromise = lateral
  access to personal laptops, backups, phones; live PII/PCI sits next to the founder's digital identity.
- No datacenter controls — no DDoS scrubbing, no physical security, residential SLA; a residential IP
  deanonymizes to a home address (stalking/physical-safety risk for a named founder).
- Most residential ISP ToS prohibit production servers; can't credibly sign a DPA naming a private
  residence; data-residency/sub-processor clauses become unanswerable.
- "Where is our audio processed?" — "a SOC2-aligned datacenter region" passes; "my house" ends it.

*Recommendation:* **Reject the home box as internet-facing primary.** Keep a compliant cloud as the
audio entry point. If the GPU is bought, restrict it to **offline benchmarking/model-eval on synthetic
samples only** — never internet-exposed, never touching real customer audio. Precondition before customer
audio touches any node: PCI scope assessment, signed DPA, network isolation, managed DDoS/patching —
none achievable in a residence.

**🟡 Optimizer**

*Core:* The goal is reliability for the entry point, and a home GPU doesn't deliver it — it relocates the
single point of failure into your apartment behind a residential ISP/NAT. The break-even math doesn't
favor capex at this scale.

- Break-even is mediocre and worsens: naive 5.3–8 mo payback, but +power (~108 kWh/mo, ~$13–35) + cooling,
  and if the RunPod failover stays warm you pay $187/mo anyway → real payback ~9–14 mo, assuming zero
  hardware failure / driver rot / your time.
- Residential hosting is *lower* availability, not higher.
- You own the wrong layer: the thing that must be stable is a **hostname + TLS + health-check + failover**,
  not a GPU. Couple them and every GPU swap is a client-facing event.
- Capex freezes the stack mid-spike (Whisper/Opus-MT/Piper may all change); cloud lets you resize.
- Solo bootstrapped: capex + babysitting is the most expensive hour you're not spending on pilot users.

*Recommendation:* **Reject the buy; condition on a decoupled router.** Stand up a $5–10/mo VPS as a stable
public hostname + TLS reverse proxy that health-checks GPU *backends* and fails over. Run RunPod primary
(always-warm) + a second RunPod region as warm/fast-cold failover, with an automated pre-warm + documented
spin-up runbook. ~90% of the reliability value for ~$200/mo, zero capex, zero NAT, and a topology that
scales (backends become a pool the client never sees). Revisit a home GPU only if sustained concurrent
load makes owning genuinely cheaper than renting.

---

### Synthesis

Unanimous, with unusually strong agreement: a home GPU as the **primary internet-facing audio node** is a
net **downgrade** on every axis that matters here. It lowers availability (residential ISP/power/no-remote-hands
≈ 98–99% vs datacenter ~99.9%), can't actually rescue a live call (the server is in the audio path; node death
= dead call; RunPod's 29s cold start means the failover itself drops the next call), and explodes security/
compliance exposure (spoken PCI data on a home LAN, PCI scope, unsignable DPA, personal-life blast radius). The
trigger was a status page, not a real outage — this is premature hardening at 1–4 users while the actual moat
(audio reliability, desktop client, pilot #2) goes unbuilt. The constructive convergence: **own the right layer**
— a cheap always-on router/proxy (hostname+TLS+health-check), with GPU *backends* as a swappable pool — and add
**cloud multi-region** redundancy only when a logged outage justifies it.

### Dissent

None on the core call — unanimous reject of home-as-primary. The only permitted use, raised by Skeptic and
Security, is a home GPU as an **offline dev/benchmark box on synthetic samples**, never internet-exposed and never
touching real customer audio. Optimizer's "decoupled router" is the agreed forward pattern, not a dissent.

### Decision

**REJECT** — do not put a home GPU in the production audio path as the primary always-warm node.

- **Now (1–4 users):** stay on the single RunPod always-warm pod; make pre-warm bulletproof (automated, monitored).
- **Right layer for reliability:** decouple the entry point into a cheap always-on **router/proxy** (stable
  hostname + TLS + health-check + failover); GPU backends become a swappable pool the client never sees.
- **When redundancy is actually justified** (gated by a *real logged outage*, ~Stage 4–5): **cloud multi-region/
  multi-provider** failover (RunPod region A + B, or RunPod + a second managed GPU cloud), not home hardware.
- **A home GPU is acceptable only** as an offline dev/benchmark box on synthetic samples — never internet-facing,
  never real customer audio.

**Most important reason:** a residential node is *less* available and *far* riskier (PCI/PII, personal-life blast
radius) than the datacenter it would replace — it moves the single point of failure into your living room while
solving a problem you don't yet have.
