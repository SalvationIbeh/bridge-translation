"""
audit_s4_persistence.py — S4 zero-persistence RUNTIME proof (STAGE2_DESIGN §9 S4, §8 cond. 2).

The static audit (code review: STT wraps PCM in io.BytesIO, no open(...,'w') in the
pipeline, Job bytes GC'd per session) is already done. This harness proves it at
RUNTIME: it drives real sessions through a running server while a co-located
filesystem monitor watches every write, then asserts that NO audio, transcript, or
translation bytes ever touched disk — only the allowlisted model cache and .pyc files.

  ┌─ §8 Security gate, condition 2: "zero audio/transcript persistence PROVEN
  └─ (not just intended)". This is the proof artifact for that condition.

THIS HARNESS MUST RUN ON THE SAME HOST AS THE SERVER (the pod in Phase B; locally
for the off-pod smoke test) so it observes the server process's actual filesystem
writes. Driving the server from a remote box proves nothing about the server's disk.

Two independent checks — BOTH must pass for a green verdict:

  1. Filesystem monitor — a dependency-free polling watcher records every path
     created/modified under the watched roots (server dir, repo root, MODELS_DIR,
     system temp, plus any --watch-root) during the session window. Each path is
     classified ALLOWED (model cache, *.pyc, this run's own runs/s4-* output) or
     SUSPECT (everything else — audio *.wav/*.pcm/*.opus, transcript text, core
     dumps, swap). Any SUSPECT path → FAIL.

  2. Log-content scan — point --server-log at the file the server's stdout was
     redirected to; the scan asserts the verbatim source transcript and translation
     of every driven utterance NEVER appear in the log. Any hit means content leaked
     to logs → FAIL. Run once at LOG_LEVEL=INFO and once at LOG_LEVEL=DEBUG (the
     prior leak was a debug line — see PROJECT_STATUS S4). The report stores only
     line numbers + a redacted marker, never the leaked text itself.

Gold-standard deep mode (Linux/pod, optional, syscall-level): run the server under
    strace -f -e trace=write,openat,creat,unlink -o /tmp/srv.strace python server/main.py
and confirm no write/openat for a real file outside the socket + model cache. The
polling watcher is the portable default; strace is the irrefutable proof.

Usage — Phase B, ON THE POD:
    # term 1 — server, logs teed to a file, DEBUG to stress the log check:
    AUTH_SIGNING_KEY=<key> MODELS_DIR=/models LOG_LEVEL=DEBUG \
        python server/main.py 2>&1 | tee /tmp/bridge-srv.log
    # term 2 — the audit (co-located):
    AUTH_SIGNING_KEY=<key> SERVER_URL=ws://localhost:8000/ws/translate \
        python scripts/audit_s4_persistence.py --server-log /tmp/bridge-srv.log \
            --watch-root /tmp --watch-root /dev/shm

Off-pod self-test — Phase A, no server / models / websockets needed:
    python scripts/audit_s4_persistence.py --self-test

Output: runs/s4-persistence-<ts>.json + a PASS / PARTIAL / FAIL verdict.
Exit code: 0 PASS, 2 PARTIAL (fs clean, log check skipped), 1 FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # Windows consoles default to cp1252 and choke on ✅ / → / box chars
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]

# Extensions/markers that must NEVER appear on disk during a session.
LEAK_EXTS = {".wav", ".pcm", ".raw", ".mp3", ".flac", ".ogg", ".opus", ".m4a",
             ".webm", ".aiff", ".aif", ".au"}
TEXT_EXTS = {".txt", ".json", ".csv", ".log", ".srt", ".vtt", ".transcript", ".tsv"}
DUMP_HINTS = ("core.", ".dmp", ".mdmp", ".hprof", ".stackdump", "core_")
# A bare file literally named "core" (Linux core dump) — matched separately.

# Min length for a transcript "secret" to be worth scanning for (avoid matching
# short common words; driven utterances are full sentences, so this is safe).
SECRET_MIN_LEN = 12


# ── path helpers ──────────────────────────────────────────────────────────────
def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _is_under(path: str, root: str) -> bool:
    if not root:
        return False
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root + os.sep)


# ── classification ────────────────────────────────────────────────────────────
def classify(path: str, models_dir: str, repo_root: str,
             allow_paths: list[str], allow_globs: list[str] | None = None
             ) -> tuple[str, str, str]:
    """Return (verdict, severity, reason).
    verdict  in {ALLOWED, SUSPECT}
    severity in {"", "leak", "review"} — "leak" = audio/transcript/dump signature
             (the gate-failing class); "review" = an unexpected write to scrutinize."""
    import fnmatch

    npath = _norm(path)
    name = os.path.basename(npath)
    low = name.lower()

    for ap in allow_paths:
        if npath == _norm(ap):
            return "ALLOWED", "", "operator-designated path (log sink / audit output)"

    if low.endswith(".pyc") or (os.sep + "__pycache__" + os.sep) in npath + os.sep:
        return "ALLOWED", "", "python bytecode cache"

    if _is_under(path, models_dir):
        return "ALLOWED", "", "model cache (MODELS_DIR)"

    runs_dir = os.path.join(repo_root, "runs")
    if _is_under(path, runs_dir) and low.startswith("s4-"):
        return "ALLOWED", "", "this audit's own output (runs/s4-*)"

    for g in allow_globs or []:
        if fnmatch.fnmatch(npath, _norm(g)) or fnmatch.fnmatch(low, g.lower()):
            return "ALLOWED", "", f"operator allow-glob ({g})"

    ext = os.path.splitext(low)[1]
    if ext in LEAK_EXTS:
        return "SUSPECT", "leak", f"AUDIO ARTIFACT written to disk ({ext})"
    if low == "core" or any(h in low for h in DUMP_HINTS):
        return "SUSPECT", "leak", "crash / core dump"
    if ext in TEXT_EXTS:
        return "SUSPECT", "leak", f"possible transcript/text artifact ({ext})"
    return "SUSPECT", "review", "unexpected write"


# ── filesystem watcher (polling, dependency-free, co-located) ─────────────────
class FsWatcher(threading.Thread):
    """Polls the watched roots and records every file created or modified since
    prime(). Polling (vs an inotify lib) keeps this dependency-free so it runs on a
    bare pod; a transient file is caught if it survives one poll interval. For
    sub-interval transient writes, use the strace deep mode (see module docstring)."""

    def __init__(self, roots: list[str], interval: float, exclude_dirs: set[str]):
        super().__init__(daemon=True)
        self.roots = sorted({_norm(r) for r in roots if os.path.isdir(r)})
        self.interval = interval
        self.exclude_dirs = exclude_dirs
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.baseline: dict[str, tuple[float, int]] = {}
        self.events: dict[str, str] = {}   # path -> "created" | "modified"
        self.scan_errors = 0

    def _scan(self) -> dict[str, tuple[float, int]]:
        snap: dict[str, tuple[float, int]] = {}
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(fp)
                        snap[_norm(fp)] = (st.st_mtime, st.st_size)
                    except OSError:
                        self.scan_errors += 1
        return snap

    def prime(self) -> None:
        self.baseline = self._scan()

    def _diff(self, snap: dict[str, tuple[float, int]]) -> None:
        with self._lock:
            for p, meta in snap.items():
                base = self.baseline.get(p)
                if base is None:
                    self.events.setdefault(p, "created")
                elif meta != base and self.events.get(p) != "created":
                    self.events[p] = "modified"

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self._diff(self._scan())
            self._stop_evt.wait(self.interval)

    def stop_and_collect(self) -> dict[str, str]:
        self._stop_evt.set()
        self.join(timeout=5)
        self._diff(self._scan())   # final pass — catch persistent files
        with self._lock:
            return dict(self.events)


# ── log-content scan ──────────────────────────────────────────────────────────
def scan_log(log_path: str, secrets: set[str]) -> list[dict] | None:
    """Return a list of hits (line numbers only — never the leaked text), or None
    if the log file is missing. A hit means a verbatim transcript/translation string
    appeared in the server log = a content leak."""
    candidates = [s.strip().lower() for s in secrets if len(s.strip()) >= SECRET_MIN_LEN]
    if not candidates:
        return []
    try:
        hits: list[dict] = []
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                ll = line.lower()
                for sec in candidates:
                    if sec in ll:
                        # Record the location only — NOT the line text (it contains
                        # the leaked content; persisting it here would re-leak it).
                        hits.append({"line": i, "leaked_chars": len(sec)})
                        break
        return hits
    except FileNotFoundError:
        return None


# ── live session driver ───────────────────────────────────────────────────────
async def drive_sessions(server: str, auth, clips: list[tuple[Path, str]],
                         src: str, tgt: str, timeout: float) -> tuple[set[str], list[dict]]:
    """Stream each clip through the running server. Returns (secrets, turns).
    secrets = the verbatim source/STT/MT strings to look for in the log; used
    in-memory only and never written to the report."""
    from benchmark_s1_telephony import stream_clip  # lazy: needs `websockets`
    secrets: set[str] = set()
    turns: list[dict] = []
    for path, ref in clips:
        r = await stream_clip(server, auth, path.read_bytes(), src, tgt, False, timeout)
        turn: dict = {"clip": path.stem, "ok": "error" not in r}
        if "error" in r:
            turn["error"] = r["error"]
        else:
            if ref:
                secrets.add(ref)
            for k in ("original", "translated"):
                if r.get(k):
                    secrets.add(r[k])
            # lengths only — no content in the artifact (CLAUDE.md §7)
            turn["original_len"] = len(r.get("original") or "")
            turn["translated_len"] = len(r.get("translated") or "")
            turn["audio_bytes"] = r.get("audio_bytes")
        turns.append(turn)
    return secrets, turns


# ── self-test (off-pod, no server/models/websockets) ──────────────────────────
def self_test() -> int:
    import tempfile

    print("S4 audit self-test — validating classifier + watcher + log scan (no server).")
    tmp = Path(tempfile.mkdtemp(prefix="s4-selftest-"))
    watch, models = tmp / "watch", tmp / "models"
    watch.mkdir()
    models.mkdir()
    pyc_dir = watch / "__pycache__"
    pyc_dir.mkdir()

    w = FsWatcher([str(watch), str(models)], interval=0.05,
                  exclude_dirs=set())  # don't exclude __pycache__ here: test it's ALLOWED
    w.prime()
    w.start()
    time.sleep(0.15)
    # ALLOWED writes
    (models / "opus-mt-es-en.bin").write_bytes(b"\x00" * 64)
    (pyc_dir / "mod.cpython-311.pyc").write_bytes(b"\x00" * 8)
    # SUSPECT writes
    (watch / "leaked_audio.wav").write_bytes(b"RIFF\x00\x00")
    (watch / "session_transcript.txt").write_text("hola", encoding="utf-8")
    (watch / "core").write_bytes(b"\x7fELF")
    time.sleep(0.25)
    events = w.stop_and_collect()

    classified = {p: classify(p, str(models), str(tmp), []) for p in events}
    suspects = {p for p, (v, _s, _r) in classified.items() if v == "SUSPECT"}
    allowed = {p for p, (v, _s, _r) in classified.items() if v == "ALLOWED"}

    ok = True
    exp_suspect = {_norm(str(watch / "leaked_audio.wav")),
                   _norm(str(watch / "session_transcript.txt")),
                   _norm(str(watch / "core"))}
    exp_allowed = {_norm(str(models / "opus-mt-es-en.bin")),
                   _norm(str(pyc_dir / "mod.cpython-311.pyc"))}

    missing_suspect = exp_suspect - suspects
    leaked_as_allowed = exp_suspect & allowed
    misflagged_allowed = exp_allowed & suspects
    if missing_suspect:
        print(f"  ✗ watcher/classifier missed SUSPECT files: {missing_suspect}")
        ok = False
    if leaked_as_allowed:
        print(f"  ✗ SUSPECT files misclassified ALLOWED: {leaked_as_allowed}")
        ok = False
    if misflagged_allowed:
        print(f"  ✗ ALLOWED files misclassified SUSPECT: {misflagged_allowed}")
        ok = False
    if ok:
        print(f"  ✓ watcher caught {len(events)} writes; classifier: "
              f"{len(suspects)} SUSPECT, {len(allowed)} ALLOWED (as expected)")

    # log-scan engine
    secret = "el cliente quiere revisar el saldo de su cuenta bancaria"
    clean = tmp / "clean.log"
    clean.write_text("2026 [INFO] linguabridge: Session opened: 1a2b3c4d | es->en\n"
                     "2026 [INFO] linguabridge: Session cleaned up: 1a2b3c4d\n", encoding="utf-8")
    dirty = tmp / "dirty.log"
    dirty.write_text(f"2026 [DEBUG] stt_worker: result -> {secret}\n", encoding="utf-8")

    clean_hits = scan_log(str(clean), {secret})
    dirty_hits = scan_log(str(dirty), {secret})
    if clean_hits != []:
        print(f"  ✗ log scan false-positive on clean log: {clean_hits}")
        ok = False
    elif not dirty_hits:
        print("  ✗ log scan MISSED a planted leak in the dirty log")
        ok = False
    elif any(secret.lower() in json.dumps(h).lower() for h in dirty_hits):
        # the hit payload must never carry the leaked text itself
        print("  ✗ log-scan hit payload contains the leaked text (must be redacted)")
        ok = False
    else:
        print(f"  ✓ log scan: clean→0 hits, dirty→{len(dirty_hits)} hit(s), payload redacted")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("✅ SELF-TEST PASS — S4 audit engines work; ready for the pod sitting."
                  if ok else "❌ SELF-TEST FAIL — fix the harness before trusting a live run."))
    return 0 if ok else 1


# ── main ──────────────────────────────────────────────────────────────────────
async def run_live(args) -> int:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from benchmark_s1_telephony import load_refs, make_auth_provider

    models_dir = args.models_dir or os.getenv("MODELS_DIR") or str(REPO_ROOT / "models")
    # Default roots = the server's own write surface. The global OS temp is NOT
    # watched by default (a polling watcher can't attribute writes to a process, so
    # on a shared host it catches cross-process noise). On the quiet pod, add the
    # real scratch dirs explicitly: --watch-root /tmp --watch-root /dev/shm.
    roots = [str(REPO_ROOT / "server"), str(REPO_ROOT), models_dir]
    roots.extend(args.watch_root or [])
    roots = sorted({r for r in roots if os.path.isdir(r)})

    clip_paths = sorted(Path(args.clip_dir).glob("*.pcm"))[: args.clips]
    if not clip_paths:
        print(f"ERROR: no .pcm clips in {args.clip_dir}", file=sys.stderr)
        return 1
    refs = load_refs(Path(args.manifest)) if Path(args.manifest).exists() else {}
    clips = [(p, refs.get(p.stem, "")) for p in clip_paths]
    auth = make_auth_provider(args.auth)

    allow_paths = []
    if args.server_log:
        allow_paths.append(args.server_log)

    print(f"S4 zero-persistence runtime audit → {args.server}")
    print(f"host={platform.node()}  watching {len(roots)} root(s):")
    for r in roots:
        print(f"    {r}")
    print(f"driving {len(clips)} session(s) from {args.clip_dir}  "
          f"({args.src}->{args.tgt})  poll={args.poll_interval}s")
    print(f"log scan: {args.server_log or 'SKIPPED (no --server-log → verdict caps at PARTIAL)'}")
    print("=" * 72)

    watcher = FsWatcher(roots, args.poll_interval, {".git", "__pycache__", "node_modules"})
    watcher.prime()
    watcher.start()
    secrets, turns = await drive_sessions(args.server, auth, clips, args.src, args.tgt, args.timeout)
    time.sleep(args.poll_interval * 3)   # let post-turn async cleanup settle
    events = watcher.stop_and_collect()

    classified = []
    for p, ev in sorted(events.items()):
        verdict_, severity_, reason_ = classify(p, models_dir, str(REPO_ROOT),
                                                allow_paths, args.allow_glob)
        classified.append({"path": p, "event": ev, "verdict": verdict_,
                           "severity": severity_, "reason": reason_})
    suspect = [c for c in classified if c["verdict"] == "SUSPECT"]
    leaks = [c for c in suspect if c["severity"] == "leak"]
    reviews = [c for c in suspect if c["severity"] == "review"]
    allowed = [c for c in classified if c["verdict"] == "ALLOWED"]
    fs_pass = not suspect

    log_hits = scan_log(args.server_log, secrets) if args.server_log else None
    if log_hits is None and args.server_log:
        print(f"WARN: --server-log {args.server_log} not found — log check skipped.", file=sys.stderr)
    log_pass = (log_hits == []) if log_hits is not None else None

    ok_turns = [t for t in turns if t.get("ok")]
    if fs_pass and log_pass is True:
        verdict, code = "PASS", 0
    elif fs_pass and log_pass is None:
        verdict, code = "PARTIAL", 2
    else:
        verdict, code = "FAIL", 1
    if not ok_turns:
        verdict, code = "FAIL", 1   # nothing actually exercised the pipeline

    # ── console ───────────────────────────────────────────────────────────────
    print(f"\nsessions driven: {len(turns)}  ok: {len(ok_turns)}  "
          f"secrets tracked: {len(secrets)}")
    print(f"filesystem: {len(events)} write(s) observed → {len(allowed)} ALLOWED, "
          f"{len(leaks)} LEAK, {len(reviews)} REVIEW")
    for c in leaks:
        print(f"    ✗ LEAK [{c['event']}] {c['reason']}: {c['path']}")
    for c in reviews:
        print(f"    ⚠ REVIEW [{c['event']}] {c['reason']}: {c['path']}  "
              f"(clear with --allow-glob if benign)")
    if args.verbose:
        for c in allowed:
            print(f"    · allowed [{c['event']}] {c['reason']}: {c['path']}")
    if log_pass is None:
        print("log scan: SKIPPED")
    elif log_pass:
        print(f"log scan: clean — 0 verbatim transcript/translation hits in {args.server_log}")
    else:
        lines = ", ".join(str(h["line"]) for h in log_hits)
        print(f"    ✗ LOG LEAK: {len(log_hits)} verbatim hit(s) at line(s) {lines} "
              f"(inspect {args.server_log} directly — text intentionally not copied here)")

    verdict_line = {
        "PASS": "✅ S4 RUNTIME PROOF PASS — no audio/transcript persisted, no log leakage.",
        "PARTIAL": "🟡 S4 PARTIAL — filesystem clean, but provide --server-log to clear the log check.",
        "FAIL": "❌ S4 FAIL — see SUSPECT writes / log hits above. Fix before any real customer audio.",
    }[verdict]
    print("\n" + verdict_line)

    # ── report (NO transcript content — counts/paths/lines only) ──────────────
    report = {
        "audit": "s4_persistence_runtime",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "server": args.server,
        "direction": f"{args.src}->{args.tgt}",
        "models_dir": models_dir,
        "watch_roots": roots,
        "poll_interval_s": args.poll_interval,
        "sessions": {"driven": len(turns), "ok": len(ok_turns), "turns": turns},
        "secrets_tracked": len(secrets),
        "filesystem": {
            "writes_observed": len(events),
            "allowed_count": len(allowed),
            "leak": leaks,
            "review": reviews,
            "allowed": allowed,
            "scan_errors": watcher.scan_errors,
        },
        "log_scan": {
            "log_path": args.server_log,
            "ran": log_pass is not None,
            "hits": log_hits if log_hits is not None else [],
            "note": "hits carry line numbers only; leaked text is never copied into this artifact",
        },
        "fs_pass": fs_pass,
        "log_pass": log_pass,
        "verdict": verdict,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = REPO_ROOT / "runs" / f"s4-persistence-{ts}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport → {out}")
    return code


def run_monitor_only(args) -> int:
    """FS-watch-only mode: this process does NOT drive sessions — an external client
    does (e.g. the desktop probe or a remote benchmark through the proxy). Prime the
    watcher, hold for --hold seconds while the operator drives real traffic, then
    classify writes. The log-content scan can't run here (we don't know the driven
    transcripts), so the verdict caps at PARTIAL. Use when audio clips can't be
    co-located with the server (e.g. on a pod reachable only via a proxy)."""
    models_dir = args.models_dir or os.getenv("MODELS_DIR") or "/models"
    roots = sorted({r for r in (list(args.watch_root or []) + [models_dir])
                    if os.path.isdir(r)})
    if not roots:
        print("ERROR: --monitor-only needs at least one existing --watch-root", file=sys.stderr)
        return 1
    repo_root = os.getenv("S4_REPO_ROOT") or os.path.dirname(models_dir) or "/"

    print("S4 zero-persistence MONITOR-ONLY — drive traffic externally")
    print(f"host={platform.node()}  hold={args.hold}s  watching {len(roots)} root(s):")
    for r in roots:
        print(f"    {r}")
    print("=" * 72)

    watcher = FsWatcher(roots, args.poll_interval, {".git", "__pycache__", "node_modules"})
    watcher.prime()
    watcher.start()
    print(f"watching… DRIVE SESSIONS NOW — collecting for {args.hold}s", flush=True)
    time.sleep(args.hold)
    events = watcher.stop_and_collect()

    classified = []
    for p, ev in sorted(events.items()):
        v, sev, reason = classify(p, models_dir, repo_root, [], args.allow_glob)
        classified.append({"path": p, "event": ev, "verdict": v,
                           "severity": sev, "reason": reason})
    suspect = [c for c in classified if c["verdict"] == "SUSPECT"]
    leaks = [c for c in suspect if c["severity"] == "leak"]
    reviews = [c for c in suspect if c["severity"] == "review"]
    allowed = [c for c in classified if c["verdict"] == "ALLOWED"]
    fs_pass = not suspect

    print(f"\nfilesystem: {len(events)} write(s) → {len(allowed)} ALLOWED, "
          f"{len(leaks)} LEAK, {len(reviews)} REVIEW")
    for c in leaks:
        print(f"    ✗ LEAK [{c['event']}] {c['reason']}: {c['path']}")
    for c in reviews:
        print(f"    ⚠ REVIEW [{c['event']}] {c['reason']}: {c['path']}")
    if args.verbose:
        for c in allowed:
            print(f"    · allowed [{c['event']}] {c['reason']}: {c['path']}")

    verdict, code = ("PARTIAL", 2) if fs_pass else ("FAIL", 1)
    print("\n" + {
        "PARTIAL": "🟡 S4 FS-CLEAN (monitor-only — no co-located log scan → PARTIAL). "
                   "Zero audio/transcript writes observed under live load.",
        "FAIL": "❌ S4 FAIL — SUSPECT writes above. Fix before any real customer audio.",
    }[verdict])

    report = {
        "audit": "s4_persistence_monitor_only",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "hold_s": args.hold,
        "watch_roots": roots,
        "poll_interval_s": args.poll_interval,
        "filesystem": {"writes_observed": len(events), "allowed_count": len(allowed),
                       "leak": leaks, "review": reviews, "allowed": allowed,
                       "scan_errors": watcher.scan_errors},
        "fs_pass": fs_pass, "log_pass": None, "verdict": verdict,
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(os.getenv("S4_OUT_DIR", "/tmp")) / f"s4-persistence-{ts}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport → {out}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="validate the audit engines off-pod (no server/models/websockets)")
    ap.add_argument("--server", default=os.getenv("SERVER_URL", "ws://localhost:8000/ws/translate"))
    ap.add_argument("--auth", default=os.getenv("AUTH_TOKEN", "test"),
                    help="static token; ignored when AUTH_SIGNING_KEY is set (mints per-conn)")
    ap.add_argument("--server-log", default=os.getenv("SERVER_LOG"),
                    help="path the server's stdout is tee'd to — required for the log-content check")
    ap.add_argument("--watch-root", action="append",
                    help="extra dir to monitor (repeatable); on Linux add /tmp and /dev/shm")
    ap.add_argument("--allow-glob", action="append",
                    help="path/name glob to clear a reviewed-benign write (repeatable)")
    ap.add_argument("--models-dir", default=None, help="allowlisted model cache root (default: $MODELS_DIR)")
    ap.add_argument("--clip-dir", default="samples/realtime-pcm")
    ap.add_argument("--manifest", default="samples/stage1-benchmark.json")
    ap.add_argument("--clips", type=int, default=3, help="how many clips to drive")
    ap.add_argument("--src", default="es")
    ap.add_argument("--tgt", default="en")
    ap.add_argument("--poll-interval", type=float, default=0.1, help="watcher poll seconds")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--verbose", action="store_true", help="also print ALLOWED writes")
    ap.add_argument("--monitor-only", action="store_true",
                    help="FS-watch only; drive sessions externally (e.g. via proxy). Caps at PARTIAL.")
    ap.add_argument("--hold", type=float, default=60.0,
                    help="--monitor-only: seconds to watch while traffic is driven externally")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.monitor_only:
        return run_monitor_only(args)
    return asyncio.run(run_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
