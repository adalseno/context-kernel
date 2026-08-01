#!/usr/bin/env python3
"""
repo_slice.py — T2 della pipeline: slice a livello REPOSITORY.

Dato un sintomo (stack trace, messaggio d'errore, path espliciti), costruisce
il grafo degli import del repo e proietta via tutto cio' che non puo'
influenzare il bug:

    seed (frame dello stack / raise site / path espliciti)
      + dipendenze transitive   (cio' che il seed importa, tutta la chiusura)
      + importatori vicini      (chi usa il seed: la causa puo' stare nel caller)
      + test correlati          (importano file della slice: repro + comportamento atteso)

Tutto il resto e' fuori slice, ma RECUPERABILE: il manifest dichiara le
esclusioni (modello page-fault). L'esclusione e' un prior, non un divieto.

GARANZIA (onesta): la reachability sugli import e' deterministica, ma a
livello repo NON e' answer-preserving per costruzione — dynamic import,
DI, config file e alias sfuggono al grafo. Per questo il manifest e'
progettato per il recupero on-demand, e kernel-verify resta il gate.

Zero dipendenze, zero rete. Uso:
    python3 repo_slice.py <repo_root> --symptom "traceback o errore..."
    python3 repo_slice.py <repo_root> --symptom-file /tmp/bug.txt --json
    python3 repo_slice.py <repo_root> --seed app/db.py --importers-depth 2
"""
from __future__ import annotations

import argparse
import importlib.util
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time

# Stream a UTF-8: su Windows il default e' la codepage locale (il manifest
# contiene path e simboli arbitrari). Su POSIX e' un no-op. Mai fatale.
for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                          # noqa: BLE001
        pass

EXCLUDE_DIRS = {
    "node_modules", "dist", "build", "out", "target", "vendor", "coverage",
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    ".next", ".nuxt", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", ".idea", ".vscode", "egg-info",
}
PY_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
PHP_EXTS = {".php"}
GO_EXTS = {".go"}
# Linguaggi SENZA pack preciso: coperti dal GRAFO GENERICO (riferimenti
# testuali, garanzia dichiarata piu' debole — mai muti, mai "non supportati").
GENERIC_EXTS = {".rs", ".java", ".rb", ".c", ".h", ".cc", ".cpp", ".hpp",
                ".cs", ".swift", ".kt", ".kts", ".scala", ".sh", ".bash",
                ".pl", ".pm", ".lua", ".ex", ".exs", ".erl", ".ml", ".hs",
                ".dart", ".r", ".jl", ".zig", ".nim"}
SRC_EXTS = PY_EXTS | JS_EXTS | PHP_EXTS | GO_EXTS | GENERIC_EXTS
# Oltre questo numero di file generici il mention-graph O(file x nomi) non
# paga: gli archi generici si saltano (esclusione dichiarata nel docstring
# di _generic_edges; i file restano scansionati e seedabili).
GENERIC_GRAPH_MAX = 4000
MAX_FILES = 50_000
GREP_MAX_BYTES = 512_000        # non greppare file enormi
GREP_MAX_HITS = 20

TEST_PAT = re.compile(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")

# frame Python:  File "app/db.py", line 88
PY_FRAME = re.compile(r'File "([^"]+)", line \d+')
# come sopra ma cattura anche la riga (per il T2b: simbolo che la racchiude)
PY_FRAME_LINE = re.compile(r'File "([^"]+)", line (\d+)')
# frame JS:  at fn (web/index.js:3:5)   |   at web/index.js:3:5
JS_FRAME = re.compile(r"\(?([\w@./\\-]+\.(?:js|jsx|ts|tsx|mjs|cjs)):\d+(?::\d+)?\)?")
# frame PHP:  in /a/b.php on line 12  |  #0 /a/b.php(12): Foo->bar()
PHP_FRAME = re.compile(r"([\w./\\-]+\.php)(?:\(\d+\)|(?:\s+on\s+line\s+|:)\d+)")
# frame Go (goroutine dump):  \t/abs/path/db/db.go:12 +0x1b  |  db/db.go:12
GO_FRAME = re.compile(r"([\w@./\\-]+\.go):\d+")
# come GO_FRAME ma cattura anche la RIGA (per la slice a simbolo T2b sui .go):
# goroutine dump  db/db.go:12  e  fallimenti di test  db_test.go:12:
GO_FRAME_LINE = re.compile(r"([\w@./\\-]+\.go):(\d+)")
# path nudi nel testo del sintomo (alternanza derivata da SRC_EXTS: ogni
# linguaggio scansionato e' anche seedabile; suffissi lunghi prima)
_EXT_ALT = "|".join(sorted((e.lstrip(".") for e in SRC_EXTS),
                           key=len, reverse=True))
BARE_PATH = re.compile(rf"([\w@./\\-]+\.(?:{_EXT_ALT}))\b")
# letterali quotati (probabile messaggio d'errore -> raise site)
QUOTED = re.compile(r"[\"']([^\"'\n]{8,120})[\"']")

JS_IMPORT = re.compile(
    r"""(?:import|export)\s+[^'"]*?from\s+['"]([^'"]+)['"]"""
    r"""|import\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\s+['"]([^'"]+)['"]"""
)

# PHP: dichiarazioni per la mappa FQCN -> file, e archi use/require.
# `use` di gruppo (use Foo\{A, B}) gestito a parte; il `use ($var)` delle
# closure non matcha (richiede parentesi); il `use Trait;` nel corpo di una
# classe matcha ed e' un arco voluto (il trait e' una dipendenza).
PHP_NAMESPACE = re.compile(r"^\s*namespace\s+([\w\\]+)\s*[;{]", re.M)
PHP_DECL = re.compile(
    r"^\s*(?:final\s+|abstract\s+|readonly\s+)*(?:class|interface|trait|enum)\s+(\w+)",
    re.M)
PHP_USE = re.compile(
    r"^\s*use\s+(?:function\s+|const\s+)?\\?([\w\\]+)(?:\s+as\s+\w+)?\s*;", re.M)
PHP_GROUP_USE = re.compile(r"^\s*use\s+\\?([\w\\]+)\\\{([^}]+)\}\s*;", re.M)
PHP_REQUIRE = re.compile(
    r"(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?"
    r"['\"]\.?/?([^'\"]+\.php)['\"]")

# Go: il modulo da go.mod, gli import (singoli e a blocco). L'unita' di
# import in Go e' il PACKAGE (= directory): un arco verso un package e'
# un arco verso tutti i suoi .go. Import fuori dal modulo (stdlib, terze
# parti) non si risolvono MAI per indovinamento: si scartano.
GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)
GO_IMPORT_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.M | re.S)
GO_IMPORT_ONE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.M)
GO_IMPORT_STR = re.compile(r'"([^"]+)"')


# --- raccolta file ----------------------------------------------------------

def collect_files(root: str) -> list[str]:
    """Path relativi dei sorgenti, potando le dir escluse a priori."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            if os.path.splitext(name)[1] in SRC_EXTS:
                # sempre "/" anche su Windows: il grafo, i manifest e i
                # confronti coi seed assumono path in forma POSIX
                found.append(os.path.relpath(
                    os.path.join(dirpath, name), root).replace(os.sep, "/"))
                if len(found) >= MAX_FILES:
                    return sorted(found)
    return sorted(found)


# --- grafo degli import -----------------------------------------------------

def _py_module_map(files: list[str], root_pkg: str | None = None) -> dict[str, str]:
    """dotted path -> file. `pkg/mod.py` -> pkg.mod; `pkg/__init__.py` -> pkg.
    Se il ROOT stesso e' un package (ha __init__.py), gli import interni usano
    il suo nome come prefisso (root=pandas/ -> `from pandas.core import x`):
    registra anche l'alias prefissato, altrimenti nulla si risolve."""
    mm: dict[str, str] = {}
    for f in files:
        if not f.endswith(".py"):
            continue
        parts = f[:-3].replace(os.sep, "/").split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mm[".".join(parts)] = f
            if root_pkg:
                mm[".".join([root_pkg, *parts])] = f
        elif root_pkg:                         # __init__.py del root
            mm[root_pkg] = f
    return mm


def _resolve_py(mod: str, mm: dict[str, str]) -> str | None:
    """Risolve un dotted import: match esatto, prefissi, poi suffisso univoco."""
    parts = mod.split(".")
    for i in range(len(parts), 0, -1):        # a.b.c -> a.b -> a
        hit = mm.get(".".join(parts[:i]))
        if hit:
            return hit
    tail = "." + mod
    suffix = [f for d, f in mm.items() if d.endswith(tail)]
    return suffix[0] if len(suffix) == 1 else None


