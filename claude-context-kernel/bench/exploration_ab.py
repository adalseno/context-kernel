#!/usr/bin/env python3
"""
exploration_ab.py — l'iniezione ambientale del working set AIUTA?

Esperimento controllato sul costo di ESPLORAZIONE: per K raise-site reali
del repo (protocollo di sufficiency_bench) si sintetizza un sintomo
DEGRADATO — classe d'errore + poche parole del messaggio + file chiamante,
come i bug report veri — e si chiede a un agente di localizzare il punto
esatto che solleva l'errore. Due bracci per caso:

  control : solo il sintomo degradato;
  slice   : sintomo + manifest T2 (slicer deterministico, deps<=1 imp<=1)
            iniettato nel prompt — cio' che fa posttool_symptom.

Metriche per run: tool call distinte, token di input processati, secondi,
localizzazione corretta (il file del raise compare nella risposta finale).

Runner (CK_AB_EXPLORE_RUNNER): `claude` (default — claude -p headless,
modello CK_AB_EXPLORE_MODEL, default haiku, tool Read/Grep/Glob) oppure
`pi` (serve un modello con function calling vero, es. deepseek-v4-flash
con DEEPSEEK_API_KEY). Negli agent figli gli hook context-kernel sono
SPENTI via env: symptom_slice inietterebbe la slice anche nel control.

Primo run (2026-07-17, Django 2972 file, 5 casi, haiku): correttezza 5/5
in ENTRAMBI i bracci; call medie 5.8->4.8 (-17%), token processati
186k->164k (-12%). Varianza alta: il beneficio si concentra sui casi a
esplorazione lunga (caso peggiore: 10->6 call, 304k->148k), sui casi
facili il manifest puo' perfino allungare la strada. N piccolo:
preliminare, riproducibile.

Uso:
    python3 exploration_ab.py <repo> [--cases 5] [--json]
                              [--difficulty easy|hard]

`--difficulty hard` toglie ANCHE le parole del messaggio (resta classe
d'errore + modulo chiamante): il grep del letterale diventa impossibile e
l'esplorazione deve navigare il grafo — il regime dove l'ipotesi prevede
che la correttezza possa divergere tra i bracci.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH_DIR)
import sufficiency_bench as sb                  # noqa: E402

SLICER = sb.SLICER
RUNNER = os.environ.get("CK_AB_EXPLORE_RUNNER", "claude")  # claude | pi
PROVIDER = os.environ.get("CK_AB_EXPLORE_PROVIDER", "deepseek")
MODEL = os.environ.get("CK_AB_EXPLORE_MODEL",
                       "haiku" if RUNNER == "claude" else "deepseek-v4-flash")
TIMEOUT = int(os.environ.get("CK_AB_EXPLORE_TIMEOUT", "240"))
MAX_TURNS = os.environ.get("CK_AB_EXPLORE_MAX_TURNS", "20")

# Igiene sperimentale: nelle run figlie gli hook del kernel vanno SPENTI,
# altrimenti symptom_slice inietterebbe la slice anche nel braccio control
# (il prompt contiene un sintomo) e T1 sporcherebbe il conteggio token.
CHILD_ENV = {**os.environ,
             "CK_SYMPTOM_MIN_FILES": "9999999",
             "CK_POST_SYMPTOM": "0",
             "CK_MIN_TOKENS": "9999999",
             "CK_DELTA": "0",
             "CK_PRETOOL": "0"}

PROMPT = """You are working in the repository in the current directory. A user \
reports this error, in the partial form you get in the real world:

{symptom}

Find the EXACT point in the repository that RAISES this error. Explore with \
the tools available (read, grep, find, read-only bash). Do not modify \
anything. When you are sure, the LAST line of your answer must be ONLY:
relative/path/file.py::function_name"""

INJECT = """

[context-kernel] A tool has already computed the working set relevant to \
this symptom (deterministic slicer over the import graph). Use it as a \
prior, not as a prohibition:

