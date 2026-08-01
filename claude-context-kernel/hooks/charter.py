#!/usr/bin/env python3
"""
charter.py — persistenza della CARTA DEL TASK (T3).

La carta prodotta dalla skill kernel-invariants nasce come testo in
conversazione: per diventare ATTIVA (guardia sugli Edit, sopravvivenza alla
compaction) deve vivere in uno stato leggibile dagli hook. Qui: salvataggio
per-repo + estrazione DETERMINISTICA delle citazioni file:riga. Un vincolo
senza citazione non e' indicizzabile: resta nel testo ma la guardia non puo'
agganciarlo (la regola della skill — "un vincolo senza citazione non e' un
vincolo" — qui diventa meccanica).

Uso (dalla skill kernel-invariants, o a mano):
  python3 charter.py save    --repo DIR  # carta su stdin
  python3 charter.py get     [--repo DIR]  # carta attiva (default: cwd)
  python3 charter.py refresh [--repo DIR]  # ri-ancora le citazioni slittate
  python3 charter.py clear   [--repo DIR]

Le citazioni INVECCHIANO: il codice cresce e file:riga slitta (osservato
dal T4 su tre release di fila). Al save ogni citazione cattura un'ANCORA
(il contenuto della riga citata); refresh la ricerca nel file attuale —
match unico -> riga aggiornata (anche nel testo), zero o ambiguo ->
IRRISOLVIBILE dichiarata, mai indovinata. Le carte salvate prima delle
ancore non sono ri-ancorabili: il rimedio e' rigenerarle.

Mai fatale: su qualsiasi imprevisto esce 0 senza output.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass

STATE = os.path.expanduser(
    os.environ.get("CK_CHARTER_STATE", "~/.context-kernel-charter.json"))
MAX_TEXT = int(os.environ.get("CK_CHARTER_MAX", "12000"))   # caratteri

# citazione file:riga come la produce la skill: (path/file.py:123)
CITE = re.compile(r"\(([^()\s:]+\.[A-Za-z0-9_]{1,8}):(\d+)\)")


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:                          # noqa: BLE001
        pass
    return {}


def save_state(st: dict) -> None:
    try:
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni le ultime 8 carte
        tmp = f"{STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
    except Exception:                          # noqa: BLE001
        pass


def parse_citations(text: str) -> dict:
    """{path_citato: [{"line": N, "vincolo": riga_intera}, ...]} — ogni
    citazione porta con se' la riga della carta da cui viene, cosi' la
    guardia inietta la PROPOSIZIONE, non un puntatore."""
    files: dict[str, list[dict]] = {}
    for raw in text.split("\n"):
        line = raw.strip()
        for m in CITE.finditer(line):
            # chiave sempre in forma POSIX, anche su Windows: lo stato e'
            # portabile e i consumatori rinormalizzano al sep nativo
            path = os.path.normpath(m.group(1)).replace(os.sep, "/")
            entry = {"line": int(m.group(2)), "vincolo": line[:240]}
            if entry not in files.setdefault(path, []):
                files[path].append(entry)
    return files


def _anchor_citations(repo: str, files: dict) -> None:
    """Cattura l'ancora di ogni citazione: il contenuto (strip) della riga
    citata, letto ADESSO — al refresh sara' la stringa da ricercare."""
    for path, entries in files.items():
        try:
            with open(os.path.join(repo, os.path.normpath(path)),
                      encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            continue
        for e in entries:
            if 1 <= e["line"] <= len(lines):
                snippet = lines[e["line"] - 1].strip()[:160]
                if snippet:
                    e["anchor"] = snippet


def save_charter(repo: str, text: str) -> None:
    repo = os.path.normpath(os.path.abspath(repo))
    text = text.strip()[:MAX_TEXT]
    if not text:
        return
    files = parse_citations(text)
    _anchor_citations(repo, files)
    st = load_state()
    st[repo] = {"text": text, "ts": time.time(), "files": files}
    save_state(st)


def refresh_charter(repo: str) -> list[str]:
    """Ri-ancora le citazioni slittate della carta del repo. Ritorna il
    rapporto per citazione: ok / ri-ancorata / irrisolvibile (dichiarata,
    mai indovinata) / senza ancora (carta pre-ancore: rigenerarla)."""
    rec = get_for_repo(repo)
    if not rec:
        return ["no active charter for this repo"]
    st = load_state()
    root = rec["repo"]
    files, text = st[root]["files"], st[root]["text"]
    report: list[str] = []
    for path, entries in files.items():
        try:
            with open(os.path.join(root, os.path.normpath(path)),
                      encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f.read().split("\n")]
        except OSError:
            report.append(f"UNRESOLVABLE   {path}: file not readable")
            continue
        for e in entries:
            cite, anchor = f"{path}:{e['line']}", e.get("anchor")
            if not anchor:
                report.append(f"NO ANCHOR      {cite}: charter saved before "
                              "anchors existed — regenerate the charter")
                continue
            if 1 <= e["line"] <= len(lines) and lines[e["line"] - 1] == anchor:
                report.append(f"OK             {cite}")
                continue
            hits = [i + 1 for i, l in enumerate(lines) if l == anchor]
            if len(hits) == 1:
                new = hits[0]
                old_cite, new_cite = f"({path}:{e['line']})", f"({path}:{new})"
                text = text.replace(old_cite, new_cite)
                # la citazione va sostituita in TUTTE le proposizioni che la
                # portano: con >=2 citazioni sulla stessa riga di carta ogni
                # entry gemella copia l'intera riga — aggiornarne una sola
                # farebbe divergere guardia (vincolo) e get (testo)
                for entries2 in files.values():
                    for e2 in entries2:
                        e2["vincolo"] = e2["vincolo"].replace(old_cite, new_cite)
                e["line"] = new
                report.append(f"RE-ANCHORED    {cite} -> :{new}")
            else:
                # zero o ambigua: dichiarare, mai indovinare (stessa regola
                # della risoluzione FQCN: suffisso ambiguo -> None)
                report.append(f"UNRESOLVABLE   {cite}: anchor found "
                              f"{len(hits)} times")
    st[root]["text"] = text
    st[root]["ts"] = time.time()
    save_state(st)
    return report


def get_for_repo(repo: str) -> dict | None:
    """Carta del repo (match esatto o repo antenato del path dato)."""
    st = load_state()
    repo = os.path.normpath(os.path.abspath(repo))
    if repo in st:
        return {"repo": repo, **st[repo]}
    for root, rec in sorted(st.items(), key=lambda kv: -kv[1].get("ts", 0)):
        if repo.startswith(root.rstrip(os.sep) + os.sep):
            return {"repo": root, **rec}
    return None


def latest() -> dict | None:
    st = load_state()
    if not st:
        return None
    root = max(st, key=lambda k: st[k].get("ts", 0))
    return {"repo": root, **st[root]}


def constraints_for(file_path: str, cwd: str | None = None) -> tuple[str, list[dict]]:
    """Vincoli della carta attiva che citano questo file. Il match e' per
    SUFFISSO di path (la carta cita path relativi al repo, l'editor path
    assoluti). Ritorna (repo, [vincoli]); ("", []) se nessuno."""
    fp = os.path.normpath(os.path.abspath(file_path))
    rec = get_for_repo(os.path.dirname(fp)) or (
        get_for_repo(cwd) if cwd else None)
    if not rec:
        return "", []
    hits: list[dict] = []
    for cited, entries in (rec.get("files") or {}).items():
        cited_n = os.path.normpath(cited)
        if fp.endswith(os.sep + cited_n) or os.path.basename(fp) == cited_n:
            hits.extend(entries)
    return rec["repo"], hits


def main() -> int:
    try:
        args = sys.argv[1:]
        if not args:
            return 0
        cmd = args[0]
        repo = os.getcwd()
        if "--repo" in args:
            repo = args[args.index("--repo") + 1]
        if cmd == "save":
            save_charter(repo, sys.stdin.read())
            rec = get_for_repo(repo)
            n = sum(len(v) for v in (rec or {}).get("files", {}).values())
            print(f"charter saved for {os.path.abspath(repo)}: "
                  f"{n} constraints indexed (file:line citations)")
        elif cmd == "get":
            rec = get_for_repo(repo) or latest()
            if rec:
                print(rec["text"])
        elif cmd == "refresh":
            for line in refresh_charter(repo):
                print(line)
        elif cmd == "clear":
            st = load_state()
            st.pop(os.path.normpath(os.path.abspath(repo)), None)
            save_state(st)
        return 0
    except Exception:                          # noqa: BLE001
        return 0


if __name__ == "__main__":
    sys.exit(main())
