# Bridge — Roadmap to Production

**Document type:** Execution roadmap
**Version:** 0.1
**Companion to:** Bridge PRD v0.1, Bridge TRD v0.2
**Profile:** Solo builder + AI coding assistant, aggressive timeline, bootstrapped
**Last updated:** May 19, 2026
**Owner:** Salvation

---

## How to Read This Document

Eight stages, sequential. Each stage has:
- **Goal** — the one thing this stage exists to learn or prove.
- **Time budget** — realistic ceiling, not optimistic floor.
- **Cost budget** — out-of-pocket spend (API credits, tools, services).
- **Success criteria** — concrete, measurable. You pass or you don't.
- **Kill / pivot criteria** — what makes you stop, not just slow down.
- **Decision before next stage** — go / no-go / pivot.

If you cannot pass a stage's success criteria, **do not advance**. The whole point of the gates is that earlier failures get cheaper. A failed Stage 2 costs you a weekend. A failed Stage 6 costs you four months.

Two principles override everything:

1. **One user at a time until Stage 6.** Your wife is User #1. Don't recruit User #2 until you've made User #1 successful. Don't recruit User #10 until you have 3 paying.
2. **Spike before commit.** Every stage produces a learning, not just a deliverable. If you find yourself defending a previous decision instead of revising it, stop and revise.

---

## Stage 0 — Reality Check (1 week)

**Goal:** Confirm there's a real customer problem before writing a line of code.

**What you do:**
- Have a 45-minute conversation with your wife where you take notes, not assumptions. What exactly happened when she got sidelined? How often does it happen? What would have to be true for her — or her manager — to use a tool like this? Would her company pay for it, or would she use it personally?
- Talk to 3 more CS people in any company. LinkedIn outreach is fine. Aim for 30-minute calls. Same questions.
- Ask her manager (or anyone in CS ops) the harder question: would you let an agent use an AI translator on a real call? What would you need to see first? What's your hard no?

**Time budget:** 1 week, ~5 hours of conversations.

**Cost budget:** $0.

**Success criteria:**
- At least 2 of 4 people (your wife counts as 1) describe a version of the sidelining problem unprompted.
- At least 1 of them says some version of "I'd try it" or "I'd let my agents try it."
- You can write the problem statement in 2 sentences without referencing your wife specifically.

**Kill / pivot criteria:**
- Nobody else describes the problem → the market is smaller than you thought. Pivot to a different ICP (maybe sales calls, telehealth, field service) or shelve.
- Everyone says "interesting but I'd never deploy it" → the buyer doesn't exist. Stop.

**Decision before Stage 1:** Is there a buyer who will eventually pay, and a user who will try the v0?

---

## Stage 1 — Janky Pipeline Proof (1 weekend)

**Goal:** Prove the translation pipeline works at acceptable latency on your own hardware. Stop hypothesizing about latency budgets — measure them.

**What you build:**
- A Python script. ~200 lines.
- Reads from a virtual audio cable (BlackHole on macOS, VB-Cable on Windows — both free).
- Streams to Deepgram for Spanish STT.
- Sends text to GPT-4o-mini for translation.
- Sends translation to ElevenLabs Flash for English TTS.
- Plays output through another virtual cable or your speakers.
- One direction only (Spanish → English).
- Logs timestamps at every stage.

**What you do NOT build:** UI, error handling, the reverse direction, anything Rust, anything cross-platform. This is throwaway code. Plan its funeral.

**Time budget:** 2 days (Saturday + Sunday).

**Cost budget:** $20 in API credits across all three providers.

**Success criteria:**
- You can speak Spanish into your laptop and hear English back.
- p50 latency, mouth-to-ear, ≤ 1.5 seconds (yes, slower than the TRD target — you have no optimization yet).
- It works at least 8 out of 10 attempts on a clean utterance.

**Kill / pivot criteria:**
- Latency is consistently above 3 seconds → either you're doing something wrong, or the architecture in the TRD won't work. Spend one more day debugging; if still stuck, escalate to a forum or rethink.
- Translation quality is laughably bad on simple sentences → providers are weaker than assumed. Test alternates before continuing.

**Decision before Stage 2:** Does the pipeline produce intelligible, reasonably-timed translated speech?

---

## Stage 2 — Bidirectional + Real Call (1 week)

**Goal:** Prove the pipeline works on a real call, not just a script reading from a microphone.

**What you build:**
- Add the reverse direction (English → Spanish) to your Python script.
- Both directions run in parallel.
- Set up a Zoom or WhatsApp call between two laptops (or laptop + phone). Route one laptop's audio through your script.
- Have a 10-minute conversation with your wife where you speak only English and she speaks only Spanish.

**Time budget:** 1 week of evenings.

