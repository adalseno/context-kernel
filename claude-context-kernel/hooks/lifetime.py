#!/usr/bin/env python3
"""
lifetime.py — Context Lifetime Estimator (il termine MISURATO dello scheduler).

Lo scheduler della compattazione (compact_advisor.py) deve decidere QUANDO
conviene compattare: costo(compattazione) < costo(contesto enorme). Il termine
duro e' costo(compattazione) = rischio di buttare contesto che poi servira'.
Quel rischio NON si predice con una tabella di TTL a orologio ("stack trace 30s,
README 10min") — la vita utile di un pezzo di contesto non si misura in secondi
di parete ma in distanza-di-EVENTO: uno stack trace muore quando il bug e'
risolto, non dopo 30 secondi.

E il segnale c'e' gia', MISURATO in produzione: faults.log. Ogni page fault e'
la prova che un pezzo eliso NON era morto — e' rientrato perche' serviva. Qui NON
si inventa nessun numero: si legge la curva di sopravvivenza EMPIRICA dal log che
il plugin gia' scrive (`ts,kind,bucket,token,sessione`), auto-calibrata sulla sua
stessa storia. E' un istogramma sui log, non una hazard function a priori.

Contratto:
- `recall_pressure`  in [0,1]: quanto BATTE, di recente, buttare contesto. Alta =
  le elisioni recenti rientrano spesso (contesto ancora VIVO) -> TIENI, compatta
  piu' tardi. Bassa = i drop non tornano (contesto MORTO) -> compatta prima.
  Neutra 0.5 al freddo (poca storia) -> lo scheduler resta al comportamento base.
- `liveness_by_bucket`: la sopravvivenza per CLASSE di contenuto (estensione/tool).
- `adaptive_threshold`: mappa la pressione sulla soglia dell'avviso, dentro una
  banda LIMITATA attorno alla base — una stima patologica non puo' mai spingere
  la soglia a valori assurdi. Solo TIMING dell'avviso: i tassi di compressione
  di compress.py non si toccano (invariante T5: gli appresi RILASSANO, mai
  stringono). L'avviso e' comunque advisory, il PreCompact difende TS(Q) e il
  page fault recupera: la posta e' bassa per costruzione.

Read-only sul log. Mai fatale. CLI: `python3 lifetime.py` stampa il banco.
"""
from __future__ import annotations

import os
import sys

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                            # embed per-path: stream dell'host
    pass

FAULT_LOG = os.path.expanduser(
    os.environ.get("CK_FAULT_LOG", "~/.context-kernel-faults.log")
)

# Al di sotto di questa storia non c'e' segnale: neutro, niente adattamento.
MIN_EVENTS = 8
# Coda recente valutata contro il resto della storia (distanza-di-EVENTO, non
# secondi): la finestra e' in EVENTI, l'orologio di parete non entra nel calcolo.
RECENT_N = 40
# Semiampiezza della banda: pressione 0 -> base-0.5*SPAN, 1 -> base+0.5*SPAN.
# QUESTO e' il limite di sicurezza: una stima patologica sposta la soglia di al
# piu' SPAN/2 (0.12) dalla base scelta dall'operatore, mai oltre. Nessun clamp
# ASSOLUTO che ignori la base: romperebbe la garanzia 'neutro -> base esatta'.
SPAN = 0.24


def fault_events(path: str | None = None) -> list[tuple[str, str, str, int]]:
    """Eventi di page fault ORDINATI dal ledger: (ts, kind, bucket, token).
    Preserva l'ordine (la recency e' il segnale) — read_faults di savings.py li
    AGGREGA e la perde, per questo qui si rilegge. Log assente/vuoto = lista
    vuota = caso migliore (nessuna scommessa persa)."""
    p = path or FAULT_LOG
    out: list[tuple[str, str, str, int]] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 5:
                    continue
                try:
                    tok = int(parts[3])
                except ValueError:
                    continue
                out.append((parts[0], parts[1], parts[2], max(0, tok)))
    except OSError:
        pass
    return out


