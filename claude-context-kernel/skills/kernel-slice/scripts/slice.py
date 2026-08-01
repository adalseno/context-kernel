#!/usr/bin/env python3
"""
slice.py — proiezione FORMALE del codice: backward reachability slice.

Uso:
    python3 slice.py <file.py> <simbolo> [<simbolo2> ...]

Stampa solo le definizioni top-level raggiungibili dai simboli target sul
grafo def-use (piu' gli import usati). Cio' che non e' raggiungibile e' nel
kernel della mappa "comportamento del target" e non puo' cambiarne il senso.
Deterministico, answer-preserving per costruzione rispetto a quei simboli.

Python (.py): answer-preserving ESATTO — l'AST della stdlib da' l'insieme
d'uso preciso di ogni unita' top-level.
Go (.go): answer-preserving CONSERVATIVO — senza un parser Go nella stdlib,
l'insieme d'uso di ogni unita' e' la SOVRA-approssimazione "tutti gli
identificatori del corpo" (stringhe e commenti mascherati): la slice puo'
trattenere piu' del minimo, ma non lascia mai fuori un'unita' da cui il target
dipende. Confine delle unita': la convenzione gofmt (dichiarazioni top-level a
colonna 0). Rete di sicurezza: se una qualunque unita' ha graffe/parentesi
sbilanciate (split errato su codice non-gofmt) -> fallback al file intero.
Altri linguaggi: fallback = file intero.
"""
from __future__ import annotations

import ast
import re
import sys

# Stream a UTF-8: su Windows il default e' la codepage locale. No-op su
# POSIX. Mai fatale.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                          # noqa: BLE001
        pass


