#!/usr/bin/env python3
"""
session_brief.py — SessionStart hook: UNA riga di consapevolezza (~40 token).

T1 e' invisibile per design, ma un modello che SA di vivere in un ambiente
compresso ne usa i meccanismi (page fault, slice ambientale) invece di
subirli: senza questo brief e' capitato che il modello giudicasse il plugin
"mai usato" mentre gli aveva risparmiato 277k token. Mai fatale.
"""
from __future__ import annotations

import json
import os
import sys

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass

ENABLED = os.environ.get("CK_BRIEF", "1") != "0"
LOG_PATH = os.path.expanduser(
    os.environ.get("CK_LOG", "~/.context-kernel-savings.log"))
AB_STATE = os.path.expanduser(
    os.environ.get("CK_AB_STATE", "~/.context-kernel-ab.json"))
CANARY_STATE = os.path.expanduser(
    os.environ.get("CK_CANARY_STATE", "~/.context-kernel-canary.json"))
# Snapshot TS(Q) scritto da precompact_snapshot.py: alla SessionStart con
# source=="compact" viene reiniettato qui — la sessione post-compact riparte
# col task state (carta T3 + working set T2), non col solo riassunto.
COMPACT_STATE = os.path.expanduser(
    os.environ.get("CK_COMPACT_STATE", "~/.context-kernel-compact.json"))
COMPACT_MAX_AGE_S = int(os.environ.get("CK_COMPACT_MAX_AGE", "1800"))
# Snapshot TS(Q) scritto da session_end_snapshot.py alla SessionEnd, con
# chiave il REPO: alla SessionStart successiva sullo stesso repo (source
# startup/resume) viene reiniettato se fresco — il task sopravvive anche al
# riavvio, non solo alla compaction.
RESUME_STATE = os.path.expanduser(
    os.environ.get("CK_RESUME_STATE", "~/.context-kernel-resume.json"))
RESUME_MAX_AGE_S = int(os.environ.get("CK_RESUME_MAX_AGE", "86400"))


def savings_line() -> str:
    """Totale storico dal ledger CSV (ts,tool,before,after,saved,session)."""
    try:
        n = saved = 0
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 5:
                    n += 1
                    saved += int(parts[4])
        if n:
            return f" So far: {n} compressions, ~{saved:,} tokens saved."
    except Exception:                          # noqa: BLE001
        pass
    return ""


