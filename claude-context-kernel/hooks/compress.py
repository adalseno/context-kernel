#!/usr/bin/env python3
"""
compress.py — PostToolUse hook per claude-context-kernel.

Riduce i token di un output di tool PRIMA che entri nel contesto del modello,
sostituendolo via `hookSpecificOutput.updatedToolOutput`. Deterministico e
veloce (<600ms): niente rete, niente LLM.

Strategia "signal-preserving" — butta il rumore, tiene il segnale:
  1. rimuove sequenze ANSI e spam di progress-bar (\\r);
  2. deduplica righe consecutive identiche  ->  "riga  [x N]";
  3. collassa run di righe vuote;
  4. se ancora oltre budget: tiene testa + coda + TUTTE le righe che
     sembrano errori/warning, elidendo il rumore in mezzo con un marcatore.

Contratto hook: legge JSON da stdin, scrive JSON su stdout.
Su qualsiasi imprevisto e' un no-op sicuro (stampa "{}" e esce 0): non
deve mai rompere una sessione.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import time
import zlib
from typing import Callable

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass

try:
    import fcntl
except ImportError:                            # Windows: niente flock
    fcntl = None
try:
    import msvcrt
except ImportError:                            # POSIX: niente locking()
    msvcrt = None


@contextlib.contextmanager
def _locked(path: str):
    """Lock advisory sul file di stato: piu' sessioni concorrenti (anche
    headless) fanno read-modify-write sugli stessi JSON e senza mutua
    esclusione si perdono aggiornamenti (canary failed++, record reads).
    flock su POSIX, msvcrt.locking su Windows (LK_LOCK: bloccante con
    retry, ~10s). Se il lock non si ottiene, procedi comunque: l'hook
    non blocca mai."""
    lockf = None
    locked_nt = False
    try:
        if fcntl is not None:
            lockf = open(path + ".lock", "w")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        elif msvcrt is not None:
            lockf = open(path + ".lock", "a+")
            lockf.seek(0)
            msvcrt.locking(lockf.fileno(), msvcrt.LK_LOCK, 1)
            locked_nt = True
    except Exception:                          # noqa: BLE001
        if lockf is not None:
            try:
                lockf.close()
            except Exception:                  # noqa: BLE001
                pass
        lockf = None
        locked_nt = False
    try:
        yield
    finally:
        if lockf is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
                elif locked_nt:
                    lockf.seek(0)
                    msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
                lockf.close()
            except Exception:                  # noqa: BLE001
                pass


def _atomic_dump(obj, path: str) -> None:
    """Scrittura atomica: tmp nella stessa dir + os.replace. Un lettore
    concorrente non vede mai un JSON scritto a meta'."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)

# --- configurazione (override via env) ------------------------------------
MIN_TOKENS = int(os.environ.get("CK_MIN_TOKENS", "800"))   # soglia sotto cui non tocca
HEAD = int(os.environ.get("CK_HEAD", "45"))                # righe di testa da tenere
TAIL = int(os.environ.get("CK_TAIL", "20"))               # righe di coda da tenere
# Tool su cui agire. Override con CK_TOOLS="Bash,Grep" (es. per escludere
# Read quando un agent deve giudicare sorgenti riga per riga).
MATCHERS = tuple(
    t.strip() for t in os.environ.get(
        "CK_TOOLS", "Bash,Grep,Read,Glob,WebFetch"
    ).split(",") if t.strip()
)
# Agent "giudici": la loro Read non va MAI alterata — l'elisione nasconderebbe
# proprio le righe sotto giudizio. Il payload hook dei subagent porta
# agent_type (verificato col tap, Claude Code 2.1.210): meccanismo, non
# convenzione. Override con CK_AGENT_SKIP="tipo1,tipo2" (vuoto = disattivo).
AGENT_SKIP_READ = tuple(
    t.strip() for t in os.environ.get(
        "CK_AGENT_SKIP", "kernel-verifier,kernel-extractor,kernel-scout"
    ).split(",") if t.strip()
)
# Escape per-comando: se il comando Bash contiene il marcatore, l'output passa
# INTATTO (niente compressione ne' campionamento A/B). Per quando servono TUTTE
# le righe di un comando specifico: `pytest -x  # ck:raw`.
RAW_MARK = os.environ.get("CK_RAW_MARK", "# ck:raw")
# Estrazioni ESPLICITE via shell: `sed -n 'A,Bp' file`, `awk 'NR>=A...'`,
# e recall.py (il page fault del parcheggio). Stesso principio della Read
# con offset/limit (che gia' passa raw): chi scrive il comando ha GIA'
# proiettato — ha detto esattamente quali righe vuole — e T1 deve essere
# l'identita' su quell'immagine, o l'elisione toglie proprio il payload
# richiesto e forza una rilettura (fault) che costa piu' del risparmio.
# Per recall.py e' peggio: comprimerne l'output significa proiettare
# l'INVERSA della proiezione — il recupero non converge mai.
# Cap di sicurezza: un'estrazione resta un'estrazione solo se l'output e'
# nell'ordine di cio' che una finestra esplicita puo' chiedere.
EXTRACTION_RE = re.compile(
    r"recall\.py|sed\s+(-\w+\s+)*-n|awk\s+\S*['\"]\s*NR\b")
EXTRACTION_MAX = int(os.environ.get("CK_EXTRACTION_MAX", "8000"))
# Campo con cui l'harness sostituisce l'output. Claude Code: updatedToolOutput.
# Codex potrebbe usare un nome diverso: override con CK_POSTOUT_FIELD.
POSTOUT_FIELD = os.environ.get("CK_POSTOUT_FIELD", "updatedToolOutput")
LOG_PATH = os.path.expanduser(
    os.environ.get("CK_LOG", "~/.context-kernel-savings.log")
)
# --- canary end-to-end ------------------------------------------------------
# Verifica che la compressione sia stata APPLICATA davvero dall'harness, non
# solo calcolata: il transcript della sessione registra cio' che e' entrato
# nel contesto del modello. Quando comprimiamo, annotiamo il tool_use_id come
# "pending"; alla invocazione successiva cerchiamo nel transcript il
# tool_result di quell'id: se contiene il footer, la sostituzione e' avvenuta;
# se ne e' privo, l'harness ha ignorato updatedToolOutput -> allarme.
CANARY_ENABLED = os.environ.get("CK_CANARY", "1") != "0"
CANARY_STATE = os.path.expanduser(
    os.environ.get("CK_CANARY_STATE", "~/.context-kernel-canary.json")
)
CANARY_TTL_S = int(os.environ.get("CK_CANARY_TTL", "86400"))
# Pending della STESSA sessione mai comparsi nel transcript: sono quasi sempre
# compressioni avvenute dentro subagent (il loro tool_result vive nel
# transcript del subagent, non qui) -> non giudicabili, drop dopo 1h.
CANARY_PENDING_TTL_S = int(os.environ.get("CK_CANARY_PENDING_TTL", "3600"))
CANARY_TAIL_BYTES = 4_000_000          # legge solo la coda del transcript
# AUTO-DEGRADE: dopo N violazioni NELLA STESSA sessione, comprimere e' inutile
# o dannoso (l'harness scarta updatedToolOutput -> l'output pieno entra COMUNQUE
# nel contesto, e i nostri marker sono solo rumore in piu'). Oltre la soglia la
# sessione passa a raw pass-through per il resto della sua vita. 0 = spento
# (resta il vecchio comportamento: solo avviso). Per-sessione: una nuova
# sessione riparte pulita (l'id e' unico), quindi il degrado non e' mai
# permanente ne' contagioso tra sessioni.
CANARY_DEGRADE_N = int(os.environ.get("CK_CANARY_DEGRADE_N", "3"))
# AUTO-HEAL: il canary si gestisce da solo anche in USCITA dal guasto, nelle
# due direzioni, sempre su evidenza dal transcript (mai a tempo):
# (a) auto-ack: ogni compressione verificata e' una sonda naturale del
#     contratto; dopo HEAL_M verified CONSECUTIVE (senza nuovi failure) i
#     failed aperti vengono riconosciuti come transitori. Contatore separato
#     (failed_auto_acked) e archivio in auto_acks coi failure originali:
#     nulla sparisce, si spegne solo l'allarme che chiedeva --reset-canary.
# (b) un-degrade a sonda: una sessione degradata resta raw, ma ogni PROBE_K
#     output comprimibili UNO ripassa dal flusso normale come sonda; PROBE_M
#     sonde verificate consecutive tolgono il degrado. Senza prove nel
#     transcript la sessione resta raw per sempre (com'era prima).
# CK_CANARY_AUTOHEAL=0 spegne entrambe (resta la gestione manuale).
CANARY_AUTOHEAL = os.environ.get("CK_CANARY_AUTOHEAL", "1") != "0"
CANARY_HEAL_M = int(os.environ.get("CK_CANARY_HEAL_M", "5"))
CANARY_PROBE_K = int(os.environ.get("CK_CANARY_PROBE_K", "10"))
CANARY_PROBE_M = int(os.environ.get("CK_CANARY_PROBE_M", "3"))
FOOTER_MARK = "[context-kernel:"


def session_id(transcript_path: str | None) -> str:
    """Identificatore corto della sessione (dal nome del transcript)."""
    if not transcript_path:
        return "-"
    base = os.path.basename(transcript_path)
    if base.endswith(".jsonl"):
        base = base[:-6]
    return base[:8] or "-"


def log_savings(tool: str, before: int, after: int, session: str = "-",
                agent: str = "-") -> None:
    """Registra il risparmio in CSV: ts,tool,before,after,saved,sessione,agent.
    `agent` e' l'id corto del subagent quando la compressione avviene in un
    subagent/workflow ("-" nel main loop): la sessione resta quella MADRE,
    cosi' statusline e aggregati per sessione non cambiano. Mai fatale.
    (Le righe storiche a 5 o 6 campi restano valide per savings.py.)"""
    if os.environ.get("CK_LOG_OFF") == "1":
        return
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts},{tool},{before},{after},{before - after},"
                    f"{session},{agent}\n")
    except Exception:                          # noqa: BLE001
        pass

