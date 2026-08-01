"""Test di savings.py: parsing del CSV, riepilogo e statusline."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import _util


def _run(log_path: str):
    return _util.run_script(_util.SAVINGS, "", env={"CK_LOG": log_path})


def _run_statusline(log_path: str, stdin_obj, env: dict | None = None):
    stdin = stdin_obj if isinstance(stdin_obj, str) else json.dumps(stdin_obj)
    return _util.run_script(
        _util.SAVINGS, stdin, args=["--statusline"],
        env={"CK_LOG": log_path,
             "CK_CANARY_STATE": "/inesistente-canary",
             "CK_AB_STATE": "/inesistente-ab",
             "CK_CONTEXT_STATE": "/inesistente-ctx",
             "CK_STATUSLINE_COLOR": "0",
             **(env or {})})


class TestSavings(unittest.TestCase):

    def test_report_totals_and_per_tool(self):
        fd, log = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("2026-01-01T00:00:00,Bash,1000,100,900\n")
            fh.write("riga malformata da ignorare\n")
            fh.write("2026-01-01T00:01:00,Read,500,250,250\n")
        try:
            out = _run(log).stdout
        finally:
            os.unlink(log)

        self.assertIn("compressions:      2", out)   # la malformata non conta
        self.assertIn("1,500", out)                  # before totale
        self.assertIn("1,150", out)                  # risparmiati
        self.assertIn("Bash", out)
        self.assertIn("Read", out)

    # --- statusline -----------------------------------------------------
    STATUS_STDIN = {"session_id": "abcd1234-5678-9999",
                    "model": {"display_name": "Fable 5"},
                    "workspace": {"current_dir": "/tmp/mio-progetto"}}

    def _statusline_log(self) -> str:
        fd, log = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("2026-01-01T00:00:00,Bash,10000,1000,9000,abcd1234\n")
            fh.write("2026-01-01T00:01:00,Read,5000,2000,3000,abcd1234\n")
            fh.write("2026-01-01T00:02:00,Bash,9000,1000,8000,altrasess\n")
        return log

    def test_statusline_slim_by_default(self):
        """Default ASCIUTTO: risparmio sessione + quota totale, niente
        contatori diagnostici (ctx, A/B, fault) e niente parole in piu'."""
        log = self._statusline_log()
        try:
            proc = _run_statusline(log, self.STATUS_STDIN)
        finally:
            os.unlink(log)
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout
        self.assertIn("Fable 5", out)
        self.assertIn("mio-progetto", out)
        self.assertIn("-12.0k · tot -83%", out)     # 9000+3000 di abcd1234
        self.assertNotIn("sessione", out)
        self.assertNotIn("totale", out)
        self.assertNotIn("su ctx", out)
        self.assertEqual(len(proc.stdout.strip().split("\n")), 1)

    def test_statusline_session_and_total_verbose(self):
        log = self._statusline_log()
        try:
            proc = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_STATUSLINE_VERBOSE": "1"})
        finally:
            os.unlink(log)
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout
        self.assertIn("Fable 5", out)
        self.assertIn("mio-progetto", out)
        self.assertIn("-12.0k session", out)       # 9000+3000, solo abcd1234
        self.assertIn("-20.0k total", out)          # tutte le righe
        self.assertIn("total (-83%)", out)          # 20000 elisi su 24000 before
        self.assertNotIn("su ctx", out)              # senza tracker niente % sessione
        self.assertEqual(len(proc.stdout.strip().split("\n")), 1)

    def test_statusline_session_pct_from_context_tracker(self):
        """Col tracker del contesto la statusline rapporta il risparmio di
        sessione al contesto che ci SAREBBE stato (ctx attuale + risparmiato)."""
        log = self._statusline_log()
        fd, ctx = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                # ctx attuale 28k; risparmiati 12k -> sarebbe stato 40k, -30%
                json.dump({"abcd1234": {"model": "m", "context_tokens": 28000}}, f)
            proc = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_CONTEXT_STATE": ctx,
                                        "CK_STATUSLINE_VERBOSE": "1"})
        finally:
            for p in (log, ctx):
                os.unlink(p)
        self.assertIn("-12.0k session (-30% of ctx ~40.0k)", proc.stdout)
        self.assertEqual(len(proc.stdout.strip().split("\n")), 1)

    def test_statusline_pct_omitted_when_session_empty(self):
        """Sessione senza risparmi: niente percentuale, formato base intatto."""
        log = self._statusline_log()
        fd, ctx = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"ffffffff": {"context_tokens": 28000}}, f)
            stdin = dict(self.STATUS_STDIN, session_id="ffffffff-0000")
            proc = _run_statusline(log, stdin,
                                   env={"CK_CONTEXT_STATE": ctx,
                                        "CK_STATUSLINE_VERBOSE": "1"})
        finally:
            for p in (log, ctx):
                os.unlink(p)
        self.assertIn("-0 session · -20.0k total (-83%)", proc.stdout)

    def test_statusline_shows_pending_ab_and_canary_alarm(self):
        log = self._statusline_log()
        fd, ab = tempfile.mkstemp(suffix=".json")
        fd2, canary = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"pending": [{"ts": 1}, {"ts": 2}]}, f)
            with os.fdopen(fd2, "w") as f:
                json.dump({"failed": 1, "verified": 10}, f)
            slim = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_AB_STATE": ab,
                                        "CK_CANARY_STATE": canary})
            proc = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_AB_STATE": ab,
                                        "CK_CANARY_STATE": canary,
                                        "CK_STATUSLINE_VERBOSE": "1"})
            # slim: il canary (ALLARME) resta, il contatore A/B (diagnostica) no
            self.assertIn("⚠ canary", slim.stdout)
            self.assertNotIn("A/B", slim.stdout)
            self.assertIn("A/B: 2 pending", proc.stdout)
            self.assertIn("⚠ canary", proc.stdout)
        finally:
            for p in (log, ab, canary):
                os.unlink(p)

    def test_statusline_never_fatal(self):
        """Niente log, stdin non JSON: comunque exit 0 e UNA riga."""
        proc = _run_statusline("/inesistente.csv", "niente json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ck ⚡ -0 · tot -0", proc.stdout)

    def test_statusline_colors_on_and_off(self):
        """Coi colori attivi (default): risparmio in verde, allarmi rosso/
        giallo. CK_STATUSLINE_COLOR=0 -> nessun escape ANSI."""
        log = self._statusline_log()
        fd, canary = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"failed": 1}, f)
            on = _run_statusline(log, self.STATUS_STDIN,
                                 env={"CK_STATUSLINE_COLOR": "1",
                                      "CK_CANARY_STATE": canary})
            off = _run_statusline(log, self.STATUS_STDIN,
                                  env={"CK_CANARY_STATE": canary})
        finally:
            os.unlink(log)
            os.unlink(canary)
        self.assertIn("\033[33mck ⚡", on.stdout)   # marchio giallo (default)
        self.assertIn("\033[32m", on.stdout)       # verde sul risparmio
        self.assertIn("\033[31m⚠ canary", on.stdout)  # rosso sull'allarme
        self.assertNotIn("\033[", off.stdout)      # spento: testo puro
        self.assertIn("-12.0k", off.stdout)

    def test_statusline_brand_color_override(self):
        log = self._statusline_log()
        try:
            cyan = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_STATUSLINE_COLOR": "1",
                                        "CK_STATUSLINE_BRAND": "cyan"})
            none = _run_statusline(log, self.STATUS_STDIN,
                                   env={"CK_STATUSLINE_COLOR": "1",
                                        "CK_STATUSLINE_BRAND": "none"})
        finally:
            os.unlink(log)
        self.assertIn("\033[36mck ⚡", cyan.stdout)
        self.assertIn("ck ⚡ \033[32m", none.stdout)   # marchio non colorato

    # --- dashboard HTML -------------------------------------------------
    def test_html_report_contains_charts_and_totals(self):
        log = self._statusline_log()
        fd, out = tempfile.mkstemp(suffix=".html")
        os.close(fd)
        try:
            proc = _util.run_script(_util.SAVINGS, "", args=["--html", out],
                                    env={"CK_LOG": log,
                                         "CK_CANARY_STATE": "/inesistente",
                                         "CK_AB_STATE": "/inesistente"})
            self.assertEqual(proc.returncode, 0)
            self.assertIn(out, proc.stdout)        # stampa il percorso
            html = open(out, encoding="utf-8").read()
        finally:
            os.unlink(log)
            os.unlink(out)
        self.assertIn("<svg", html)
        self.assertIn("-20.0k", html)              # totale risparmiato
        self.assertIn("Bash", html)
        self.assertIn("abcd1234", html)            # per-sessione
        self.assertIn("Table", html)             # vista accessibile
        self.assertNotIn("NaN", html)

    def test_html_report_empty_log_never_fatal(self):
        fd, out = tempfile.mkstemp(suffix=".html")
        os.close(fd)
        try:
            proc = _util.run_script(_util.SAVINGS, "", args=["--html", out],
                                    env={"CK_LOG": "/inesistente.csv"})
            self.assertEqual(proc.returncode, 0)
            html = open(out, encoding="utf-8").read()
        finally:
            os.unlink(out)
        self.assertIn("0", html)                   # tiles a zero, niente crash

    def test_missing_log_is_friendly(self):
        out = _run("/percorso/che/non/esiste.csv").stdout
        self.assertIn("No log yet", out)

    def test_empty_log_is_friendly(self):
        fd, log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            out = _run(log).stdout
        finally:
            os.unlink(log)
        self.assertIn("Log present but empty", out)


if __name__ == "__main__":
    unittest.main()