def ab_line() -> str:
    """Promemoria: campioni A/B fermi in attesa del giudizio. ab_verify.py e'
    manuale (o cron): senza questa riga i campioni restano li' per sempre."""
    try:
        with open(AB_STATE, encoding="utf-8") as f:
            n = len(json.load(f).get("pending") or [])
        if n:
            root = (os.environ.get("CLAUDE_PLUGIN_ROOT")
                    or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            return (f" A/B: {n} samples awaiting judgement — `python3 "
                    f"{os.path.join(root, 'hooks', 'ab_verify.py')}`.")
    except Exception:                          # noqa: BLE001
        pass
    return ""


def canary_line() -> str:
    """Failure canary aperti: il brief li CONTESTUALIZZA invece di lasciare il
    solo ⚠ in statusline — con l'evidenza (verified consecutive dopo l'ultimo
    failure) si vede subito se e' un problema vivo o un residuo che l'auto-ack
    riconoscera' da solo. Muta quando non c'e' nulla di aperto."""
    try:
        with open(CANARY_STATE, encoding="utf-8") as f:
            st = json.load(f)
        fl = st.get("failed", 0)
        if not fl:
            return ""
        streak = st.get("heal_streak", 0)
        ndeg = len(st.get("degraded_sessions", []))
        deg = f", {ndeg} sessions auto-degraded" if ndeg else ""
        return (f" Canary: {fl} open failures "
                f"(last: {st.get('last_failure')}), {streak} compressions "
                f"verified OK since{deg} — if the evidence keeps up they "
                "auto-acknowledge; if they reappear, investigate.")
    except Exception:                          # noqa: BLE001
        pass
    return ""


def compact_restore(payload: dict) -> str:
    """TS(Q) fotografato da precompact_snapshot.py: se questa SessionStart
    viene da una compaction, riportalo nel contesto. Vuoto se non c'e' nulla
    (o lo snapshot e' vecchio: un'altra faccenda, non questo task)."""
    try:
        import time
        with open(COMPACT_STATE, encoding="utf-8") as f:
            st = json.load(f)
        session = (payload.get("session_id")
                   or os.path.basename(payload.get("transcript_path") or "-")[:8])
        rec = st.get(session)
        if not rec:                            # fallback: lo snapshot piu' recente
            rec = max(st.values(), key=lambda r: r.get("ts", 0), default=None)
        if not rec or time.time() - rec.get("ts", 0) > COMPACT_MAX_AGE_S:
            return ""
        parts = ["\n[context-kernel] TS(Q) survived the compaction — "
                 "the summary is a projection NOT indexed by the task; "
                 "this is the task state snapshotted beforehand:"]
        if rec.get("charter_head"):
            parts.append("--- active task charter (T3) ---\n"
                         + rec["charter_head"])
        if rec.get("slice_head"):
            parts.append("--- active working set (T2) ---\n"
                         + rec["slice_head"])
        return "\n".join(parts)
    except Exception:                          # noqa: BLE001
        return ""


def resume_restore(payload: dict) -> str:
    """TS(Q) della sessione PRECEDENTE su questo repo (session_end_snapshot):
    reiniettato a startup/resume se fresco. La carta viene mostrata solo se
    ANCORA attiva (un `charter.py clear` nel frattempo la fa sparire anche da
    qui). Vuoto se non c'e' nulla di fresco."""
    try:
        import time
        with open(RESUME_STATE, encoding="utf-8") as f:
            st = json.load(f)
        cwd = os.path.normpath(payload.get("cwd") or os.getcwd())
        rec = st.get(cwd)
        if not rec:                            # repo antenato del cwd
            for root, r in st.items():
                if cwd.startswith(root.rstrip(os.sep) + os.sep):
                    rec = r
                    break
        if not rec or time.time() - rec.get("ts", 0) > RESUME_MAX_AGE_S:
            return ""
        charter_head = rec.get("charter_head") or ""
        if charter_head:
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import charter as _charter
                if not _charter.get_for_repo(cwd):
                    charter_head = ""          # carta pulita nel frattempo
            except Exception:                  # noqa: BLE001
                pass
        slice_head = rec.get("slice_head") or ""
        if not charter_head and not slice_head:
            return ""
        parts = ["\n[context-kernel] TS(Q) from the previous session on this "
                 "repo (a restart is a discontinuity just like a compaction — "
                 "the task state survives both). If the task has changed, "
                 "ignore this and optionally clear it with charter.py clear:"]
        if charter_head:
            parts.append("--- active task charter (T3) ---\n" + charter_head)
        if slice_head:
            parts.append("--- working set (T2) from the last session ---\n"
                         + slice_head)
        return "\n".join(parts)
    except Exception:                          # noqa: BLE001
        return ""


def main() -> int:
    if not ENABLED:
        print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)         # contratto: JSON su stdin
        if not isinstance(payload, dict):
            payload = {}
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0
    ctx = (
        "[context-kernel] active: long tool outputs arrive compressed "
        "(footer `[context-kernel: ...]`). Page fault: if a Read arrives "
        "ELIDED or marked UNCHANGED, reading the same file again lets it "
        "through in full. For bugs with a concrete symptom there is the "
        "kernel-repo-slice skill (T2); with a traceback in the prompt the "
        "slice is injected automatically." + savings_line() + ab_line()
        + canary_line()
    )
    if payload.get("source") == "compact":
        ctx += compact_restore(payload)
    else:                                      # startup/resume/clear
        ctx += resume_restore(payload)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