# --- ledger dei PAGE FAULT (il lato DISTORSIONE della curva) -----------------
# savings.log misura il RATE (token tolti); da solo e' meta' della verita'.
# Ogni elisione e' una scommessa: "la risposta non avra' bisogno del resto".
# Quando la scommessa perde, il resto RIENTRA nel contesto — riletta integrale
# di un file eliso, riesecuzione di un comando eliso, recall di un output
# parcheggiato. Quel rientro E' la distorsione, e qui si misura in PRODUZIONE
# (non solo nell'oracolo offline del bench): ogni fault e' attribuito
# all'elisione che l'ha causato col suo costo (i token risparmiati che sono
# tornati). Solo numeri e categorie, mai contenuto — come savings.log.
FAULT_LOG = os.path.expanduser(
    os.environ.get("CK_FAULT_LOG", "~/.context-kernel-faults.log")
)


def log_fault(kind: str, bucket: str, tokens: int, session: str = "-") -> None:
    """Registra un page fault: ts,kind,bucket,token,sessione.
    kind = reread (Read integrale post-elisione) | recmd (riesecuzione Bash/MCP
    post-elisione) | recall (recupero mirato da parcheggio). bucket = estensione
    del file / nome del tool / "recall". token = costo del recupero (risparmio
    rientrato). Stesso kill-switch del risparmio (CK_LOG_OFF). Mai fatale."""
    if os.environ.get("CK_LOG_OFF") == "1":
        return
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(FAULT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts},{kind},{bucket},{max(0, int(tokens))},{session}\n")
    except Exception:                          # noqa: BLE001
        pass

SHAPE_LOG = os.path.expanduser(
    os.environ.get("CK_SHAPE_LOG", "~/.context-kernel-shapes.log")
)


def log_unknown_shape(tool: str, resp) -> None:
    """Sentinella delle forme: un tool trattato da cui NON si e' estratto
    testo ha una forma di tool_response che non conosciamo (vedi il caso
    Read annidato, invisibile per mesi). Registra le chiavi, mai il contenuto."""
    if os.environ.get("CK_LOG_OFF") == "1":
        return
    try:
        def _has_text(v, depth: int = 0) -> bool:
            if isinstance(v, str):
                # solo testo SOSTANZIOSO: i metadati corti (path, mode, ...)
                # non sono output perso
                return len(v.strip()) >= 200
            if depth >= 2:
                return False
            if isinstance(v, dict):
                return any(_has_text(x, depth + 1) for x in v.values())
            if isinstance(v, list):
                return any(_has_text(x, depth + 1) for x in v)
            return False

        if not _has_text(resp):               # niente testo perso: non e' un miss
            return
        shape: dict = {"tool": tool, "type": type(resp).__name__}
        if isinstance(resp, dict):
            shape["keys"] = sorted(resp.keys())
            nested = {k: sorted(v.keys()) for k, v in resp.items()
                      if isinstance(v, dict)}
            if nested:
                shape["nested"] = nested
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(SHAPE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, **shape}, sort_keys=True) + "\n")
    except Exception:                          # noqa: BLE001
        pass


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# \bfault e non fault: "default" lo conterrebbe; \bkilled\b: "skilled";
# \boom\b: "room"/"zoom". timed\s?out copre anche ETIMEDOUT.
SIGNAL = re.compile(
    r"error|errore|fail|fatal|exception|traceback|warn|"
    r"deprecat|notice|strict|"
    r"\bfault|\bkilled\b|timed\s?out|timeout|not found|no such|conflict|"
    r"\babort|reject|unhandled|unable|invalid|unexpected|"
    r"sigsegv|sigabrt|sigkill|out of memory|\boom\b|broken pipe|"
    r"\bE\d{3,}\b|✗|✘|❌|panic|denied|refused|cannot|missing|undefined",
    re.IGNORECASE,
)

# Nei SORGENTI il segnale e' la STRUTTURA (firme, classi, import), non le
# parole error/exception: la regex log-oriented si INVERTE sul codice —
# tiene gli `except Exception: pass` e butta la logica vera.
CODE_EXTS = (".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
             ".go", ".rs", ".java", ".rb", ".c", ".h", ".cc", ".cpp", ".hpp",
             ".swift", ".kt", ".php", ".cs", ".scala", ".sh", ".bash", ".zsh")
# Copertura per linguaggio (audit 2026-07-17): Python def/class/import/@,
# JS/TS function/export/const/interface/enum/type/async function,
# Go func/type/package, Rust fn/impl/struct/trait/pub/mod/use/async fn,
# PHP use/namespace/require/include/final/abstract + visibilita',
# Ruby require/include/module, C/C++ #include/#define/typedef/template/
# static/using, C# using/namespace, Kotlin fun/data class/sealed/override,
# Scala object/case class, Swift extension/protocol, shell source/function.
# Limite noto: le funzioni C "tipo-prima" (int main(...)) non hanno keyword
# e restano coperte solo da HEAD/TAIL + page fault.
CODE_SIGNAL = re.compile(
    r"^\s*(?:async\s|def\s|class\s|import\s|from\s+\S+\s+import\s|@\w|"
    r"function\s|export\s|const\s|interface\s|enum\s|type\s+\w+|"
    r"func\s|fn\s|impl\s|struct\s|trait\s|pub\s|package\s|module\s|mod\s|"
    r"use\s|using\s|namespace\s|object\s|extension\s|protocol\s|fun\s|"
    r"final\s|abstract\s|static\s|sealed\s|override\s|"
    r"require(?:_\w+)?\b|include(?:_\w+)?\b|source\s|"
    r"case\s+class\s|data\s+class\s|typedef\s|template\s*<|"
    r"#\s*(?:include|define|pragma|ifn?def|endif)\b|"
    r"public\s|private\s|protected\s)"
)

# Un output Bash puo' trasportare CODICE (grep di sorgenti, dump di simboli) o
# un DIFF: la SIGNAL log-oriented non li riconosce come segnale e li elide. Due
# degradati A/B misurati (2026-07-18, sessione 762f1563): ~40 nomi di funzioni
# da un grep elisi come rumore; un header di hunk `@@ ... @@ def squash` perso
# con la funzione contenitore e il numero di riga. Aggiungiamo due riconoscitori
# STRUTTURALI al segnale del SOLO ramo Bash — mirati: scattano su codice-dentro-
# shell e diff, non allargano la compressione dei log ordinari.
GREP_PREFIX = re.compile(r"^(?:[^\s:]+:)?\d+[:-]")   # file:NN: | NN: | NN- (grep -n/-rn/-C)
DIFF_STRUCT = re.compile(r"^(?:@@ |diff --git |\+\+\+ |--- )")
# In un diff le righe CAMBIATE (+/- singolo, esclusi i marcatori di file
# +++/---) sono il payload, non la struttura: per il task "rivedi le modifiche"
# la loro elisione non e' sufficiency-preserving. Degradato A/B misurato
# (2026-07-18, refactoring di 3 file/12 hunk/273 righe): 1.29.1 tiene 12/12
# hunk header ma solo 19/240 righe +/- (testa/coda) e 1/12 dichiarazioni
# `+public function ...` — il + iniziale rompe l'ancora ^\s* di CODE_SIGNAL.
DIFF_LINE = re.compile(r"^[+-](?![+-]{2})")


def _bash_signal(line: str) -> bool:
    """Segnale per output Bash: gli errori/log di SIGNAL, PIU' la struttura di
    un diff (l'hunk header porta funzione contenitore e numeri di riga) e le
    dichiarazioni di codice anche dietro un prefisso di grep (NN:/file:NN:)."""
    if SIGNAL.search(line):
        return True
    if DIFF_STRUCT.match(line):
        return True
    return bool(CODE_SIGNAL.match(GREP_PREFIX.sub("", line, count=1)))


def _diff_aware(lines: list[str]) -> bool:
    """Vero se l'output E' un diff: contiene almeno 2 righe di STRUTTURA
    (header di file o di hunk). I log ordinari con bullet '- voce' (npm,
    composer, changelog) non le hanno, quindi non attivano il riconoscimento
    contestuale delle righe cambiate — la compressione dei log resta piena."""
    return sum(1 for l in lines if DIFF_STRUCT.match(l)) >= 2


def _diff_signal(line: str) -> bool:
    """Segnale per un output riconosciuto come diff: le righe cambiate (+/-)
    sono payload, PIU' tutto cio' che il ramo Bash gia' considera segnale
    (struttura, codice, errori). Si elidono SOLO le righe di contesto (prefisso
    spazio) e il rumore, MAI le righe cambiate."""
    return _bash_signal(line) or bool(DIFF_LINE.match(line))


def _sig(signal: "re.Pattern | Callable[[str], bool]", line: str) -> bool:
    """Un predicato di segnale puo' essere una regex (log/codice) o una
    funzione (il ramo Bash che unisce piu' riconoscitori)."""
    return signal(line) if callable(signal) else bool(signal.search(line))


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _blocks_text(blocks: list) -> str:
    """Testo dai content block MCP: [{"type": "text", "text": ...}, ...]."""
    parts = [b.get("text") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"
             and isinstance(b.get("text"), str)]
    return "\n".join(p for p in parts if p)


def extract_output(payload: dict) -> tuple[str, str | None]:
    """Ritorna (testo_output, file_path_se_read)."""
    resp = payload.get("tool_response", payload.get("tool_output"))
    text = ""
    if isinstance(resp, str):
        text = resp
    elif isinstance(resp, list):
        # tool MCP: lista di content block
        text = _blocks_text(resp)
    elif isinstance(resp, dict):
        for k in ("stdout", "output", "content", "result", "text"):
            v = resp.get(k)
            if isinstance(v, str) and v:
                text = v
                break
        if not text:
            # Read: forma annidata {"type": "text", "file": {"content": ...}}
            f = resp.get("file")
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                text = f["content"]
        if not text and isinstance(resp.get("content"), list):
            # tool MCP: {"content": [blocchi], "isError": ...}
            text = _blocks_text(resp["content"])
        err = resp.get("stderr")
        if isinstance(err, str) and err.strip():
            text = (text + "\n" + err) if text else err
    tin = payload.get("tool_input", {})
    fpath = tin.get("file_path") if isinstance(tin, dict) else None
    return text, fpath


