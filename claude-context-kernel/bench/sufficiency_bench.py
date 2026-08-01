#!/usr/bin/env python3
"""
sufficiency_bench.py — misura OGGETTIVA della distorsione dello slicer (T2).

Il problema: il rate (token rimossi) e' oggettivo, ma finora la distorsione
(A(C')=A(C)) era giudicata in-sessione dal modello. Qui usiamo un oracolo
deterministico: la SUFFICIENZA. Per K bug-site reali del repo (statement
`raise` con messaggio letterale distintivo) sintetizziamo il sintomo PARZIALE
che un utente riporterebbe — il frame del CALLER (non del raise-site!) piu'
il messaggio d'errore — e misuriamo se lo slicer riporta il raise-site nel
working set. Se il file che solleva l'errore e' fuori slice, la risposta al
task "trova e fixa il bug" cambia di sicuro: distorsione = 1 per quel caso.

Il sintomo e' volutamente parziale (niente frame del raise-site): il
raise-site deve entrare via grafo (deps del caller) o via grep del letterale
— le stesse strade che il sintomo reale offre. Zero rete, zero API, stdlib.

Uso:
    python3 sufficiency_bench.py <repo> [--cases 20] [--json]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
SLICER = os.path.join(os.path.dirname(BENCH_DIR), "skills", "kernel-repo-slice",
                      "scripts", "repo_slice.py")

RAISE_PAT = re.compile(
    r'raise\s+(\w+)\(\s*f?["\']([^"\'{}\n]{20,120})["\']', re.MULTILINE)

# configurazioni (deps_depth, importers_depth) da confrontare
CONFIGS = ((0, 2), (2, 2), (1, 1))


def _load_slicer():
    spec = importlib.util.spec_from_file_location("ck_repo_slice", SLICER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_cases(rs, root: str, files: list[str], reverse: dict, n: int):
    """[(raise_file, caller, error_class, message)] deterministici."""
    cases = []
    for rel in sorted(files):
        if not rel.endswith(".py") or rs.TEST_PAT.search(rel):
            continue
        importers = sorted(reverse.get(rel, ()))
        callers = [i for i in importers if not rs.TEST_PAT.search(i)]
        if not callers:
            continue                            # senza caller niente sintomo parziale
        try:
            src = open(os.path.join(root, rel), encoding="utf-8",
                       errors="replace").read()
        except Exception:                       # noqa: BLE001
            continue
        m = RAISE_PAT.search(src)
        if m:
            cases.append((rel, callers[0], m.group(1), m.group(2)))
    if len(cases) <= n:
        return cases
    stride = len(cases) / n                     # campione uniforme, deterministico
    return [cases[int(i * stride)] for i in range(n)]


def run(root: str, n_cases: int, as_json: bool) -> int:
    rs = _load_slicer()
    root = os.path.abspath(root)
    files = [f.replace(os.sep, "/") for f in rs.collect_files(root)]
    if not files:
        print("no source", file=sys.stderr)
        return 2
    graph = rs.build_graph(root, files)
    reverse: dict[str, set[str]] = {}
    for f, deps in graph.items():
        for d in deps:
            reverse.setdefault(d, set()).add(f)
    refs = rs._test_ref_edges(root, files)

    cases = find_cases(rs, root, files, reverse, n_cases)
    if not cases:
        print("no candidate raise site (with caller and literal message)",
              file=sys.stderr)
        return 2

    case_seeds = []                            # i seed non dipendono dalla config
    for raise_file, caller, err, msg in cases:
        symptom = (f"Traceback (most recent call last):\n"
                   f'  File "{os.path.join(root, caller)}", line 1, in <module>\n'
                   f"{err}: {msg}")
        case_seeds.append([s for s, _ in rs.find_seeds(root, files, symptom, [])])

    results = []
    for deps_d, imp_d in CONFIGS:
        ok = 0
        tot_slice = 0
        misses = []
        for (raise_file, caller, err, msg), seeds in zip(cases, case_seeds):
            kept = rs.slice_repo(graph, seeds, imp_d, refs, deps_d) if seeds else {}
            tot_slice += len(kept)
            if raise_file in kept:
                ok += 1
            else:
                misses.append(raise_file)
        results.append({
            "deps_depth": deps_d, "importers_depth": imp_d,
            "cases": len(cases),
            "sufficiency": round(ok / len(cases), 3),
            "rate": round(1 - (tot_slice / len(cases)) / len(files), 3),
            "slice_medio": round(tot_slice / len(cases), 1),
            "miss": misses[:5],
        })

    out = {"repo": root, "scanned": len(files), "results": results}
    if as_json:
        print(json.dumps(out, indent=1))
        return 0
    print(f"# sufficiency bench — {root}")
    print(f"sorgenti: {len(files)} | casi: {len(cases)} "
          f"(real raise sites, partial symptom: caller frame + message)")
    print(f"{'deps':>5} {'imp':>4} {'suff.':>7} {'rate':>7} {'slice medio':>12}")
    for r in results:
        print(f"{r['deps_depth']:>5} {r['importers_depth']:>4} "
              f"{r['sufficiency']:>6.0%} {r['rate']:>6.0%} {r['slice_medio']:>12}")
        if r["miss"]:
            print(f"      miss: {', '.join(r['miss'])}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--cases", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return run(args.root, args.cases, args.json)


if __name__ == "__main__":
    sys.exit(main())
