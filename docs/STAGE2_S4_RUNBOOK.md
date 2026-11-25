# Stage 2 — S4 Zero-Persistence Runtime Proof Runbook (one pod sitting)

**Goal:** produce the §8 security-gate evidence artifact for **condition 2** —
*"zero audio/transcript persistence PROVEN (not just intended)"* — by running the
pipeline live while a co-located monitor watches every disk write and scans the
server log for transcript leakage.

**The one thing that's different from S1/S2/S3:** this harness runs **ON THE POD**,
not from the dev box. A polling watcher sees writes on the host it runs on, so it
must sit next to the server process. Driving the server remotely proves nothing
about the server's disk.

**Status going in:** the static audit is done (STT wraps PCM in `io.BytesIO`, no
`open(...,'w')` in the pipeline, `Job` bytes GC'd per session; two old DEBUG
transcript-leaks already fixed — PROJECT_STATUS S4). The harness
(`scripts/audit_s4_persistence.py`) is validated off-pod (`--self-test` PASS, plus
clean/leaky mock-server wiring). This sitting converts "intended" → "proven".

**Prereqs:** image built from a commit including `scripts/audit_s4_persistence.py`.
Same fresh-signing-key discipline as S3 (runbook §0 there) — generate a NEW
`AUTH_SIGNING_KEY`, never reuse a burned one.

---

## 0. Pod setup — boot in the LEAK-stress configuration

Boot the server at **`LOG_LEVEL=DEBUG`** (the prior leak was a debug line — DEBUG is
the strict case for the log scan) and tee stdout to a file the audit can read:

```bash
# on the pod, in the repo root:
git pull   # ensure scripts/audit_s4_persistence.py is present
export AUTH_SIGNING_KEY=<fresh-key>   MODELS_DIR=/models   LOG_LEVEL=DEBUG
python server/main.py 2>&1 | tee /tmp/bridge-srv.log
# wait for "=== Server ready ===" and /health 200
```

Leave that running. Open a second shell on the pod for the audit.

## 1. Run the audit (co-located, second shell)

```bash
export AUTH_SIGNING_KEY=<fresh-key>   # same key — harness mints a token per turn
export SERVER_URL=ws://localhost:8000/ws/translate   # localhost: same host as server
python scripts/audit_s4_persistence.py \
    --server-log /tmp/bridge-srv.log \
    --watch-root /tmp --watch-root /dev/shm \
    --clips 5 --src es --tgt en
```

What it does: drives 5 real es→en sessions while watching the server dir, repo,
`MODELS_DIR`, `/tmp`, and `/dev/shm`; classifies every write; scans the teed log for
the verbatim source/STT/MT text of each utterance.

Then repeat once for the reverse leg (proves both directions persist nothing):

```bash
python scripts/audit_s4_persistence.py --server-log /tmp/bridge-srv.log \
    --watch-root /tmp --watch-root /dev/shm \
    --clips 5 --src en --tgt es \
    --clip-dir samples/realtime-pcm-en --manifest samples/stage2-en-es.json
```

## 2. Reading the verdict

- **`✅ PASS`** — exit 0. No `LEAK`-class write (audio/transcript/dump), no log hit.
  This run's `runs/s4-persistence-*.json` is the gate-2 evidence artifact.
- **`⚠ REVIEW` lines** — an unexpected non-audio write (e.g. a library lock/cache in
  `/tmp`). Inspect each. If genuinely benign, re-run clearing it with
  `--allow-glob '<pattern>'` and record why in the commit. Do **not** allow-glob
  anything you haven't looked at.
- **`❌ FAIL`** — a `LEAK` write or a verbatim transcript hit in the log. The report
  gives the path / log line numbers (never the leaked text). Fix in code, rebuild the
  image, re-sit. Budget for **two sittings** (proof, then fix-confirm).

## 3. Gold-standard deep mode (optional, strongest proof)

The polling watcher can't attribute writes to a process and can miss a sub-interval
transient file. For an irrefutable syscall-level proof, boot the server under strace
and confirm no real-file `write`/`openat` outside the socket + model cache:

```bash
strace -f -e trace=write,openat,creat,unlink -o /tmp/srv.strace \
    python server/main.py   # then drive the audit as in §1
grep -aE 'openat\(.*O_WRONLY|O_CREAT|^write\(' /tmp/srv.strace \
    | grep -avE '/models|\.pyc|socket|/proc|/dev/(null|urandom)'
# expect: nothing audio/transcript-shaped
```

## 4. After PASS

1. Commit the passing `runs/s4-persistence-*.json` (force-add — `runs/` is
   gitignored): `git add -f runs/s4-persistence-*.json`.
2. Mark **S4 ✅ PROVEN** in `PROJECT_STATUS.md` and STAGE2_DESIGN §9 / §8 condition 2.
3. Stop the pod. S4 is one of four §8 conditions — TLS/WSS, signed DPA + region
   pinning, and two-party-consent + PCI-avoidance still gate real customer audio
   (the DPA is the long pole — start it in parallel, it needs no pod).
