"""Test di slice.py: correttezza della slice e proprieta' answer-preserving."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest

import _util

FIXTURE = '''\
import math
import os

C = 2

def g(x):
    return math.sqrt(x) + C

def f(x):
    return g(x) * 2

def h(y):
    return os.path.join("a", y)
'''


def _slice(path: str, *symbols: str):
    return subprocess.run(
        [sys.executable, _util.SLICE, path, *symbols],
        capture_output=True, text=True, timeout=30,
    )


class TestSlice(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(FIXTURE)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_slice_keeps_transitive_dependencies(self):
        out = _slice(self.path, "f").stdout
        self.assertIn("def f", out)
        self.assertIn("def g", out)            # f usa g
        self.assertIn("C = 2", out)            # g usa C
        self.assertIn("import math", out)      # g usa math

    def test_slice_drops_unreachable_units(self):
        out = _slice(self.path, "f").stdout
        self.assertNotIn("def h", out)         # h non raggiungibile da f
        self.assertNotIn("import os", out)     # os usato solo da h

    def test_slice_of_leaf_drops_everything_else(self):
        out = _slice(self.path, "h").stdout
        self.assertIn("def h", out)
        self.assertIn("import os", out)
        self.assertNotIn("def f", out)
        self.assertNotIn("import math", out)

    def test_slice_is_valid_python(self):
        ast.parse(_slice(self.path, "f").stdout)

    def test_answer_preserving_by_construction(self):
        """La proprieta' centrale: eseguire la slice da' lo STESSO risultato
        del modulo intero, per il simbolo target."""
        full_ns: dict = {}
        exec(compile(FIXTURE, "<full>", "exec"), full_ns)
        sliced_ns: dict = {}
        exec(compile(_slice(self.path, "f").stdout, "<slice>", "exec"), sliced_ns)
        self.assertEqual(sliced_ns["f"](9), full_ns["f"](9))
        self.assertEqual(sliced_ns["f"](16), full_ns["f"](16))

    def test_unknown_symbol_fails_safe_to_whole_file(self):
        out = _slice(self.path, "simbolo_inesistente").stdout
        self.assertIn("def f", out)
        self.assertIn("def h", out)            # file intero: nessuna perdita

    def test_non_python_file_falls_back_to_whole_content(self):
        fd, txt = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("contenuto qualunque, non python")
        try:
            out = _slice(txt, "f").stdout
            self.assertIn("contenuto qualunque", out)
        finally:
            os.unlink(txt)

    def test_missing_args_exits_nonzero_with_usage(self):
        proc = subprocess.run([sys.executable, _util.SLICE],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)


GO_FIXTURE = '''\
package main

import (
	"fmt"
	"strings"
)

const Prefix = "LOG: "

type Level int

func decorate(s string) string {
	return Prefix + strings.TrimSpace(s)
}

func orphan() int {
	return 42
}

func Handle(lvl Level, msg string) string {
	if lvl > 0 {
		return fmt.Sprintf("%d %s", lvl, decorate(msg))
	}
	return decorate(msg)
}
'''


def _write(suffix: str, content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


class TestGoSlice(unittest.TestCase):
    """Slice def-use CONSERVATIVA su Go (1.22.0): senza parser, l'insieme d'uso
    e' la sovra-approssimazione 'tutti gli identificatori del corpo' -> puo'
    tenere di piu', mai lasciare fuori una dipendenza del target."""

    @classmethod
    def setUpClass(cls):
        cls.path = _write(".go", GO_FIXTURE)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_keeps_transitive_deps(self):
        out = _slice(self.path, "Handle").stdout
        self.assertIn("func Handle", out)
        self.assertIn("func decorate", out)        # Handle -> decorate
        self.assertIn("const Prefix", out)         # decorate -> Prefix
        self.assertIn("type Level", out)           # Handle usa il tipo Level

    def test_drops_unreachable(self):
        out = _slice(self.path, "Handle").stdout
        self.assertNotIn("func orphan", out)       # non raggiungibile da Handle

    def test_leaf_drops_the_rest(self):
        out = _slice(self.path, "orphan").stdout
        self.assertIn("func orphan", out)
        self.assertNotIn("func Handle", out)
        self.assertNotIn("func decorate", out)

    def test_package_and_imports_always_kept(self):
        out = _slice(self.path, "orphan").stdout   # orphan non usa gli import
        self.assertIn("package main", out)
        self.assertIn('"fmt"', out)                # tenuti comunque (conservativo)

    def test_unknown_symbol_whole_file(self):
        out = _slice(self.path, "NonEsiste").stdout
        self.assertIn("func Handle", out)
        self.assertIn("func orphan", out)          # file intero: nessuna perdita

    def test_masking_no_false_boundaries(self):
        """Una raw-string con 'func ...{' e '}' a colonna 0 non deve creare
        confini di unita' fantasma: il blocco var Banner resta UN'unita' intera
        (con dentro il testo 'func fake'), 'fake' non e' un simbolo seedabile."""
        src = ('package main\n\n'
               'var Banner = `\nfunc fake() {\n}\n`\n\n'
               'func real() string {\n\treturn Banner\n}\n')
        path = _write(".go", src)
        try:
            out = _slice(path, "real").stdout
            self.assertIn("func real", out)
            self.assertIn("var Banner", out)       # real usa Banner, tenuto
            self.assertIn("func fake", out)        # ma solo come CONTENUTO di Banner
            # prova che 'fake' non e' stato riconosciuto come dichiarazione:
            # seedare 'fake' cade nel fail-safe = file intero (nessun simbolo)
            leaf = _slice(path, "fake").stdout
            self.assertIn("func real", leaf)       # file intero: 'fake' non e' un simbolo
        finally:
            os.unlink(path)

    def test_non_gofmt_falls_back_to_whole_file(self):
        """Split infido (dichiarazione a colonna 0 dentro un corpo, non-gofmt):
        unita' sbilanciata -> rete di sicurezza -> file intero, mai una slice
        che sotto-approssima."""
        src = ('package main\n\n'
               'func outer() int {\nvar x = inner()\nreturn x\n}\n\n'
               'func inner() int { return 1 }\n')
        path = _write(".go", src)
        try:
            out = _slice(path, "outer").stdout
            self.assertIn("func outer", out)
            self.assertIn("func inner", out)       # NON persa: fallback intero
        finally:
            os.unlink(path)

    def test_conservative_dep_deep_in_body_is_kept(self):
        """Una dipendenza usata solo in fondo a un blocco annidato resta:
        la sovra-approssimazione guarda TUTTI gli identificatori del corpo."""
        src = ('package main\n\n'
               'const Secret = 7\n\n'
               'func Top() int {\n'
               '\tfor i := 0; i < 3; i++ {\n'
               '\t\tif i == 2 {\n\t\t\treturn Secret\n\t\t}\n\t}\n'
               '\treturn 0\n}\n')
        path = _write(".go", src)
        try:
            out = _slice(path, "Top").stdout
            self.assertIn("const Secret", out)     # dipendenza mai lasciata fuori
        finally:
            os.unlink(path)

    def test_grouped_var_names_bound(self):
        """Nomi legati in un blocco var ( ... ) raggruppato sono seedabili."""
        src = ('package main\n\n'
               'var (\n\ta = 1\n\tb = compute()\n)\n\n'
               'func compute() int { return 5 }\n')
        path = _write(".go", src)
        try:
            out = _slice(path, "b").stdout         # b -> compute()
            self.assertIn("func compute", out)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
