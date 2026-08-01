"""Test della proiezione JSON-aware per i tool MCP (release 1.9.0):
array omogenei -> campioni + schema, delta sulle chiamate ripetute,
page fault sulla replica post-elisione, forme content-block."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import _util


def _items(n: int) -> list[dict]:
    return [{"id": i, "user": f"utente{i}",
             "text": ("lorem ipsum dolor sit amet consectetur adipiscing "
                      f"elit sed do eiusmod tempor incididunt {i}")}
            for i in range(n)]


def mcp_payload(text: str, tool: str = "mcp__xapi__get_posts",
                session: str = "sess-mcp1", shape: str = "list") -> dict:
    if shape == "list":
        resp = [{"type": "text", "text": text}]
    elif shape == "dict":
        resp = {"content": [{"type": "text", "text": text}]}
    else:
        resp = text
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "transcript_path": f"/tmp/{session}.jsonl",
        "tool_input": {"query": "x"},
        "tool_response": resp,
    }


class TestJsonMcp(unittest.TestCase):

    def setUp(self):
        self.env = {"CK_LOG_OFF": "1", "CK_CANARY": "0", "CK_AB_RATE": "0"}

    def _run(self, payload, env=None):
        return _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, **(env or {})})

    def test_homogeneous_array_projected_to_samples(self):
        text = json.dumps(_items(40))
        out = _util.hook_json(self._run(mcp_payload(text)))
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(updated, list)          # forma preservata
        body = updated[0]["text"]
        self.assertIn("dropped 37 of 40 objects", body)
        self.assertIn("id", body)                     # schema delle chiavi
        self.assertIn("utente0", body)                # campioni in testa
        self.assertNotIn("utente39", body)            # coda elisa
        self.assertIn("[context-kernel:", body)       # footer

    def test_nested_array_projected(self):
        text = json.dumps({"meta": {"count": 40}, "items": _items(40)})
        out = _util.hook_json(self._run(mcp_payload(text)))
        body = out["hookSpecificOutput"]["updatedToolOutput"][0]["text"]
        self.assertIn("dropped 37 of 40 objects", body)
        self.assertIn('"count": 40', body)            # il resto resta intatto

    def test_dict_content_shape_preserved(self):
        text = json.dumps(_items(40))
        out = _util.hook_json(self._run(mcp_payload(text, shape="dict")))
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(updated, dict)
        self.assertIn("dropped 37 of 40", updated["content"][0]["text"])

    def test_replay_after_elision_passes_integral(self):
        cmds = os.path.join(tempfile.gettempdir(),
                            f"ck-mcp-{os.getpid()}.json")
        try:
            env = {"CK_CMDS_STATE": cmds}
            text = json.dumps(_items(40))
            out1 = _util.hook_json(self._run(mcp_payload(text), env=env))
            self.assertIn("dropped", str(out1))
            # stessa chiamata, stesso output: la copia in contesto era ELISA
            # -> page fault, passa integrale (no-op)
            out2 = _util.hook_json(self._run(mcp_payload(text), env=env))
            self.assertEqual(out2, {})
        finally:
            if os.path.exists(cmds):
                os.remove(cmds)

    def test_identical_call_delta_marker(self):
        cmds = os.path.join(tempfile.gettempdir(),
                            f"ck-mcpd-{os.getpid()}.json")
        try:
            # sotto CK_MIN_TOKENS (niente compressione) ma sopra il minimo
            # del delta: la replica identica merita il marker
            env = {"CK_CMDS_STATE": cmds, "CK_MIN_TOKENS": "999999"}
            text = "risultato senza struttura json " * 40
            out1 = _util.hook_json(self._run(mcp_payload(text), env=env))
            self.assertEqual(out1, {})                # prima volta: registra
            out2 = _util.hook_json(self._run(mcp_payload(text), env=env))
            body = out2["hookSpecificOutput"]["updatedToolOutput"][0]["text"]
            self.assertIn("IDENTICAL", body)
            self.assertIn("MCP call", body)
        finally:
            if os.path.exists(cmds):
                os.remove(cmds)

    def test_image_only_block_is_noop(self):
        payload = mcp_payload("x")
        payload["tool_response"] = [{"type": "image", "data": "AAAA"}]
        self.assertEqual(_util.hook_json(self._run(payload)), {})

    def test_small_output_untouched(self):
        text = json.dumps(_items(9))[:400]
        # sotto MIN_TOKENS: no-op anche se JSON omogeneo
        payload = mcp_payload(text)
        self.assertEqual(_util.hook_json(self._run(payload)), {})

    def test_disabled_via_env(self):
        text = json.dumps(_items(40))
        out = _util.hook_json(self._run(mcp_payload(text),
                                        env={"CK_MCP": "0"}))
        self.assertEqual(out, {})

    def test_scalar_array_not_projected(self):
        # array di scalari: json_project non scatta (solo array di OGGETTI)
        text = json.dumps({"nums": list(range(2000))})
        parsed = _util.hook_json(self._run(mcp_payload(text)))
        self.assertNotIn("dropped", str(parsed))


if __name__ == "__main__":
    unittest.main()