def _py_imports(path: str, rel: str, mm: dict[str, str]) -> set[str]:
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except Exception:                          # noqa: BLE001
        return set()
    pkg = rel[:-3].replace(os.sep, "/").split("/")
    pkg = pkg[:-1] if pkg[-1] != "__init__" else pkg[:-2]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                hit = _resolve_py(a.name, mm)
                if hit:
                    out.add(hit)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                     # relativo: risali di level-1
                base = pkg[: len(pkg) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            hit = _resolve_py(mod, mm)
            if hit:
                out.add(hit)
            for a in node.names:               # from m import sub  (sub modulo?)
                hit = _resolve_py(f"{mod}.{a.name}", mm)
                if hit:
                    out.add(hit)
    return out


def _js_imports(path: str, rel: str, fileset: set[str]) -> set[str]:
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except Exception:                          # noqa: BLE001
        return set()
    base = os.path.dirname(rel)
    out: set[str] = set()
    for m in JS_IMPORT.finditer(src):
        spec = next(g for g in m.groups() if g)
        if not spec.startswith("."):           # solo relativi: i pacchetti sono fuori repo
            continue
        p = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
        candidates = [p] + [p + e for e in JS_EXTS] + [f"{p}/index{e}" for e in JS_EXTS]
        for c in candidates:
            if c in fileset:
                out.add(c)
                break
    return out


def _php_class_map(root: str, files: list[str]) -> dict[str, str]:
    """FQCN (Namespace\\Classe) -> file relativo, dalle DICHIARAZIONI
    (class/interface/trait/enum), senza composer: la convenzione PSR-4 rende
    il namespace deducibile dal file stesso. Primo dichiarante vince."""
    cm: dict[str, str] = {}
    for rel in files:
        if not rel.endswith(".php"):
            continue
        try:
            src = open(os.path.join(root, rel), encoding="utf-8",
                       errors="replace").read()
        except Exception:                      # noqa: BLE001
            continue
        ns = PHP_NAMESPACE.search(src)
        prefix = ns.group(1) + "\\" if ns else ""
        for m in PHP_DECL.finditer(src):
            cm.setdefault(prefix + m.group(1), rel.replace(os.sep, "/"))
    return cm


def _resolve_php(fqcn: str, cm: dict[str, str]) -> str | None:
    """FQCN esatto, poi suffisso `\\Classe` se univoco (mai indovinare)."""
    hit = cm.get(fqcn.lstrip("\\"))
    if hit:
        return hit
    tail = "\\" + fqcn.rsplit("\\", 1)[-1]
    hits = {f for c, f in cm.items() if c.endswith(tail)}
    return next(iter(hits)) if len(hits) == 1 else None


def _php_imports(path: str, rel: str, cm: dict[str, str],
                 fileset: set[str]) -> set[str]:
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except Exception:                          # noqa: BLE001
        return set()
    reln = rel.replace(os.sep, "/")
    out: set[str] = set()
    for m in PHP_USE.finditer(src):
        hit = _resolve_php(m.group(1), cm)
        if hit and hit != reln:
            out.add(hit)
    for m in PHP_GROUP_USE.finditer(src):
        base = m.group(1)
        for name in m.group(2).split(","):
            name = name.strip()
            for kw in ("function ", "const "):
                name = name.removeprefix(kw)
            name = name.split(" as ")[0].strip().lstrip("\\")
            if name:
                hit = _resolve_php(f"{base}\\{name}", cm)
                if hit and hit != reln:
                    out.add(hit)
    base_dir = os.path.dirname(reln)
    for m in PHP_REQUIRE.finditer(src):
        spec = m.group(1).replace("\\", "/")
        for cand in (os.path.normpath(os.path.join(base_dir, spec)).replace(os.sep, "/"),
                     spec.lstrip("/")):
            if cand in fileset and cand != reln:
                out.add(cand)
                break
    return out


def _go_module(root: str) -> str | None:
    """Il module path dichiarato in go.mod alla radice. Senza go.mod gli
    import interni non sono risolvibili senza indovinare: None e il grafo
    Go resta vuoto (esclusione dichiarata, non silenziosa)."""
    try:
        src = open(os.path.join(root, "go.mod"), encoding="utf-8",
                   errors="replace").read()
    except Exception:                          # noqa: BLE001
        return None
    m = GO_MODULE.search(src)
    return m.group(1) if m else None


def _go_dir_map(files: list[str]) -> dict[str, set[str]]:
    """directory relativa -> set dei suoi .go (il package Go e' la dir)."""
    dm: dict[str, set[str]] = {}
    for rel in files:
        if rel.endswith(".go"):
            reln = rel.replace(os.sep, "/")
            dm.setdefault(os.path.dirname(reln), set()).add(reln)
    return dm


def _go_imports(path: str, rel: str, module: str | None,
                dm: dict[str, set[str]]) -> set[str]:
    """Archi verso i package INTERNI al modulo (import con prefisso del
    module path -> directory -> tutti i suoi .go). Stdlib e terze parti
    scartate: mai risolvere per indovinamento."""
    if not module:
        return set()
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except Exception:                          # noqa: BLE001
        return set()
    imports: set[str] = set(GO_IMPORT_ONE.findall(src))
    for block in GO_IMPORT_BLOCK.findall(src):
        imports.update(GO_IMPORT_STR.findall(block))
    reln = rel.replace(os.sep, "/")
    out: set[str] = set()
    for imp in imports:
        if imp == module:
            target = ""
        elif imp.startswith(module + "/"):
            target = imp[len(module) + 1:]
        else:
            continue                           # stdlib / fuori modulo
        # i _test.go NON sono nel package per chi lo importa: arrivano
        # solo come test correlati (convenzione X_test.go -> X.go)
        out.update(f for f in dm.get(target, ())
                   if f != reln and not f.endswith("_test.go"))
    return out


_NAME_TOKEN = re.compile(r"[\w-]+\.[A-Za-z0-9_]+")
_WORD = re.compile(r"\w+")

# --- indice di simboli esterno (ctags): promuove il grafo generico a preciso -
# Idea di SCIP/Sourcegraph (consumare un indice gia' prodotto invece di
# ri-risolvere), realizzata SENZA dipendenze: SCIP e' protobuf, ma il file
# `tags` di ctags e' testo — una mappa simbolo->file che moltissimi repo (e
# editor) shippano gia'. Se presente, un file generico che CITA un simbolo
# definito altrove ottiene un arco PRECISO verso il definitore, dove prima
# c'era solo l'euristica nome-file/stem. AGGIUNGE archi (mai li toglie: la
# chiusura cresce -> piu' answer-preserving, mai meno); simbolo definito in
# PIU' file = ambiguo -> saltato, mai indovinato (la regola FQCN). Nessun tags
# = comportamento invariato.
CTAGS_ENABLED = os.environ.get("CK_CTAGS", "1") != "0"
CTAGS_MAX_BYTES = 8_000_000
CTAGS_FILE = "tags"


def _ctags_map(root: str) -> dict[str, str]:
    """symbol -> file relativo, dai soli simboli UNIVOCAMENTE definiti nel file
    `tags` (ctags) alla radice. {} se assente/disabilitato/troppo grande."""
    if not CTAGS_ENABLED:
        return {}
    path = os.path.join(root, CTAGS_FILE)
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > CTAGS_MAX_BYTES:
            return {}
        raw = open(path, encoding="utf-8", errors="replace").read()
    except Exception:                          # noqa: BLE001
        return {}
    seen: dict[str, set[str]] = {}
    for line in raw.split("\n"):
        if not line or line.startswith("!_TAG_"):   # righe di metadati
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sym = parts[0]
        if len(sym) < 3 or not sym.isidentifier():
            continue
        f = parts[1].replace("\\", "/").replace(os.sep, "/").lstrip("./")
        seen.setdefault(sym, set()).add(f)
    return {s: next(iter(fs)) for s, fs in seen.items() if len(fs) == 1}


def _generic_edges(root: str, files: list[str],
                   sym_map: dict[str, str] | None = None) -> dict[str, set[str]]:
    """GRAFO GENERICO per i linguaggi senza pack preciso: A -> B se A cita B
    per NOME FILE letterale (es. #include "render.h") o per STEM a parola
    intera, con guardie che non indovinano mai: stem lungo >=3 e UNIVOCO nel
    repo (due config.rs -> nessun arco per "config"). Solo tra file generici.
    Classe dichiarata nel manifest come "riferimento testuale". Oltre
    GENERIC_GRAPH_MAX file generici gli archi si saltano (il costo e'
    O(file x nomi)): i file restano scansionati e seedabili.
    Se sym_map (indice ctags) e' dato, aggiunge archi PRECISI: A -> B quando A
    cita un simbolo definito univocamente in B (definitore generico, != A)."""
    gen = [f.replace(os.sep, "/") for f in files
           if os.path.splitext(f)[1] in GENERIC_EXTS]
    edges: dict[str, set[str]] = {f: set() for f in gen}
    if not gen or len(gen) > GENERIC_GRAPH_MAX:
        return edges
    genset = set(gen)
    sym_map = {s: d for s, d in (sym_map or {}).items() if d in genset}
    by_name: dict[str, set[str]] = {}
    by_stem: dict[str, set[str]] = {}
    for f in gen:
        base = os.path.basename(f)
        by_name.setdefault(base, set()).add(f)
        by_stem.setdefault(os.path.splitext(base)[0], set()).add(f)
    for f in gen:
        path = os.path.join(root, f)
        try:
            if os.path.getsize(path) > GREP_MAX_BYTES:
                continue
            src = open(path, encoding="utf-8", errors="replace").read()
        except Exception:                      # noqa: BLE001
            continue
        names = set(_NAME_TOKEN.findall(src))
        words = set(_WORD.findall(src))
        mine = os.path.basename(f)
        for name, targets in by_name.items():
            if name != mine and name in names:
                edges[f].update(t for t in targets if t != f)
        for stem, targets in by_stem.items():
            if len(stem) >= 3 and len(targets) == 1 and stem in words:
                t = next(iter(targets))
                if t != f:
                    edges[f].add(t)
        if sym_map:                            # indice ctags: archi PRECISI
            for sym in words & sym_map.keys():
                d = sym_map[sym]
                if d != f:
                    edges[f].add(d)
    return edges


# --- language packs -----------------------------------------------------------
# La tabella dichiarativa dei linguaggi PRECISI. Ogni pack: estensioni +
# factory che precomputa lo stato del linguaggio e ritorna la funzione
# archi (path, rel) -> set. Aggiungere un linguaggio = una entry qui + una
# fixture nei test + il bench di sufficienza (gate: senza misura resta nel
# grafo generico). Tutto cio' che non e' in tabella e non e' generico
# (oggi: JS/TS) usa il pack "js"; le estensioni GENERIC_EXTS usano
# _generic_edges (riferimenti testuali, classe dichiarata).


def _pack_py(root, files, fileset):
    root_pkg = (os.path.basename(os.path.normpath(root))
                if os.path.exists(os.path.join(root, "__init__.py")) else None)
    mm = _py_module_map(files, root_pkg)
    return lambda path, rel: _py_imports(path, rel, mm)


def _pack_js(root, files, fileset):
    return lambda path, rel: _js_imports(path, rel, fileset)


def _pack_php(root, files, fileset):
    cm = _php_class_map(root, files)
    return lambda path, rel: _php_imports(path, rel, cm, fileset)


def _pack_go(root, files, fileset):
    mod = _go_module(root)
    dm = _go_dir_map(files)
    return lambda path, rel: _go_imports(path, rel, mod, dm)


LANG_PACKS = {
    "python": {"exts": PY_EXTS, "factory": _pack_py},
    "js": {"exts": JS_EXTS, "factory": _pack_js},
    "php": {"exts": PHP_EXTS, "factory": _pack_php},
    "go": {"exts": GO_EXTS, "factory": _pack_go},
}


def build_graph(root: str, files: list[str],
                sym_map: dict[str, str] | None = None) -> dict[str, set[str]]:
    """file -> set(file importati). Deterministico, best-effort per file
    rotto. Dispatch per estensione via LANG_PACKS; le estensioni generiche
    passano dal mention-graph (_generic_edges), promosso dall'indice ctags
    (sym_map) quando presente."""
    fileset = set(f.replace(os.sep, "/") for f in files)
    ext_fn: dict[str, object] = {}
    for pack in LANG_PACKS.values():
        if any(os.path.splitext(f)[1] in pack["exts"] for f in files):
            fn = pack["factory"](root, files, fileset)
            for e in pack["exts"]:
                ext_fn[e] = fn
    generic = (_generic_edges(root, files, sym_map)
               if any(os.path.splitext(f)[1] in GENERIC_EXTS for f in files)
               else {})
    graph: dict[str, set[str]] = {}
    for rel in files:
        ext = os.path.splitext(rel)[1]
        if ext in GENERIC_EXTS:
            graph[rel] = generic.get(rel.replace(os.sep, "/"), set())
        elif ext in ext_fn:
            graph[rel] = ext_fn[ext](os.path.join(root, rel), rel)
        else:
            graph[rel] = set()
    return graph


# --- archi euristici test -> sorgente ----------------------------------------

REF_MAX_BYTES = 200_000


def _test_ref_edges(root: str, files: list[str]) -> dict[str, set[str]]:
    """I test spesso caricano i sorgenti SENZA import statico (importlib da
    path, subprocess): archi invisibili al grafo. Due euristiche deterministiche,
    usate SOLO per marcare i test correlati (mai nella chiusura delle
    dipendenze, che inquinerebbero):
      1. convenzione dei nomi: test_X.* / X_test.* -> X.*;
      2. citazione: il test contiene il basename del sorgente tra virgolette.
    Solo basename NON ambigui (unici nel repo): niente indovinelli."""
    rels = [f.replace(os.sep, "/") for f in files]
    by_base: dict[str, list[str]] = {}
    for rel in rels:
        if TEST_PAT.search(rel):
            continue
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    unique = {b: rs[0] for b, rs in by_base.items() if len(rs) == 1}

    refs: dict[str, set[str]] = {}
    for rel in rels:
        if not TEST_PAT.search(rel):
            continue
        base = rel.rsplit("/", 1)[-1]
        stem, dot, ext = base.rpartition(".")
        for cand in (stem.removeprefix("test_"), stem.removesuffix("_test")):
            if cand and cand != stem:
                target = unique.get(f"{cand}.{ext}")
                if target and target != rel:
                    refs.setdefault(rel, set()).add(target)
        path = os.path.join(root, rel.replace("/", os.sep))
        try:
            if os.path.getsize(path) > REF_MAX_BYTES:
                continue
            content = open(path, encoding="utf-8", errors="replace").read()
        except Exception:                      # noqa: BLE001
            continue
        for b, target in unique.items():
            if target != rel and (f'"{b}"' in content or f"'{b}'" in content):
                refs.setdefault(rel, set()).add(target)
    return refs


# --- seed dal sintomo -------------------------------------------------------

def find_seeds(root: str, files: list[str], symptom: str,
               explicit: list[str],
               diff: list[str] | None = None,
               diff_why: str = "file changed in the diff",
               ) -> list[tuple[str, str]]:
    """Ritorna [(file, motivo)]. Path dai frame + espliciti + file del diff
    + grep dei letterali."""
    fileset = {f.replace(os.sep, "/") for f in files}
    rootn = os.path.abspath(root).replace(os.sep, "/").rstrip("/") + "/"
    seeds: dict[str, str] = {}

    def match_path(token: str, why: str) -> None:
        raw = token.replace("\\", "/")
        # path assoluto DENTRO il root: relativizza subito (il suffix-match
        # fallirebbe per ambiguita' coi gemelli, es. tests/io/excel/__init__.py)
        if raw.startswith(rootn) and raw[len(rootn):] in fileset:
            seeds.setdefault(raw[len(rootn):], why)
            return
        t = raw.lstrip("./")
        if t in fileset:
            seeds.setdefault(t, why)
            return
        # suffisso piu' LUNGO prima: un path assoluto fuori dal root (frame di
        # stack trace) deve agganciarsi per suffisso, non scendere subito al
        # basename (che su repo grandi e' spesso ambiguo, es. generic.py x3)
        parts = [p for p in t.split("/") if p]
        for k in range(len(parts), 0, -1):
            suf = "/".join(parts[-k:])
            hits = [f for f in fileset if f == suf or f.endswith("/" + suf)]
            if len(hits) == 1:
                seeds.setdefault(hits[0], why)
                return
            if len(hits) > 1:                  # ambiguo anche cosi': non indovinare
                return

    for e in explicit:
        match_path(e, "explicit seed")
    for e in diff or []:
        match_path(e, diff_why)
    for pat, why in ((PY_FRAME, "frame stack trace"),
                     (JS_FRAME, "frame stack trace"),
                     (PHP_FRAME, "frame stack trace"),
                     (GO_FRAME, "frame stack trace"),
                     (BARE_PATH, "path in the symptom")):
        for m in pat.finditer(symptom):
            match_path(m.group(1), why)

    literals = [q for q in QUOTED.findall(symptom)
                if "/" not in q and "\\" not in q]
    for lit in literals[:3]:
        hits = 0
        for rel in files:
            path = os.path.join(root, rel)
            try:
                if os.path.getsize(path) > GREP_MAX_BYTES:
                    continue
                if lit in open(path, encoding="utf-8", errors="replace").read():
                    seeds.setdefault(rel.replace(os.sep, "/"),
                                     f'contains the literal "{lit[:40]}"')
                    hits += 1
                    if hits >= GREP_MAX_HITS:
                        break
            except Exception:                  # noqa: BLE001
                continue
    return sorted(seeds.items())


# --- slice ------------------------------------------------------------------

def slice_repo(graph: dict[str, set[str]], seeds: list[str],
               importers_depth: int,
               test_refs: dict[str, set[str]] | None = None,
               deps_depth: int = 0,
               ) -> dict[str, tuple[str, int, str]]:
    """file -> (ruolo, hop, via). Ruoli: seed, dipendenza, importatore, test."""
    norm = {f.replace(os.sep, "/"): deps for f, deps in graph.items()}
    reverse: dict[str, set[str]] = {f: set() for f in norm}
    for f, deps in norm.items():
        for d in deps:
            reverse.setdefault(d.replace(os.sep, "/"), set()).add(f)

    kept: dict[str, tuple[str, int, str]] = {s: ("seed", 0, "") for s in seeds}

    frontier = list(seeds)                     # dipendenze: chiusura completa
    hop = 0                                    # (o limitata con deps_depth>0:
    while frontier:                            # sui repo monolitici la chiusura
        if deps_depth and hop >= deps_depth:   # piena esplode, vedi pandas)
            break
        hop += 1
        nxt: list[str] = []
        for f in frontier:
            for d in sorted(norm.get(f, ())):
                if d not in kept:
                    kept[d] = ("dependency", hop, f)
                    nxt.append(d)
        frontier = nxt

    frontier = list(seeds)                     # importatori: profondita' limitata
    for hop in range(1, importers_depth + 1):
        nxt = []
        for f in frontier:
            for imp in sorted(reverse.get(f, ())):
                if imp not in kept and not TEST_PAT.search(imp):
                    kept[imp] = ("importer", hop, f)      # i test li etichetta lo stadio dopo
                    nxt.append(imp)
        frontier = nxt

    seed_set = set(seeds)                      # test correlati: SOLO chi usa un
    refs = test_refs or {}                     # seed (import o riferimento
    for f, deps in norm.items():               # euristico). Legare i test a
        if f in kept or not TEST_PAT.search(f):  # tutta la slice esplode sui
            continue                           # repo grandi (pandas: 806 test
        used = sorted(d for d in (set(deps) | refs.get(f, set()))  # via compat)
                      if d in seed_set)
        if used:
            kept[f] = ("test", 1, used[0])
    return kept


# --- output -----------------------------------------------------------------

ORDER = {"seed": 0, "dependency": 1, "importer": 2, "test": 3}
# Estremi ancorati (lost-in-the-middle): il modello attende testa e coda, non il
# mezzo. Ordine SOLO, non selezione: nulla aggiunto/tolto, safe per pi. Il seed
# resta in testa; l'importatore (il caller — "la causa puo' stare nel caller")
# sale all'estremo basso; i test (repro, non causa) scendono in mezzo, dove
# l'attenzione e' minima.
ORDER_ANCHORED = {"seed": 0, "dependency": 1, "test": 2, "importer": 3}


def render(root: str, scanned: int, seeds: list[tuple[str, str]],
           kept: dict[str, tuple[str, int, str]], max_out: int,
           as_json: bool, budget_note: str | None = None,
           t2b: dict | None = None,
           cold: dict[str, int] | None = None,
           dyn_blind: list[str] | None = None,
           suf: tuple[int, int, list[str]] | None = None,
           sym_count: int = 0, cov_note: str | None = None,
           anchor_ends: bool = False) -> str:
    cold = cold or {}
    dyn_blind = dyn_blind or []
    order = ORDER_ANCHORED if anchor_ends else ORDER
    rows = sorted(kept.items(), key=lambda kv: (order[kv[1][0]], kv[1][1], kv[0]))
    truncated = max(0, len(rows) - max_out)
    rows = rows[:max_out]
    excluded = scanned - len(kept)

    if as_json:
        return json.dumps({
            "repo": root, "operator": f"T2@{t2_version()}",
            "scanned": scanned, "kept": len(kept),
            "excluded": excluded, "truncated": truncated,
            "seeds": [{"path": p, "why": w} for p, w in seeds],
            "files": [{"path": p, "role": r, "hop": h, "via": v,
                       **({"graph": "generic"}
                          if os.path.splitext(p)[1] in GENERIC_EXTS else {}),
                       **({"cold": cold[p]} if p in cold else {})}
                      for p, (r, h, v) in rows],
            "note": "exclusion = prior, not prohibition: page fault on demand",
            **({"symbol_index": {"source": "ctags", "symbols": sym_count}}
               if sym_count else {}),
            **({"coverage_note": cov_note} if cov_note else {}),
            **({"sufficiency": {"covered": suf[0], "closure": suf[1],
                                "sufficient": not suf[2],
                                "expected_faults": suf[2]}}
               if suf and suf[1] else {}),
            **({"dynamic_blind": dyn_blind} if dyn_blind else {}),
            **({"budget": budget_note} if budget_note else {}),
            **({"t2b": {"total_tokens": t2b["total"], "fits": t2b["fits"],
                        "slices": [{"seed": s, "symbols": sy, "tokens": tk,
                                    "outcome": e, "extract": c}
                                   for s, sy, tk, e, c in t2b["entries"]]}}
               if t2b else {}),
        }, ensure_ascii=False, indent=1)

    pct = (1 - len(kept) / scanned) * 100 if scanned else 0.0
    out = ["# kernel repo slice — manifest",
           f"operator: T2@{t2_version()}"]
    if budget_note:
        out.append(f"budget: {budget_note}")
    out += [
           f"repo: {root}",
           f"sources scanned: {scanned}  |  slice: {len(kept)} files (-{pct:.0f}%)"]
    gen_exts = sorted({os.path.splitext(p)[1] for p, _ in rows
                       if os.path.splitext(p)[1] in GENERIC_EXTS})
    if gen_exts:
        promo = (f" — PROMOTED by a ctags index ({sym_count} unique symbols): "
                 "precise symbol->definer edges, no longer merely heuristic"
                 if sym_count else "")
        out.append("generic graph (textual references, a declared guarantee "
                   f"weaker than an import graph) for: {', '.join(gen_exts)}"
                   + promo)
    out += ["", "## seeds (from the symptom)"]
    out += [f"- {p}  <- {w}" for p, w in seeds] or ["- (none)"]
    slice_hdr = ("## slice files (by relevance, ends anchored: seeds first, "
                 "importers/callers last; the tests move to the middle — "
                 "lost-in-the-middle)"
                 if anchor_ends else "## slice files (by relevance)")
    out += ["", slice_hdr]
    for p, (role, hop, via) in rows:
        detail = {"seed": "seed",
                  "dependency": f"dependency {hop} hop(s) away (via {via})",
                  "importer": f"importer {hop} hop(s) away from {via}",
                  "test": f"related test (uses {via})"}[role]
        if role != "seed" and os.path.splitext(p)[1] in GENERIC_EXTS:
            detail += " [generic graph]"
        if p in cold:
            detail += (f" [T5 cold: never opened in {cold[p]} sessions — "
                       "wide prior, kept in the slice]")
        out.append(f"- {p} — {detail}")
    if truncated:
        out.append(f"- … {truncated} more files in the slice (raise --max-files)")
    if t2b:
        out += ["", "## T2b — per-symbol slice (file-level budget unsatisfiable)"]
        for s, sy, tk, esito, cmds in t2b["entries"]:
            if sy:
                out.append(f"- {s} :: {', '.join(sy)}  (~{tk} tokens)")
            else:
                out.append(f"- {s} — {esito} (~{tk} tokens)")
            for c in cmds:
                out.append(f"  extract: {c}")
        stato = "FITS the budget" if t2b["fits"] else "still over budget"
        out.append(f"- symbol total: ~{t2b['total']} tokens ({stato}). "
                   "Read the slices with the commands above, NOT the whole "
                   "files; page fault = fall back to the file only if the "
                   "slice is not enough.")
    if cov_note:
        out += ["", "## coverage (dynamic reachability, declared)", cov_note]
    if dyn_blind:
        out += ["", "## unresolved dynamic references (declared blind spots)",
                "dynamic imports in the seeds with a non-literal argument or "
                "outside the repo: NOT guessed (FQCN rule). The graph does not "
                "follow them — if the bug is behind one of these, read the "
                "call site:"]
        out += [f"- {b}" for b in dyn_blind]
    if suf and suf[1]:
        cov, tot, faults = suf
        out += ["", "## sufficiency (T4: predicted distortion, over the static graph)"]
        if not faults:
            out.append(f"SUFFICIENT: the projection contains the whole "
                       f"answer-preserving closure of the seeds ({tot} "
                       "dependency units). No page fault expected from the "
                       "static structure.")
        else:
            shown = faults[:10]
            out.append(f"INSUFFICIENT for the budget/limit: {cov}/{tot} units "
                       f"of the answer-preserving closure present; {len(faults)} "
                       "projected away = EXPECTED PAGE FAULTS (re-read them if "
                       "the reasoning touches them, do not guess):")
            out += [f"- {f}" for f in shown]
            if len(faults) > len(shown):
                out.append(f"- … {len(faults) - len(shown)} more units")
    out += ["", "## outside the slice (page-fault model)",
            f"{excluded} sources excluded by the import graph. The exclusion is "
            "a prior, not a prohibition: if a file outside the slice looks "
            "relevant (config, DI, dynamic imports), read it anyway."]
    return "\n".join(out)


# --- selezione sotto budget (operatore costo) --------------------------------
# Il budget e' in TOKEN, non in file: la risorsa vera e' la finestra di
# contesto che il working set occupera' quando i file verranno letti
# (10 file enormi costano piu' di 100 piccoli). Stima: size/4, la stessa
# euristica di est_tokens in compress.py. La scala e' misurata col bench
# di sufficienza (regge a ogni gradino: la distorsione dipende dai seed).
# --- cache del manifest (operator hash-skip) ---------------------------------
# T2 e' deterministico: stesso repo (fingerprint mtime/size), stesso sintomo,
# stessi parametri, stessa VERSIONE dell'operatore -> stesso manifest.
# La cache salta grafo+grep (pandas: ~12s -> istantaneo). L'hash di versione
# realizza anche il versionamento degli operatori: cambia lo script, cambia
# la chiave, la cache si invalida da sola.
SLICE_CACHE_ENABLED = os.environ.get("CK_SLICE_CACHE", "1") != "0"
SLICE_CACHE_PATH = os.path.expanduser(
    os.environ.get("CK_SLICE_CACHE_PATH", "~/.context-kernel-slice-cache.json"))


def t2_version() -> str:
    """Hash corto di repo_slice.py + slice.py: la versione dell'operatore."""
    h = hashlib.sha1()
    for p in (os.path.abspath(__file__), SLICE_PY):
        try:
            h.update(open(p, "rb").read())
        except Exception:                      # noqa: BLE001
            pass
    return h.hexdigest()[:8]


def _repo_fingerprint(root: str, files: list[str]) -> str:
    h = hashlib.sha1()
    for rel in sorted(files):
        try:
            stt = os.stat(os.path.join(root, rel))
            h.update(f"{rel}:{stt.st_mtime_ns}:{stt.st_size};".encode())
        except Exception:                      # noqa: BLE001
            h.update(f"{rel}:?;".encode())
    return h.hexdigest()


def cache_key(root, files, symptom, explicit, imp_d, deps_d, budget,
              max_files, as_json, priors=None, diff=None, ctags="",
              churn="", cov="", anchor=False) -> str:
    blob = json.dumps({
        "fp": _repo_fingerprint(root, files), "symptom": symptom,
        "seeds": sorted(explicit), "imp": imp_d, "deps": deps_d,
        "budget": budget, "max": max_files, "json": as_json,
        "op": t2_version(), "priors": priors, "diff": sorted(diff or []),
        "dynref": DYNREF_ENABLED, "ctags": ctags, "churn": churn, "cov": cov,
        "anchor": anchor,
    }, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def cache_get(key: str) -> str | None:
    if not SLICE_CACHE_ENABLED:
        return None
    try:
        with open(SLICE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f).get(key, {}).get("out")
    except Exception:                          # noqa: BLE001
        return None


def cache_put(key: str, out: str) -> None:
    if not SLICE_CACHE_ENABLED:
        return
    try:
        try:
            with open(SLICE_CACHE_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:                      # noqa: BLE001
            st = {}
        st[key] = {"out": out, "ts": time.time()}
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-20]:
            st.pop(k, None)
        with open(SLICE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:                          # noqa: BLE001
        pass


# Budget AUTOMATICO: legge lo stato scritto dal hook T1 (compress.py aggiorna
# ~/.context-kernel-context.json a ogni tool call con l'occupazione della
# finestra presa dall'ultimo blocco "usage" del transcript). budget =
# frazione dello headroom. Il PreToolUse inietta `--budget auto` da solo.
CONTEXT_STATE = os.path.expanduser(
    os.environ.get("CK_CONTEXT_STATE", "~/.context-kernel-context.json"))
BUDGET_MAX = int(os.environ.get("CK_BUDGET_MAX", "80000"))


def _resolve_window(model: str | None, used: int) -> tuple[int, str]:
    """Fonte UNICA della finestra: hooks/window.py, caricato per path (lo
    slicer vive in skills/). Fallback locale identico e dichiarato se il
    layout del plugin non c'e' (porting, script copiato da solo)."""
    wp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "..", "..", "hooks", "window.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ck_window",
                                                      os.path.normpath(wp))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.resolve_window(model, used)
    except Exception:                          # noqa: BLE001
        win = int(os.environ.get("CK_CONTEXT_WINDOW", "0") or 0)
        if win > 0:
            return win, "env"
        if "[1m]" in (model or "").lower():
            return 1_000_000, "pattern [1m]"
        return max(200_000, max(0, used) * 115 // 100 + 50_000), "stima"


def auto_budget() -> tuple[int, str]:
    """(budget, spiegazione). Fallback fisso e dichiarato se manca lo stato."""
    try:
        with open(CONTEXT_STATE, encoding="utf-8") as f:
            st = json.load(f)
        sid, rec = max(st.items(), key=lambda kv: kv[1].get("ts", 0))
    except Exception:                          # noqa: BLE001
        return 30_000, "auto: no context state (T1 hook never ran?) -> fallback 30k"
    used = int(rec.get("context_tokens", 0))
    model = rec.get("model") or "?"
    win, _src = _resolve_window(model, used)
    head = max(0, win - used)
    budget = max(8_000, min(int(head * 0.4), BUDGET_MAX))
    age_m = int((time.time() - rec.get("ts", 0)) / 60)
    stale = f" (estimate {age_m}m old)" if age_m > 30 else ""
    return budget, (f"auto: session {sid}, model {model}, window ~{win // 1000}k, "
                    f"in use ~{used // 1000}k, headroom ~{head // 1000}k -> "
                    f"budget {budget // 1000}k{stale}")


BUDGET_LADDER = ((0, 2), (3, 2), (2, 2), (2, 1), (1, 1))

# --- T2b: slice per SIMBOLO quando il budget a livello di file e' --------------
# insoddisfacibile (repo monolitici: pandas/frame.py da solo ~200k token).
# Dai frame del traceback ricava il simbolo top-level che racchiude ogni riga
# e calcola il costo del backward-slice def-use (riusa kernel-slice).
SLICE_PY = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "kernel-slice", "scripts", "slice.py"))


def _load_symbol_slicer():
    try:
        spec = importlib.util.spec_from_file_location("ck_slice", SLICE_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                          # noqa: BLE001
        return None


def _symbol_slice_text(sl, source: str, targets: set[str]) -> str | None:
    """Backward slice def-use (stessa semantica di slice.py, senza stampa).
    None se nessun target e' un simbolo top-level del file."""
    tree = ast.parse(source)
    units = []
    for i, node in enumerate(tree.body):
        b = sl.bound_names(node)
        if not b:
            continue
        units.append((i, node, b, sl.free_names(node) - b,
                      isinstance(node, (ast.Import, ast.ImportFrom))))
    name_to_key = {n: i for i, _, b, _, _ in units for n in b}
    by_key = {i: (node, b, uses, imp) for i, node, b, uses, imp in units}
    seeds = {name_to_key[t] for t in targets if t in name_to_key}
    if not seeds:
        return None
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
    used: set[str] = set()
    for k in keep:
        used |= by_key[k][2]
    for k, (node, b, _u, imp) in by_key.items():
        if imp and (b & used):
            keep.add(k)
    return "\n\n\n".join(ast.unparse(by_key[k][0]) for k in sorted(keep))


def _symbol_targets(source: str, lines: list[int]):
    """Dalle righe dei frame: {"top": {nomi}, "methods": [(cls, met, a, b)]}.
    Se la riga cade in un METODO di una classe top-level, bastano le righe
    del metodo (una classe intera puo' costare quanto il file: NDFrame ~99k)."""
    out = {"top": set(), "methods": []}
    try:
        tree = ast.parse(source)
    except Exception:                          # noqa: BLE001
        return out
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        inside = [ln for ln in lines if node.lineno <= ln <= end]
        if not inside:
            continue
        if isinstance(node, ast.ClassDef):
            for ln in inside:
                meth = None
                for ch in node.body:
                    if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        chend = getattr(ch, "end_lineno", ch.lineno)
                        if ch.lineno <= ln <= chend:
                            meth = (node.name, ch.name, ch.lineno, chend)
                            break
                if meth:
                    if meth not in out["methods"]:
                        out["methods"].append(meth)
                else:
                    out["top"].add(node.name)  # riga nel corpo classe
        else:
            out["top"].add(node.name)
    return out


def _go_symbol_targets(sl, source: str, lines: list[int]) -> set[str]:
    """Nomi delle funzioni Go top-level (metodi inclusi: sono top-level) la cui
    span di righe contiene una riga di frame. In Go non c'e' la nidificazione
    classe/metodo di Python, quindi bastano i nomi top-level."""
    out: set[str] = set()
    try:
        units = sl.go_units(source)
    except Exception:                              # noqa: BLE001
        units = None
    if not units:
        return out
    for kind, _otext, mtext, a, b in units:
        if kind == "func" and any(a <= ln <= b for ln in lines):
            out |= sl._go_bound(kind, mtext)
    return out


def t2b_symbol_slices(root: str, seeds: list[str], symptom: str):
    """Per ogni seed: [(seed, etichette, token, esito, comandi)] + totale.
    Funzioni top-level -> backward slice def-use (slice.py, Python esatto / Go
    conservativo); metodi di classe Python -> solo le righe del metodo (sed);
    niente dal sintomo -> file intero. esito: 'slice' | 'metodi' | 'file
    intero (...)'."""
    sl = _load_symbol_slicer()
    frame_lines: dict[str, list[int]] = {}
    for rx in (PY_FRAME_LINE, GO_FRAME_LINE):
        for m in rx.finditer(symptom):
            p = m.group(1).replace("\\", "/")
            for s in seeds:
                if p == s or p.endswith("/" + s):
                    frame_lines.setdefault(s, []).append(int(m.group(2)))
    entries = []
    total = 0
    for s in seeds:
        full_path = os.path.join(root, s)
        try:
            source = open(full_path, encoding="utf-8", errors="replace").read()
        except Exception:                      # noqa: BLE001
            continue
        whole = len(source) // 4
        if s.endswith(".go") and sl is not None:
            # Go: slice def-use conservativa a livello di funzione top-level.
            gt = _go_symbol_targets(sl, source, frame_lines.get(s, []))
            if not gt:
                entries.append((s, [], whole,
                                "whole file (no symbol from the symptom)", []))
                total += whole
                continue
            try:
                text = sl.slice_go(source, gt)
            except Exception:                      # noqa: BLE001
                text = None
            if text is None:                       # split non fidato / fail-safe
                entries.append((s, [], whole,
                                "whole file (Go slice not trusted)", []))
                total += whole
                continue
            tok = len(text) // 4
            entries.append((s, sorted(gt), tok, "slice",
                            [f"python3 {SLICE_PY} {full_path} "
                             + " ".join(sorted(gt))]))
            total += tok
            continue
        if not s.endswith(".py") or sl is None:
            entries.append((s, [], whole, "whole file (not Python/Go)", []))
            total += whole
            continue
        tg = _symbol_targets(source, frame_lines.get(s, []))
        if not tg["top"] and not tg["methods"]:
            entries.append((s, [], whole,
                            "whole file (no symbol from the symptom)", []))
            total += whole
            continue
        labels: list[str] = []
        cmds: list[str] = []
        tok = 0
        src_lines = source.split("\n")
        for cls, met, a, b in tg["methods"]:
            seg = "\n".join(src_lines[a - 1:b])
            tok += len(seg) // 4
            labels.append(f"{cls}.{met} (lines {a}-{b})")
            cmds.append(f"sed -n '{a},{b}p' {full_path}")
        if tg["top"]:
            try:
                text = _symbol_slice_text(sl, source, tg["top"])
            except Exception:                  # noqa: BLE001
                text = None
            if text is None:
                tok += whole
                labels.append("(slice failed: whole file)")
                cmds.append(f"cat {full_path}")
            else:
                tok += len(text) // 4
                labels += sorted(tg["top"])
                cmds.append(f"python3 {SLICE_PY} {full_path} "
                            + " ".join(sorted(tg["top"])))
        esito = "methods" if tg["methods"] and not tg["top"] else "slice"
        entries.append((s, labels, tok, esito, cmds))
        total += tok
    return entries, total


def _slice_tokens(root: str, kept) -> int:
    tot = 0
    for rel in kept:
        try:
            tot += os.path.getsize(os.path.join(root, rel)) // 4
        except Exception:                      # noqa: BLE001
            pass
    return tot


def _k(n: int) -> str:
    return f"~{n/1000:.1f}k" if n >= 1000 else f"~{n}"


def slice_within_budget(root, graph, seeds, refs, budget: int, symptom: str = ""):
    """Ritorna (kept, nota, t2b). Prova le config dalla piu' ricca; se nessuna
    rientra, fallback minimo seed+test; se nemmeno quello rientra scende a
    granularita' di SIMBOLO sui seed (T2b). t2b = None oppure
    {"entries": [(seed, simboli, token, esito)], "total": N, "fits": bool}."""
    for deps_d, imp_d in BUDGET_LADDER:
        kept = slice_repo(graph, seeds, imp_d, refs, deps_d)
        tok = _slice_tokens(root, kept)
        if tok <= budget:
            nota = (f"<= {_k(budget)} tokens: chose config deps="
                    f"{deps_d or 'full'} imp={imp_d} "
                    f"({len(kept)} files, {_k(tok)} tokens)")
            # il primo-che-rientra e' un criterio di CENSO, non di merito:
            # se nella famiglia c'e' una config piu' piccola ANCORA
            # sufficiente (gap vuoto), e' lei l'argmin — vedi min_sufficient
            better = min_sufficient(root, graph, seeds, refs, kept, budget)
            if better:
                return better[0], f"{nota} | {better[1]}", None
            return kept, nota, None
    minimal = {s: ("seed", 0, "") for s in seeds}
    full = slice_repo(graph, seeds, 0, refs, 1)
    for f, (role, hop, via) in full.items():
        if role == "test" and via in minimal:
            minimal[f] = (role, hop, via)
    tok = _slice_tokens(root, minimal)
    if tok <= budget:
        return minimal, (f"<= {_k(budget)} tokens: minimal seed+test fallback "
                         f"({len(minimal)} files, {_k(tok)} tokens)"), None
    entries, t2b_tok = t2b_symbol_slices(root, seeds, symptom)
    t2b = {"entries": entries, "total": t2b_tok, "fits": t2b_tok <= budget}
    if t2b["fits"]:
        nota = (f"file-level {_k(tok)} tokens > budget {_k(budget)} -> T2b: "
                f"per-SYMBOL slice of the seeds = {_k(t2b_tok)} tokens (fits)")
    else:
        nota = (f"{_k(budget)} tokens UNSATISFIABLE even per symbol: "
                f"file minimum {_k(tok)}, symbol minimum {_k(t2b_tok)} — "
                f"returned the file-level minimum")
    return minimal, nota, t2b


# --- sufficienza (T4): la distorsione PREDETTA, deterministica ----------------
# La D del rate-distortion, calcolata invece che indovinata da un autorater a
# modello (cfr. "Sufficient Context", ICLR 2025): la proiezione P e' SUFFICIENTE
# per i seed sse contiene tutta la CHIUSURA answer-preserving R sul grafo
# statico. R = chiusura BACKWARD delle dipendenze dei seed (imp_d=0: gli
# importatori sono blast-radius, non richiesti dal comportamento del seed; lo
# slice formale def-use e' backward). R\P = unita' che l'answer-preservation
# richiede ma il budget/limite ha tolto = i PAGE FAULT ATTESI, dichiarati in
# anticipo. E' il segnale di astensione del paper, ma esatto: se P!=R, il
# manifest dice DOVE rileggere invece di lasciar indovinare il modello.
# Scope onesto identico al resto: "sufficiente SUL GRAFO STATICO" (gli archi
# dinamici li recupera #2/il page fault). Non tocca pi: misura, non proietta.
def sufficiency_gap(graph, seeds, refs, kept, imp_d):
    """(coperti, totale_R, [file droppati = page fault attesi]). R = chiusura
    delle dipendenze dei seed (deps piene, niente importatori)."""
    ref = slice_repo(graph, seeds, 0, refs, 0)     # imp_d=0, deps_d=0 (piene)
    proj = set(kept)
    dropped = sorted(f for f in ref if f not in proj)
    return len(ref) - len(dropped), len(ref), dropped


# --- argmin |pi_i(C)| soggetto a sufficienza ---------------------------------
# La scala del budget e' di fatto una FAMIGLIA di proiettori Pi_Q = {pi_i}:
# finora se ne sceglieva UNO per censo (il primo che rientra nel budget) o per
# parametri fissi, e la sufficienza veniva solo DICHIARATA a posteriori. Qui
# la sufficienza diventa il vincolo di scelta: fra le config della famiglia
# si prende la slice PIU' PICCOLA (token) che copre l'intera chiusura R dei
# seed — gap vuoto, zero page fault attesi. Due limiti di policy, deliberati:
# (a) mai imp_d=0: gli importatori non pesano nella sufficienza statica ma
#     sono margine di robustezza (chi chiama il seed) — almeno 1 hop resta;
# (b) una config diversa si accetta SOLO a gap vuoto: mai piu' fault attesi
#     della scelta di partenza. Deterministico, solo BFS gia' esistenti.
ARGMIN_ENABLED = os.environ.get("CK_ARGMIN", "1") != "0"
DESCENT_LADDER = BUDGET_LADDER + ((0, 1),)


def min_sufficient(root, graph, seeds, refs, kept, budget=0):
    """(kept', nota) della config piu' piccola della famiglia con sufficienza
    piena e token < della scelta corrente (e <= budget, se dato); None se
    nessuna la batte."""
    if not ARGMIN_ENABLED:
        return None
    cur_tok = _slice_tokens(root, kept)
    best = None                                # (tok, kept, deps_d, imp_d)
    for deps_d, imp_d in DESCENT_LADDER:
        cand = slice_repo(graph, seeds, imp_d, refs, deps_d)
        _cov, _tot, dropped = sufficiency_gap(graph, seeds, refs, cand, imp_d)
        if dropped:                            # fault attesi: fuori famiglia
            continue
        tok = _slice_tokens(root, cand)
        if tok >= cur_tok or (budget and tok > budget):
            continue
        if best is None or tok < best[0]:
            best = (tok, cand, deps_d, imp_d)
    if best is None:
        return None
    tok, cand, deps_d, imp_d = best
    return cand, (f"sufficient argmin: deps={deps_d or 'full'} imp={imp_d} "
                  f"({len(cand)} files, {_k(tok)} tokens, "
                  f"-{1 - tok / cur_tok:.0%} vs the initial choice, "
                  f"0 expected faults)")


# --- prior appresi (loop T5 -> T2) --------------------------------------------
# Scritti da `revealed.py --aggregate --write-priors` (attuazione ESPLICITA
# dell'umano) sui pattern RICORRENTI (>=2 sessioni): seed candidati = file
# aperti FUORI slice in piu' sessioni; freddi = in slice ma mai aperti.
# Direzione fail-safe: i prior AGGIUNGONO seed (mai sostituiscono quelli del
# sintomo) e FLAGGANO i freddi nel manifest (mai esclusi: prior, non divieto).
PRIORS_ENABLED = os.environ.get("CK_PRIORS", "1") != "0"
PRIORS_STATE = os.path.expanduser(
    os.environ.get("CK_PRIORS_STATE", "~/.context-kernel-priors.json"))


def load_priors(root: str) -> dict | None:
    """Record dei prior per questo repo, o None. Mai fatale."""
    if not PRIORS_ENABLED:
        return None
    try:
        with open(PRIORS_STATE, encoding="utf-8") as f:
            st = json.load(f)
        rec = st.get(os.path.normpath(os.path.abspath(root)))
        return rec if isinstance(rec, dict) else None
    except Exception:                          # noqa: BLE001
        return None


def prior_seeds(priors: dict | None, fileset: set[str],
                have: set[str]) -> list[tuple[str, str]]:
    """Seed appresi ancora esistenti nel repo e non gia' in slice."""
    out: list[tuple[str, str]] = []
    for s in (priors or {}).get("seeds") or []:
        p = str(s.get("path") or "").replace(os.sep, "/")
        if p and p in fileset and p not in have:
            out.append((p, "learned prior (T5: opened outside the slice in "
                           f"{s.get('sessions', '?')} sessions)"))
    return out


def cold_map(priors: dict | None) -> dict[str, int]:
    return {str(c.get("path") or ""): int(c.get("sessions") or 0)
            for c in (priors or {}).get("cold") or []}


# --- riferimenti dinamici (attacca il limite #1: grafo solo STATICO) ---------
# La reachability sugli import e' deterministica ma cieca a importlib /
# __import__ / import_module: il grafo non li vede, e la slice puo' escludere
# un file davvero raggiunto a runtime. Un resolver SUPERVISIONATO li scandaglia
# SOLO nei file seed (mai in tutto il repo: sarebbe rumore) e AGGIUNGE un seed
# solo quando l'argomento e' un LETTERALE risolvibile a un file del repo, col
# call site visibile nel motivo. Argomento non letterale (variabile, f-string,
# nome calcolato) o fuori repo -> punto cieco DICHIARATO, mai indovinato: la
# regola FQCN (charter #3) applicata agli import dinamici. Fail-safe identico
# ai prior (charter #5): AGGIUNGE seed, mai slice dai soli riferimenti
# dinamici. Ambito: un hop dai seed (il transitivo dinamico sarebbe illimitato).
DYNREF_ENABLED = os.environ.get("CK_DYNREF", "1") != "0"
_DYN_CALLS = {"import_module", "__import__"}


def _resolve_dyn(mod: str, mm: dict[str, str]) -> str | None:
    """Risoluzione per import_module/__import__: match ESATTO, poi suffisso
    univoco. NIENTE prefix-walk verso il package genitore (a differenza di
    _resolve_py, usata per `import a.b.c`): import_module carica IL modulo
    nominato o fallisce — risalire a un antenato esistente sarebbe indovinare
    (regola FQCN, charter #3). Modulo assente -> None -> punto cieco."""
    hit = mm.get(mod)
    if hit:
        return hit
    tail = "." + mod
    suffix = [f for d, f in mm.items() if d.endswith(tail)]
    return suffix[0] if len(suffix) == 1 else None


def _dyn_call_target(node: ast.Call) -> tuple[str | None, bool]:
    """(modulo_letterale|None, e_import_dinamico). Riconosce
    importlib.import_module(X), import_module(X), __import__(X). arg None =
    import dinamico presente ma argomento NON letterale (punto cieco)."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        name = fn.attr
    elif isinstance(fn, ast.Name):
        name = fn.id
    else:
        return None, False
    if name not in _DYN_CALLS:
        return None, False
    if (node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return node.args[0].value, True
    return None, True


def dynamic_seeds(root: str, seed_files: list[str], files: list[str],
                  have: set[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Scandaglia i file seed per import dinamici. Ritorna
    (seed_aggiunti, punti_ciechi): seed = [(file, motivo col call site)];
    punti_ciechi = ["file:riga (perche')"]. Mai fatale."""
    if not DYNREF_ENABLED:
        return [], []
    root_pkg = (os.path.basename(os.path.normpath(root))
                if os.path.exists(os.path.join(root, "__init__.py")) else None)
    mm = _py_module_map(files, root_pkg)
    added: dict[str, str] = {}
    blind: list[str] = []
    for rel in seed_files:
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, rel),
                                  encoding="utf-8", errors="replace").read())
        except Exception:                      # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            mod, is_dyn = _dyn_call_target(node)
            if not is_dyn:
                continue
            ln = getattr(node, "lineno", 0)
            if mod is None:
                blind.append(f"{rel}:{ln} (non-literal argument)")
                continue
            hit = _resolve_dyn(mod, mm)
            if hit and hit not in have and hit not in added:
                added[hit] = (f'dynamic reference: import "{mod}" '
                              f"({rel}:{ln})")
            elif not hit:
                blind.append(f'{rel}:{ln} ("{mod}" fuori repo o ambiguo)')
    return sorted(added.items()), sorted(set(blind))


