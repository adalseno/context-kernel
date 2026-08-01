#!/usr/bin/env python3
"""
charter_guard.py — PreToolUse hook su Edit/Write E Bash: la CARTA DEL TASK
(T3) da checklist post-hoc a INVARIANTE ATTIVO.

Quando un Edit/Write sta per toccare un file CITATO in un vincolo della
carta attiva (salvata con charter.py), il vincolo viene iniettato come
contesto PRIMA della modifica: il modello ce l'ha davanti mentre scrive,
e il verifier T4 diventa la verifica di qualcosa di gia' visto.

Su Bash chiude la SCAPPATOIA: un file citato si modifica benissimo anche
da shell (sed -i, tee, redirect, mv/rm, git checkout/restore) e la guardia
sugli editor non lo vedrebbe mai. Il riconoscimento e' conservativo: solo
comandi che matchano un pattern di SCRITTURA noto E nominano un file
citato; meglio un falso negativo che rumore su ogni ls.

Conservativo: nessuna carta -> no-op; file non citato -> no-op; stesso
file gia' segnalato di recente -> no-op (dedup TTL: un refactoring lungo
sullo stesso file non deve ricevere lo stesso vincolo a ogni Edit).
Se l'harness ignorasse additionalContext su PreToolUse sarebbe un no-op
silenzioso: non blocca ne' modifica mai la chiamata. Mai fatale.
VERIFICATO DAL VIVO (2026-07-17, Claude Code): il contratto e' onorato —
i vincoli della carta arrivano al modello PRIMA dell'Edit del file citato,
e il dedup TTL evita la ripetizione al secondo Edit consecutivo.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass
import charter

ENABLED = os.environ.get("CK_GUARD", "1") != "0"
BASH_ENABLED = os.environ.get("CK_GUARD_BASH", "1") != "0"
TTL_S = int(os.environ.get("CK_GUARD_TTL", "600"))
MAX_VINCOLI = int(os.environ.get("CK_GUARD_MAX", "8"))
STATE = os.path.expanduser(
    os.environ.get("CK_GUARD_STATE", "~/.context-kernel-guard.json"))

TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Pattern di SCRITTURA da shell: la guardia scatta solo se il comando ne
# matcha uno E nomina un file citato dalla carta. Lista chiusa e leggibile:
# niente euristica generica sui comandi sconosciuti.
WRITE_CMD = re.compile(
    r"(?:\bsed\b[^|;&]*\s-[a-zA-Z]*i|"         # sed -i / -i.bak / -ri
    r"\bperl\b[^|;&]*\s-[a-zA-Z]*i|"           # perl -pi -e
    r"\btee\b|"
    r"\bmv\b|\bcp\b|\brm\b|"
    r"\btruncate\b|"
    r"\bdd\b[^|;&]*\bof=|"
    r"\bgit\s+(?:checkout|restore)\b|"
    r"(?<![-=>])>{1,2})")                      # redirect > e >> (non -> o =>)

# Redirect "rumore" da scartare PRIMA del match: non scrivono su nessun
# file citabile. `2>/dev/null`, `>>/dev/null`, `2>&1`, `> NUL` (Windows).
# Senza questo filtro un innocuo `grep pattern file_citato 2>/dev/null`
# fa scattare la guardia (falso positivo osservato dal vivo, 2026-07-17).
_NOISE_REDIR = re.compile(
    r"[0-9]*>{1,2}\s*(?:/dev/null\b|&[0-9]+|NUL\b)", re.IGNORECASE)

_TOKEN = re.compile(r"[\w@./\\-]+")


def bash_hits(cmd: str, rec: dict) -> tuple[str | None, list[dict]]:
    """(file citato nominato dal comando, vincoli) se il comando matcha un
    pattern di scrittura E nomina un file citato dalla carta. Il match sui
    path e' lo stesso della guardia editor: suffisso o basename."""
    if not WRITE_CMD.search(_NOISE_REDIR.sub(" ", cmd)):
        return None, []
    tokens = [os.path.normpath(t.replace("\\", "/")).replace(os.sep, "/")
              for t in _TOKEN.findall(cmd)]
    hit_path = None
    hits: list[dict] = []
    for cited, entries in (rec.get("files") or {}).items():
        cited_n = os.path.normpath(cited).replace(os.sep, "/")
        base = os.path.basename(cited_n)
        for t in tokens:
            if (t == cited_n or t.endswith("/" + cited_n)
                    or os.path.basename(t) == base):
                hits.extend(entries)
                hit_path = hit_path or cited_n
                break
    return hit_path, hits


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
        now = time.time()
        for k in list(st):                     # scarta i record scaduti
            if now - st[k].get("ts", 0) > max(TTL_S, 3600):
                st.pop(k, None)
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-64]:
            st.pop(k, None)
        tmp = f"{STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception:                          # noqa: BLE001
        pass


def already_warned(session: str, fpath: str, charter_ts: float) -> bool:
    """Stesso file, stessa carta, entro il TTL: tacere. Se la carta e' stata
    RISALVATA (ts diverso) i vincoli possono essere cambiati -> riparla."""
    key = hashlib.sha1(f"{session}|{fpath}".encode()).hexdigest()[:16]
    rec = _state_load().get(key)
    return (rec is not None and rec.get("cts") == charter_ts
            and time.time() - rec.get("ts", 0) < TTL_S)


def remember(session: str, fpath: str, charter_ts: float) -> None:
    key = hashlib.sha1(f"{session}|{fpath}".encode()).hexdigest()[:16]
    st = _state_load()
    st[key] = {"ts": time.time(), "cts": charter_ts}
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
        tname = payload.get("tool_name")
        tin = payload.get("tool_input") or {}
        if tname in TOOLS:
            fpath = tin.get("file_path") or tin.get("notebook_path")
            if not fpath:
                print("{}")
                return 0
            repo, hits = charter.constraints_for(fpath, payload.get("cwd"))
            intro = ("[context-kernel] The file you are about to edit is "
                     "cited in the TASK CHARTER (T3). Constraints the edit "
                     "must respect:")
        elif tname == "Bash" and BASH_ENABLED:
            cmd = str(tin.get("command") or "")
            rec0 = charter.get_for_repo(payload.get("cwd") or os.getcwd())
            if not cmd or not rec0:
                print("{}")
                return 0
            fpath, hits = bash_hits(cmd, rec0)
            repo = rec0["repo"]
            intro = ("[context-kernel] The Bash command you are about to run "
                     "appears to WRITE to a file cited in the TASK CHARTER "
                     f"(T3): {fpath}. The editor guard would not cover you "
                     "here. Constraints to respect:")
        else:
            print("{}")
            return 0
        if not hits:
            print("{}")
            return 0
        rec = charter.get_for_repo(repo) or {}
        session = (payload.get("session_id")
                   or os.path.basename(payload.get("transcript_path") or "-")[:8])
        cts = rec.get("ts", 0)
        if already_warned(session, fpath, cts):
            print("{}")
            return 0
        vincoli = "\n".join(f"- {h['vincolo']}" for h in hits[:MAX_VINCOLI])
        extra = len(hits) - MAX_VINCOLI
        if extra > 0:
            vincoli += f"\n- … {extra} more constraints (charter.py get)"
        ctx = (f"{intro}\n{vincoli}\n"
               "After the fix, walk the charter constraint by constraint "
               "(kernel-verifier), or read it again with: python3 "
               f"\"{os.path.abspath(charter.__file__)}\" get --repo {repo}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": ctx,
        }}))
        remember(session, fpath, cts)
        print(f"context-kernel[guard]: {len(hits)} constraints for {fpath}",
              file=sys.stderr)
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