def liveness_by_bucket(
    events: list[tuple[str, str, str, int]] | None = None,
) -> dict[str, tuple[int, int]]:
    """Sopravvivenza per CLASSE di contenuto: bucket -> (n_fault, token_rientrati).
    Un bucket che rientra spesso e' una classe ancora VIVA (buttarla costa). E'
    la curva empirica, non una tabella di TTL indovinata."""
    evs = fault_events() if events is None else events
    agg: dict[str, list[int]] = {}
    for _ts, _kind, bucket, tok in evs:
        row = agg.setdefault(bucket, [0, 0])
        row[0] += 1
        row[1] += tok
    return {b: (n, t) for b, (n, t) in agg.items()}


def recall_pressure(
    events: list[tuple[str, str, str, int]] | None = None,
    recent: int = RECENT_N,
) -> float:
    """Quanto BATTE buttare contesto, di recente, misurato contro la storia.

    Densita' di token-rientrati per evento nella coda recente vs nel resto:
    recente >> storia -> i drop tornano a mordere -> pressione ->1 (TIENI).
    recente << storia -> non mordono piu' -> pressione ->0 (compatta prima).
    recente ~ storia, o poca storia -> 0.5 (neutro: nessun adattamento).

    Auto-calibrato sul log: nessuna soglia di token inventata. La normalizzazione
    contro la propria storia e' cio' che tiene questo 'misurato' e non 'predetto'."""
    evs = fault_events() if events is None else events
    if len(evs) < MIN_EVENTS:
        return 0.5
    tail = evs[-recent:]
    prior = evs[:-recent]
    if not prior:                              # tutto 'recente': niente baseline
        return 0.5
    rate_recent = sum(t for *_x, t in tail) / len(tail)
    rate_prior = sum(t for *_x, t in prior) / len(prior)
    if rate_prior <= 0:
        # Nessun costo storico: se ora c'e' costo -> massima pressione, altrimenti
        # niente segnale (neutro).
        return 0.85 if rate_recent > 0 else 0.5
    ratio = rate_recent / rate_prior
    # ratio 1 -> 0.5 ; 2x -> ~0.75 ; 0.5x -> ~0.25 . Saturazione dolce, clampata.
    return max(0.0, min(1.0, 0.5 * ratio))


def adaptive_threshold(base: float, pressure: float, span: float = SPAN) -> float:
    """Soglia dell'avviso di compattazione modulata dalla pressione, DENTRO una
    banda limitata attorno alla BASE scelta dall'operatore. pressure 0.5
    (neutro/freddo) -> ESATTAMENTE base: fallback grazioso al comportamento fisso,
    per qualunque base. Alta -> soglia su (avvisa tardi, TIENI il contesto vivo).
    Bassa -> soglia giu' (avvisa presto, il contesto e' morto). Lo scostamento e'
    limitato a span/2: e' il limite di sicurezza. Clamp finale solo a [0,1] di
    validita' — nessuna banda assoluta che scavalchi la base."""
    thr = base + (pressure - 0.5) * span
    return min(1.0, max(0.0, thr))


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def main() -> int:
    """Banco di misura: la pressione corrente, la soglia che consiglierebbe, e la
    sopravvivenza per classe. Da leggere come le altre telemetrie del plugin."""
    try:
        base = float(os.environ.get("CK_COMPACT_ADVISE", "0.70") or 0.70)
    except ValueError:
        base = 0.70
    evs = fault_events()
    p = recall_pressure(evs)
    thr = adaptive_threshold(base if base > 0 else 0.70, p)
    live = liveness_by_bucket(evs)

    if not evs:
        print("lifetime: no page fault recorded — no signal, "
              f"threshold at its base value {_fmt_pct(base if base > 0 else 0.70)} "
              "(no adaptation).")
        return 0

    verdict = ("KEEP (live context, drops come back)" if p > 0.58
               else "COMPACT EARLIER (dead context, drops do not come back)"
               if p < 0.42 else "neutral (no push either way)")
    print(f"lifetime: {len(evs)} page faults, pressure {p:.2f} -> {verdict}")
    print(f"  warning threshold: {_fmt_pct(base if base > 0 else 0.70)} base "
          f"-> {_fmt_pct(thr)} adaptive")
    if live:
        print("  survival by class (n recoveries, tokens clawed back):")
        for bucket, (n, t) in sorted(live.items(), key=lambda x: -x[1][1])[:10]:
            print(f"    {bucket:16s} {n:4d}x   ~{t:,} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
