#!/usr/bin/env python3
"""
symptom_slice.py — UserPromptSubmit hook: T2 AMBIENTALE.

Se il prompt dell'utente contiene un SINTOMO concreto (traceback, errore con
coordinate file:riga), esegue lo slicer deterministico del repository (T2,
cachato) e inietta la testa del manifest come contesto aggiuntivo: il modello
si trova il working set davanti senza doversi ricordare della skill — lo
stesso principio del budget automatico (l'operatore di costo e' ambientale).

Conservativo: nessun sintomo forte -> no-op; repo piccolo -> no-op (si legge
e basta); slicer lento o rotto -> no-op. Mai fatale: su qualsiasi imprevisto
stampa "{}" ed esce 0.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass

ENABLED = os.environ.get("CK_SYMPTOM", "1") != "0"
MIN_FILES = int(os.environ.get("CK_SYMPTOM_MIN_FILES", "50"))
TIMEOUT = float(os.environ.get("CK_SYMPTOM_TIMEOUT", "10"))
MAX_LINES = int(os.environ.get("CK_SYMPTOM_MAX_LINES", "40"))

# --- stato del task attivo (multi-Q) -----------------------------------------
# Tutta la teoria assume un Q per volta, ma le sessioni derivano: arriva un
# secondo sintomo e la proiezione fatta per Q1 non ha garanzie su Q2. Qui:
# ogni slice iniettata registra sessione -> (seed, file, testa del manifest).
# Al sintomo successivo, se i SEED differiscono (il sintomo identifica il
# task), e' un cambio Q1 -> Q2: si dichiara, col diff dei manifest. E' il
# marker che mancava all'unico caso di indebolimento non dichiarato.
TASK_STATE = os.path.expanduser(
    os.environ.get("CK_TASK_STATE", "~/.context-kernel-taskstate.json"))
SWITCH_ENABLED = os.environ.get("CK_TASK_SWITCH", "1") != "0"

# Sintomi FORTI: un traceback vero o un errore con coordinate file:riga.
# Meglio un falso negativo che iniettare rumore su un prompt qualunque.
STRONG = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r'File "[^"]+", line \d+'),
    re.compile(r"^\s*at .+\(.+:\d+:\d+\)", re.MULTILINE),  # stack JS/TS
    re.compile(r"\b\w+(?:Error|Exception)\b\s*:"),
    re.compile(r"\bpanic(?::| at)\s"),
    re.compile(r"\b[\w/.-]+\.(?:py|js|jsx|ts|tsx|go|rs|rb|java|c|cc|cpp|php):\d+\b"),
    # PHP: "PHP Fatal error:", frame "#0 /a/b.php(12):", "in b.php on line 12"
    re.compile(r"PHP (?:Fatal|Parse|Recoverable fatal) error"),
    re.compile(r"\b[\w/.-]+\.php(?:\(\d+\)|\s+on\s+line\s+\d+)"),
]

CODE_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
             ".java", ".rb", ".c", ".cc", ".cpp", ".cs", ".php", ".swift")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             "dist", "build", ".next", "vendor", ".tox", "target"}


def has_symptom(prompt: str) -> bool:
    return any(rx.search(prompt) for rx in STRONG)


def repo_big_enough(root: str) -> bool:
    """Sotto MIN_FILES sorgenti la slice non paga: si legge e basta
    (stessa soglia documentata nella skill kernel-repo-slice)."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(CODE_EXTS):
                count += 1
                if count >= MIN_FILES:
                    return True
    return False