# --- slice dal diff (il working set di una PR/review) -------------------------
# Il caso "review" e' identico al caso "sintomo": dati i file toccati da un
# cambiamento, cosa devo leggere per giudicarlo? I file modificati sono i
# seed; dipendenze, importatori (il blast radius) e test correlati arrivano
# dal grafo come sempre.

def git_diff_files(root: str, ref: str) -> tuple[list[str], int]:
    """(file sorgente modificati vs ref, quanti NON-sorgente scartati).
    Solleva RuntimeError se git fallisce (repo non git, ref inesistente)."""
    proc = subprocess.run(
        ["git", "-C", root, "diff", "--name-only", "--diff-filter=d", ref],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "git diff failed").strip()[:200])
    changed = [l.strip() for l in proc.stdout.split("\n") if l.strip()]
    src = [f for f in changed
           if os.path.splitext(f)[1].lower() in SRC_EXTS]
    return src, len(changed) - len(src)


# --- prior da accoppiamento evolutivo (git co-change) ------------------------
# Prior COLD-START ortogonale a T5 (che impara da cosa hai aperto tu): cosa il
# REPO cambia insieme. Se nella storia git i file X e Y sono stati toccati negli
# STESSI commit, un task su X ha buone probabilita' di toccare Y (logical
# coupling, un segnale classico). Disponibile alla PRIMA sessione, quando T5 non
# ha ancora dati. Direzione fail-safe identica ai prior T5 (charter #5):
# AGGIUNGE seed col motivo, non esclude mai, non semina una slice da solo (gira
# solo se il sintomo ha gia' prodotto seed). Recurrence >=2 come le altre
# attuazioni di prior (charter #2): un co-cambio isolato non basta.
CHURN_ENABLED = os.environ.get("CK_CHURN", "1") != "0"
CHURN_COMMITS = int(os.environ.get("CK_CHURN_COMMITS", "200"))
CHURN_MIN = int(os.environ.get("CK_CHURN_MIN", "2"))       # co-change >= 2 commit
CHURN_MAX = int(os.environ.get("CK_CHURN_MAX", "5"))       # cap sui seed aggiunti


