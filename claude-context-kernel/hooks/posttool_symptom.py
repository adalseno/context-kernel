#!/usr/bin/env python3
"""
posttool_symptom.py — PostToolUse hook: T2 AMBIENTALE sui TEST FALLITI.

Gemello di symptom_slice.py sull'altro lato del turno: la' il sintomo arriva
nel prompt dell'utente, qui compare nell'OUTPUT di un Bash (pytest rosso,
traceback di uno script, `go test` che fallisce). Quando succede, lo slicer
deterministico (T2, cachato) calcola il working set e lo inietta come
contesto aggiuntivo: il modello ha il manifest davanti nel momento esatto
in cui inizia il debug.

Piu' conservativo del gemello: qui i falsi positivi costano di piu' (un grep
su fixture che CONTENGONO un traceback non e' un test fallito), quindi:
  - servono firme di FALLIMENTO vero (traceback, FAILED, --- FAIL:), non
    basta una parola "error";
  - i comandi di sola lettura (grep, cat, tail, ...) sono esclusi;
  - lo stesso fallimento non viene ri-iniettato (dedup su hash, TTL).
Mai fatale: su qualsiasi imprevisto stampa "{}" ed esce 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time

# riusa soglie, firma del repo e path dello slicer dal gemello UserPromptSubmit
try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass
import symptom_slice as _sym

ENABLED = os.environ.get("CK_POST_SYMPTOM", "1") != "0"
TTL_S = int(os.environ.get("CK_POST_SYMPTOM_TTL", "600"))
STATE = os.path.expanduser(
    os.environ.get("CK_POST_SYMPTOM_STATE", "~/.context-kernel-posttool.json"))
RAW_MARK = os.environ.get("CK_RAW_MARK", "# ck:raw")

# Firme di FALLIMENTO vero: un output di test andato male, non una riga che
# parla di errori. Meglio un falso negativo che rumore dopo ogni Bash.
FAILURE = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^FAILED [\w/.-]+::", re.MULTILINE),          # pytest summary
    re.compile(r"^=+ FAILURES =+$", re.MULTILINE),            # pytest banner
    re.compile(r"^=+ ERRORS =+$", re.MULTILINE),
    re.compile(r"^--- FAIL: ", re.MULTILINE),                 # go test
    re.compile(r"^FAIL\s+[\w@/.-]+\s*\(", re.MULTILINE),      # jest/vitest suite
    re.compile(r"^\s*\d+\) .+\n\s+\w*(?:Assertion)?Error:", re.MULTILINE),  # mocha
]

# Comandi di sola lettura: il loro output puo' CITARE un traceback (fixture,
# log vecchi, sorgenti dei test) senza che nulla sia fallito ORA.
READONLY_CMD = re.compile(
    r"^\s*(?:command\s+)?(?:grep|rg|ugrep|cat|head|tail|less|more|find|ls|"
    r"git\s+(?:log|show|diff|blame)|rtk\s+(?:grep|cat|log))\b")

# `cd repo && pytest` esegue ALTROVE: il payload porta il cwd di SESSIONE,
# ma il repo da affettare e' quello del cd. Lo stesso prefisso aggirava la
# guardia read-only (`cd x && grep ...` non inizia con grep): valutarla sul
# comando DOPO i cd chiude anche quel punto cieco.
CD_PREFIX = re.compile(r"""^\s*cd\s+("[^"]+"|'[^']+'|[^\s;&|]+)\s*(?:&&|;)\s*""")


def effective_cwd_and_cmd(cmd: str, cwd: str) -> tuple[str, str]:
    """Risolve i prefissi `cd DIR &&`/`;` (anche ripetuti) e ritorna
    (directory effettiva, comando rimanente)."""
    base, rest = cwd, cmd
    while True:
        m = CD_PREFIX.match(rest)
        if not m:
            return base, rest
        target = os.path.expanduser(os.path.expandvars(m.group(1).strip("\"'")))
        base = target if os.path.isabs(target) else os.path.join(base, target)
        rest = rest[m.end():]


def has_failure(text: str) -> bool:
    return any(rx.search(text) for rx in FAILURE)


def _state_load() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:                          # noqa: BLE001
        pass
    return {}


def _state_save(st: dict) -> None:
    try:
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni le ultime 8 sessioni
        tmp = f"{STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception:                          # noqa: BLE001
        pass


def already_injected(session: str, digest: str) -> bool:
    """Lo stesso fallimento (stessa coda di output) entro il TTL: tacere.
    Il modello ha gia' il manifest; ri-iniettarlo a ogni rilancio dei test
    sarebbe rumore."""
    rec = _state_load().get(session)
    return (rec is not None and rec.get("hash") == digest
            and time.time() - rec.get("ts", 0) < TTL_S)


def remember(session: str, digest: str) -> None:
    st = _state_load()
    st[session] = {"hash": digest, "ts": time.time()}
    _state_save(st)


def main() -> int:
    if not ENABLED:
        print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0

    try:
        if payload.get("tool_name") != "Bash" or payload.get("agent_id"):
            print("{}")                        # solo Bash della sessione madre
            return 0
        tin = payload.get("tool_input") or {}
        cmd = str(tin.get("command") or "")
        run_dir, core_cmd = effective_cwd_and_cmd(
            cmd, payload.get("cwd") or os.getcwd())
        if (READONLY_CMD.match(core_cmd) or (RAW_MARK and RAW_MARK in cmd)
                or "repo_slice" in cmd):
            print("{}")
            return 0

        resp = payload.get("tool_response") or {}
        text = ""
        if isinstance(resp, str):
            text = resp
        elif isinstance(resp, dict):
            text = "\n".join(v for v in (resp.get("stdout"), resp.get("stderr"))
                             if isinstance(v, str) and v.strip())
        if not text.strip() or not has_failure(text):
            print("{}")
            return 0

        cwd = os.path.normpath(run_dir)
        if not os.path.isdir(cwd) or not _sym.repo_big_enough(cwd):
            print("{}")
            return 0

        # il traceback vive in coda: e' la coda che identifica il fallimento
        # ed e' la coda che fa da sintomo per lo slicer
        tail = text[-8000:]
        session = (payload.get("session_id")
                   or os.path.basename(payload.get("transcript_path") or "-")[:8])
        digest = hashlib.sha1(tail[-4000:].encode("utf-8", "replace")).hexdigest()[:12]
        if already_injected(session, digest):
            print("{}")
            return 0

        proc = subprocess.run(
            [sys.executable, _sym.slicer_path(), cwd,
             "--symptom", tail, "--budget", "auto"],
            capture_output=True, text=True, timeout=_sym.TIMEOUT,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or "## seed" not in out:
            print("{}")                        # niente seed: meglio tacere
            return 0
        head = "\n".join(out.split("\n")[:_sym.MAX_LINES])
        ctx = ("[context-kernel] Failure detected in the command output: "
               "working set computed by the deterministic slicer (T2). Use it "
               "as a prior for debugging, not as a prohibition; to recompute "
               "it with different parameters there is the kernel-repo-slice "
               "skill.\n" + head)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": ctx,
        }}))
        remember(session, digest)
        # il working set attivo va registrato anche da qui: il rilevatore di
        # cambio-task e la compaction devono vedere l'ULTIMO Q, da qualunque
        # lato del turno sia arrivato il sintomo
        _sym.task_remember(_sym.hook_session(payload), cwd, out)
        print(f"context-kernel[posttool]: slice injected from {cwd}",
              file=sys.stderr)
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
