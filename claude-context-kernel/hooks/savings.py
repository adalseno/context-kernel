#!/usr/bin/env python3
"""
savings.py — riepiloga i token risparmiati dal compressore.

Uso:
    python3 savings.py                 # totale + breakdown per tool/sessione
    python3 savings.py --reset-canary  # riconosce i fallimenti canary storici
    python3 savings.py --statusline    # riga per la statusline di Claude Code
    python3 savings.py --html [path]   # dashboard HTML self-contained
    CK_LOG=/path python3 savings.py    # log alternativo

Legge il CSV scritto da compress.py (default ~/.context-kernel-savings.log):
    timestamp,tool,before,after,saved[,sessione]
(la colonna sessione e' il basename corto del transcript: distingue le
compressioni di QUESTA sessione da quelle delle sessioni headless concorrenti,
es. i distiller di evolver/reforge)
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass

LOG_PATH = os.path.expanduser(os.environ.get("CK_LOG", "~/.context-kernel-savings.log"))
CANARY_STATE = os.path.expanduser(
    os.environ.get("CK_CANARY_STATE", "~/.context-kernel-canary.json")
)
AB_STATE = os.path.expanduser(
    os.environ.get("CK_AB_STATE", "~/.context-kernel-ab.json")
)
CONTEXT_STATE = os.path.expanduser(
    os.environ.get("CK_CONTEXT_STATE", "~/.context-kernel-context.json")
)
FAULT_LOG = os.path.expanduser(
    os.environ.get("CK_FAULT_LOG", "~/.context-kernel-faults.log")
)


def read_faults() -> tuple[int, int, dict, dict]:
    """(n_fault, token_rientrati, per_kind{kind:[tok,count]}, per_bucket{...})
    dal ledger dei page fault scritto da compress.py/recall.py:
        timestamp,kind,bucket,token,sessione
    kind = reread|recmd|recall. Un ledger assente o vuoto e' il caso migliore
    (nessuna scommessa persa)."""
    n = tok = 0
    per_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_bucket: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    try:
        with open(FAULT_LOG, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 5:
                    continue
                kind, bucket = parts[1], parts[2]
                try:
                    t = int(parts[3])
                except ValueError:
                    continue
                n += 1
                tok += t
                per_kind[kind][0] += t
                per_kind[kind][1] += 1
                per_bucket[bucket][0] += t
                per_bucket[bucket][1] += 1
    except OSError:
        pass
    return n, tok, per_kind, per_bucket


_FAULT_LABEL = {"reread": "full re-reads", "recmd": "re-runs",
                "recall": "targeted recalls"}


def fault_status(saved_total: int = 0) -> str | None:
    """La DISTORSIONE misurata in PRODUZIONE, non solo nell'oracolo del bench:
    quanto dei token risparmiati e' poi rientrato via page fault. Chiude la
    curva rate-distortion — rate = risparmio, distorsione = questi recuperi.
    La domanda giusta non e' 'l'elisione era perfetta?' ma 'quanto e' costato
    il fault?' — e adesso e' un numero."""
    n, tok, per_kind, _ = read_faults()
    if not n:
        return None
    frac = (f" = {tok / saved_total:.1%} of the savings clawed back"
            if saved_total > 0 else "")
    lines = [f"  page faults (distortion): {n} recoveries, "
             f"~{tok:,} tokens clawed back{frac}"]
    for kind, (t, c) in sorted(per_kind.items(), key=lambda x: -x[1][0]):
        lines.append(f"    {_FAULT_LABEL.get(kind, kind):22s} "
                     f"{c:4d}x   ~{t:,} tokens")
    return "\n".join(lines)


def reset_canary() -> int:
    """Riconosce i fallimenti storici: failed -> failed_acked. Da usare DOPO
    averli indagati e spiegati, per non tenere l'allarme ⚠ acceso per sempre."""
    import json
    try:
        with open(CANARY_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:                          # noqa: BLE001
        print("No canary state to reset.")
        return 0
    fl = st.get("failed", 0)
    if not fl:
        print("No active failures: nothing to acknowledge.")
        return 0
    st["failed_acked"] = st.get("failed_acked", 0) + fl
    st["failed"] = 0
    st["failures"] = []
    # sblocca anche l'auto-degrade: dopo aver indagato e riconosciuto, una
    # sessione ancora viva torna a comprimere (se il contratto e' ripristinato).
    ndeg = len(st.get("degraded_sessions", []))
    st["degraded_sessions"] = []
    st["probe"] = {}                           # niente degradate -> niente sonde
    with open(CANARY_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)
    deg = f" Unblocked {ndeg} auto-degraded sessions." if ndeg else ""
    print(f"Acknowledged {fl} canary failures (historical: {st['failed_acked']}).{deg} "
          "The alarm will fire again only on NEW failures.")
    return 0


def canary_status() -> str | None:
    """Stato del canary end-to-end: le compressioni loggate risultano
    APPLICATE davvero (footer presente nel transcript)?"""
    try:
        import json
        with open(CANARY_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:                          # noqa: BLE001
        return None
    v, fl = st.get("verified", 0), st.get("failed", 0)
    acked = st.get("failed_acked", 0)
    auto = st.get("failed_auto_acked", 0)
    pend = len(st.get("pending", []))
    hist_parts = []
    if acked:
        hist_parts.append(f"{acked} historical, acknowledged")
    if auto:
        hist_parts.append(f"{auto} auto-acknowledged on evidence")
    hist = f" ({', '.join(hist_parts)})" if hist_parts else ""
    ndeg = len(st.get("degraded_sessions", []))
    probe_k = int(os.environ.get("CK_CANARY_PROBE_K", "10"))
    sonda = (f"; recovery probe active (1 every {probe_k} outputs)"
             if os.environ.get("CK_CANARY_AUTOHEAL", "1") != "0" and probe_k > 0
             else "")
    deg = (f"\n          AUTO-DEGRADE: {ndeg} sessions switched to raw pass-through "
           f"(compression suspended after too many violations){sonda}") if ndeg else ""
    if fl:
        sessions = {f.get("session", "?") for f in st.get("failures", [])}
        where = f" [sessions: {', '.join(sorted(sessions))}]" if sessions else ""
        heal_m = int(os.environ.get("CK_CANARY_HEAL_M", "5"))
        streak = st.get("heal_streak", 0)
        heal = (f"\n          auto-heal: {streak} consecutive verified "
                f"since the last failure (auto-ack at {heal_m})"
                if os.environ.get("CK_CANARY_AUTOHEAL", "1") != "0" else "")
        return (f"  CANARY: ⚠ {fl} compressions NOT applied by the harness "
                f"(last: {st.get('last_failure')}){where} — savings overstated!\n"
                f"          {v} verified ok, {pend} pending{hist}{deg}{heal}\n"
                f"          (investigate, then: python3 savings.py --reset-canary)")
    if v:
        return (f"  canary: ✓ {v} compressions verified as applied in the transcript "
                f"(last: {st.get('last_ok')}), {pend} pending{hist}")
    if pend:
        return f"  canary: {pend} compressions awaiting verification{hist}"
    return None


def ab_status() -> str | None:
    """Ledger dell'A/B di answer-invariance (T4 campionato): il canary prova
    che la compressione e' entrata, l'A/B misura se ha preservato il segnale."""
    try:
        import json
        with open(AB_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:                          # noqa: BLE001
        return None
    ok, deg = st.get("ok", 0), st.get("degraded", 0)
    pend = len(st.get("pending", []))
    if not (ok or deg or pend):
        return None
    line = f"  A/B invariance: {ok} invariant, {deg} degraded"
    if deg:
        line = f"  A/B invariance: ⚠ {deg} degraded out of {ok + deg} judged"
    if pend:
        line += (f", {pend} samples pending "
                 f"(judge them: python3 hooks/ab_verify.py)")
    return line


def _fmt_k(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 100_000:
        return f"{n / 1000:.1f}k"
    return f"{n // 1000}k"


def statusline() -> int:
    """UNA riga per la statusline di Claude Code (settings -> statusLine).
    Legge da stdin il JSON di stato che l'harness passa alla statusline
    (session_id, model, workspace) e mostra il risparmio ALL'UTENTE, non al
    modello: sessione corrente + totale storico, con gli allarmi compatti.
    Mai fatale e sempre una riga: una statusline che sparisce e' un bug."""
    import json
    sess = model = cwd = ""
    try:
        st = json.load(sys.stdin)
        sess = (st.get("session_id") or "")[:8]
        m = st.get("model") or {}
        model = m.get("display_name") or m.get("id") or ""
        cwd = os.path.basename(
            (st.get("workspace") or {}).get("current_dir")
            or st.get("cwd") or "")
    except Exception:                          # noqa: BLE001
        pass

    tot = mine = tot_before = 0
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) not in (5, 6, 7):
                    continue
                try:
                    b, s = int(parts[2]), int(parts[4])
                except ValueError:
                    continue
                tot += s
                tot_before += b
                if sess and len(parts) >= 6 and parts[5] == sess:
                    mine += s
    except Exception:                          # noqa: BLE001
        pass

    # Colori ANSI a 16 (verde=risparmio, rosso=canary, giallo=A/B in attesa):
    # seguono il tema del terminale dell'utente, chiaro o scuro che sia.
    # Mai SOLO colore: icone e testo restano il canale primario.
    # CK_STATUSLINE_COLOR=0 spegne ogni escape (statusline in testo puro).
    color = os.environ.get("CK_STATUSLINE_COLOR", "1") != "0"
    green, yellow, red = ("\033[32m", "\033[33m", "\033[31m") if color else ("",) * 3
    dim, reset = ("\033[2m", "\033[0m") if color else ("", "")
    # Colore del marchio "ck ⚡": giallo di default, CK_STATUSLINE_BRAND per
    # cambiarlo (red/green/yellow/blue/magenta/cyan/none). Nota: red e' anche
    # il colore dell'allarme canary — l'icona ⚠ resta il discriminante.
    _brand_codes = {"red": "31", "green": "32", "yellow": "33",
                    "blue": "34", "magenta": "35", "cyan": "36"}
    _bc = _brand_codes.get(
        os.environ.get("CK_STATUSLINE_BRAND", "yellow").strip().lower())
    brand = f"\033[{_bc}m" if (color and _bc) else ""

    # Di default la riga e' ASCIUTTA: risparmio sessione + quota totale, stop.
    # A/B in attesa, fault e i rapporti sul contesto sono diagnostica: utile
    # quando la cerchi, rumore quando la vedi a ogni prompt. Torna tutto con
    # CK_STATUSLINE_VERBOSE=1. L'allarme canary invece resta SEMPRE: e' un
    # allarme, non un contatore.
    verbose = os.environ.get("CK_STATUSLINE_VERBOSE", "0") == "1"
    core = f"-{_fmt_k(mine)} session" if verbose else f"-{_fmt_k(mine)}"
    if verbose:
        # "-N sessione" da solo non dice quanto pesa: rapportarlo al contesto
        # che ci SAREBBE stato senza compressione (ctx attuale + risparmiato,
        # dal tracker di compress.py). Il totale storico invece si rapporta
        # solo a se' stesso: quota elisa degli output toccati (come il report).
        try:
            with open(CONTEXT_STATE, encoding="utf-8") as f:
                ctx = int(
                    (json.load(f).get(sess) or {}).get("context_tokens") or 0)
            if mine and ctx:
                would_be = ctx + mine
                core += f" (-{mine / would_be:.0%} of ctx ~{_fmt_k(would_be)})"
        except Exception:                      # noqa: BLE001
            pass
        core += f" · -{_fmt_k(tot)} total"
        if tot and tot_before:
            core += f" (-{tot / tot_before:.0%})"
    elif tot and tot_before:
        core += f" · tot -{tot / tot_before:.0%}"
    else:
        core += f" · tot -{_fmt_k(tot)}"
    seg = f"{brand}ck ⚡{reset if brand else ''} {green}{core}{reset}"
    try:
        with open(CANARY_STATE, encoding="utf-8") as f:
            if json.load(f).get("failed"):
                seg += f" · {red}⚠ canary{reset}"
    except Exception:                          # noqa: BLE001
        pass
    if verbose:
        try:
            with open(AB_STATE, encoding="utf-8") as f:
                pend = len(json.load(f).get("pending") or [])
            if pend:
                seg += f" · {yellow}A/B: {pend} pending{reset}"
        except Exception:                      # noqa: BLE001
            pass
        # Lato distorsione, in grigio: i fault non sono allarmi (il recupero
        # e' per progetto), ma vederli tiene onesta la curva.
        try:
            _n, ftok, _pk, _pb = read_faults()
            if ftok:
                seg += f" · {dim}↩{_fmt_k(ftok)} fault{reset}"
        except Exception:                      # noqa: BLE001
            pass

    prefix = " · ".join(p for p in (model, cwd) if p)
    print(f"{dim}{prefix} ·{reset} {seg}" if prefix else seg)
    return 0


# --- dashboard HTML ---------------------------------------------------------
# Palette: istanza di riferimento della skill dataviz, valori usati INVARIATI
# (categorical slot 1 blu, status good/warning/critical, chrome & ink).
# Un solo hue per grafico (una misura), identita' dalle etichette, non dal
# colore; status SEMPRE icona+testo, mai solo colore; tabella come vista
# accessibile; dark mode con gli step scuri selezionati (non un flip).

_HTML_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; background: #f9f9f7; color: #0b0b0b;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 20px; margin: 0 0 2px; }
.sub { color: #52514e; margin: 0 0 24px; }
.card { background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10);
  border-radius: 10px; padding: 16px 18px; margin: 0 0 16px; }
.card h2 { font-size: 13px; font-weight: 600; color: #52514e;
  margin: 0 0 10px; text-transform: uppercase; letter-spacing: .04em; }
.tiles { display: flex; flex-wrap: wrap; gap: 16px; }
.tile { flex: 1 1 150px; }
.tile .v { font-size: 28px; font-weight: 650; }
.tile .l { color: #52514e; font-size: 12px; }
.status { display: inline-flex; align-items: center; gap: 6px; }
.status .dot { font-size: 15px; }
.ok   .dot { color: #0ca30c; }  .warn .dot { color: #fab219; }
.crit .dot { color: #d03b3b; }
svg text { font: 11px system-ui, sans-serif; fill: #898781;
  font-variant-numeric: tabular-nums; }
svg .lbl { fill: #0b0b0b; }
svg .grid { stroke: #e1e0d9; stroke-width: 1; }
svg .axis { stroke: #c3c2b7; stroke-width: 1; }
svg .line { stroke: #2a78d6; stroke-width: 2; fill: none; }
svg .areaf { fill: #2a78d6; opacity: .12; }
svg .bar { fill: #2a78d6; rx: 0; }
table { border-collapse: collapse; width: 100%;
  font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 5px 10px;
  border-bottom: 1px solid #e1e0d9; }
th:first-child, td:first-child { text-align: left; }
th { color: #52514e; font-weight: 600; }
.tip { position: fixed; pointer-events: none; background: #fcfcfb;
  border: 1px solid rgba(11,11,11,0.25); border-radius: 6px;
  padding: 4px 8px; font-size: 12px; display: none; z-index: 9; }
@media (prefers-color-scheme: dark) {
  body { background: #0d0d0d; color: #ffffff; }
  .sub, .tile .l, .card h2, th { color: #c3c2b7; }
  .card, .tip { background: #1a1a19; border-color: rgba(255,255,255,0.10); }
  svg .lbl { fill: #ffffff; }
  svg .grid { stroke: #2c2c2a; }  svg .axis { stroke: #383835; }
  svg .line { stroke: #3987e5; } svg .areaf { fill: #3987e5; }
  svg .bar { fill: #3987e5; }
  th, td { border-color: #2c2c2a; }
}
"""

_HTML_JS = """
const tip = document.getElementById('tip');
function showTip(evt, text) {
  tip.textContent = text; tip.style.display = 'block';
  tip.style.left = (evt.clientX + 12) + 'px';
  tip.style.top = (evt.clientY - 10) + 'px';
}
function hideTip() { tip.style.display = 'none'; }
for (const el of document.querySelectorAll('[data-tip]')) {
  el.addEventListener('mousemove', e => showTip(e, el.dataset.tip));
  el.addEventListener('mouseleave', hideTip);
}
"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _svg_cumulative(rows: list[tuple]) -> str:
    """Curva cumulativa dei token risparmiati nel tempo (una serie)."""
    from datetime import datetime
    pts = []
    cum = 0
    for ts, _tool, _b, _a, s, *_rest in rows:
        cum += s
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
        pts.append((t, cum))
    if len(pts) < 2:
        return "<p class='sub'>(at least 2 timestamped compressions needed)</p>"
    if len(pts) > 400:                        # tenere leggero l'HTML
        step = len(pts) // 400 + 1
        pts = pts[::step] + [pts[-1]]
    W, H, PL, PB, PT = 800, 220, 62, 24, 8
    x0, x1 = pts[0][0], pts[-1][0]
    y1 = pts[-1][1] or 1
    def X(t): return PL + (t - x0) / max(1, (x1 - x0)) * (W - PL - 8)
    def Y(v): return PT + (1 - v / y1) * (H - PT - PB)
    path = " ".join(f"{'M' if i == 0 else 'L'}{X(t):.1f},{Y(v):.1f}"
                    for i, (t, v) in enumerate(pts))
    area = (path + f" L{X(x1):.1f},{Y(0):.1f} L{X(x0):.1f},{Y(0):.1f} Z")
    gy = [f"<line class='grid' x1='{PL}' x2='{W-8}' y1='{Y(y1*f):.1f}' "
          f"y2='{Y(y1*f):.1f}'/>"
          f"<text x='{PL-6}' y='{Y(y1*f)+4:.1f}' text-anchor='end'>"
          f"{_fmt_k(int(y1*f))}</text>" for f in (0.5, 1.0)]
    from datetime import datetime as _dt
    lab0 = _dt.fromtimestamp(x0).strftime("%d/%m %H:%M")
    lab1 = _dt.fromtimestamp(x1).strftime("%d/%m %H:%M")
    dots = "".join(
        f"<circle cx='{X(t):.1f}' cy='{Y(v):.1f}' r='8' fill='transparent' "
        f"data-tip='{_dt.fromtimestamp(t).strftime('%d/%m %H:%M')} — "
        f"-{v:,} token'/>" for t, v in pts)
    return (f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' "
            f"aria-label='cumulative curve of tokens saved'>"
            + "".join(gy)
            + f"<line class='axis' x1='{PL}' x2='{W-8}' y1='{Y(0):.1f}' y2='{Y(0):.1f}'/>"
            + f"<path class='areaf' d='{area}'/><path class='line' d='{path}'/>"
            + f"<text x='{PL}' y='{H-6}'>{lab0}</text>"
            + f"<text x='{W-8}' y='{H-6}' text-anchor='end'>{lab1}</text>"
            + f"<text class='lbl' x='{X(x1)-4:.1f}' y='{Y(y1)+12:.1f}' "
            + f"text-anchor='end'>-{_fmt_k(y1)}</text>"
            + dots + "</svg>")


def _svg_hbars(items: list[tuple[str, int]], unit: str = "token") -> str:
    """Barre orizzontali di una misura (un solo hue, identita' dalle
    etichette). 4px di arrotondamento solo sul data-end, 2px di gap."""
    if not items:
        return "<p class='sub'>(no data)</p>"
    vmax = max(v for _, v in items) or 1
    ROW, BAR, PL, W = 26, 16, 150, 800
    H = len(items) * ROW + 6
    parts = [f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' "
             f"aria-label='bars by {unit}'>"]
    for i, (name, v) in enumerate(items):
        y = i * ROW + 4
        w = max(2, v / vmax * (W - PL - 80))
        parts.append(
            f"<text class='lbl' x='{PL-8}' y='{y+BAR-4}' text-anchor='end'>"
            f"{_esc(name[:18])}</text>"
            f"<path class='bar' d='M{PL},{y} h{w-4:.1f} a4,4 0 0 1 4,4 "
            f"v{BAR-8} a4,4 0 0 1 -4,4 h-{w-4:.1f} z' "
            f"data-tip='{_esc(name)} — -{v:,} {unit}'/>"
            f"<text x='{PL+w+6:.1f}' y='{y+BAR-4}'>-{_fmt_k(v)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def html_report(out_path: str | None = None) -> int:
    """Dashboard self-contained: tiles, curva cumulativa, per-tool,
    per-sessione, ledger A/B e canary, tabella (vista accessibile)."""
    rows = []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) not in (5, 6, 7):
                    continue
                try:
                    b, a, s = int(parts[2]), int(parts[3]), int(parts[4])
                except ValueError:
                    continue
                rows.append((parts[0], parts[1], b, a, s,
                             parts[5] if len(parts) >= 6 else "-",
                             parts[6] if len(parts) == 7 else "-"))
    except OSError:
        pass

    before = sum(r[2] for r in rows)
    saved = sum(r[4] for r in rows)
    pct = saved / before if before else 0.0
    per_tool: dict[str, int] = defaultdict(int)
    per_sess: dict[str, int] = defaultdict(int)
    sub_n = sub_saved = 0                      # quota dai subagent/workflow
    for _ts, tool, _b, _a, s, sess, agent in rows:
        per_tool[tool] += s
        if sess != "-":
            per_sess[sess] += s
        if agent != "-":
            sub_n += 1
            sub_saved += s
    tools = sorted(per_tool.items(), key=lambda x: -x[1])
    sessions = sorted(per_sess.items(), key=lambda x: -x[1])[:8]

    import json
    def _load(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                      # noqa: BLE001
            return {}
    canary = _load(CANARY_STATE)
    ab = _load(AB_STATE)
    c_failed = canary.get("failed", 0)
    c_cls, c_icon, c_txt = ("ok", "✓", f"{canary.get('verified', 0)} verified")
    if c_failed:
        c_cls, c_icon, c_txt = ("crit", "✗", f"{c_failed} NOT applied")
    ab_deg = ab.get("degraded", 0)
    ab_pend = len(ab.get("pending") or [])
    ab_cls, ab_icon = ("warn", "⚠") if ab_deg else ("ok", "✓")
    ab_txt = f"{ab.get('ok', 0)} invariant, {ab_deg} degraded"
    if ab_pend:
        ab_txt += f", {ab_pend} pending"

    # lato distorsione: token rientrati via page fault + breakdown per tipo
    f_n, f_tok, f_kind, _f_bucket = read_faults()
    f_pct = f_tok / saved if saved else 0.0
    f_bars = sorted(((_FAULT_LABEL.get(k, k), v[0]) for k, v in f_kind.items()),
                    key=lambda x: -x[1])

    table = "".join(
        f"<tr><td>{_esc(t)}</td><td>{n:,}</td><td>{v:,}</td></tr>"
        for t, v, n in ((t, v, sum(1 for r in rows if r[1] == t))
                        for t, v in tools))
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>context-kernel — token savings</title>
<style>{_HTML_CSS}</style></head><body><div class="wrap">
<h1>context-kernel</h1>
<p class="sub">{len(rows):,} compressions · {rows[0][0][:16] if rows else '—'} → {rows[-1][0][:16] if rows else '—'}{f' · {sub_n:,} of them in subagents (~{sub_saved:,} tokens)' if sub_n else ''}</p>
<div class="card"><div class="tiles">
<div class="tile"><div class="v">-{_fmt_k(saved)}</div><div class="l">tokens saved</div></div>
<div class="tile"><div class="v">-{pct:.0%}</div><div class="l">of the outputs touched</div></div>
<div class="tile"><div class="v">{len(rows):,}</div><div class="l">compressions</div></div>
<div class="tile"><div class="v status {c_cls}"><span class="dot">{c_icon}</span>canary</div><div class="l">{_esc(c_txt)}</div></div>
<div class="tile"><div class="v status {ab_cls}"><span class="dot">{ab_icon}</span>A/B</div><div class="l">{_esc(ab_txt)}</div></div>
<div class="tile"><div class="v">{f'-{_fmt_k(f_tok)}' if f_tok else '0'}</div><div class="l">clawed back via faults{f' ({f_pct:.0%} of the savings)' if f_tok else ''}</div></div>
</div></div>
<div class="card"><h2>Cumulative savings</h2>{_svg_cumulative(rows)}</div>
<div class="card"><h2>By tool</h2>{_svg_hbars(tools)}</div>
<div class="card"><h2>By session (top 8)</h2>{_svg_hbars(sessions)}</div>
<div class="card"><h2>Distortion — tokens clawed back via page faults ({f_n})</h2>{_svg_hbars(f_bars) if f_bars else "<p class='sub'>(no page faults recorded — no bet lost)</p>"}</div>
<div class="card"><h2>Table</h2>
<table><tr><th>tool</th><th>compressions</th><th>tokens saved</th></tr>
{table}</table></div>
<div id="tip" class="tip"></div>
<script>{_HTML_JS}</script>
</div></body></html>"""

    out = out_path or os.path.expanduser("~/.context-kernel-report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(out)
    return 0


def main() -> int:
    if "--statusline" in sys.argv[1:]:
        return statusline()
    if "--html" in sys.argv[1:]:
        idx = sys.argv.index("--html")
        arg = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        return html_report(arg)
    if "--reset-canary" in sys.argv[1:]:
        return reset_canary()
    if not os.path.exists(LOG_PATH):
        print(f"No log yet ({LOG_PATH}). Use the plugin and come back here.")
        return 0

    n = 0
    before_tot = after_tot = 0
    per_tool: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # before, after, count
    per_session: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # saved, count
    first = last = None

    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            # 5 = storico senza sessione; 6 = con sessione; 7 = con agent
            # (subagent/workflow). La colonna agent non serve al report testuale
            # ma le righe a 7 campi sono il formato ATTUALE: scartarle svuotava
            # il report ("Log presente ma vuoto") su ogni log recente.
            if len(parts) not in (5, 6, 7):
                continue
            ts, tool, before, after = parts[:4]
            session = parts[5] if len(parts) >= 6 else "-"
            try:
                b, a = int(before), int(after)
            except ValueError:
                continue
            n += 1
            before_tot += b
            after_tot += a
            per_tool[tool][0] += b
            per_tool[tool][1] += a
            per_tool[tool][2] += 1
            per_session[session][0] += b - a
            per_session[session][1] += 1
            first = first or ts
            last = ts

    if n == 0:
        print("Log present but empty.")
        return 0

    saved = before_tot - after_tot
    pct = saved / before_tot if before_tot else 0.0
    # stima costo input Opus 4.8: $5 / 1M token
    dollars = saved / 1_000_000 * 5.0

    print(f"context-kernel — token savings  ({first} -> {last})")
    print("=" * 56)
    print(f"  compressions:      {n}")
    print(f"  tokens in:         {before_tot:,}")
    print(f"  tokens after:      {after_tot:,}")
    print(f"  SAVED:             {saved:,}  (-{pct:.0%})")
    print(f"  ~input cost avoided (Opus 4.8, first read): ${dollars:.2f}")
    print(f"  (compounds with cache reads on every subsequent turn)")
    print("\n  by tool:")
    for tool, (b, a, c) in sorted(per_tool.items(), key=lambda x: -(x[1][0] - x[1][1])):
        print(f"    {tool:10s}  {c:4d}x   -{b - a:,} tokens")

    named = {s: v for s, v in per_session.items() if s != "-"}
    if named:
        print("\n  by session (concurrent headless runs write here too):")
        for sess, (sv, c) in sorted(named.items(), key=lambda x: -x[1][0])[:8]:
            print(f"    {sess:10s}  {c:4d}x   -{sv:,} tokens")
        if per_session.get("-"):
            sv, c = per_session["-"]
            print(f"    {'(historical)':10s}  {c:4d}x   -{sv:,} tokens")
    status = canary_status()
    if status:
        print()
        print(status)
    ab = ab_status()
    if ab:
        print()
        print(ab)
    faults = fault_status(saved)
    if faults:
        print()
        print(faults)
    return 0


if __name__ == "__main__":
    sys.exit(main())