def normalize(text: str) -> str:
    text = ANSI.sub("", text)
    out = []
    for line in text.split("\n"):
        if "\r" in line:                       # progress bar: tieni l'ultimo stato
            line = line.split("\r")[-1]
        out.append(line.rstrip())
    return "\n".join(out)


def _collapse_period2(lines: list[str]) -> list[str]:
    """Collassa cicli A,B,A,B,... (spinner a 2 righe alternate): >=3 coppie
    piene diventano le 2 righe del ciclo + contatore. Il dedup semplice non li
    vede (nessuna riga e' uguale alla precedente)."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        a = lines[i]
        if i + 1 < n:
            b = lines[i + 1]
            if a != b and a.strip() and b.strip():
                k = 1
                while (i + 2 * k + 1 < n
                       and lines[i + 2 * k] == a and lines[i + 2 * k + 1] == b):
                    k += 1
                if k >= 3:
                    out.append(a)
                    out.append(f"{b}  [x {k} coppie alternate]")
                    i += 2 * k
                    continue
        out.append(a)
        i += 1
    return out


def dedup(lines: list[str]) -> list[str]:
    result: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        while j + 1 < n and lines[j + 1] == lines[i]:
            j += 1
        count = j - i + 1
        if count >= 3 and lines[i].strip():
            result.append(f"{lines[i]}  [x {count}]")
        else:
            result.extend(lines[i : j + 1])
        i = j + 1
    result = _collapse_period2(result)
    # collassa run di righe vuote
    collapsed: list[str] = []
    blank = 0
    for l in result:
        if not l.strip():
            blank += 1
            if blank <= 1:
                collapsed.append(l)
        else:
            blank = 0
            collapsed.append(l)
    return collapsed


ELISION_MARK = "[context-kernel: elided"
# marker delle elisioni STRUTTURALI (oggetti JSON, non righe)
JSON_MARK = "[context-kernel: dropped"


def has_elision(text: str) -> bool:
    return ELISION_MARK in text or JSON_MARK in text

_FIRST_NUM = re.compile(r"\d+")


def _numeric_continuity(elided: list[str]) -> str | None:
    """Se le righe elise portano una numerazione aritmetica ininterrotta
    (step 045, step 046, ... — primo intero di ogni riga), dichiararlo nel
    marker: il lettore puo' fidarsi che non manca nessun elemento.
    (Dal primo DEGRADATO del ledger A/B, 2026-07-17: l'elisione di un log di
    migrazione perdeva proprio la verificabilita' della continuita'.)"""
    if len(elided) < 3:
        return None
    nums = []
    for line in elided:
        m = _FIRST_NUM.search(line)
        if not m:
            return None
        nums.append(int(m.group()))
    step = nums[1] - nums[0]
    if step == 0 or any(b - a != step for a, b in zip(nums, nums[1:])):
        return None
    passo = f" with step {step}" if step not in (1, -1) else ""
    return f"unbroken numbering {nums[0]}→{nums[-1]}{passo}"


def signal_preserving_truncate(
        lines: list[str],
        signal: "re.Pattern | Callable[[str], bool]" = SIGNAL,
        kind: tuple[str, str] = ("noise", "carrying signal"),
        head_n: int | None = None,
        tail_n: int | None = None) -> list[str]:
    head_n = HEAD if head_n is None else head_n
    tail_n = TAIL if tail_n is None else tail_n
    if len(lines) <= head_n + tail_n + 5:
        return lines
    head = lines[:head_n]
    tail = lines[-tail_n:]
    middle = lines[head_n:-tail_n]
    kept_signal = [l for l in middle if _sig(signal, l)]
    elided_lines = [l for l in middle if not _sig(signal, l)]
    elided = len(elided_lines)
    elided_tokens = est_tokens("\n".join(middle)) - est_tokens("\n".join(kept_signal))
    # Range espliciti (1-based, riferiti all'output PRIMA dell'elisione,
    # post-normalizzazione): rendono il page fault MIRATO — su una Read si
    # puo' rileggere solo la finestra elisa con offset/limit invece del
    # file intero.
    start, end = head_n + 1, len(lines) - tail_n
    cont = _numeric_continuity(elided_lines)
    marker = (
        f"{ELISION_MARK} lines {start}-{end}: {elided} lines of {kind[0]} "
        f"(~{elided_tokens} tokens)"
        + (f"; {cont}" if cont else "")
        + f"; kept {len(kept_signal)} lines {kind[1]}]"
    )
    out = head + [marker]
    if kept_signal:
        out += kept_signal
    out += tail
    return out


def compress(text: str, fpath: str | None = None, scale: float = 1.0) -> str:
    lines = normalize(text).split("\n")
    lines = dedup(lines)
    head_n = max(10, int(HEAD * scale))
    tail_n = max(8, int(TAIL * scale))
    if fpath and fpath.lower().endswith(CODE_EXTS):
        lines = signal_preserving_truncate(
            lines, signal=CODE_SIGNAL,
            kind=("body", "of structure (def/class/import)"),
            head_n=head_n, tail_n=tail_n)
    elif _diff_aware(lines):
        lines = signal_preserving_truncate(
            lines, signal=_diff_signal,
            kind=("diff context", "changed (+/-) and structural"),
            head_n=head_n, tail_n=tail_n)
    else:
        lines = signal_preserving_truncate(
            lines, signal=_bash_signal, head_n=head_n, tail_n=tail_n)
    return "\n".join(lines).strip()


# --- canary end-to-end ------------------------------------------------------

def _canary_load() -> dict:
    try:
        with open(CANARY_STATE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("pending", [])
            st.setdefault("verified", 0)
            st.setdefault("failed", 0)
            st.setdefault("last_ok", None)
            st.setdefault("last_failure", None)
            st.setdefault("degraded_sessions", [])
            st.setdefault("heal_streak", 0)
            st.setdefault("probe", {})
            return st
    except Exception:                          # noqa: BLE001
        pass
    return {"pending": [], "verified": 0, "failed": 0,
            "last_ok": None, "last_failure": None, "degraded_sessions": [],
            "heal_streak": 0, "probe": {}}


def _canary_save(st: dict) -> None:
    try:
        _atomic_dump(st, CANARY_STATE)
    except Exception:                          # noqa: BLE001
        pass


def _transcript_result_line(transcript_path: str, tool_use_id: str) -> str | None:
    """Cerca nel transcript (JSONL) la riga col tool_result di quell'id.
    Legge solo la coda: costo limitato anche su sessioni lunghe."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            if size > CANARY_TAIL_BYTES:
                f.seek(size - CANARY_TAIL_BYTES)
                f.readline()                   # scarta la riga troncata
            for line in f:
                if tool_use_id in line and "tool_result" in line:
                    return line
    except Exception:                          # noqa: BLE001
        return None
    return None


def canary_check(payload: dict) -> str | None:
    """Verifica le compressioni pending contro il transcript reale.
    Ritorna un messaggio di allarme se una risulta NON applicata."""
    if not CANARY_ENABLED:
        return None
    tp = payload.get("transcript_path")
    if not tp:
        return None
    with _locked(CANARY_STATE):
        st = _canary_load()
        if not st["pending"]:
            return None
        now = time.time()
        iso = datetime.datetime.now().isoformat(timespec="seconds")
        sess = session_id(tp)
        alert = None
        still: list[dict] = []
        for p in st["pending"]:
            age = now - p.get("ts", 0)
            expired = age >= CANARY_TTL_S
            if p.get("transcript") != tp:      # altra sessione: non giudicabile qui
                if not expired:
                    still.append(p)
                continue
            line = _transcript_result_line(tp, p.get("id", ""))
            if line is None:                   # non ancora nel transcript
                # stessa sessione ma mai comparso: quasi certamente un subagent
                # (il suo transcript e' un altro file) -> drop dopo il TTL breve
                if not expired and age < CANARY_PENDING_TTL_S:
                    still.append(p)
                continue
            # Match sul footer ESATTO (coi numeri) registrato alla compressione:
            # il prefisso generico scatterebbe anche su contenuti che CITANO il
            # footer (doc, log, transcript riletti). Fallback al prefisso solo
            # per pending vecchi senza campo footer (retrocompatibilita', TTL 24h).
            mark = p.get("footer") or FOOTER_MARK
            if mark in line:
                st["verified"] += 1
                st["last_ok"] = iso
                # ogni verified e' evidenza che il contratto tiene: alimenta la
                # striscia che auto-riconosce i failure transitori (auto-ack)
                st["heal_streak"] = st.get("heal_streak", 0) + 1
                if p.get("probe"):
                    alert = _probe_verified(st, sess) or alert
            else:
                st["failed"] += 1
                st["last_failure"] = iso
                st["heal_streak"] = 0
                st["failures"] = (st.get("failures", []) + [
                    {"ts": iso, "session": sess, "tool": p.get("tool")}
                ])[-50:]
                if p.get("probe"):
                    # sonda fallita in sessione GIA' degradata: esito atteso,
                    # niente allarme (sarebbe rumore ogni PROBE_K output) —
                    # si azzera solo la striscia di sonde buone e si resta raw.
                    pr = st.get("probe", {}).get(sess)
                    if pr:
                        pr["ok"] = 0
                    continue
                alert = (
                    "context-kernel CANARY: the previous compression "
                    f"(tool_use {p.get('id', '?')[:16]}) does not appear applied in "
                    "the transcript: the harness ignored updatedToolOutput. The "
                    "logged savings are only theoretical until the contract is "
                    "restored (check the field shape, dict vs string). Tell the user."
                )
                # auto-degrade: N violazioni nella STESSA sessione -> smetti di
                # comprimere per il resto della sessione (il messaggio di degrado
                # sostituisce quello generico: e' piu' azionabile).
                degraded = st.setdefault("degraded_sessions", [])
                if (CANARY_DEGRADE_N > 0 and sess and sess != "-"
                        and sess not in degraded
                        and _session_failures(st, sess) >= CANARY_DEGRADE_N):
                    st["degraded_sessions"] = (degraded + [sess])[-50:]
                    alert = (
                        "context-kernel AUTO-DEGRADE: "
                        f"{_session_failures(st, sess)} compressions not applied "
                        "in this session (threshold "
                        f"{CANARY_DEGRADE_N}). The harness is ignoring "
                        "updatedToolOutput -> from now on outputs pass through "
                        "INTACT (raw pass-through), no more compression or markers "
                        "until the session restarts. Tell the user."
                    )
        st["pending"] = still
        _autoheal_ack(st, iso)
        _canary_save(st)
        return alert


