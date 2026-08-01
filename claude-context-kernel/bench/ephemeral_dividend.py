#!/usr/bin/env python3
"""
ephemeral_dividend.py — misura del dividendo del parcheggio (1.16.0).

La tesi: l'inversa garantita (parcheggio + recall mirato) autorizza un tasso
di compressione piu' aggressivo sugli output EFFIMERI (Bash/WebFetch/MCP).
Qui la tesi si misura su corpus REALE — gli output dei tool registrati nei
transcript locali di Claude Code — confrontando due bracci:

    A (baseline):   scala live di default        (adaptive_start 0.75)
    B (dividendo):  scala live * CK_EPHEMERAL_SCALE (0.75 * 0.5)

Metriche, tutte deterministiche (zero API, zero rete, stdlib):
  - economia: token dopo-compressione per braccio, extra risparmio di B;
  - ritenzione del segnale: OGNI riga dell'originale (normalizzato) che
    matcha la regex di segnale deve comparire nell'output compresso di
    ENTRAMBI i bracci — e' l'invariante per costruzione dell'operatore
    (le righe di segnale del corpo non vengono MAI elise), qui confermato
    empiricamente, non assunto;
  - pressione sul parcheggio: elisioni per braccio (ogni elisione effimera
    e' una voce parcheggiata: dimensiona il costo dell'aggressivita').

I CONTENUTI restano locali: lo script stampa solo aggregati.

Uso:
    python3 ephemeral_dividend.py [--transcripts DIR] [--json]
                                  [--scale 0.5] [--adaptive 0.75]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
COMPRESS = os.path.join(os.path.dirname(BENCH_DIR), "hooks", "compress.py")

EPHEMERAL_PREFIX = "mcp__"
EPHEMERAL_TOOLS = ("Bash", "WebFetch")
MAX_RAW_BYTES = 200_000                    # oltre: fuori corpus (outlier)
MIN_TOKENS_CORPUS = 50                     # sotto: rumore, mai toccato comunque


def _load_compress():
    spec = importlib.util.spec_from_file_location("ck_compress", COMPRESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iter_outputs(transcripts_dir: str):
    """(tool_name, testo) per ogni tool_result effimero nei transcript.
    Il nome del tool sta nella riga assistant del tool_use; il testo nella
    riga user successiva col tool_result dello stesso id."""
    for base, _dirs, files in os.walk(transcripts_dir):
        for fn in sorted(files):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(base, fn)
            names: dict[str, str] = {}
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"tool_use"' not in line and '"tool_result"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:      # noqa: BLE001
                            continue
                        content = ((obj.get("message") or {}).get("content")
                                   or [])
                        if not isinstance(content, list):
                            continue
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "tool_use":
                                names[c.get("id", "")] = c.get("name", "")
                            elif c.get("type") == "tool_result":
                                name = names.get(c.get("tool_use_id", ""), "")
                                if not (name in EPHEMERAL_TOOLS
                                        or name.startswith(EPHEMERAL_PREFIX)):
                                    continue
                                cc = c.get("content")
                                if isinstance(cc, str):
                                    text = cc
                                elif isinstance(cc, list):
                                    text = "\n".join(
                                        b.get("text", "") for b in cc
                                        if isinstance(b, dict)
                                        and b.get("type") == "text")
                                else:
                                    continue
                                yield name, text
            except OSError:
                continue


def project(mod, tool: str, text: str, scale: float) -> tuple[str, bool]:
    """(output, elisione?) del braccio a quella scala — usa gli operatori
    VERI di compress.py, replicando solo l'aritmetica della soglia."""
    before = mod.est_tokens(text)
    if before < max(200, int(mod.MIN_TOKENS * scale)):
        return text, False
    if tool.startswith(EPHEMERAL_PREFIX):
        out = mod.json_project(text)
        if out is None:
            out = mod.compress(text, None, scale=scale)
    elif tool == "WebFetch":
        out = mod.compress(mod.prose_project(text) or text, None, scale=scale)
    else:
        out = mod.compress(text, None, scale=scale)
    if mod.est_tokens(out) >= before:          # nessun guadagno: no-op
        return text, False
    return out, mod.ELISION_MARK in out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts",
                    default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--scale", type=float, default=0.5,
                    help="CK_EPHEMERAL_SCALE for arm B")
    ap.add_argument("--adaptive", type=float, default=0.75,
                    help="live adaptive scale shared by both arms")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    mod = _load_compress()
    arms = {"A": args.adaptive, "B": args.adaptive * args.scale}
    tot = {k: {"after": 0, "elisions": 0, "signal_lost": 0} for k in arms}
    n = 0
    before_tot = 0
    per_tool: dict[str, int] = {}
    for tool, text in iter_outputs(args.transcripts):
        if mod.FOOTER_MARK in text:            # gia' compresso dal vivo
            continue
        if len(text) > MAX_RAW_BYTES:
            continue
        before = mod.est_tokens(text)
        if before < MIN_TOKENS_CORPUS:
            continue
        n += 1
        before_tot += before
        key = tool if tool in EPHEMERAL_TOOLS else "MCP"
        per_tool[key] = per_tool.get(key, 0) + 1
        norm_lines = mod.normalize(text).split("\n")
        signal_lines = {l for l in norm_lines if mod.SIGNAL.search(l)}
        for arm, scale in arms.items():
            out, elided = project(mod, tool, text, scale)
            tot[arm]["after"] += mod.est_tokens(out)
            tot[arm]["elisions"] += int(elided)
            if elided:
                # ritenzione = la riga (strip-ata) compare in una riga
                # dell'output: copre il .strip() finale di compress() e il
                # dedup che annota le ripetizioni contenendo l'originale
                out_lines = out.split("\n")
                out_set = set(out_lines)
                misses = [l for l in signal_lines if l not in out_set]
                lost = sum(
                    1 for l in misses
                    if l.strip() and not any(l.strip() in ol
                                             for ol in out_lines))
                tot[arm]["signal_lost"] += lost
                if lost:
                    tot[arm].setdefault("lost_by_tool", {})
                    tot[arm]["lost_by_tool"][key] = (
                        tot[arm]["lost_by_tool"].get(key, 0) + lost)

    if n == 0:
        print("empty corpus: no ephemeral output found", file=sys.stderr)
        return 1
    res = {
        "outputs": n, "per_tool": per_tool, "token_before": before_tot,
        "arms": {
            arm: {
                "scale": round(arms[arm], 4),
                "token_after": tot[arm]["after"],
                "saving_pct": round(100 * (1 - tot[arm]["after"] / before_tot), 1),
                "elisions": tot[arm]["elisions"],
                "signal_lines_lost": tot[arm]["signal_lost"],
                "lost_by_tool": tot[arm].get("lost_by_tool", {}),
            } for arm in arms
        },
    }
    res["extra_saving_tokens"] = tot["A"]["after"] - tot["B"]["after"]
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    a, b = res["arms"]["A"], res["arms"]["B"]
    print(f"corpus: {n} real ephemeral outputs ({per_tool}), "
          f"~{before_tot:,} raw tokens")
    print(f"A baseline (scale {a['scale']}): ~{a['token_after']:,} tokens "
          f"(-{a['saving_pct']}%), {a['elisions']} elisions, "
          f"{a['signal_lines_lost']} signal lines lost")
    print(f"B dividend (scale {b['scale']}): ~{b['token_after']:,} tokens "
          f"(-{b['saving_pct']}%), {b['elisions']} elisions, "
          f"{b['signal_lines_lost']} signal lines lost")
    print(f"extra savings from B: ~{res['extra_saving_tokens']:,} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