def git_cochange(root: str, seed_files: set[str],
                 fileset: set[str]) -> list[tuple[str, int]]:
    """[(file, n_commit)] dei file che co-cambiano coi seed nella storia recente
    (>= CHURN_MIN, cap CHURN_MAX, per co-change decrescente). Mai fatale: repo
    non-git / git assente -> []."""
    if not CHURN_ENABLED or not seed_files:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", root, "log", "--no-merges", f"-{CHURN_COMMITS}",
             "--name-only", "--pretty=format:@"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return []
    except Exception:                          # noqa: BLE001
        return []
    counts: dict[str, int] = {}
    cur: set[str] = set()

    def flush() -> None:
        if cur & seed_files:                   # commit che tocca un seed
            for f in cur - seed_files:
                counts[f] = counts.get(f, 0) + 1

    for line in proc.stdout.split("\n"):
        if line == "@":
            flush()
            cur = set()
        elif line.strip():
            cur.add(line.strip().replace(os.sep, "/"))
    flush()
    out = [(f, n) for f, n in counts.items()
           if n >= CHURN_MIN and f in fileset]
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:CHURN_MAX]


# --- reachability DINAMICA da un artefatto di copertura ----------------------
# Il grafo e' statico; import dinamici / DI / config / reflection gli sfuggono.
# Ma se il repo ha un file di copertura (l'ha prodotto una run di test), quello
# registra i file REALMENTE eseguiti: verita' d'esecuzione, non euristica. I
# file eseguiti che il grafo statico NON raggiunge dai seed sono la reachability
# dinamica mancante -> prior additivi (charter #5: aggiungono, mai escludono;
# la copertura e' un prior, non un divieto — un file non coperto puo' comunque
# servire). Se sono troppi (copertura di suite intera, non del solo scenario)
# non si semina alla cieca: si DICHIARA il conteggio come hint di page fault.
# Stesso pattern zero-dip di ctags (#5), ma sull'asse dinamico. CK_COV=0 spegne.
COV_ENABLED = os.environ.get("CK_COV", "1") != "0"
COV_MAX = int(os.environ.get("CK_COV_MAX", "12"))