**Cost budget:** $30 in API credits.

**Success criteria:**
- You and your wife successfully complete a basic CS-style scripted scenario ("I'd like to check my balance" / "Sure, what's your account number") end-to-end in 10 minutes.
- Latency feels usable (not necessarily fast — just not maddening).
- No crashes. Recoverable hiccups are OK.

**Kill / pivot criteria:**
- The two-directional crosstalk produces audio chaos that can't be fixed with simple ducking → the architecture needs rethinking before any more building. Pause, revise TRD §4.7, then come back.
- Your wife says "I'd never use this on a real call" → listen to her. She's User #1.

**Decision before Stage 3:** Does this feel like the seed of a real product, or a science project? Be honest.

---

## Stage 3 — Single-Platform Desktop Prototype (3-4 weeks)

**Goal:** Turn the script into something your wife can actually install and use during her real workday.

**Pick ONE platform.** Whichever your wife uses. If she's on Windows, you build Windows-only. If macOS, macOS-only. **Do not build both.** Two platforms is a multiplier on every bug, every install issue, every audio quirk.

**What you build:**
- A minimal desktop app (Tauri or, if it's slowing you down, Electron is fine for v0 — you can rewrite later).
- Mini control window: start/stop, kill switch, language pair, one-line status.
- The Python pipeline becomes a local subprocess, or rewrites into the app's runtime — your call. AI assistant can do either. Latency matters more than elegance.
- Uses BlackHole or VB-Cable as the virtual audio device — **do NOT build your own driver yet.**
- Installer that works on one machine (yours, then your wife's).

**What you do NOT build:** Your own audio driver. The backend. Authentication. Licensing. Settings panels beyond the basics. A pretty UI. Captions (yet — add in Stage 4 if you have time).

**Time budget:** 3-4 weeks of evenings, or 2 weeks full-time.

**Cost budget:** $50-100 in API credits, plus signing certificate if Windows ($0-300 depending on type).

**Success criteria:**
- Your wife can install it on her work laptop without you in the room (you walk her through it once over a screenshare; she should be able to redo it from a written guide).
- She can start translation, take a 5-minute simulated call with you, and stop translation.
- p50 latency in the real install ≤ 1.2s; p95 ≤ 2.0s.
- It doesn't crash during the call.

**Kill / pivot criteria:**
- IT/security on her work laptop blocks the install entirely and won't budge → you have a B2B distribution problem before you have a product. Talk to her manager about exceptions, or test on her personal laptop, but understand this is a real risk for the eventual business.
- Latency on her real network is consistently above 3 seconds and you can't fix it → architectural rethink needed.

**Decision before Stage 4:** Can a non-technical user run this on a real machine, end-to-end?

---

## Stage 4 — Real-Use Pilot With Your Wife (4-6 weeks)

**Goal:** Find out what breaks when a real user uses it on real calls for real customers. This is the highest-information stage in the whole roadmap.

**Hard prerequisite:** Permission from her manager. Read this carefully — using AI translation on a real customer call without disclosure may violate her company's policies, two-party-consent laws in some US states, or both. Either:
- Get written permission from her manager AND a script for disclosing to callers, OR
- Run on Spanish-speaking friends/family who consent, posing as test customers, with realistic scenarios.

Pick one. The second option is slower-feedback but eliminates the legal risk. **Do not skip this prerequisite.** A "we'll deal with it later" attitude here can end the project or her job.

**What you do:**
- Your wife uses the tool on calls (real with permission, or simulated with friends) for 2-4 weeks.
- You instrument *everything*: latency per utterance, confidence scores, crashes, manual overrides, kill switch usage, call duration.
- After each session, 10-minute debrief. What broke? What felt wrong? What did the caller notice?
- Weekly: triage and fix the top 3 issues. Ship updates.

**Time budget:** 4-6 weeks.

**Cost budget:** $200-400 in API credits. This is your first real consumption signal — track $/minute of actual call time. **This number will define the business.**

**Success criteria (any one of these is a hit; all three is a home run):**
- She prefers using Bridge to declining Spanish calls (low bar — it just has to beat the status quo of "say I don't speak Spanish").
- She can complete a Spanish call in under 2x the time of an equivalent English call (translation overhead is real but bounded).
- At least 1 caller, post-call, did not realize they were talking through translation (the gold standard).

**Kill / pivot criteria:**
- After 2 weeks, your wife stops using it voluntarily → it's not good enough yet. Find out why before continuing.
- $/minute cost is so high that even at $80/seat/month, gross margin is negative → the unit economics don't work at v1 quality. Need to cut quality or find cheaper providers before scaling.
- Audio reliability problems (device disconnect, Bluetooth, echo) make it unusable on >20% of calls → this is the #1 risk from TRD §3.5 made real. Allocate time to it. Don't move to Stage 5 until fixed.

**Decision before Stage 5:** Is this real, or is it a demo that doesn't survive contact with reality? Be brutal with yourself here.

---

## Stage 5 — Three More Pilot Users (6-8 weeks)

**Goal:** Prove this works for someone who is not your wife. Different employer, different softphone, different headset, different network, different Spanish accent.

**Where to find them:**
- Your wife's coworkers (lowest-friction first).
- LinkedIn outreach to CS managers at companies with bilingual needs (telehealth, fintech, e-commerce, immigration services).
- CS subreddits, CS communities on LinkedIn.

You want 3 users by end of stage, ideally at 3 different companies. Free, in exchange for feedback.

**What you build during this stage:**
- Whatever the new users break. You're not building net-new features — you're plugging holes.
- A basic onboarding flow (right now you install it for them; that doesn't scale).
- A second platform if and only if a real user blocks on it (don't build Windows for hypothetical Windows users; build it for actual Windows users who said they'd use it).

**What you also build (parallel track):**
- Pricing conversations. Ask each user: "If this worked perfectly, what would your company pay per seat per month?" The answer is rarely a real number — listen for hesitation, comparisons, anchors.
- Your own audio driver, IF you keep hitting BlackHole/VB-Cable limits. Otherwise defer.

**Time budget:** 6-8 weeks.

**Cost budget:** $500-1000 in API credits across 3-4 users (one being your wife continuing).

**Success criteria:**
- 3 users actively using it weekly.
- At least 2 of them say (or strongly imply) they'd pay for it.
- Crash rate < 1 per 10 calls across all users.
- p50 latency ≤ 1s across all users and networks.

**Kill / pivot criteria:**
- After 8 weeks you can't find 3 users → the market may be narrower than thought, or your outreach isn't working. Reassess ICP.
- Users use it for a week and stop → the product isn't sticky. Find out why before continuing.
- The audio reliability problems are different for every user and you can't keep up → consider hiring a contract audio engineer (yes, this breaks bootstrapping, but it may be the only thing that saves the product).

**Decision before Stage 6:** Have you found a small pattern of users who'd pay? If yes, time to charge them.

---

## Stage 6 — First Paid Customer (4-6 weeks)

**Goal:** Get one company to write a check, however small.

**What you do:**
- Convert one of your Stage 5 users to paid. Or pitch their company. Or find one new design partner willing to pay.
- Price aggressively low: $25-40/seat/month for the first paying customer. You want the contract more than the revenue.
- Sign a real (even if simple) contract. Include termination terms, SLA, data handling.
- Build the absolute minimum required for a real customer:
  - Account creation (you may still onboard manually — that's fine).
  - Billing (Stripe Billing on a per-seat subscription — half a day's work with AI assistance).
  - Basic admin view of usage (even if it's a Notion page you update weekly).

**What you do NOT build:** Self-serve signup. SSO. Enterprise compliance. A polished website. These come later.

**Time budget:** 4-6 weeks (most of it is sales, not engineering).

**Cost budget:** $200-500 in legal templates (use Clerky, LawDepot, or a TermsFeed-style generator; don't pay a real lawyer yet unless the customer requires it).

**Success criteria:**
- One signed contract, money in your bank account.
- Customer's agents are using Bridge daily without your intervention.
- You have one written testimonial or quote you can use in sales.

**Kill / pivot criteria:**
- After 6 weeks, no one will pay even $25/seat → either the product isn't ready or the price discovery is off. Probably both. Slow down on building, intensify on selling.

**Decision before Stage 7:** Does paid usage look different from free usage? Are they actually deploying it broadly, or just on a couple of seats? Get honest signal here before scaling.

---

## Stage 7 — Repeatable Sales Motion (3-6 months)

**Goal:** Get to 5-10 paying customers and figure out *how* you sell, not just *that* you sell.

**What you build/do:**
- A real onboarding flow (self-serve install, but white-glove activation).
- The second platform if you haven't already.
- Native integration with whichever softphone your customers most use. Usually Zendesk or Five9 — let the customers pick.
- A simple admin dashboard.
- Pricing experiments — raise prices on new customers, observe conversion.
- Hire help. This is probably the right time to bring on a part-time or contract audio engineer if your reliability is still wobbly. Or a part-time SDR if you're drowning in outreach.

**Time budget:** 3-6 months.

**Cost budget:** This depends on what you've raised or earned. If you're at $5-10k MRR, you can spend $2-5k/month on tools and contractors.

**Success criteria:**
- 5-10 paying customers.
- $5-15k MRR.
- Churn under 10% monthly.
- You can describe your ideal customer in one sentence.
- You have a sales process (even a janky one) that's reproducible — not "we got lucky" but "we did these 5 things and it worked."

**Kill / pivot criteria:**
- After 6 months at this stage with under 3 paying customers → the sales motion isn't working. Don't push harder; rethink ICP, pricing, or product.
- Churn above 25% monthly → you have a retention problem that more sales won't fix. Stop selling, start interviewing churned customers.

**Decision before Stage 8:** Are you running a business now, or still running an experiment? If a business, time to raise or scale. If still an experiment, more iteration first.

---

## Stage 8 — Scale (open-ended)

**Goal:** Whatever you want it to be — fundraising, expanding languages, expanding to mobile, expanding to other verticals, growing the team.

At this point the roadmap has done its job. Stage 8 is shaped by the choices you've earned the right to make, which depend on what you've learned in stages 0-7. Don't plan this stage now. Plan it from the inside.

A few notes from outside the maze:

- **Languages.** Your second language is the most expensive one — you'll re-test everything (STT quality, MT quality, TTS quality, accent variants). The third onward gets cheaper. Pick the second based on customer demand, not market size.
- **Mobile.** Don't until customers force it. Field-service CS may eventually pull you here.
- **Fundraising.** With real paid pilots and even $20k MRR you can have a credible seed-round conversation. Without paid pilots, you can't, even with a beautiful PRD.
- **Hiring order.** First hire is almost always a senior engineer who owns what you're worst at (probably audio reliability). Second hire depends on what's broken — could be sales, could be infra, rarely product.

---

## Overall Timeline Summary

| Stage | Duration | Cumulative | Cost (cumulative) |
|---|---|---|---|
| 0 — Reality check | 1 week | 1 week | $0 |
| 1 — Janky pipeline | 1 weekend | 2 weeks | $20 |
| 2 — Bidirectional + real call | 1 week | 3 weeks | $50 |
| 3 — Desktop prototype | 3-4 weeks | 7 weeks | $200 |
| 4 — Wife pilot | 4-6 weeks | 13 weeks | $600 |
| 5 — 3 more pilots | 6-8 weeks | 21 weeks | $1,600 |
| 6 — First paid | 4-6 weeks | 27 weeks | $2,100 |
| 7 — Repeatable sales | 3-6 months | 1 year | varies |
| 8 — Scale | open | — | — |

**Translation: ~5-6 months to first paying customer if everything goes well. ~12 months to a real business. Plan for double.**

These numbers assume the AI assistant works well, every stage succeeds first time, your wife stays cooperative, and nothing blocks you for more than a few days. None of that is realistic. **Add 50% to every estimate and you'll be closer.**

---

## Cross-Cutting Principles

Things that apply at every stage:

1. **Always have a tagged "good build" you can roll back to.** AI-assisted code can ratchet broken faster than you can debug. Git tags after every working state.
2. **Write down what you learned at each stage.** A 1-page memo per stage. You will forget. Future-you will need it for investors, contractors, or rewriting the PRD.
3. **Talk to a user every week.** Even at Stage 1. The user-conversation cadence is more important than the build cadence.
4. **Your wife is not your customer-discovery sample.** She is User #1 and she is invaluable, but she is also biased toward making you feel good. Force at least one external conversation per stage.
5. **The product is not the moat.** The audio reliability + trust UX + accumulated workflow knowledge is the moat. Spend time there even when it's not glamorous.
6. **Stop reading docs and start shipping.** This roadmap is itself a doc. Don't reread it weekly. Reread it at each junction, then forget it until the next one.

---

## What This Roadmap Deliberately Leaves Out

So you know what you're signing up for, and what's coming later:

- **A formal go-to-market plan.** Premature until Stage 6.
- **A hiring plan.** You're solo until you're not. The first hire is decided by what's most broken at Stage 7, not pre-planned now.
- **A fundraising plan.** Skipped intentionally. Raise when you have signal that justifies the dilution. Most companies that look like Bridge can get to $50-100k MRR before raising, which gets you a better deal.
- **A formal QA/test strategy.** At your scale, manual testing on your own machine + your wife's machine is fine. Automated test infrastructure comes when you have 10 customers.
- **A formal compliance roadmap.** SOC 2, GDPR, HIPAA, PCI all come in Stage 7+ when a customer asks. Build only what's asked for.

---

## Final Honest Note

The hardest part of this roadmap is not Stage 3 (the technical build). It's Stage 4 (real-use pilot) — because that's where the project finds out if it's real. Many AI-translation prototypes look great in a demo and fall apart on minute 18 of a real call.

If you can survive Stage 4 with your wife still wanting to use it, you have something. If you can't, you've learned a great deal at low cost — which is exactly what this roadmap is designed to do.

Build the smallest possible thing. Put it in front of a real user. Be ready to throw it away. Repeat.
