"""Test di charter.py (persistenza della carta T3) e charter_guard.py
(la carta come invariante attivo sugli Edit/Write)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import _util

CARTA = """# carta del task
Q: add esplode sui tipi misti

## vincoli
1. [contratto]    add(a, b) somma numeri; TypeError sui misti  (pkg/calc.py:1)
2. [comportamento] main() ritorna un int  (app.py:3)
3. [invariante]   il pool non e' mai None dopo setup  (pkg/calc.py:2)

## percorso del sintomo
TypeError nasce in pkg/calc.py, raggiunto da app.main
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-charter-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "pkg"))
        self.charter_state = os.path.join(self.tmp, "charter.json")
        self.guard_state = os.path.join(self.tmp, "guard.json")
        self.env = {"CK_CHARTER_STATE": self.charter_state,
                    "CK_GUARD_STATE": self.guard_state}
        _util.run_script(_util.CHARTER, CARTA, env=self.env,
                         args=["save", "--repo", self.repo])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _edit_payload(self, fpath: str, session: str = "s1") -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "session_id": session,
            "cwd": self.repo,
            "tool_input": {"file_path": fpath,
                           "old_string": "a", "new_string": "b"},
        }

    def _bash_payload(self, command: str, session: str = "s1") -> dict:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "session_id": session,
            "cwd": self.repo,
            "tool_input": {"command": command},
        }


class TestCharter(_Base):

    def test_save_indexes_citations(self):
        st = json.load(open(self.charter_state))
        rec = st[os.path.normpath(os.path.abspath(self.repo))]
        self.assertIn("pkg/calc.py", rec["files"])
        self.assertEqual(len(rec["files"]["pkg/calc.py"]), 2)
        self.assertIn("app.py", rec["files"])

    def test_get_returns_charter(self):
        proc = _util.run_script(_util.CHARTER, "", env=self.env,
                                args=["get", "--repo", self.repo])
        self.assertIn("carta del task", proc.stdout)
        self.assertIn("TypeError sui misti", proc.stdout)

    def test_clear_removes(self):
        _util.run_script(_util.CHARTER, "", env=self.env,
                         args=["clear", "--repo", self.repo])
        proc = _util.run_script(_util.CHARTER, "", env=self.env,
                                args=["get", "--repo", self.repo])
        self.assertNotIn("TypeError", proc.stdout)

    def test_vincolo_senza_citazione_non_indicizzato(self):
        carta = "# carta del task\n\n## vincoli\n1. [contratto] senza cita\n"
        repo2 = os.path.join(self.tmp, "repo2")
        os.makedirs(repo2)
        proc = _util.run_script(_util.CHARTER, carta, env=self.env,
                                args=["save", "--repo", repo2])
        self.assertIn("0 constraints indexed", proc.stdout)


class TestCharterGuard(_Base):

    def test_edit_of_cited_file_injects_constraints(self):
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath), env=self.env))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK CHARTER", ctx)
        self.assertIn("TypeError sui misti", ctx)
        self.assertIn("pool non e' mai None", ctx)     # entrambi i vincoli
        self.assertNotIn("ritorna un int", ctx)        # vincolo di ALTRO file

    def test_same_file_deduped_within_ttl(self):
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        _util.run_hook(_util.GUARD, self._edit_payload(fpath), env=self.env)
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath), env=self.env))
        self.assertEqual(out, {})

    def test_resaved_charter_speaks_again(self):
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        _util.run_hook(_util.GUARD, self._edit_payload(fpath), env=self.env)
        _util.run_script(_util.CHARTER, CARTA + "\n4. [x] nuovo  (pkg/calc.py:9)\n",
                         env=self.env, args=["save", "--repo", self.repo])
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath), env=self.env))
        self.assertIn("additionalContext", out.get("hookSpecificOutput", {}))

    def test_uncited_file_is_noop(self):
        fpath = os.path.join(self.repo, "altro.py")
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath), env=self.env))
        self.assertEqual(out, {})

    def test_write_tool_also_guarded(self):
        payload = self._edit_payload(os.path.join(self.repo, "app.py"))
        payload["tool_name"] = "Write"
        payload["tool_input"] = {"file_path": payload["tool_input"]["file_path"],
                                 "content": "x"}
        out = _util.hook_json(_util.run_hook(_util.GUARD, payload, env=self.env))
        self.assertIn("ritorna un int",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_no_charter_is_noop(self):
        env = {"CK_CHARTER_STATE": os.path.join(self.tmp, "vuoto.json"),
               "CK_GUARD_STATE": self.guard_state}
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath), env=env))
        self.assertEqual(out, {})

    def test_disabled_via_env(self):
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        out = _util.hook_json(_util.run_hook(
            _util.GUARD, self._edit_payload(fpath),
            env={**self.env, "CK_GUARD": "0"}))
        self.assertEqual(out, {})

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_hook(_util.GUARD, "niente json", env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})


