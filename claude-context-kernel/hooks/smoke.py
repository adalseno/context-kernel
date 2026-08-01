#!/usr/bin/env python3
"""
smoke.py — the LIVE verification ritual, scripted (1.17.0).

The empirical fact this file institutionalises: EVERY live check of a release
has found bugs that 300+ tests did not see (additionalContext ignored, canary
vs parking, fixtures in the real store, ...) — because the tests exercise the
operators, while the ritual exercises the CONTRACT with the real harness.
Hence: a release is not "green" until the smoke passes in a real session.

A TWO-command protocol, to be run in a live Claude Code session on this repo
(plain Bash, without `# ck:raw` on the generate step):

    python3 hooks/smoke.py generate    # 400 lines with a NEEDLE COMPUTED at
                                       # runtime (never in the command, never
                                       # in the context) — the hook compresses it
    python3 hooks/smoke.py check       # checks against the REAL TRANSCRIPT
                                       # what the harness ACTUALLY did

What `check` asserts (PASS/FAIL per item, exit != 0 on any FAIL):
  1. the generate tool_result is in the session transcript;
  2. it is the COMPRESSED version (footer present: updatedToolOutput honoured);
  3. the needle was ELIDED from the context;
  4. the footer declares the parking spot and the key;
  5. the key exists in the parking store;
  6. recall.py KEY --grep finds the needle, numbered (inverse page fault);
  7. the canary accumulated no NEW failures since generate (no false alarms
     on the contract);
  8. a 4-point advisor check against the session's REAL context state (warning
     at a low threshold; one-shot; subagent silent; high threshold silent) —
     declared SKIP if the session tap has not been written yet.

DECLARED coverage: the ephemeral leg is Bash (representative: same parking
path as WebFetch/MCP); a real compact, resume and the guards stay in the
manual ritual (they need harness events that cannot be scripted from here).

State between the two commands: CK_SMOKE_STATE (default ~/.context-kernel-smoke
.json) — unique batch id, needle, canary snapshot. Zero network, zero API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host
    pass

HOOKS = os.path.dirname(os.path.abspath(__file__))
SMOKE_STATE = os.path.expanduser(
    os.environ.get("CK_SMOKE_STATE", "~/.context-kernel-smoke.json"))
PARK_STATE = os.path.expanduser(
    os.environ.get("CK_PARK_STATE", "~/.context-kernel-park.json"))
CANARY_STATE = os.path.expanduser(
    os.environ.get("CK_CANARY_STATE", "~/.context-kernel-canary.json"))
CONTEXT_STATE = os.path.expanduser(
    os.environ.get("CK_CONTEXT_STATE", "~/.context-kernel-context.json"))
TRANSCRIPTS = os.path.expanduser(
    os.environ.get("CK_SMOKE_TRANSCRIPTS", "~/.claude/projects"))
RECENT_S = 2 * 3600                        # transcript piu' vecchi: fuori
N_LINES = 400
NEEDLE_AT = 237                            # 1-based, in mezzo al rumore


def _canary_failed() -> int:
    try:
        with open(CANARY_STATE, encoding="utf-8") as f:
            return int((json.load(f) or {}).get("failed", 0))
    except Exception:                      # noqa: BLE001
        return 0


def generate() -> int:
    """Emette il lotto sintetico con l'ago calcolato e salva lo stato."""
    seed = f"{time.time_ns()}-{os.getpid()}"
    digest = hashlib.sha1(seed.encode()).hexdigest()
    run_id = digest[:8]
    # ago DECIMALE (niente hex: non deve somigliare a hash/segnale) e
    # formulazione senza parole di segnale (error/warn/fail/path)
    needle = f"sentinel-{int(digest, 16) % 100_000:05d}"
    state = {"ts": time.time(), "id": run_id, "needle": needle,
             "canary_failed": _canary_failed()}
    tmp = f"{SMOKE_STATE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, SMOKE_STATE)
    print(f"smoke context-kernel — batch {run_id} — start")
    for i in range(2, N_LINES):
        if i == NEEDLE_AT:
            print(f"line {i:03d} — {needle} recorded in the nightly batch")
        else:
            print(f"line {i:03d} — batch processing completed with no changes")
    print(f"smoke context-kernel — batch {run_id} — end")
    return 0