def _session_failures(st: dict, sess: str) -> int:
    """Quante violazioni registrate per QUESTA sessione."""
    return sum(1 for f in st.get("failures", []) if f.get("session") == sess)


def _probe_verified(st: dict, sess: str) -> str | None:
    """Sonda verificata in una sessione degradata: PROBE_M consecutive sono
    l'evidenza che l'harness onora di nuovo updatedToolOutput -> il degrado
    si toglie. Chiamata con lo stato gia' lockato da canary_check."""
    if not (CANARY_AUTOHEAL and CANARY_PROBE_M > 0):
        return None
    pr = st.setdefault("probe", {}).setdefault(sess, {"count": 0, "ok": 0})
    pr["ok"] += 1
    if sess in st.get("degraded_sessions", []) and pr["ok"] >= CANARY_PROBE_M:
        st["degraded_sessions"] = [
            s for s in st["degraded_sessions"] if s != sess]
        st["probe"].pop(sess, None)
        return (
            "context-kernel RECOVERY: "
            f"{CANARY_PROBE_M} consecutive canary probes appear applied in "
            "the transcript — the harness honours updatedToolOutput again. "
            "The auto-degrade for this session is lifted: compression "
            "resumes."
        )
    return None


def _autoheal_ack(st: dict, iso: str) -> None:
    """Auto-ack con evidenza: HEAL_M verified consecutive senza nuovi failure
    -> i failed aperti erano transitori e vengono riconosciuti da soli (niente
    piu' --reset-canary manuale per i glitch). Contatore separato da
    failed_acked e failure originali archiviati in auto_acks: nulla sparisce,
    si spegne solo l'allarme. Chiamata con lo stato gia' lockato."""
    if not (CANARY_AUTOHEAL and CANARY_HEAL_M > 0):
        return
    fl = st.get("failed", 0)
    streak = st.get("heal_streak", 0)
    if fl <= 0 or streak < CANARY_HEAL_M:
        return
    st["failed_auto_acked"] = st.get("failed_auto_acked", 0) + fl
    st["auto_acks"] = (st.get("auto_acks", []) + [{
        "ts": iso, "n": fl, "streak": streak,
        "failures": st.get("failures", [])[-10:],
    }])[-20:]
    st["failed"] = 0
    st["failures"] = []
    print(f"context-kernel: canary auto-ack — {fl} transient failures "
          f"acknowledged ({streak} consecutive verified compressions "
          "since the last one)", file=sys.stderr)


def canary_probe_due(payload: dict) -> bool:
    """Sessione degradata: conta gli output abbastanza grandi da comprimere e
    ogni PROBE_K ne lascia ripassare UNO dal flusso normale come sonda (il
    pending viene marcato probe; il verdetto lo da' canary_check al giro
    dopo). Gli output sotto soglia non consumano lo slot: la sonda deve
    cadere su una compressione REALE, altrimenti non produce evidenza."""
    if not (CANARY_ENABLED and CANARY_AUTOHEAL and CANARY_PROBE_K > 0):
        return False
    tp = payload.get("transcript_path")
    sess = session_id(tp) if tp else "-"
    if sess == "-" or payload.get("agent_id"):
        return False                           # subagent: pending mai giudicabile
    try:
        text, _ = extract_output(payload)
        if est_tokens(text) < MIN_TOKENS:
            return False
    except Exception:                          # noqa: BLE001
        return False
    with _locked(CANARY_STATE):
        st = _canary_load()
        pr = st.setdefault("probe", {}).setdefault(sess, {"count": 0, "ok": 0})
        pr["count"] += 1
        _canary_save(st)
        return pr["count"] % CANARY_PROBE_K == 0


def canary_degraded(payload: dict) -> bool:
    """True se questa sessione ha superato la soglia di violazioni e va servita
    in raw pass-through. Letto ANCHE quando canary_check non trova nuovi
    fallimenti: il degrado puo' essere stato deciso a un giro precedente."""
    if not CANARY_ENABLED or CANARY_DEGRADE_N <= 0:
        return False
    tp = payload.get("transcript_path")
    sess = session_id(tp) if tp else "-"
    if not tp or sess == "-":
        return False
    with _locked(CANARY_STATE):
        return sess in _canary_load().get("degraded_sessions", [])


def canary_record(payload: dict, footer: str) -> None:
    """Annota la compressione appena emessa: verra' verificata al giro dopo.
    Il footer esatto (coi numeri) e' il marcatore da cercare nel transcript."""
    if not CANARY_ENABLED:
        return
    if payload.get("agent_id"):
        # subagent: il suo tool_result vive nel transcript del subagent,
        # ma transcript_path qui punta alla sessione madre -> il pending
        # non sarebbe MAI verificabile. Non registrare (il TTL breve
        # resta come rete di sicurezza per stati vecchi).
        return
    tid = payload.get("tool_use_id")
    tp = payload.get("transcript_path")
    if not tid or not tp:
        return
    with _locked(CANARY_STATE):
        st = _canary_load()
        # il tool e' il fingerprint del failure: distingue "l'harness ha rotto
        # il contratto per QUESTA forma di payload" da un glitch generico
        entry = {"id": tid, "transcript": tp, "ts": time.time(),
                 "footer": footer, "tool": payload.get("tool_name")}
        if payload.get("_ck_probe"):
            entry["probe"] = True              # sonda di un-degrade
        st["pending"] = (st["pending"] + [entry])[-50:]
        _canary_save(st)


TAP_FLAG = os.path.expanduser(
    os.environ.get("CK_TAP_FLAG", "~/.context-kernel-tap")
)
# Fotografia dell'occupazione della finestra di contesto, presa dall'ultimo
# blocco "usage" del transcript. La scrive il hook (che gira comunque su ogni
# tool call), la legge repo_slice.py per `--budget auto`: il budget si
# calcola DA SOLO da finestra - occupato, zero numeri passati a mano.
CONTEXT_STATE = os.path.expanduser(
    os.environ.get("CK_CONTEXT_STATE", "~/.context-kernel-context.json")
)


# --- delta sulle RILETTURE (idea "Delta Context", rifocalizzata) -------------
# Il prompt caching gia' sconta il prefisso invariato: il costo vero sono i
# CONTENUTI RIDONDANTI NUOVI — lo stesso file riletto e' ripagato pieno.
# Qui: prima Read normale (registriamo hash+contenuto); rilettura INVARIATA ->
# marker di poche righe; rilettura CAMBIATA -> unified diff contro la copia
# gia' nel contesto. Escape page-fault: se il modello rilegge SUBITO dopo un
# marker (vuole davvero il contenuto), la volta dopo passa integrale.
# --- PARCHEGGIO degli output effimeri elisi ----------------------------------
# Il page fault delle Read funziona perche' il file e' ancora su disco:
# l'inversa della proiezione e' un access path. Per Bash/MCP/WebFetch
# l'inversa NON esisteva: il comando e' gia' girato (rieseguirlo puo' essere
# non-idempotente o costoso) e cio' che l'elisione toglieva era perso.
# Qui: l'ORIGINALE integrale viene parcheggiato su disco al momento
# dell'elisione, e il footer dichiara la via di recupero MIRATA
# (recall.py --grep/--lines: paghi i token di cio' che chiedi, non
# dell'output intero). Deterministico: grep e range, nessun ranking.
PARK_ENABLED = os.environ.get("CK_PARK", "1") != "0"
# --- dividendo del parcheggio -----------------------------------------------
# L'elisione e' il tipo RISCHIOSO di compressione; il suo rischio dipende
# dall'esistenza dell'inversa. Per i file l'inversa c'e' sempre (il file e'
# su disco); per gli EFFIMERI (Bash/WebFetch/MCP) esiste solo da quando il
# parcheggio garantisce il recall mirato. Quindi il tasso puo' essere piu'
# aggressivo ESATTAMENTE sull'insieme dei tool parcheggiati, ed ESATTAMENTE
# quando il parcheggio e' attivo: con CK_PARK=0 il moltiplicatore si spegne
# da solo (niente inversa -> niente aggressivita'). Misurato su corpus
# reale prima di scegliere il default (bench/ephemeral_dividend.py).
EPHEMERAL_SCALE = float(os.environ.get("CK_EPHEMERAL_SCALE", "0.5") or 1.0)
PARK_STATE = os.path.expanduser(
    os.environ.get("CK_PARK_STATE", "~/.context-kernel-park.json"))
PARK_MAX_BYTES = int(os.environ.get("CK_PARK_MAX", str(512 * 1024)))
PARK_KEEP = int(os.environ.get("CK_PARK_KEEP", "80"))  # il tasso aggressivo
# raddoppia le elisioni effimere (misurato: 626->1532 sul corpus reale) —
# lo store dell'inversa scala con l'aggressivita' che autorizza
PARK_TTL_S = int(os.environ.get("CK_PARK_TTL", "86400"))