{manifest}"""


def degraded_symptom(err: str, msg: str, caller: str,
                     difficulty: str = "easy") -> str:
    """Due gradi di degradazione. `easy` (run 1.8.0): classe + 3 parole del
    messaggio + modulo chiamante. `hard`: SOLO classe + chiamante — senza
    parole del messaggio il grep diretto del letterale e' impossibile e
    l'esplorazione deve navigare il grafo: e' il regime dove l'ipotesi
    prevede che la correttezza possa divergere tra i bracci."""
    if difficulty == "hard":
        return (f"{err} somewhere during execution.\n"
                f"(no message available, traceback lost; the module involved "
                f"on the application side should be {caller})")
    vague = " ".join(msg.split()[:3])
    return (f"{err}: {vague} ...\n"
            f"(the traceback was lost; the module involved on the application "
            f"side should be {caller})")


def slice_manifest(root: str, symptom: str) -> str:
    proc = subprocess.run(
        [sys.executable, SLICER, root, "--symptom", symptom,
         "--deps-depth", "1", "--importers-depth", "1"],
        capture_output=True, text=True, timeout=120)
    lines = (proc.stdout or "").strip().split("\n")
    return "\n".join(lines[:80])


def _run_pi(root: str, prompt: str) -> str:
    proc = subprocess.run(
        ["pi", "--mode", "json", "-p", "--no-session",
         "--provider", PROVIDER, "--model", MODEL, "-t", "read,bash", prompt],
        cwd=root, capture_output=True, text=True, timeout=TIMEOUT,
        env=CHILD_ENV)
    return proc.stdout


def _run_claude(root: str, prompt: str) -> str:
    # prompt via stdin: --allowedTools e' variadico e inghiottirebbe un
    # argomento posizionale messo dopo di lui
    proc = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--output-format", "stream-json",
         "--verbose", "--max-turns", MAX_TURNS,
         "--allowedTools", "Read,Grep,Glob"],
        cwd=root, input=prompt, capture_output=True, text=True,
        timeout=TIMEOUT, env=CHILD_ENV)
    return proc.stdout


def run_agent(root: str, prompt: str) -> dict:
    t0 = time.time()
    raw = _run_pi(root, prompt) if RUNNER == "pi" else _run_claude(root, prompt)
    calls = (len(set(re.findall(r'"toolCallId":"([^"]+)"', raw)))
             if RUNNER == "pi" else raw.count('"type":"tool_use"'))
    tokens_in = 0
    final = ""
    for line in raw.split("\n"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if RUNNER == "pi":
            if ev.get("type") != "message_end":
                continue
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            tokens_in += int((msg.get("usage") or {}).get("input") or 0)
            text = "\n".join(p.get("text", "")
                             for p in (msg.get("content") or [])
                             if p.get("type") == "text")
            if text.strip():
                final = text
        else:
            if ev.get("type") == "result":
                final = str(ev.get("result") or final)
                u = ev.get("usage") or {}
                tokens_in = (int(u.get("input_tokens") or 0)
                             + int(u.get("cache_read_input_tokens") or 0)
                             + int(u.get("cache_creation_input_tokens") or 0))
    return {"calls": calls, "tokens_in": tokens_in,
            "seconds": round(time.time() - t0, 1), "answer": final[-300:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--cases", type=int, default=5)
    ap.add_argument("--difficulty", choices=("easy", "hard"), default="easy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rs = sb._load_slicer()
    root = os.path.abspath(args.root)
    files = [f.replace(os.sep, "/") for f in rs.collect_files(root)]
    graph = rs.build_graph(root, files)
    reverse: dict[str, set[str]] = {}
    for f, deps in graph.items():
        for d in deps:
            reverse.setdefault(d, set()).add(f)
    cases = sb.find_cases(rs, root, files, reverse, args.cases)
    if not cases:
        print("no case", file=sys.stderr)
        return 2

    rows = []
    for i, (raise_file, caller, err, msg) in enumerate(cases):
        symptom = degraded_symptom(err, msg, caller, args.difficulty)
        manifest = slice_manifest(root, symptom)
        for arm in ("control", "slice"):
            prompt = PROMPT.format(symptom=symptom)
            if arm == "slice":
                prompt += INJECT.format(manifest=manifest)
            try:
                r = run_agent(root, prompt)
            except subprocess.TimeoutExpired:
                r = {"calls": -1, "tokens_in": -1, "seconds": TIMEOUT,
                     "answer": "(timeout)"}
            correct = (raise_file in r["answer"]
                       or os.path.basename(raise_file) in r["answer"])
            rows.append({"case": i, "raise_file": raise_file, "arm": arm,
                         "correct": correct, **r})
            print(f"caso {i} [{arm:7s}] calls={r['calls']:>3} "
                  f"tok_in={r['tokens_in']:>7} {r['seconds']:>6}s "
                  f"{'OK' if correct else 'MISS'} ({raise_file})",
                  file=sys.stderr)

    def agg(arm: str, key: str) -> float:
        vals = [r[key] for r in rows if r["arm"] == arm and r[key] >= 0]
        return round(sum(vals) / len(vals), 1) if vals else -1

    summary = {a: {"calls": agg(a, "calls"), "tokens_in": agg(a, "tokens_in"),
                   "seconds": agg(a, "seconds"),
                   "correct": sum(1 for r in rows
                                  if r["arm"] == a and r["correct"])}
               for a in ("control", "slice")}
    out = {"repo": root, "model": f"{PROVIDER}/{MODEL}",
           "difficulty": args.difficulty,
           "cases": len(cases), "rows": rows, "summary": summary}
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"\n# exploration A/B — {root} ({len(cases)} casi, "
              f"{PROVIDER}/{MODEL})")
        for a in ("control", "slice"):
            s = summary[a]
            print(f"{a:8s} calls medi {s['calls']:>5} | tok_in medi "
                  f"{s['tokens_in']:>8} | {s['seconds']:>5}s | "
                  f"corrette {s['correct']}/{len(cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
