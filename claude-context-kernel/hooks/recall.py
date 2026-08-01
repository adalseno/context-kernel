#!/usr/bin/env python3
"""
recall.py — TARGETED page fault over parked ephemeral outputs.

When T1 elides a Bash/MCP/WebFetch output, the full original is parked
(compress.py:park_output) and the footer states its key. This CLI recovers
ONLY what is needed — a grep or a line range — so the fault costs the tokens
of the question, not of the whole output. No ranking, no model: grep and
arithmetic, both deterministic.

    python3 recall.py --list                     # what is parked
    python3 recall.py --search 'ERROR|WARN'      # search the WHOLE parking lot
    python3 recall.py KEY --grep 'ERROR|WARN'    # matching lines (+context)
    python3 recall.py KEY --lines 120-180        # exact range
    python3 recall.py KEY --head 40              # head
    python3 recall.py KEY --all                  # in full (you pay for it all)

--search is the session's "recall storage": when you do not know WHICH parked
output holds what you are after, it finds it across all of them and gives you
the key for a targeted recall. It is the inverse of the projection made
navigable, not an operator: grep over every blob, deterministic, and you pay
only for the matches shown.

Hint: add `# ck:raw` to the command to exempt the recall output from T1
compression (it is already a targeted selection).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import zlib

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host
    pass

PARK_STATE = os.path.expanduser(
    os.environ.get("CK_PARK_STATE", "~/.context-kernel-park.json"))
FAULT_LOG = os.path.expanduser(
    os.environ.get("CK_FAULT_LOG", "~/.context-kernel-faults.log"))
GREP_CONTEXT = 2
MAX_GREP_LINES = 200


def _load() -> dict:
    try:
        with open(PARK_STATE, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:                          # noqa: BLE001
        return {}


def _text(entry: dict) -> str:
    return zlib.decompress(base64.b64decode(entry["z"])).decode(
        "utf-8", "replace")


def _log_fault(shown: str) -> None:
    """Un recall E' il pagamento di un page fault sull'output parcheggiato: ne
    registra il costo (i token EFFETTIVAMENTE restituiti — grep/lines/head
    recuperano una fetta, --all paga tutto) nel ledger dei fault, il lato
    distorsione accanto al risparmio. Mirror di compress.log_fault, tenuto
    locale per lo stesso motivo di PARK_STATE (recall non deve dipendere da
    compress). Solo numeri, mai contenuto; stesso kill-switch. Mai fatale."""
    if os.environ.get("CK_LOG_OFF") == "1":
        return
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        tok = max(0, len(shown) // 4)
        with open(FAULT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts},recall,recall,{tok},-\n")
    except Exception:                          # noqa: BLE001
        pass


def _search_all(st: dict, pattern: str, context: int) -> int:
    """Il 'recall storage' della sessione: grep su TUTTI i blob parcheggiati,
    raggruppato per entry, coi match+contesto e la chiave per il recall mirato.
    Cap globale sulle righe mostrate: cerchi in tutto, ma paghi una fetta. Puro
    access-path (l'inverso reso navigabile), deterministico, mai un ranking."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        print(f"invalid regex: {e}", file=sys.stderr)
        return 2
    if not st:
        print("parking lot empty")
        return 0
    now = time.time()
    out: list[str] = []
    shown = 0
    matched = 0
    for k, e in sorted(st.items(), key=lambda kv: -kv[1].get("ts", 0)):
        lines = _text(e).split("\n")
        idx = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not idx:
            continue
        matched += 1
        age = int((now - e.get("ts", now)) / 60)
        trunc = " [TRUNCATED]" if e.get("trunc") else ""
        out.append(f"== {k}  {e.get('tool', '?')}  {age}min ago  "
                   f"{e.get('cmd', '')[:60]}  ({len(idx)} matches){trunc} ==")
        keep: set[int] = set()
        for i in idx:
            keep.update(range(max(0, i - context),
                              min(len(lines), i + context + 1)))
        last = -2
        capped = False
        for i in sorted(keep):
            if shown >= MAX_GREP_LINES:
                out.append(f"  … capped at {MAX_GREP_LINES} lines: narrow the regex "
                           "or use `recall KEY --grep`")
                capped = True
                break
            if i != last + 1:
                out.append("  …")
            out.append(f"  {i + 1}\t{lines[i]}")
            last = i
            shown += 1
        out.append(f"  -> recall {k} --grep '{pattern}'  |  --lines A-B")
        if capped:
            break
    if not matched:
        print(f"no parked output matches /{pattern}/ "
              f"({len(st)} parked)")
        return 0
    text = "\n".join(out)
    print(text)
    _log_fault(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("key", nargs="?", help="key from the [parked: ...] footer")
    ap.add_argument("--grep", metavar="REGEX")
    ap.add_argument("--search", metavar="REGEX",
                    help="search ALL parked outputs (recall storage)")
    ap.add_argument("-C", "--context", type=int, default=GREP_CONTEXT)
    ap.add_argument("--lines", metavar="A-B")
    ap.add_argument("--head", type=int, metavar="N")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    st = _load()
    if args.search:
        return _search_all(st, args.search, args.context)
    if args.list or not args.key:
        if not st:
            print("parking lot empty")
            return 0
        now = time.time()
        for k, e in sorted(st.items(), key=lambda kv: -kv[1].get("ts", 0)):
            age = int((now - e.get("ts", now)) / 60)
            trunc = " [TRUNCATED at parking time]" if e.get("trunc") else ""
            print(f"{k}  {e.get('tool', '?'):<10} {age:>4}min ago  "
                  f"{e.get('cmd', '')[:80]}{trunc}")
        return 0

    entry = st.get(args.key)
    if not entry:
        print(f"key '{args.key}' missing or expired (TTL). "
              "Use `--list` to see the parking lot.", file=sys.stderr)
        return 2
    lines = _text(entry).split("\n")
    if entry.get("trunc"):
        print("# NOTE: original TRUNCATED at parking time (over the cap)",
              file=sys.stderr)

    if args.grep:
        try:
            rx = re.compile(args.grep)
        except re.error as e:
            print(f"invalid regex: {e}", file=sys.stderr)
            return 2
        hit_idx = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hit_idx:
            print(f"no line matches /{args.grep}/ "
                  f"({len(lines)} lines parked)")
            return 0
        keep: set[int] = set()
        for i in hit_idx:
            keep.update(range(max(0, i - args.context),
                              min(len(lines), i + args.context + 1)))
        out: list[str] = []
        shown = 0
        last = -2
        for i in sorted(keep):
            if shown >= MAX_GREP_LINES:
                out.append(f"… more matches beyond the cap of {MAX_GREP_LINES} "
                           "lines (narrow the regex or use --lines)")
                break
            if i != last + 1:
                out.append("…")
            out.append(f"{i + 1}\t{lines[i]}")
            last = i
            shown += 1
        text = "\n".join(out)
        print(text)
        _log_fault(text)
        return 0

    if args.lines:
        m = re.fullmatch(r"(\d+)-(\d+)", args.lines.strip())
        if not m:
            print("--lines expects the A-B format (1-based)", file=sys.stderr)
            return 2
        a, b = int(m.group(1)), int(m.group(2))
        out = [f"{i}\t{lines[i - 1]}"
               for i in range(max(1, a), min(len(lines), b) + 1)]
        text = "\n".join(out)
        print(text)
        _log_fault(text)
        return 0

    if args.all:
        text = "\n".join(lines)
        print(text)
        _log_fault(text)
        return 0

    n = args.head or 40
    out = [f"{i + 1}\t{lines[i]}" for i in range(min(n, len(lines)))]
    if len(lines) > n:
        out.append(f"… {len(lines) - n} lines remaining "
                   "(--grep, --lines A-B, or --all for the whole thing)")
    text = "\n".join(out)
    print(text)
    _log_fault(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