def _result_text(obj: dict) -> str | None:
    """Testo del tool_result in una riga di transcript gia' parsata."""
    for c in (obj.get("message") or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            cc = c.get("content")
            if isinstance(cc, str):
                return cc
            if isinstance(cc, list):
                return "\n".join(b.get("text", "") for b in cc
                                 if isinstance(b, dict)
                                 and b.get("type") == "text")
    return None


def _find_result(run_id: str) -> tuple[str, str] | None:
    """(transcript_path, testo del tool_result del generate) — cerca il
    lotto per id nei transcript recenti, dal piu' fresco."""
    marker = f"batch {run_id}"
    cands = []
    for base, _dirs, files in os.walk(TRANSCRIPTS):
        for fn in files:
            if fn.endswith(".jsonl"):
                p = os.path.join(base, fn)
                try:
                    if time.time() - os.path.getmtime(p) < RECENT_S:
                        cands.append(p)
                except OSError:
                    pass
    for p in sorted(cands, key=os.path.getmtime, reverse=True):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if marker not in line or "tool_result" not in line:
                        continue
                    try:
                        text = _result_text(json.loads(line))
                    except Exception:      # noqa: BLE001
                        continue
                    if text and marker in text:
                        return p, text
        except OSError:
            continue
    return None


def _advisor_checks(transcript: str) -> tuple[str, list[str]]:
    """('PASS'|'SKIP'|'FAIL', dettagli) — advisor sul context state reale."""
    sid = os.path.basename(transcript)[:-6][:8]
    try:
        with open(CONTEXT_STATE, encoding="utf-8") as f:
            rec = (json.load(f) or {}).get(sid) or {}
        if int(rec.get("context_tokens") or 0) <= 0:
            return "SKIP", ["session tap not written yet"]
    except Exception:                      # noqa: BLE001
        return "SKIP", ["context state missing"]
    adv = os.path.join(HOOKS, "compact_advisor.py")
    iso = f"{SMOKE_STATE}.advise.{os.getpid()}"
    payload = json.dumps({"tool_name": "Bash", "transcript_path": transcript})

    def run(threshold: str, pl: str = payload, state: str | None = None) -> str:
        # Sonda il meccanismo FISSO dell'avviso in modo deterministico: l'adatta-
        # mento sulla lifetime (CK_COMPACT_ADAPT) leggerebbe il fault log REALE e
        # renderebbe il probe non riproducibile. La soglia adattiva ha i suoi test.
        env = {**os.environ, "CK_COMPACT_ADVISE": threshold,
               "CK_COMPACT_ADAPT": "0",
               "CK_ADVISE_STATE": state or iso}
        try:
            return subprocess.run(
                [sys.executable, adv], input=pl, capture_output=True,
                text=True, env=env, timeout=30).stdout.strip()
        except Exception:                  # noqa: BLE001
            return "<subprocess error>"

    details, ok = [], True
    first = run("0.05")
    if "additionalContext" in first and "/compact" in first:
        details.append("warning at low threshold: PASS")
    else:
        details.append(f"warning at low threshold: FAIL ({first[:80]})")
        ok = False
    if run("0.05") == "{}":
        details.append("one-shot per session: PASS")
    else:
        details.append("one-shot per session: FAIL")
        ok = False
    sub = json.dumps({"tool_name": "Bash", "transcript_path": transcript,
                      "agent_id": "smoke-sub"})
    if run("0.05", pl=sub, state=iso + ".b") == "{}":
        details.append("subagent silent: PASS")
    else:
        details.append("subagent silent: FAIL")
        ok = False
    if run("0.99", state=iso + ".c") == "{}":
        details.append("high threshold silent: PASS")
    else:
        details.append("high threshold silent: FAIL")
        ok = False
    for suffix in ("", ".b", ".c"):
        try:
            os.unlink(iso + suffix)
        except OSError:
            pass
    return ("PASS" if ok else "FAIL"), details


def check() -> int:
    try:
        with open(SMOKE_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:                      # noqa: BLE001
        print("FAIL  smoke state missing: run `smoke.py generate` first")
        return 1
    run_id, needle = st["id"], st["needle"]
    results: list[tuple[str, str]] = []

    found = _find_result(run_id)
    if not found:
        print(f"FAIL  batch {run_id} not found in any recent transcript "
              f"under {TRANSCRIPTS} — different session, or the result "
              "has not been written (yet)")
        return 1
    transcript, text = found
    results.append(("PASS", f"batch {run_id} found in transcript "
                    f"{os.path.basename(transcript)}"))

    if "[context-kernel:" in text and "tokens, -" in text:
        results.append(("PASS", "tool_result COMPRESSED in the transcript "
                        "(updatedToolOutput honoured by the harness)"))
    else:
        results.append(("FAIL", "tool_result FULL in the transcript: "
                        "the harness ignored updatedToolOutput"))
    if needle not in text:
        results.append(("PASS", "needle elided from the context"))
    else:
        results.append(("FAIL", "needle still present: no elision "
                        "(thresholds? plugin disabled?)"))

    m = re.search(r"parked: python3 .*recall\.py\"? ([0-9a-f]{10})", text)
    key = m.group(1) if m else None
    if key:
        results.append(("PASS", f"footer declares the parking spot (key {key})"))
        try:
            with open(PARK_STATE, encoding="utf-8") as f:
                in_store = key in (json.load(f) or {})
        except Exception:                  # noqa: BLE001
            in_store = False
        results.append(("PASS", "key present in the store") if in_store
                       else ("FAIL", "key missing from the parking store"))
        rec = subprocess.run(
            [sys.executable, os.path.join(HOOKS, "recall.py"),
             key, "--grep", "sentinel"],
            capture_output=True, text=True, timeout=30)
        if needle in rec.stdout and str(NEEDLE_AT) in rec.stdout:
            results.append(("PASS", "recall --grep finds the needle, numbered "
                            "(inverse page fault working)"))
        else:
            results.append(("FAIL", "recall does not find the needle "
                            f"({rec.stdout[:80]!r})"))
    else:
        results.append(("FAIL", "footer without a parking hint"))

    failed_now = _canary_failed()
    if failed_now <= st.get("canary_failed", 0):
        results.append(("PASS", "canary: no new failures since generate"))
    else:
        results.append(("FAIL", f"canary: failed {st.get('canary_failed', 0)}"
                        f" -> {failed_now} — investigate BEFORE trusting the ledger"))

    verdict, details = _advisor_checks(transcript)
    results.append((verdict, "advisor (4 points): " + "; ".join(details)))

    bad = 0
    for v, msg in results:
        print(f"{v:5s} {msg}")
        bad += int(v == "FAIL")
    print(f"\nsmoke: {len(results) - bad}/{len(results)} points passed"
          + ("" if not bad else f", {bad} FAILED"))
    return 1 if bad else 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "generate":
        return generate()
    if cmd == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
