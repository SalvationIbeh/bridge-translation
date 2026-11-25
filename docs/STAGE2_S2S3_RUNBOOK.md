# Stage 2 — S2 Load Test + S3 Auth Validation Runbook (one pod sitting)

**Goal:** in one sitting: (1) prove the signed-token auth + abuse limits work on the live
pod (S3), (2) measure real concurrent capacity and set `MAX_SESSIONS` (S2).

**Prereqs:** image built from a commit including `server/auth.py` (push to main with
`server/**` changes auto-builds `ghcr.io/salvationibeh/bridge-server:latest` — confirm the
GitHub Action succeeded before renting the pod).

---

## 0. Pod setup — signed-token mode

1. Generate a **fresh** signing key on the dev box (never reuse the old AUTH_TOKEN value):
   ```powershell
   $py = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
   & $py -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. In the RunPod template/pod env: set `AUTH_SIGNING_KEY=<that value>`, **remove
   `AUTH_TOKEN`**. Reset the pod so it pulls `:latest` + new env.
3. Add the same key to the dev box `.env`: `AUTH_SIGNING_KEY=<that value>`.
4. Boot checks: logs say `Auth: signed-token mode (HMAC, TTL<=300s)` (NOT the legacy
   warning), warm-up OK for both pairs, `/health` 200.

```powershell
# dev box env for all steps below
$env:SERVER_URL = "wss://<podid>-8000.proxy.runpod.net/ws/translate"
$env:AUTH_SIGNING_KEY = "<the same key>"   # harnesses auto-mint per connection
```

## 1. S3 validation — prove rejections, then acceptance

```powershell
# (a) Old static token → must be rejected (AUTH_FAILED)
$env:AUTH_SIGNING_KEY = ""; $env:AUTH_TOKEN = "<your-test-token>"
& $py server/test_client.py samples/realtime-pcm/card_block.pcm es en      # expect AUTH_FAILED

# (b) Garbage + expired token → rejected
$env:AUTH_TOKEN = "v1.123.fake.deadbeef"
& $py server/test_client.py samples/realtime-pcm/card_block.pcm es en      # expect AUTH_FAILED

# (c) Freshly minted token → accepted
$env:AUTH_SIGNING_KEY = "<the key>"
$env:AUTH_TOKEN = (& $py scripts/mint_token.py)
& $py server/test_client.py samples/realtime-pcm/card_block.pcm es en      # expect translation

# (d) Replay: run (c)'s client again with the SAME token → AUTH_FAILED (single-use nonce)
& $py server/test_client.py samples/realtime-pcm/card_block.pcm es en
```
Record pass/fail for a–d. (a–b prove the endpoint is closed; c–d prove tokens work and
can't be replayed.) Server logs show the rejection reason; the client only sees AUTH_FAILED.

## 2. S2 load ramp — set the real MAX_SESSIONS

```powershell
& $py scripts/benchmark_s2_load.py --levels 1,2,4,6,8
```
- Mixed directions automatic (en clips exist). Each level: N concurrent realtime streams
  × the clip set; PASS = zero errors and P95 < 2000ms.
- The summary prints **Recommended MAX_SESSIONS** = highest passing level.
- If level 8 still passes easily on the rented GPU, optionally extend: `--levels 10,12`
  (raise pod `MAX_SESSIONS` env first if you exceed the image's 10).
- Watch `nvidia-smi` during the top level: note GPU util % and memory — that's the
  capacity headroom datapoint for cost-per-call math.

## 3. Wrap up (before stopping the pod)

1. Stop the pod.
2. `git add -f runs/s2-load-*.json` + commit.
3. Set `MAX_SESSIONS` in `server/Dockerfile` to the measured number (next image build
   picks it up) — or pod env if it differs per GPU type.
4. Log spend: `& $py scripts/log_cost.py add --provider runpod --usd <hrs × rate> --note "S2+S3 sitting"`.
5. Update `PROJECT_STATUS.md` S2/S3 rows with measured results.

## Decide

| Outcome | Next |
|---|---|
| S3 a–d all correct + S2 gives MAX_SESSIONS ≥ 4 | S2+S3 closed. Remaining §8 gates before real audio: S4 zero-persistence proof, DPA/region pinning, consent+PCI plan |
| Replay (d) NOT rejected | Bug in nonce path — fix before calling S3 done; do not pilot |
| S2 P95 blows up at low concurrency | Check which stage saturates (stage_ms in the JSON); workers are 2/2/2 — try 3/3/3 on a bigger GPU before concluding |
| MAX_SESSIONS < 4 per GPU | Cost-per-call math changes — recompute $/call before any capacity claims |
