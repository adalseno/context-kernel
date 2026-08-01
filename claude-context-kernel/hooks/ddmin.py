#!/usr/bin/env python3
"""
ddmin.py — EMPIRICAL minimality (delta debugging, Zeller & Hildebrandt 2002).

The sufficiency oracle (T4) PREDICTS distortion over the static graph; ddmin
PROVES it by running: given an input that reproduces a failure and a pass/fail
oracle, it finds the 1-MINIMAL subset that STILL reproduces — no further
element can be removed without losing the failure. It is the maximum rate at
zero distortion, measured instead of estimated, on the QUERY side: a minimised
symptom/repro is a tighter Q -> a more precise projection (T2). Deterministic,
stdlib, zero API.

    # minimise the lines of an input that still make a command fail
    python3 ddmin.py --oracle 'python3 buggy.py {} ; test $? -eq 1' \
                     --input case.txt --unit line

    # minimise character by character (e.g. a JSON path that triggers a panic)
    python3 ddmin.py --oracle './repro.sh {}' --input payload.txt --unit char

ORACLE CONTRACT: it receives the candidate (the {} placeholder replaced by the
path of a temporary file; without {} the candidate arrives on stdin) and EXITS
with --fail-exit (default 0) when the failure is STILL present ("reproduces").
Any other exit = does not reproduce. You write the oracle: it is what defines
"the same failure" (a grep on the panic, an exit code, a diff).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

try:
    import _utf8  # noqa: F401 — stream UTF-8 (Windows)
except ImportError:
    pass


def ddmin(units: list[str], reproduces) -> list[str]:
    """1-minimal failing subset (isolamento). `reproduces`: list -> bool.
    POSIZIONALE (indici, non valori): corretto anche con righe/caratteri
    duplicati. Deterministico: stessa lista + stesso oracolo -> stesso esito.
    Zeller & Hildebrandt: raddoppia la granularita' finche' togliere un blocco
    (o restare col suo complemento) mantiene il fallimento; ferma a
    1-minimalita' (nessun singolo elemento in piu' e' rimovibile)."""
    n = 2
    while len(units) >= 2:
        size = max(1, len(units) // n)
        starts = list(range(0, len(units), size))
        reduced = False
        # prima i complementi (riducono di piu'), poi i singoli blocchi
        for s in starts:
            complement = units[:s] + units[s + size:]
            if 0 < len(complement) < len(units) and reproduces(complement):
                units = complement
                n = max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            for s in starts:
                chunk = units[s:s + size]
                if 0 < len(chunk) < len(units) and reproduces(chunk):
                    units = chunk
                    n = 2
                    reduced = True
                    break
        if not reduced:
            if n >= len(units):
                break
            n = min(len(units), 2 * n)
    return units


def _split(text: str, unit: str) -> list[str]:
    if unit == "char":
        return list(text)
    return text.split("\n")


def _join(units: list[str], unit: str) -> str:
    return "".join(units) if unit == "char" else "\n".join(units)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oracle", required=True,
                    help="command; {} = path of the candidate (without {} -> stdin); "
                         "exit --fail-exit = reproduces")
    ap.add_argument("--input", required=True, help="file holding the input to minimise")
    ap.add_argument("--unit", choices=("line", "char"), default="line")
    ap.add_argument("--fail-exit", type=int, default=0,
                    help="oracle exit code that means 'reproduces' (default 0)")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    try:
        text = open(args.input, encoding="utf-8", errors="replace").read()
    except Exception as e:                         # noqa: BLE001
        print(f"input not readable: {e}", file=sys.stderr)
        return 2

    calls = {"n": 0}

    def reproduces(units: list[str]) -> bool:
        calls["n"] += 1
        cand = _join(units, args.unit)
        fd, tmp = tempfile.mkstemp(prefix="ddmin-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(cand)
            if "{}" in args.oracle:
                cmd = args.oracle.replace("{}", tmp)
                stdin = None
            else:
                cmd = args.oracle
                stdin = cand
            try:
                p = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=args.timeout,
                                   input=stdin)
            except subprocess.TimeoutExpired:
                return False                       # timeout = non riproduce
            return p.returncode == args.fail_exit
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    units = _split(text, args.unit)
    if not reproduces(units):
        print("the FULL input does not reproduce (exit != --fail-exit): "
              "check the oracle or --fail-exit. Nothing to minimise.",
              file=sys.stderr)
        return 2

    before = len(units)
    minimal = ddmin(units, reproduces)
    out = _join(minimal, args.unit)
    print(out)
    pct = (1 - len(minimal) / before) * 100 if before else 0.0
    print(f"[ddmin: {before} -> {len(minimal)} {args.unit} (-{pct:.0f}%), "
          f"{calls['n']} oracle calls, 1-minimal]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