def slicer_path() -> str:
    plugin_root = (os.environ.get("CLAUDE_PLUGIN_ROOT")
                   or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(plugin_root, "skills", "kernel-repo-slice",
                        "scripts", "repo_slice.py")


def hook_session(payload: dict) -> str:
    return (payload.get("session_id")
            or os.path.basename(payload.get("transcript_path") or "-")[:8])


_MANIFEST_LINE = re.compile(r"^- (\S+)")


def manifest_files(out: str, seeds_only: bool = False) -> list[str]:
    """Path citati dal manifest dello slicer (sezioni seed + slice)."""
    files: list[str] = []
    section = None
    for line in out.split("\n"):
        if line.startswith("## "):
            section = line
            continue
        if section is None or "fuori slice" in section:
            continue
        if seeds_only and "seed" not in section:
            continue
        m = _MANIFEST_LINE.match(line)
        if m and not m.group(1).startswith(("…", "(")) \
                and m.group(1) not in files:
            files.append(m.group(1))
    return files


def _task_load() -> dict:
    try:
        with open(TASK_STATE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:                          # noqa: BLE001
        return {}
    return {}


def task_remember(session: str, repo: str, out: str) -> None:
    """Registra il working set attivo della sessione (per il rilevatore di
    cambio-task e per la sopravvivenza alla compaction). Mai fatale."""
    try:
        st = _task_load()
        st[session] = {
            "repo": repo,
            "seeds": manifest_files(out, seeds_only=True),
            "files": manifest_files(out),
            "head": "\n".join(out.split("\n")[:MAX_LINES]),
            "ts": time.time(),
        }
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni le ultime 8 sessioni
        tmp = f"{TASK_STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, TASK_STATE)
    except Exception:                          # noqa: BLE001
        pass


def task_switch_note(session: str, repo: str, out: str) -> str | None:
    """Se la sessione aveva gia' un working set per un ALTRO sintomo (seed
    diversi), dichiara il cambio task col diff dei manifest. None se e' il
    primo sintomo, lo stesso task, o un altro repo."""
    if not SWITCH_ENABLED:
        return None
    try:
        prev = _task_load().get(session)
        if not prev or prev.get("repo") != repo:
            return None
        old_seeds = set(prev.get("seeds") or [])
        new_seeds = set(manifest_files(out, seeds_only=True))
        if not old_seeds or not new_seeds or old_seeds == new_seeds:
            return None
        old_files = set(prev.get("files") or [])
        new_files = manifest_files(out)
        fresh = [f for f in new_files if f not in old_files]
        dropped = len(old_files - set(new_files))
        detail = ""
        if fresh:
            shown = ", ".join(fresh[:8])
            more = f" (+{len(fresh) - 8})" if len(fresh) > 8 else ""
            detail = (f" Files required by Q2 that are absent from the "
                      f"previous working set: {shown}{more}.")
        if dropped:
            detail += (f" {dropped} files from the previous working set are "
                       f"no longer needed for Q2.")
        return ("[context-kernel] TASK SWITCH detected (Q1 -> Q2): the symptom "
                "differs from the one the previous working set was computed "
                "for. That projection was indexed on Q1 and carries no "
                "guarantee for Q2 — the manifest above REPLACES it as the "
                "prior." + detail)
    except Exception:                          # noqa: BLE001
        return None


def main() -> int:
    if not ENABLED:
        print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0

    prompt = payload.get("prompt") or ""
    cwd = payload.get("cwd") or os.getcwd()
    # slash command, o pilotaggio manuale gia' in corso: non intrometterti
    if (not prompt or prompt.lstrip().startswith("/")
            or "kernel-repo-slice" in prompt or "repo_slice" in prompt):
        print("{}")
        return 0

    try:
        if (not has_symptom(prompt) or not os.path.isdir(cwd)
                or not repo_big_enough(cwd)):
            print("{}")
            return 0
        proc = subprocess.run(
            [sys.executable, slicer_path(), cwd,
             "--symptom", prompt[:8000], "--budget", "auto"],
            capture_output=True, text=True, timeout=TIMEOUT,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or "## seed" not in out:
            print("{}")                        # niente seed: meglio tacere
            return 0
        head = "\n".join(out.split("\n")[:MAX_LINES])
        ctx = ("[context-kernel] Symptom detected in the prompt: working set "
               "computed by the deterministic slicer (T2). Use it as a prior "
               "for exploration, not as a prohibition; to recompute it with "
               "different parameters there is the kernel-repo-slice skill.\n"
               + head)
        session = hook_session(payload)
        note = task_switch_note(session, cwd, out)
        if note:
            ctx += "\n" + note
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }}))
        task_remember(session, cwd, out)
        print(f"context-kernel[symptom]: slice injected from {cwd}",
              file=sys.stderr)
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
