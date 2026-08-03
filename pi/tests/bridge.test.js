import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const bridge = resolve("pi/runtime/pi_bridge.py");
const stateDir = await mkdtemp(join(tmpdir(), "context-kernel-pi-test-"));

function call(payload) {
  const proc = spawnSync("python3", [bridge], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    env: {
      ...process.env,
      CK_LOG_OFF: "1",
      CK_CANARY: "0",
      CK_READS_STATE: join(stateDir, "reads.json"),
      CK_MIN_TOKENS: "20",
      CK_DELTA_MIN: "20",
    },
  });
  assert.equal(proc.status, 0, proc.stderr);
  return JSON.parse(proc.stdout.trim());
}

test("Pi pre-tool bridge reuses quiet command rules", () => {
  const result = call({ mode: "rewrite", command: "npm install | head" });
  assert.equal(result.changed, true);
  assert.match(result.command, /npm install --no-fund --no-audit --no-progress \| head/);
});

test("Pi post-tool bridge preserves signal while reducing noise", () => {
  const lines = Array.from({ length: 150 }, (_, index) => `ordinary output ${index}`);
  lines[80] = "ERROR: database unavailable";
  const result = call({ mode: "compress", tool: "bash", text: lines.join("\n"), session: "signal" });
  assert.equal(result.changed, true);
  assert.match(result.text, /ERROR: database unavailable/);
  assert.match(result.text, /\[context-kernel: \d+ -> \d+ token/);
  assert.ok(result.after < result.before);
});

test("Pi read delta suppresses one unchanged reread then permits a page fault", () => {
  // Stay below T1's line-elision threshold so the first read is recorded but
  // not otherwise compressed; the second read exercises delta suppression.
  const text = Array.from({ length: 60 }, (_, index) => `line ${index}: value`).join("\n");
  const payload = { mode: "compress", tool: "read", text, input: { path: "sample.py" }, session: "delta" };
  const first = call(payload);
  const second = call(payload);
  const third = call(payload);
  assert.equal(first.changed, false);
  assert.equal(second.changed, true);
  assert.match(second.text, /file UNCHANGED/);
  assert.equal(third.changed, false);
});

test("Bridge fails safe for unknown modes", () => {
  const result = call({ mode: "unknown" });
  assert.equal(typeof result.error, "string");
});