def bound_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        out: set[str] = set()
        for t in node.targets:
            out |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return out
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.Import):
        return {(a.asname or a.name.split(".")[0]) for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {(a.asname or a.name) for a in node.names}
    return set()


def free_names(node: ast.AST) -> set[str]:
    local: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local |= {a.arg for a in n.args.args + n.args.kwonlyargs}
            if n.args.vararg:
                local.add(n.args.vararg.arg)
            if n.args.kwarg:
                local.add(n.args.kwarg.arg)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
    used = {n.id for n in ast.walk(node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return used - local


# --- Go: slice def-use CONSERVATIVO (tokenizer, niente parser) ---------------

def _go_mask(src: str) -> str:
    """Copia di src (STESSA lunghezza) con il contenuto di commenti, stringhe
    e rune azzerato a spazi: cosi' le graffe/keyword dentro stringhe o commenti
    non falsano ne' la profondita' ne' i confini delle unita'. I newline
    restano, per preservare i numeri di riga."""
    out = list(src)
    n = len(src)
    i = 0

    def blank(a: int, b: int) -> None:
        for j in range(a, b):
            if out[j] != "\n":
                out[j] = " "

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = i
            while i < n and src[i] != "\n":
                i += 1
            blank(j, i)
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = i
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            blank(j, i)
        elif c in ('"', "'"):
            j = i
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == "\\" else 1
            i = min(n, i + 1)
            blank(j, i)
        elif c == "`":                             # raw string
            j = i
            i += 1
            while i < n and src[i] != "`":
                i += 1
            i = min(n, i + 1)
            blank(j, i)
        else:
            i += 1
    return "".join(out)


_GO_DECL_RE = re.compile(r"^(func|var|const|type|import|package)\b")
_ID_RE = re.compile(r"[A-Za-z_]\w*")


def _go_group_names(mtext: str) -> set[str]:
    """Nomi legati da una dichiarazione RAGGRUPPATA (var/const/type ( ... ))."""
    out: set[str] = set()
    for ln in mtext.split("\n")[1:]:               # salta la riga 'kind ('
        ln = ln.strip()
        if not ln or ln.startswith(")"):
            continue
        m = re.match(r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)", ln)
        if m:
            out |= {x.strip() for x in m.group(1).split(",")}
    return out


def _go_bound(kind: str, mtext: str) -> set[str]:
    first = mtext.split("\n", 1)[0]
    if kind == "func":                             # func Name / func (recv) Name
        m = re.match(r"func\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", first)
        return {m.group(1)} if m else set()
    if kind == "type":
        if re.match(r"type\s*\(", first):
            return _go_group_names(mtext)
        m = re.match(r"type\s+([A-Za-z_]\w*)", first)
        return {m.group(1)} if m else set()
    if kind in ("var", "const"):
        if re.match(rf"{kind}\s*\(", first):
            return _go_group_names(mtext)
        m = re.match(rf"{kind}\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)", first)
        return {x.strip() for x in m.group(1).split(",")} if m else set()
    return set()                                   # import/package: nessun bound


def _go_balanced(mtext: str) -> bool:
    """Graffe, parentesi tonde e quadre bilanciate nel testo mascherato:
    la firma che un'unita' e' stata tagliata BENE."""
    depth = {"{": 0, "(": 0, "[": 0}
    pairs = {"}": "{", ")": "(", "]": "["}
    for ch in mtext:
        if ch in depth:
            depth[ch] += 1
        elif ch in pairs:
            depth[pairs[ch]] -= 1
            if depth[pairs[ch]] < 0:
                return False
    return all(v == 0 for v in depth.values())


def go_units(src: str):
    """[(kind, testo_originale, testo_mascherato, riga_inizio, riga_fine)] delle
    unita' top-level (righe 1-based, fine INCLUSIVA), in ordine di sorgente.
    Confine = riga a colonna 0 che inizia con una keyword di dichiarazione (sul
    testo MASCHERATO). None se lo split non e' fidato (un'unita' sbilanciata ->
    meglio il file intero)."""
    masked = _go_mask(src)
    mlines = masked.split("\n")
    olines = src.split("\n")
    starts = [i for i, ln in enumerate(mlines) if _GO_DECL_RE.match(ln)]
    if not starts:
        return None
    units = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(mlines)
        kind = _GO_DECL_RE.match(mlines[s]).group(1)
        otext = "\n".join(olines[s:e]).rstrip("\n")
        mtext = "\n".join(mlines[s:e])
        if not _go_balanced(mtext):                # split infido -> resa onesta
            return None
        units.append((kind, otext, mtext, s + 1, e))
    return units


def slice_go(src: str, targets: set[str]) -> str | None:
    """Backward slice def-use conservativo su Go. None se: split infido,
    oppure nessun target e' un simbolo top-level (fail-safe = file intero)."""
    units = go_units(src)
    if not units:
        return None
    info = []                                      # (idx, kind, otext, bound, free)
    for i, (kind, otext, mtext, _a, _b) in enumerate(units):
        bound = _go_bound(kind, mtext)
        free = set(_ID_RE.findall(mtext)) - bound  # SOVRA-approssimazione sicura
        info.append((i, kind, otext, bound, free))
    name_to_key = {n: i for i, _, _, bnd, _ in info for n in bnd}
    seeds = {name_to_key[t] for t in targets if t in name_to_key}
    if not seeds:
        return None
    by = {i: (kind, otext, bnd, free) for i, kind, otext, bnd, free in info}
    keep, frontier = set(seeds), set(seeds)
    while frontier:
        nxt = set()
        for k in frontier:
            for u in by[k][3]:
                dep = name_to_key.get(u)
                if dep is not None and dep not in keep:
                    keep.add(dep)
                    nxt.add(dep)
        frontier = nxt
    # package e import: tenuti SEMPRE (conservativo: il mapping path->nome del
    # pacchetto e' inaffidabile, e sono economici).
    for i, (kind, _o, _b, _f) in by.items():
        if kind in ("package", "import"):
            keep.add(i)
    return "\n\n".join(by[i][1] for i in sorted(keep))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: slice.py <file.(py|go)> <symbol> [...]", file=sys.stderr)
        return 2
    path, targets = argv[1], set(argv[2:])
    src = open(path, encoding="utf-8").read()

    if path.endswith(".go"):
        out = slice_go(src, targets)
        print(out if out is not None else src)     # fail-safe: file intero
        return 0
    if not path.endswith(".py"):
        print(src)  # fallback: nessuna semantica -> file intero
        return 0

    tree = ast.parse(src)
    units = []
    for i, node in enumerate(tree.body):
        b = bound_names(node)
        if not b:
            continue
        units.append((i, node, b, free_names(node) - b,
                      isinstance(node, (ast.Import, ast.ImportFrom))))
    name_to_key = {n: i for i, _, b, _, _ in units for n in b}
    by_key = {i: (node, b, uses, is_imp) for i, node, b, uses, is_imp in units}

    seeds = {name_to_key[t] for t in targets if t in name_to_key}
    if not seeds:
        print(src)  # target ignoto: fail-safe = file intero
        return 0

    keep, frontier = set(seeds), set(seeds)
    while frontier:
        nxt = set()
        for k in frontier:
            for u in by_key[k][2]:
                dep = name_to_key.get(u)
                if dep is not None and dep not in keep and not by_key[dep][3]:
                    keep.add(dep)
                    nxt.add(dep)
        frontier = nxt

    used_names: set[str] = set()
    for k in keep:
        used_names |= by_key[k][2]
    for k, (node, b, uses, is_imp) in by_key.items():
        if is_imp and (b & used_names):
            keep.add(k)

    kept = [by_key[k][0] for k in sorted(keep)]
    print("\n\n\n".join(ast.unparse(n) for n in kept))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