def _relify(p: str, root: str) -> str:
    p = p.replace("\\", "/")
    r = root.replace(os.sep, "/").rstrip("/") + "/"
    if p.startswith(r):
        p = p[len(r):]
    return p.lstrip("./")


def _map_cov(cp: str, root: str, fileset: set[str]) -> str | None:
    """Path di copertura -> file del repo: relativizza, poi match esatto o
    suffisso UNIVOCO (mai indovinare, regola FQCN)."""
    p = _relify(cp, root)
    if p in fileset:
        return p
    cands = [f for f in fileset if f.endswith("/" + p) or p.endswith("/" + f)]
    return cands[0] if len(cands) == 1 else None


def _cov_from_sqlite(path: str) -> set[str]:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            try:                                   # coverage.py v5+ (line_bits)
                rows = cur.execute(
                    "SELECT DISTINCT f.path FROM file f "
                    "JOIN line_bits lb ON lb.file_id = f.id").fetchall()
            except Exception:                      # noqa: BLE001 — schema arcs/vecchio
                rows = cur.execute("SELECT path FROM file").fetchall()
            return {r[0] for r in rows if r and r[0]}
        finally:
            con.close()
    except Exception:                              # noqa: BLE001
        return set()


def _cov_from_cobertura(path: str) -> set[str]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except Exception:                              # noqa: BLE001
        return set()
    files = set()
    for cls in root.iter("class"):
        fn = cls.get("filename")
        if fn and any(int(l.get("hits", "0")) > 0 for l in cls.iter("line")):
            files.add(fn)
    return files