def park_output(payload: dict, text: str, saved: int = 0) -> str | None:
    """Parcheggia l'originale di un output effimero eliso; ritorna la chiave
    corta per recall.py, None se disattivo o su qualsiasi errore (il
    parcheggio e' una rete di sicurezza: mai fatale, mai bloccante). `saved`
    (i token tolti dall'elisione) resta nell'entry a titolo informativo — il
    fault di un recall pero' costa solo i token EFFETTIVAMENTE restituiti
    (recupero mirato), che recall.py logga da se'."""
    if not PARK_ENABLED:
        return None
    try:
        raw = text.encode("utf-8", "replace")
        trunc = len(raw) > PARK_MAX_BYTES
        if trunc:
            raw = raw[:PARK_MAX_BYTES]
        key = hashlib.sha1(raw[:4096] + str(len(raw)).encode()).hexdigest()[:10]
        tin = payload.get("tool_input") or {}
        entry = {
            "ts": time.time(),
            "tool": payload.get("tool_name") or "?",
            "cmd": str(tin.get("command") or tin.get("url") or "")[:200],
            "z": base64.b64encode(zlib.compress(raw)).decode("ascii"),
            "trunc": trunc,
            "saved": int(saved),
        }
        with _locked(PARK_STATE):
            try:
                with open(PARK_STATE, encoding="utf-8") as f:
                    st = json.load(f)
                if not isinstance(st, dict):
                    st = {}
            except Exception:                  # noqa: BLE001
                st = {}
            st[key] = entry
            now = time.time()
            for k in list(st):                 # scaduti fuori
                if now - st[k].get("ts", 0) > PARK_TTL_S:
                    st.pop(k, None)
            for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-PARK_KEEP]:
                st.pop(k, None)                # LRU: tieni gli ultimi
            _atomic_dump(st, PARK_STATE)
        return key
    except Exception:                          # noqa: BLE001
        return None


DELTA_ENABLED = os.environ.get("CK_DELTA", "1") != "0"
DELTA_MIN_TOKENS = int(os.environ.get("CK_DELTA_MIN", "200"))
DELTA_STORE_MAX = 32_768                   # contenuti oltre: solo hash (no diff)
READS_STATE = os.path.expanduser(
    os.environ.get("CK_READS_STATE", "~/.context-kernel-reads.json")
)

# --- delta sui COMANDI Bash ripetuti -----------------------------------------
# `git status`, `ls`, la stessa suite verde: rilanciati N volte con output
# IDENTICO. Stessa meccanica del delta sulle riletture: hash di (comando,
# output) per sessione; alla replica identica passa un marker. Riesecuzione
# subito dopo un marker = page fault -> integrale.
CMD_DELTA_ENABLED = os.environ.get("CK_CMD_DELTA", "1") != "0"
CMD_DELTA_MIN = int(os.environ.get("CK_CMD_DELTA_MIN", "200"))
CMDS_STATE = os.path.expanduser(
    os.environ.get("CK_CMDS_STATE", "~/.context-kernel-cmds.json"))

# --- proiezione grep-aware ---------------------------------------------------
# L'output content-mode di Grep e' strutturato (file:riga:testo) ma per la
# regex di segnale i match sembrano tutti importanti. Qui: raggruppa per file,
# primi K match per file, il resto diventa un conteggio per file. Nessun
# LUOGO va perso: ogni file resta citato, il page fault e' rifare il grep
# sul singolo file.
GREP_PER_FILE = int(os.environ.get("CK_GREP_PER_FILE", "5"))

# --- outline-first per le Read di file Python GIGANTI ------------------------
# La scoperta "pavimento dei monoliti" applicata alla singola Read: sopra
# OUTLINE_MIN token il file non passa nemmeno troncato — passa l'OUTLINE
# (firme con line-range esatti); il corpo si legge per simbolo con
# offset/limit (page fault mirato).
OUTLINE_ENABLED = os.environ.get("CK_OUTLINE", "1") != "0"
OUTLINE_MIN = int(os.environ.get("CK_OUTLINE_MIN", "20000"))

# --- rate adattivo al contesto residuo ---------------------------------------
# Con tanto headroom si comprime poco (fedelta' massima); quando il contesto
# supera il 60% della finestra HEAD/TAIL/MIN_TOKENS si stringono fino a meta'
# (al 90%). La curva rate-distortion resa dinamica: il rate arriva quando la
# finestra e' davvero la risorsa scarsa.
ADAPTIVE_ENABLED = os.environ.get("CK_ADAPTIVE", "1") != "0"
# Scala di partenza del rate adattivo (1.0 = soglie piene). 0.75 = la
# compressione lavora forte dall'inizio della sessione, non solo oltre
# il 60% di occupazione.
ADAPTIVE_START = float(os.environ.get("CK_ADAPTIVE_START", "0.75") or 0.75)
CONTEXT_WINDOW = int(os.environ.get("CK_CONTEXT_WINDOW", "0") or 0)
try:
    from window import resolve_window as _resolve_window   # fonte UNICA