class TestCharterGuardBash(_Base):
    """La scappatoia chiusa: un file citato si modifica anche da shell.
    La guardia scatta solo su pattern di SCRITTURA noti + file citato."""

    def _run(self, command: str, session: str = "s1", env: dict | None = None):
        return _util.hook_json(_util.run_hook(
            _util.GUARD, self._bash_payload(command, session),
            env={**self.env, **(env or {})}))

    def test_sed_i_on_cited_file_injects(self):
        out = self._run("sed -i '' 's/a/b/' pkg/calc.py")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Bash command", ctx)
        self.assertIn("TypeError sui misti", ctx)
        self.assertNotIn("ritorna un int", ctx)        # vincolo di ALTRO file

    def test_redirect_on_cited_file_injects(self):
        out = self._run("echo 'X = 1' > app.py")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ritorna un int", ctx)

    def test_git_checkout_on_cited_file_injects(self):
        out = self._run("git checkout -- pkg/calc.py")
        self.assertIn("TASK CHARTER",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_readonly_command_is_noop(self):
        self.assertEqual(self._run("grep -n TypeError pkg/calc.py"), {})
        self.assertEqual(self._run("cat pkg/calc.py"), {})

    def test_write_command_on_uncited_file_is_noop(self):
        self.assertEqual(self._run("sed -i '' 's/a/b/' pkg/altro.py"), {})

    def test_noise_redirect_devnull_is_noop(self):
        """Falso positivo osservato dal vivo: grep read-only con 2>/dev/null
        che nomina un file citato NON deve far scattare la guardia."""
        self.assertEqual(
            self._run("grep -rn TypeError pkg/calc.py 2>/dev/null"), {})
        self.assertEqual(
            self._run("ls pkg/calc.py > /dev/null 2>&1"), {})
        self.assertEqual(
            self._run("type pkg/calc.py > NUL"), {})       # Windows

    def test_arrow_tokens_are_not_redirects(self):
        """Terza classe osservata dal vivo: '->' (e '=>') dentro codice
        passato via heredoc non e' un redirect. Il comando nomina un file
        citato ma non scrive nulla."""
        self.assertEqual(
            self._run('python3 - <<EOF\nprint("a -> b", "pkg/calc.py")\nEOF'),
            {})
        self.assertEqual(
            self._run('grep -n "x => y" pkg/calc.py'), {})

    def test_real_redirect_still_fires_despite_devnull(self):
        """Un redirect VERO sul file citato scatta anche se il comando
        contiene pure un 2>/dev/null di contorno."""
        out = self._run("echo 'X = 1' > app.py 2>/dev/null")
        self.assertIn("ritorna un int",
                      out["hookSpecificOutput"]["additionalContext"])

    def test_deduped_with_editor_guard_same_file(self):
        """sed dopo un Edit sullo stesso file citato: stesso dedup TTL."""
        fpath = os.path.join(self.repo, "pkg", "calc.py")
        _util.run_hook(_util.GUARD, self._edit_payload(fpath), env=self.env)
        out = self._run("sed -i '' 's/a/b/' pkg/calc.py")
        # file gia' segnalato via Edit; il path chiave della guardia Bash e'
        # il path CITATO (relativo), quindi il dedup e' per-forma: la seconda
        # invocazione bash identica tace di sicuro
        self._run("sed -i '' 's/a/b/' pkg/calc.py")
        self.assertEqual(self._run("sed -i '' 's/x/y/' pkg/calc.py"), {})
        self.assertIn("hookSpecificOutput", out)       # la prima parla

    def test_bash_guard_disabled_via_env(self):
        out = self._run("sed -i '' 's/a/b/' pkg/calc.py",
                        env={"CK_GUARD_BASH": "0"})
        self.assertEqual(out, {})


class TestRefresh(unittest.TestCase):
    """charter.py refresh (1.18.0): le citazioni slittano col crescere del
    codice; l'ancora catturata al save le ri-risolve — match unico aggiorna,
    zero/ambiguo DICHIARA, mai indovina (stessa regola dell'FQCN)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-refresh-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "pkg"))
        self.calc = os.path.join(self.repo, "pkg", "calc.py")
        with open(self.calc, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\nPOOL = object()\n")
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            f.write("import pkg.calc\n# collegamento\ndef main():\n")
        self.state = os.path.join(self.tmp, "charter.json")
        self.env = {"CK_CHARTER_STATE": self.state}
        _util.run_script(_util.CHARTER, CARTA, env=self.env,
                         args=["save", "--repo", self.repo])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _refresh(self) -> str:
        return _util.run_script(_util.CHARTER, "", env=self.env,
                                args=["refresh", "--repo", self.repo]).stdout

    def _entries(self, path: str) -> list[dict]:
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        return st[os.path.realpath(self.repo)
                  if os.path.realpath(self.repo) in st
                  else list(st)[0]]["files"][path]

    def test_anchors_captured_at_save(self):
        anchors = {e["line"]: e.get("anchor")
                   for e in self._entries("pkg/calc.py")}
        self.assertEqual(anchors, {1: "def add(a, b):", 2: "POOL = object()"})

    def test_unchanged_citations_report_ok(self):
        out = self._refresh()
        self.assertEqual(out.count("OK "), 3, out)
        self.assertNotIn("RE-ANCHORED", out)

    def test_shifted_file_is_reanchored_in_state_and_text(self):
        with open(self.calc, "w", encoding="utf-8") as f:
            f.write("# uno\n# due\n# tre\ndef add(a, b):\nPOOL = object()\n")
        out = self._refresh()
        self.assertIn("RE-ANCHORED    pkg/calc.py:1 -> :4", out)
        self.assertIn("RE-ANCHORED    pkg/calc.py:2 -> :5", out)
        self.assertEqual({e["line"] for e in self._entries("pkg/calc.py")},
                         {4, 5})
        got = _util.run_script(_util.CHARTER, "", env=self.env,
                               args=["get", "--repo", self.repo]).stdout
        self.assertIn("(pkg/calc.py:4)", got)      # testo aggiornato
        self.assertNotIn("(pkg/calc.py:1)", got)
        for e in self._entries("pkg/calc.py"):     # anche la proposizione
            self.assertNotIn(":1)", e["vincolo"])

    def test_ambiguous_anchor_is_declared_never_guessed(self):
        with open(os.path.join(self.repo, "app.py"), "w",
                  encoding="utf-8") as f:          # la riga citata non matcha
            f.write("# uno\n# due\n# tre\ndef main():\ndef main():\n")
        out = self._refresh()
        self.assertIn("UNRESOLVABLE   app.py:3: anchor found 2 times", out)
        self.assertEqual(self._entries("app.py")[0]["line"], 3)  # non tocca

    def test_vanished_anchor_is_declared(self):
        with open(self.calc, "w", encoding="utf-8") as f:
            f.write("def somma(a, b):\nPOOL = object()\n")  # riga 1 sparita
        out = self._refresh()
        self.assertIn("UNRESOLVABLE   pkg/calc.py:1: anchor found 0 times",
                      out)
        self.assertEqual({e["line"] for e in self._entries("pkg/calc.py")},
                         {1, 2})                   # linee mai indovinate

    def test_twin_citations_on_same_line_stay_coherent(self):
        """Controesempio del T4 (1.18.0): due citazioni sulla STESSA riga di
        carta -> ogni entry copia l'intera riga; il refresh deve aggiornare
        la citazione in TUTTE le proposizioni gemelle, o guardia (vincolo)
        e get (testo) divergono."""
        carta = ("# carta\n1. [doppio] add e main insieme "
                 "(pkg/calc.py:1) e (app.py:3)\n")
        _util.run_script(_util.CHARTER, carta, env=self.env,
                         args=["save", "--repo", self.repo])
        with open(self.calc, "w", encoding="utf-8") as f:
            f.write("# uno\n# due\n# tre\ndef add(a, b):\nPOOL = object()\n")
        out = self._refresh()
        self.assertIn("RE-ANCHORED    pkg/calc.py:1 -> :4", out)
        for e in self._entries("app.py"):          # la gemella sotto app.py
            self.assertIn("(pkg/calc.py:4)", e["vincolo"])
            self.assertNotIn("(pkg/calc.py:1)", e["vincolo"])

    def test_pre_anchor_charter_is_declared(self):
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        for entries in next(iter(st.values()))["files"].values():
            for e in entries:
                e.pop("anchor", None)              # carta d'epoca pre-ancore
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump(st, f)
        out = self._refresh()
        self.assertEqual(out.count("NO ANCHOR"), 3, out)
        self.assertIn("regenerate the charter", out)


if __name__ == "__main__":
    unittest.main()