def _cov_from_lcov(path: str) -> set[str]:
    files: set[str] = set()
    cur = None
    hit = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SF:"):
                    cur, hit = line[3:], False
                elif line.startswith("DA:"):
                    try:
                        if int(line[3:].rsplit(",", 1)[1]) > 0:
                            hit = True
                    except Exception:              # noqa: BLE001
                        pass
                elif line == "end_of_record":
                    if cur and hit:
                        files.add(cur)
                    cur = None
    except Exception:                              # noqa: BLE001
        return set()
    return files


def coverage_files(root: str, fileset: set[str]) -> tuple[set[str], str]:
    """(file del repo eseguiti secondo l'artefatto, fingerprint). Prova
    coverage.xml, lcov.info, .coverage alla radice. ({}, '') se assente."""
    if not COV_ENABLED:
        return set(), ""
    for name, parser in (("coverage.xml", _cov_from_cobertura),
                         ("lcov.info", _cov_from_lcov),
                         (".coverage", _cov_from_sqlite)):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        raw = parser(p)
        if not raw:
            continue
        mapped = {m for cp in raw for m in (_map_cov(cp, root, fileset),) if m}
        try:
            stt = os.stat(p)
            fp = f"{name}:{stt.st_mtime_ns}:{stt.st_size}"
        except Exception:                          # noqa: BLE001
            fp = name
        return mapped, fp
    return set(), ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--symptom", default="")
    ap.add_argument("--symptom-file")
    ap.add_argument("--seed", action="append", default=[])
    ap.add_argument("--from-diff", nargs="?", const="HEAD", default=None,
                    metavar="REF",
                    help="seed the slice from the files changed against REF "
                         "(git diff --name-only; default HEAD). For a PR: "
                         "--from-diff main...")
    ap.add_argument("--importers-depth", type=int, default=2)
    ap.add_argument("--deps-depth", type=int, default=0,
                    help="limit the dependency closure (0 = complete)")
    ap.add_argument("--budget", default="0",
                    help="budget in estimated TOKENS (size/4) for the working "
                         "set, or 'auto' (window - used, from the T1 hook "
                         "state); 0 = off")
    ap.add_argument("--max-files", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--anchor-ends", action="store_true",
                    help="anchor the ends against lost-in-the-middle: seeds "
                         "first, importers/callers last, the tests moved to "
                         "the middle. ORDER only (no selection), safe for pi. "
                         "Off by default")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"repo not found: {root}", file=sys.stderr)
        return 2
    symptom = args.symptom
    if args.symptom_file:
        symptom += "\n" + open(args.symptom_file, encoding="utf-8",
                               errors="replace").read()

    files = collect_files(root)
    if not files:
        print("no source found", file=sys.stderr)
        return 2

    # budget risolto subito: serve alla chiave di cache
    auto_why = None
    if str(args.budget).strip().lower() == "auto":
        budget, auto_why = auto_budget()
    else:
        try:
            budget = int(args.budget)
        except ValueError:
            budget = 0

    priors = load_priors(root)

    diff_files: list[str] = []
    if args.from_diff:
        try:
            diff_files, skipped = git_diff_files(root, args.from_diff)
        except Exception as e:                 # noqa: BLE001
            print(f"--from-diff {args.from_diff}: {e}", file=sys.stderr)
            return 2
        if skipped:
            print(f"--from-diff: {skipped} changed non-source files "
                  "(doc/config) excluded from the seeds", file=sys.stderr)
        if not diff_files and not symptom and not args.seed:
            print(f"--from-diff {args.from_diff}: no source changed "
                  "— nothing to slice", file=sys.stderr)

    # cache PRIMA delle parti costose (grep dei letterali + grafo import).
    # indice ctags (se il repo ne shippa uno): promuove il grafo generico.
    # Il suo fingerprint entra nella chiave: cambia il tags, cambia la cache.
    sym_map = _ctags_map(root)
    try:
        stt = os.stat(os.path.join(root, CTAGS_FILE))
        ctags_fp = f"{stt.st_mtime_ns}:{stt.st_size}" if sym_map else ""
    except Exception:                          # noqa: BLE001
        ctags_fp = ""
    # HEAD nella chiave: il co-change dipende dalla storia git; nuovo commit ->
    # cache invalidata -> prior evolutivi ricalcolati.
    churn_fp = ""
    if CHURN_ENABLED:
        try:
            hp = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
            churn_fp = hp.stdout.strip() if hp.returncode == 0 else ""
        except Exception:                      # noqa: BLE001
            churn_fp = ""
    # copertura: verita' d'esecuzione (prior dinamico). Fingerprint in chiave.
    cov_all, cov_fp = coverage_files(
        root, {f.replace(os.sep, "/") for f in files})
    # Prior e diff entrano nella chiave: cambiano il manifest, cambia la cache.
    key = cache_key(root, files, symptom, args.seed, args.importers_depth,
                    args.deps_depth, budget, args.max_files, args.json,
                    priors, diff_files, ctags_fp, churn_fp, cov_fp,
                    args.anchor_ends)
    hit = cache_get(key)
    if hit is not None:
        print(hit)
        if not args.json:
            print(f"[cache T2@{t2_version()}: manifest reused — repo, "
                  "symptom, parameters and operator unchanged]")
        return 0

    seeds = find_seeds(root, files, symptom, args.seed, diff_files,
                       f"file changed in the diff ({args.from_diff})"
                       if args.from_diff else "file changed in the diff")
    dyn_blind: list[str] = []
    if seeds:
        # i prior appresi AGGIUNGONO seed (mai creano una slice da soli:
        # senza seed dal sintomo il fail-safe resta "nessuna proiezione")
        fileset = {f.replace(os.sep, "/") for f in files}
        learned = prior_seeds(priors, fileset, {s for s, _ in seeds})
        if learned:
            seeds = sorted(seeds + learned)
        # riferimenti dinamici: import non statici nei seed -> seed aggiunti
        # (col call site) + punti ciechi dichiarati. Stessa direzione dei prior.
        dyn, dyn_blind = dynamic_seeds(root, [s for s, _ in seeds], files,
                                       {s for s, _ in seeds})
        if dyn:
            seeds = sorted(seeds + dyn)
        # accoppiamento evolutivo (git co-change): prior cold-start additivo.
        have = {s for s, _ in seeds}
        cochange = [(f, f"co-changed with the seed in {n} commits (git)")
                    for f, n in git_cochange(root, have, fileset)
                    if f not in have]
        if cochange:
            seeds = sorted(seeds + cochange)
    if not seeds:
        print("WARNING: no seed recognised in the symptom — slice impossible.\n"
              "Pass --seed <file>, or include a stack trace / error message "
              "in the symptom. Fail-safe: no projection applied.",
              file=sys.stderr)
        print(render(root, len(files), [], {}, args.max_files, args.json,
                     anchor_ends=args.anchor_ends))
        return 0

    graph = build_graph(root, files, sym_map)
    refs = _test_ref_edges(root, files)
    # copertura: i file ESEGUITI fuori dalla chiusura statica dei seed sono la
    # reachability DINAMICA che il grafo perde -> seed additivi (charter #5).
    # Troppi (copertura di suite intera) -> non seminare alla cieca: dichiara.
    cov_note = None
    if cov_all:
        static_reach = set(slice_repo(
            graph, [s for s, _ in seeds], args.importers_depth, refs, 0))
        have = {s for s, _ in seeds}
        covdiff = sorted(f for f in cov_all
                         if f not in static_reach and f not in have)
        if 0 < len(covdiff) <= COV_MAX:
            seeds = sorted(seeds + [
                (f, "executed at runtime (coverage), invisible to the "
                    "static graph from the seeds") for f in covdiff])
        elif len(covdiff) > COV_MAX:
            cov_note = (f"{len(covdiff)} executed files (coverage) outside the "
                        "static graph from the seeds — too many to seed "
                        "precisely (whole-suite coverage, not just this "
                        "scenario); read them selectively if the reasoning "
                        "touches them")
    budget_note = None
    t2b = None
    if budget:
        kept, budget_note, t2b = slice_within_budget(
            root, graph, [s for s, _ in seeds], refs, budget, symptom)
        if auto_why:
            budget_note = f"{auto_why} | {budget_note}"
    else:
        kept = slice_repo(graph, [s for s, _ in seeds], args.importers_depth,
                          refs, args.deps_depth)
        better = min_sufficient(root, graph, [s for s, _ in seeds], refs, kept)
        if better:
            kept, budget_note = better
    suf = sufficiency_gap(graph, [s for s, _ in seeds], refs, kept,
                          args.importers_depth)
    out = render(root, len(files), seeds, kept, args.max_files, args.json,
                 budget_note, t2b, cold_map(priors), dyn_blind, suf,
                 len(sym_map), cov_note, args.anchor_ends)
    cache_put(key, out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