except ImportError:                        # installazione parziale: PARITA'
    def _resolve_window(model, used):      # type: ignore[misc]
        try:
            win = int(os.environ.get("CK_CONTEXT_WINDOW", "0") or 0)
        except ValueError:
            win = 0
        if win > 0:
            return win, "env"
        if "[1m]" in (model or "").lower():
            return 1_000_000, "pattern [1m]"
        return max(200_000, max(0, used) * 115 // 100 + 50_000), "stima"

# --- tassi APPRESI per-categoria (loop T5 -> T1) ------------------------------
# Scritti da `revealed.py --aggregate --apply-rates` (attuazione ESPLICITA
# dell'umano) sui page fault RICORRENTI (>=2): categoria = estensione del file
# letto. Direzione unica e fail-safe: "relax" scala HEAD/TAIL/soglia di un
# fattore >1 (si comprime meno), "raw" salta l'elisione; mai stringere.
RATES_ENABLED = os.environ.get("CK_RATES", "1") != "0"
RATES_STATE = os.path.expanduser(
    os.environ.get("CK_RATES_STATE", "~/.context-kernel-rates.json"))


def learned_rate(fpath: str | None) -> tuple[str, float]:
    """("keep"|"raw"|"relax", scala) per l'estensione di fpath. Mai fatale."""
    if not RATES_ENABLED or not fpath:
        return "keep", 1.0
    try:
        with open(RATES_STATE, encoding="utf-8") as f:
            cats = (json.load(f) or {}).get("categories") or {}
        rec = cats.get(os.path.splitext(fpath)[1].lower() or "(noext)")
        if not isinstance(rec, dict):
            return "keep", 1.0
        if rec.get("mode") == "raw":
            return "raw", 1.0
        if rec.get("mode") == "relax":
            return "relax", max(1.0, float(rec.get("scale") or 1.0))
    except Exception:                          # noqa: BLE001
        pass
    return "keep", 1.0

# --- proiezione JSON-aware per i tool MCP ------------------------------------
# Gli output dei tool MCP sono spesso JSON enormi e OMOGENEI: array di oggetti
# con le stesse chiavi (post, connessioni, risultati di ricerca). Il kernel
# sintattico applicato alla struttura: gli array lunghi diventano
# primi K campioni + marker con conteggio e schema delle chiavi. Nessun
# oggetto e' irrecuperabile: ripetere la stessa chiamata e' il page fault
# (stessa meccanica del delta comandi, che per gli MCP vale identica).
MCP_ENABLED = os.environ.get("CK_MCP", "1") != "0"
JSON_SAMPLE = int(os.environ.get("CK_JSON_SAMPLE", "3"))
JSON_MIN_ITEMS = int(os.environ.get("CK_JSON_MIN_ITEMS", "8"))

# --- A/B di answer-invariance (T4 campionato) --------------------------------
# Il canary prova che la sostituzione e' stata APPLICATA; non prova che la
# risposta sia rimasta nella sua classe. Qui: 1 elisione ogni CK_AB_RATE viene
# campionata (coppia originale+compresso, zlib) e giudicata OFFLINE da
# hooks/ab_verify.py via `claude -p` (abbonamento, zero chiavi API).
# Campionamento a contatore, deterministico: niente random negli hook.
AB_RATE = int(os.environ.get("CK_AB_RATE", "20"))      # 0 = disattivo
AB_STATE = os.path.expanduser(
    os.environ.get("CK_AB_STATE", "~/.context-kernel-ab.json")
)
AB_MAX_PENDING = 12                    # campioni in attesa: oltre, drop dei vecchi
AB_MAX_RAW = 65536                     # originali oltre: giudizio troppo costoso


def _reads_load() -> dict:
    try:
        with open(READS_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                          # noqa: BLE001
        return {}


def _reads_save(st: dict) -> None:
    try:
        for sess in list(st):                  # cap: 8 sessioni, 60 file l'una
            files = st[sess]
            if len(files) > 60:
                for k in sorted(files, key=lambda k: files[k].get("ts", 0))[:-60]:
                    files.pop(k, None)
        for k in sorted(st, key=lambda s: max((v.get("ts", 0)
                        for v in st[s].values()), default=0))[:-8]:
            st.pop(k, None)
        _atomic_dump(st, READS_STATE)
    except Exception:                          # noqa: BLE001
        pass


def _pack_content(text: str) -> str:
    """Contenuto per il diff delta, compresso (zlib+base64): i raw da 32KB
    l'uno gonfiavano reads.json di ~5x."""
    return base64.b64encode(
        zlib.compress(text.encode("utf-8", "replace"), 6)).decode("ascii")


def _unpack_content(rec: dict) -> str:
    z = rec.get("z")
    if z:
        try:
            return zlib.decompress(
                base64.b64decode(z)).decode("utf-8", "replace")
        except Exception:                      # noqa: BLE001
            return ""
    return rec.get("content") or ""            # record legacy non compresso


# Sentinella: l'output originale deve passare INTATTO (niente delta, niente
# compressione). E' il page fault dopo un'elisione: il contesto non ha mai
# ricevuto la copia piena, quindi qualunque rilettura la vuole davvero.
INTEGRAL = "__ck_integral__"


def delta_read(payload: dict, text: str) -> str | None:
    """None = procedi col percorso normale (e registra); INTEGRAL = passa
    l'originale intatto (page fault post-elisione); altra stringa = rimpiazzo
    (marker invariato / diff). Solo Read integrali (senza offset/limit)."""
    import hashlib
    tin = payload.get("tool_input") or {}
    fpath = tin.get("file_path")
    if not fpath or tin.get("offset") or tin.get("limit"):
        return None
    sess = session_id(payload.get("transcript_path"))
    with _locked(READS_STATE):
        st = _reads_load()
        files = st.setdefault(sess, {})
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
        rec = files.get(fpath)
        now = time.time()

        def remember(suppressed: bool) -> None:
            files[fpath] = {"hash": h, "ts": now, "suppressed": suppressed,
                            "z": _pack_content(text)
                            if len(text) <= DELTA_STORE_MAX else ""}
            _reads_save(st)

        if rec is not None and rec.get("elided"):
            # l'ultima copia entrata nel contesto era ELISA (troncatura): il
            # modello non ha mai avuto il file intero, il marker "copia valida"
            # mentirebbe. Rilettura = page fault -> integrale, cambiato o no.
            # E' la distorsione: il risparmio dichiarato su questo file rientra.
            log_fault("reread", os.path.splitext(fpath)[1].lower() or "(noext)",
                      rec.get("saved") or est_tokens(text), sess)
            remember(False)
            return INTEGRAL

        if rec is None:                        # prima lettura: registra e basta
            remember(False)
            return None
        if rec.get("hash") == h:
            if rec.get("suppressed"):          # ha riletto DOPO un marker:
                remember(False)                # vuole il contenuto -> integrale
                return None
            if est_tokens(text) < DELTA_MIN_TOKENS:
                remember(False)
                return None
            remember(True)
            return (f"[context-kernel: file UNCHANGED since the last read in "
                    f"this session (hash {h}) — the copy you already have in "
                    f"context is valid. If you need the content anyway, read "
                    f"this same file again: the next Read passes through in "
                    f"full]")
        old = _unpack_content(rec)
        if not old:                            # file grande: niente diff
            remember(False)
            return None
        import difflib
        diff = "\n".join(difflib.unified_diff(
            old.split("\n"), text.split("\n"),
            fromfile="previous read", tofile="now", lineterm="", n=2))
        remember(False)
        if not diff or est_tokens(diff) >= est_tokens(text) * 0.6:
            return None                        # diff non conviene: integrale
        return (f"[context-kernel: file CHANGED since the last read (this "
                f"session). Diff against the copy you already have in context:]\n"
                f"{diff}")


def mark_read_elided(payload: dict, text: str, saved: int = 0) -> bool:
    """Una Read integrale e' stata compressa con ELISIONE: il contesto non
    ha la copia piena. Segna il record cosi' delta_read tratti la prossima
    Read dello stesso file come page fault (passa integrale). `saved` (i token
    tolti da QUESTA elisione) resta col record cosi' un fault futuro conosce il
    costo esatto del recupero da attribuire, non una stima."""
    import hashlib
    tin = payload.get("tool_input") or {}
    fpath = tin.get("file_path")
    if not fpath or tin.get("offset") or tin.get("limit"):
        return False
    try:
        sess = session_id(payload.get("transcript_path"))
        with _locked(READS_STATE):
            st = _reads_load()
            files = st.setdefault(sess, {})
            h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
            files[fpath] = {"hash": h, "ts": time.time(), "suppressed": False,
                            "elided": True, "saved": int(saved),
                            "z": _pack_content(text)
                            if len(text) <= DELTA_STORE_MAX else ""}
            _reads_save(st)
        return True
    except Exception:                          # noqa: BLE001
        return False


def _cmds_load() -> dict:
    try:
        with open(CMDS_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                          # noqa: BLE001
        return {}


def _cmds_save(st: dict) -> None:
    try:
        for sess in list(st):                  # cap: 8 sessioni, 40 comandi
            cmds = st[sess]
            if len(cmds) > 40:
                for k in sorted(cmds, key=lambda k: cmds[k].get("ts", 0))[:-40]:
                    cmds.pop(k, None)
        for k in sorted(st, key=lambda s: max((v.get("ts", 0)
                        for v in st[s].values()), default=0))[:-8]:
            st.pop(k, None)
        _atomic_dump(st, CMDS_STATE)
    except Exception:                          # noqa: BLE001
        pass


def _invocation_key(payload: dict) -> str | None:
    """Chiave dell'invocazione per il delta: per Bash il comando, per i tool
    MCP nome + input serializzato (stessa chiamata = stessa chiave)."""
    tin = payload.get("tool_input") or {}
    tool = str(payload.get("tool_name") or "")
    if tool == "Bash":
        return str(tin.get("command") or "") or None
    if tool.startswith("mcp__"):
        try:
            return tool + "\x00" + json.dumps(tin, sort_keys=True,
                                              ensure_ascii=False)
        except Exception:                      # noqa: BLE001
            return None
    return None


def cmd_delta(payload: dict, text: str) -> str | None:
    """Delta sulle invocazioni ripetute (Bash e tool MCP): stessa chiamata +
    stesso output nella stessa sessione -> marker. None = percorso normale;
    INTEGRAL = passa intatto (riesecuzione dopo un'elisione: il contesto non
    ha mai avuto l'integrale)."""
    import hashlib
    cmd = _invocation_key(payload)
    if not cmd:
        return None
    sess = session_id(payload.get("transcript_path"))
    ck = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:12]
    h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    with _locked(CMDS_STATE):
        st = _cmds_load()
        cmds = st.setdefault(sess, {})
        rec = cmds.get(ck)
        now = time.time()

        def remember(suppressed: bool, elided: bool = False) -> None:
            cmds[ck] = {"out": h, "ts": now,
                        "suppressed": suppressed, "elided": elided}
            _cmds_save(st)

        if rec is None:                        # prima esecuzione: registra
            remember(False)
            return None
        if rec.get("out") == h:
            if rec.get("elided"):
                # l'ultima copia in contesto era ELISA: rieseguire lo stesso
                # comando e' il page fault -> integrale (distorsione misurata)
                log_fault("recmd", str(payload.get("tool_name") or "?"),
                          rec.get("saved") or est_tokens(text), sess)
                remember(False)
                return INTEGRAL
            if rec.get("suppressed"):          # rieseguito dopo il marker:
                remember(False)                # vuole l'output -> integrale
                return None
            if est_tokens(text) < CMD_DELTA_MIN:
                remember(False)
                return None
            remember(True)
            what = ("of this same command"
                    if payload.get("tool_name") == "Bash"
                    else "of this same MCP call")
            return (f"[context-kernel: output IDENTICAL to the last run "
                    f"{what} in this session (hash {h}, "
                    f"~{est_tokens(text)} tokens) — the copy you already have "
                    f"in context is valid. If you need it anyway, re-run it: "
                    f"the next one passes through in full]")
        remember(False)                        # output cambiato: registra
        return None


def mark_cmd_elided(payload: dict, text: str, saved: int = 0) -> None:
    """L'output di questa invocazione (Bash o MCP) e' stato consegnato ELISO:
    se la stessa chiamata ridara' lo stesso output, la replica passa
    integrale. `saved` viaggia col record per attribuire il costo esatto a un
    fault futuro (come mark_read_elided)."""
    import hashlib
    cmd = _invocation_key(payload)
    if not cmd:
        return
    try:
        sess = session_id(payload.get("transcript_path"))
        ck = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:12]
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
        with _locked(CMDS_STATE):
            st = _cmds_load()
            st.setdefault(sess, {})[ck] = {
                "out": h, "ts": time.time(),
                "suppressed": False, "elided": True, "saved": int(saved)}
            _cmds_save(st)
    except Exception:                          # noqa: BLE001
        pass


GREP_LINE = re.compile(r"^([^\s:][^:\n]*):(\d+)[:-]")


def grep_project(text: str) -> str | None:
    """Proiezione dell'output content-mode di Grep: raggruppa per file
    (ordine di prima apparizione), primi GREP_PER_FILE match per file, il
    resto diventa `[+N altri match in FILE]`. None se non e' content-mode
    o non c'e' nulla da elidere."""
    lines = normalize(text).split("\n")
    nonempty = [l for l in lines if l.strip()]
    if len(nonempty) < 40:
        return None
    matched = sum(1 for l in nonempty if GREP_LINE.match(l))
    if matched < len(nonempty) * 0.6:
        return None                            # non e' un grep content-mode
    order: list[str] = []
    per: dict[str, list[str]] = {}
    for l in lines:
        m = GREP_LINE.match(l)
        if not m:
            continue
        f = m.group(1)
        if f not in per:
            per[f] = []
            order.append(f)
        per[f].append(l)
    out: list[str] = []
    hidden = 0
    hidden_tok = 0
    for f in order:
        ls = per[f]
        out.extend(ls[:GREP_PER_FILE])
        extra = len(ls) - GREP_PER_FILE
        if extra > 0:
            hidden += extra
            hidden_tok += est_tokens("\n".join(ls[GREP_PER_FILE:]))
            out.append(f"  [+{extra} more matches in {f}]")
    if hidden == 0:
        return None
    total = sum(len(v) for v in per.values())
    marker = (f"{ELISION_MARK} {hidden} matches beyond the {GREP_PER_FILE}th "
              f"per file (~{hidden_tok} tokens): {total} matches in "
              f"{len(order)} files, kept the first {GREP_PER_FILE} per file — "
              f"no file was dropped; for the rest, repeat the grep on the "
              f"single file]")
    return "\n".join([marker] + out)


def json_project(text: str) -> str | None:
    """Proiezione dei payload JSON (tool MCP): ogni array di >=JSON_MIN_ITEMS
    elementi in cui quasi tutti sono OGGETTI viene proiettato a primi
    JSON_SAMPLE campioni + marker con conteggio e schema delle chiavi (lo
    schema e' il kernel sintattico della struttura: identico per tutti gli
    elementi, pagarlo N volte e' ridondanza). None se il testo non e' JSON
    o non c'e' nulla da elidere."""
    s = text.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        data = json.loads(s)
    except Exception:                          # noqa: BLE001
        return None
    hidden = {"n": 0}

    def walk(node):
        if isinstance(node, list):
            items = [walk(x) for x in node]
            if len(items) >= JSON_MIN_ITEMS:
                dicts = [x for x in items if isinstance(x, dict)]
                if len(dicts) >= len(items) * 0.8:
                    keys = sorted({k for d in dicts[:20] for k in d})
                    dropped = items[JSON_SAMPLE:]
                    try:
                        tok = est_tokens(json.dumps(dropped, ensure_ascii=False))
                    except Exception:          # noqa: BLE001
                        tok = 0
                    hidden["n"] += len(dropped)
                    shown = ", ".join(keys[:12]) + (", …" if len(keys) > 12 else "")
                    return items[:JSON_SAMPLE] + [
                        f"{JSON_MARK} {len(dropped)} of {len(items)} objects "
                        f"(~{tok} tokens) with keys {{{shown}}}; for the full "
                        f"payload repeat the same call: the next one passes "
                        f"through in full]"
                    ]
            return items
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    projected = walk(data)
    if hidden["n"] == 0:
        return None
    return json.dumps(projected, ensure_ascii=False, indent=1)


def py_outline(text: str) -> str | None:
    """Outline di un sorgente Python: import + firme di funzioni/classi/
    metodi con line-range esatti. E' il T2b applicato alla Read singola:
    il corpo si recupera per simbolo con offset/limit."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return None
    src = text.split("\n")

    def sig(node, indent: str = "") -> str:
        line = src[node.lineno - 1].strip() if node.lineno - 1 < len(src) else "?"
        return f"{indent}{line}  # lines {node.lineno}-{node.end_lineno}"

    imports = [src[n.lineno - 1] for n in tree.body
               if isinstance(n, (ast.Import, ast.ImportFrom))
               and n.lineno - 1 < len(src)]
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(sig(node))
        elif isinstance(node, ast.ClassDef):
            symbols.append(sig(node))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(sig(sub, "    "))
    if not symbols:
        return None
    header = (f"{ELISION_MARK} file body (~{est_tokens(text)} tokens, "
              f"{len(src)} lines): LARGE file projected to an OUTLINE — "
              f"{len(symbols)} symbols with line ranges; read the symbol you "
              f"need with Read offset/limit]")
    return "\n".join([header] + imports[:40] + [""] + symbols)


LINK_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?\[[^\]]*\]\(https?://|^\s*https?://\S+\s*$|"
    r"^\s*[-*•]\s*https?://")


def prose_project(text: str) -> str | None:
    """Proiezione per la prosa web (WebFetch): i run di righe-link (nav,
    footer, elenchi di URL) collassano a 2 righe + conteggio. Il testo
    vero resta intatto; il generico signal-preserving fa il resto."""
    lines = normalize(text).split("\n")
    out: list[str] = []
    hidden = 0
    i = 0
    while i < len(lines):
        if LINK_LINE.search(lines[i]):
            j = i
            while j < len(lines) and (LINK_LINE.search(lines[j])
                                      or not lines[j].strip()):
                j += 1
            run = [l for l in lines[i:j] if l.strip()]
            if len(run) >= 6:
                tok = est_tokens("\n".join(run[2:]))
                out.extend(run[:2])
                out.append(f"{ELISION_MARK} {len(run) - 2} lines of "
                           f"links/navigation (~{tok} tokens)]")
                hidden += len(run) - 2
                i = j
                continue
        out.append(lines[i])
        i += 1
    if hidden == 0:
        return None
    return "\n".join(out)


def _adaptive_scale(payload: dict) -> float:
    """Compressione FORTE dal primo token: parte a CK_ADAPTIVE_START
    (default 0.75) e stringe linearmente fino a 0.5 tra il 60% e il 90%
    di occupazione (dal tracker di update_context_state). La versione
    precedente partiva a 1.0 con headroom — osservato dal vivo come
    "la sessione si mangia i token all'inizio e rallenta solo dopo":
    il rischio di distorsione e' costante lungo la sessione, quindi
    anche il rate deve esserlo. CK_ADAPTIVE=0 disattiva del tutto la
    scala (comportamento pieno, soglie non ridotte)."""
    if not ADAPTIVE_ENABLED:
        return 1.0
    start = max(0.5, min(1.0, ADAPTIVE_START))
    try:
        with open(CONTEXT_STATE, encoding="utf-8") as f:
            rec = (json.load(f) or {}).get(
                session_id(payload.get("transcript_path"))) or {}
        used = int(rec.get("context_tokens") or 0)
        if used <= 0:
            return start
        # fonte UNICA della finestra (window.py): prima qui c'era un 200k
        # piatto — sui modelli a finestra grande la rampa stringeva troppo
        # presto rispetto all'occupazione REALE
        window, _src = _resolve_window(rec.get("model"), used)
        ratio = used / window
        if ratio <= 0.6:
            return start
        return max(0.5, start - (ratio - 0.6) * ((start - 0.5) / 0.3))
    except Exception:                          # noqa: BLE001
        return start


def _ab_load() -> dict:
    try:
        with open(AB_STATE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("counter", 0)
            st.setdefault("pending", [])
            st.setdefault("ok", 0)
            st.setdefault("degraded", 0)
            st.setdefault("last_run", None)
            return st
    except Exception:                          # noqa: BLE001
        pass
    return {"counter": 0, "pending": [], "ok": 0, "degraded": 0,
            "last_run": None}


def ab_sample(payload: dict, original: str, compressed: str) -> None:
    """Campiona la coppia (originale, compresso) di un'elisione per il
    giudizio offline di answer-invariance. Mai fatale."""
    if AB_RATE <= 0 or len(original) > AB_MAX_RAW:
        return
    try:
        with _locked(AB_STATE):
            st = _ab_load()
            st["counter"] += 1
            if st["counter"] % AB_RATE == 0:
                tin = payload.get("tool_input") or {}
                st["pending"] = (st["pending"] + [{
                    "ts": time.time(),
                    "tool": payload.get("tool_name", "?"),
                    "file": tin.get("file_path")
                    if isinstance(tin, dict) else None,
                    "session": session_id(payload.get("transcript_path")),
                    "attempts": 0,
                    "orig_z": _pack_content(original),
                    "comp_z": _pack_content(compressed),
                }])[-AB_MAX_PENDING:]
            _atomic_dump(st, AB_STATE)
    except Exception:                          # noqa: BLE001
        pass


def update_context_state(payload: dict) -> None:
    """Aggiorna ~/.context-kernel-context.json: {sessione: {model,
    context_tokens, ts}}. Throttle 20s; mai fatale."""
    tp = payload.get("transcript_path")
    if not tp:
        return
    try:
        if (os.path.exists(CONTEXT_STATE)
                and time.time() - os.path.getmtime(CONTEXT_STATE) < 20):
            return
        size = os.path.getsize(tp)
        last = None
        with open(tp, encoding="utf-8", errors="replace") as f:
            if size > 400_000:
                f.seek(size - 400_000)
                f.readline()
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:              # noqa: BLE001
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if isinstance(u, dict) and "input_tokens" in u:
                    last = (msg.get("model"), u)
        if not last:
            return
        model, u = last
        used = ((u.get("input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0))
        try:
            with open(CONTEXT_STATE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:                      # noqa: BLE001
            st = {}
        st[session_id(tp)] = {"model": model, "context_tokens": used,
                              "ts": time.time()}
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni le ultime 8 sessioni
        _atomic_dump(st, CONTEXT_STATE)
    except Exception:                          # noqa: BLE001
        pass


def tap_payload(payload: dict) -> None:
    """Diagnostica on-demand: `touch ~/.context-kernel-tap` e ogni invocazione
    appende le CHIAVI del payload (mai i contenuti) al flag file. Serve a
    ispezionare il contratto reale dell'harness (es. come distinguere i
    subagent). Costo a riposo: una stat."""
    try:
        if not os.path.exists(TAP_FLAG):
            return
        rec = {"tool": payload.get("tool_name"),
               "keys": sorted(payload.keys()),
               "session": session_id(payload.get("transcript_path"))}
        for k in ("cwd", "session_id", "agent_id", "agent_name",
                  "parent_tool_use_id", "permission_mode", "hook_event_name"):
            if k in payload:
                rec[k] = payload[k] if isinstance(payload[k], (str, int, bool)) \
                    else type(payload[k]).__name__
        with open(TAP_FLAG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:                          # noqa: BLE001
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0
    tap_payload(payload)
    update_context_state(payload)

    # il canary gira a OGNI invocazione, anche quando poi non si comprime:
    # e' il momento in cui il tool_result precedente e' gia' nel transcript.
    alert = canary_check(payload)

    def noop() -> int:
        if alert:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": alert,
            }}))
        else:
            print("{}")
        return 0

    # auto-degrade: se questa sessione ha gia' superato la soglia di violazioni
    # del canary, non comprimere piu' nulla (raw pass-through). Va prima di ogni
    # ramo di compressione, incluso il delta/parcheggio: l'harness scarta
    # updatedToolOutput, quindi ogni lavoro qui sarebbe sprecato e i marker
    # solo rumore. L'alert del giro in cui si degrada viaggia comunque.
    if canary_degraded(payload):
        if canary_probe_due(payload):
            # sonda di un-degrade: QUESTO output ripassa dal flusso normale;
            # se comprime, il pending marcato probe portera' l'evidenza che
            # decide il ripristino (canary_check al giro dopo)
            payload["_ck_probe"] = True
            print("context-kernel: canary probe in a degraded session "
                  f"(1 every {CANARY_PROBE_K} compressible outputs)",
                  file=sys.stderr)
        else:
            if not alert:
                print("context-kernel: auto-degrade active (canary) -> raw "
                      "pass-through for this session", file=sys.stderr)
            return noop()

    tool_name = payload.get("tool_name")
    # i tool MCP (mcp__server__tool) passano tutti: il nome esatto non e'
    # prevedibile, il matcher dell'hook fa gia' il primo filtro
    is_mcp = (MCP_ENABLED and isinstance(tool_name, str)
              and tool_name.startswith("mcp__"))
    if tool_name not in MATCHERS and not is_mcp:
        return noop()

    if (payload.get("tool_name") == "Read"
            and str(payload.get("agent_type", "")) in AGENT_SKIP_READ):
        return noop()                          # lettura di un agent giudice

    tin = payload.get("tool_input")
    if (payload.get("tool_name") == "Read" and isinstance(tin, dict)
            and (tin.get("offset") or tin.get("limit"))):
        # finestra chiesta ESPLICITAMENTE: il modello ha gia' detto quali
        # righe vuole, comprimerle vanifica la lettura mirata
        return noop()

    if (RAW_MARK and payload.get("tool_name") == "Bash" and isinstance(tin, dict)
            and RAW_MARK in str(tin.get("command") or "")):
        # escape per-comando: chi ha scritto il comando ha chiesto
        # esplicitamente l'output intatto
        return noop()

    text, fpath = extract_output(payload)
    # guardia anti doppia-esecuzione: se l'output porta gia' il footer in coda
    # (plugin + install.sh attivi insieme: gli hook si SOMMANO), non ricomprimere
    if text.rstrip().split("\n")[-1:] and FOOTER_MARK in text.rstrip().split("\n")[-1]:
        return noop()
    if not text.strip():
        resp = payload.get("tool_response", payload.get("tool_output"))
        if resp:                               # output presente ma non estratto
            log_unknown_shape(payload.get("tool_name", "?"), resp)
        return noop()
    before = est_tokens(text)

    if (payload.get("tool_name") == "Bash" and isinstance(tin, dict)
            and EXTRACTION_RE.search(str(tin.get("command") or ""))
            and before <= EXTRACTION_MAX):
        # estrazione esplicita (sed -n / awk NR / recall.py): l'output E' il
        # payload chiesto riga per riga — passa intatto (vedi EXTRACTION_RE)
        return noop()

    replacement = None
    if DELTA_ENABLED and payload.get("tool_name") == "Read":
        try:
            replacement = delta_read(payload, text)
        except Exception:                      # noqa: BLE001
            replacement = None
    if (CMD_DELTA_ENABLED and replacement is None
            and (payload.get("tool_name") == "Bash" or is_mcp)):
        try:
            replacement = cmd_delta(payload, text)
        except Exception:                      # noqa: BLE001
            replacement = None

    if replacement == INTEGRAL:                # page fault post-elisione
        return noop()
    if replacement is not None:
        compressed = replacement
        after = est_tokens(compressed)
        if after >= before:                    # mai peggiorare
            return noop()
    else:
        scale = _adaptive_scale(payload)
        if (PARK_ENABLED and EPHEMERAL_SCALE < 1.0
                and (payload.get("tool_name") in ("Bash", "WebFetch") or is_mcp)):
            # dividendo del parcheggio: soglia d'ingresso e head/tail piu'
            # aggressivi SOLO sull'insieme dei tool le cui elisioni vengono
            # parcheggiate (il recupero mirato e' garantito). Floor 0.25:
            # oltre, l'output degenera a solo marker anche su output medi.
            scale *= max(0.25, EPHEMERAL_SCALE)
        mode, lscale = learned_rate(
            fpath if payload.get("tool_name") == "Read" else None)
        if mode == "raw":                      # fault ricorrenti misurati:
            print("context-kernel: learned rate (T5) -> raw for "
                  f"{os.path.splitext(fpath or '')[1]}", file=sys.stderr)
            return noop()                      # l'elisione qui costava piu'
        scale *= lscale                        # di quanto risparmiava
        if before < max(200, int(MIN_TOKENS * scale)):
            return noop()
        tool = payload.get("tool_name")
        compressed = None
        if tool == "Grep":
            compressed = grep_project(text)
        if compressed is None and is_mcp:
            compressed = json_project(text)
        if compressed is None:
            src = text
            if tool == "WebFetch":
                src = prose_project(text) or text
            compressed = compress(
                src, fpath if tool == "Read" else None, scale=scale)
            if (OUTLINE_ENABLED and tool == "Read" and fpath
                    and fpath.lower().endswith((".py", ".pyi"))
                    and before >= OUTLINE_MIN):
                # sui file GIGANTI l'outline vince anche se qualche token piu'
                # grande del troncamento code-aware: i line-range rendono il
                # page fault per-simbolo, 45 righe di corpo arbitrarie no
                outl = py_outline(text)
                if outl is not None and est_tokens(outl) < before // 2:
                    compressed = outl
        after = est_tokens(compressed)
        if after >= before:                    # nessun guadagno: no-op
            return noop()

    saved = 1 - after / before
    hint = ""
    if (DELTA_ENABLED and replacement is None
            and payload.get("tool_name") == "Read"
            and ELISION_MARK in compressed
            and mark_read_elided(payload, text, before - after)):
        hint = (" [ELIDED copy: for the full text read this same file again — "
                "or just the elided range, with offset/limit from the marker]")
    if (CMD_DELTA_ENABLED and replacement is None
            and (payload.get("tool_name") == "Bash" or is_mcp)
            and has_elision(compressed)):
        # replica identica della stessa invocazione dopo un'elisione -> integrale
        mark_cmd_elided(payload, text, before - after)
    if (replacement is None and has_elision(compressed)
            and (payload.get("tool_name") in ("Bash", "WebFetch") or is_mcp)):
        # output EFFIMERO eliso: parcheggia l'originale e dichiara il
        # recupero mirato (il page fault dei file qui non esiste)
        pkey = park_output(payload, text, before - after)
        if pkey:
            rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "recall.py")
            hint += (f' [parked: python3 "{rp}" {pkey} '
                     f"--grep PATTERN | --lines A-B]")
    # Il marcatore del canary e' il footer NUDO (solo i numeri): l'hint puo'
    # contenere path tra virgolette (parcheggio) che nel JSONL del transcript
    # diventano \" — il match esatto sulla riga grezza fallirebbe: falso
    # allarme "harness ha ignorato updatedToolOutput" su compressioni sane.
    footer_core = f"[context-kernel: {before} -> {after} tokens, -{saved:.0%}]"
    footer = f"{footer_core}{hint}"
    compressed += f"\n\n{footer}"
    if replacement is None and has_elision(compressed):
        # solo le ELISIONI (il tipo rischioso di compressione) entrano nel
        # campione A/B; i delta sulle riletture sono formali (hash), non serve
        ab_sample(payload, text, compressed)
    log_savings(payload.get("tool_name", "?"), before, after,
                session_id(payload.get("transcript_path")),
                agent=(payload.get("agent_id") or "-")[:8])

    # L'output sostitutivo deve avere la STESSA forma dell'originale:
    # per Bash tool_response e' un dict {stdout, stderr, ...}, per Read e'
    # annidato {"type", "file": {"content", ...}} — una forma diversa viene
    # ignorata silenziosamente dall'harness. Rimpiazziamo il punto esatto
    # da cui il testo e' stato estratto.
    resp = payload.get("tool_response", payload.get("tool_output"))

    def _replace_blocks(blocks: list) -> list | None:
        """Sostituisce il testo nei content block MCP preservando la forma:
        il primo block testuale riceve il compresso (che gia' fonde tutti i
        testi), gli altri si svuotano. None se nessun block testuale."""
        out, replaced_ = [], False
        for b in blocks:
            if (isinstance(b, dict) and b.get("type") == "text"
                    and isinstance(b.get("text"), str) and b["text"].strip()):
                nb = dict(b)
                nb["text"] = compressed if not replaced_ else ""
                replaced_ = True
                out.append(nb)
            else:
                out.append(b)
        return out if replaced_ else None

    if isinstance(resp, list):                 # tool MCP: content block
        updated = _replace_blocks(resp)
        if updated is None:                    # forma sconosciuta: no-op sicuro
            return noop()
    elif isinstance(resp, dict):
        updated = dict(resp)
        replaced = False
        for k in ("stdout", "output", "content", "result", "text"):
            if isinstance(resp.get(k), str) and resp[k]:
                updated[k] = compressed
                replaced = True
                break
        if not replaced:
            f = resp.get("file")
            if isinstance(f, dict) and isinstance(f.get("content"), str) and f["content"]:
                nf = dict(f)
                nf["content"] = compressed
                # numLines/startLine/totalLines restano quelli della finestra
                # letta su disco: segnalano al modello la dimensione reale
                # pre-elisione (modello page-fault)
                updated["file"] = nf
                replaced = True
        if not replaced and isinstance(resp.get("content"), list):
            nb = _replace_blocks(resp["content"])
            if nb is not None:                 # tool MCP: {"content": [...]}
                updated["content"] = nb
                replaced = True
        if not replaced and isinstance(resp.get("stdout"), str):
            # testo estratto solo da stderr: senza questo fallback l'output
            # compresso andrebbe perso (stdout vuoto + stderr azzerato)
            updated["stdout"] = compressed
            replaced = True
        if not replaced:                       # forma sconosciuta: no-op sicuro
            return noop()
        if isinstance(resp.get("stderr"), str):
            updated["stderr"] = ""             # gia' fuso nel testo compresso
    else:
        updated = compressed

    hso = {
        "hookEventName": "PostToolUse",
        POSTOUT_FIELD: updated,
    }
    if alert:
        hso["additionalContext"] = alert
    print(json.dumps({"hookSpecificOutput": hso}))
    canary_record(payload, footer_core)
    # diagnostica visibile solo con `claude --debug`
    print(f"context-kernel: {payload.get('tool_name')} {before}->{after} tok "
          f"(-{saved:.0%})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
