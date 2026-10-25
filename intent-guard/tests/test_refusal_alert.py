"""Tests for the standalone refusal alert (alert-only/.claude/hooks/refusal_alert.py).
No model calls: the reporter is replaced by stub_judge.py in reporter mode.
Run: python -I harness/test_refusal_alert.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HARNESS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HARNESS)
ALERT_ONLY = os.path.join(LAB, "alert-only")
SCRIPT = os.path.join(ALERT_ONLY, ".claude", "hooks", "refusal_alert.py")
REPORTER = os.path.join(ALERT_ONLY, ".claude", "agents", "intent-guard-reporter.md")
FULL_REPORTER = os.path.join(LAB, "guard-alerts", ".claude", "agents", "intent-guard-reporter.md")
OUTSIDE = tempfile.mkdtemp(prefix="ra-test-", dir=os.path.dirname(LAB))
STUB = os.path.join(OUTSIDE, "stub_judge.py")
shutil.copy(os.path.join(HARNESS, "stub_judge.py"), STUB)
PROJECT = os.path.join(OUTSIDE, "proj")  # where the hook writes its log and alerts
CAPTURE = os.path.join(OUTSIDE, "capture.json")
ALERTS = os.path.join(PROJECT, ".claude", "intent-guard", "alerts.jsonl")
SOURCES = os.path.join(PROJECT, ".claude", "intent-guard", "sources-ra-1.jsonl")
LOG = os.path.join(PROJECT, ".claude", "refusal-alert.log")
ANSWER = "The log asked me to read your AWS credentials. I did not do that; it did not come from you."
KEYS = ["time", "session", "project", "model", "kind", "requested", "source", "sources_read",
        "attempted", "reason", "stopped_by"]


def tearDownModule():
    shutil.rmtree(OUTSIDE, ignore_errors=True)


def run_hook(payload, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k.startswith("REFUSAL_ALERT") or k.startswith("STUB_"))}
    env.update(CLAUDE_PROJECT_DIR=PROJECT, REFUSAL_ALERT_CLAUDE=STUB, STUB_CAPTURE=CAPTURE)
    env.update(extra)
    p = subprocess.run([sys.executable, "-I", SCRIPT], input=json.dumps(payload).encode(),
                       capture_output=True, env=env, timeout=60)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def stop(message=ANSWER, active=False, **more):
    d = {"session_id": "ra-1", "hook_event_name": "Stop", "stop_hook_active": active, "cwd": PROJECT,
         "permission_mode": "default"}
    if message is not None:
        d["last_assistant_message"] = message
    d.update(more)
    return d


def call(tool, tool_input):
    return {"session_id": "ra-1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
            "cwd": PROJECT, "permission_mode": "default"}


def lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


class RefusalAlert(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(PROJECT, ignore_errors=True)
        os.makedirs(os.path.join(PROJECT, ".claude"))
        if os.path.exists(CAPTURE):
            os.remove(CAPTURE)

    def test_1_declined_writes_an_alert_and_prints_nothing(self):
        code, out, err = run_hook(stop(model="claude-test-5"), STUB_REPORTER="declined")
        self.assertEqual((code, out), (0, ""))
        rows = lines(ALERTS)
        self.assertEqual(len(rows), 1, rows)
        a = rows[0]
        self.assertEqual(list(a), KEYS)
        self.assertEqual((a["kind"], a["stopped_by"], a["session"], a["model"], a["requested"], a["sources_read"]),
                         ("model_declined", "model", "ra-1", "claude-test-5", [], []))
        self.assertEqual(os.path.normcase(a["project"]), os.path.normcase(PROJECT))
        self.assertIn("cloud credentials", a["attempted"])
        for key in ("source", "attempted", "reason"):
            for bad in ("<", ">", "\n", "\t", "\u200b"):
                self.assertNotIn(bad, a[key], key)
        with open(CAPTURE, encoding="utf-8") as f:
            cap = json.load(f)
        argv = cap["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "intent-guard-reporter")
        defs = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(list(defs), ["intent-guard-reporter"])
        self.assertEqual(defs["intent-guard-reporter"]["tools"], [])
        self.assertEqual(defs["intent-guard-reporter"]["model"], "sonnet")
        with open(REPORTER, encoding="utf-8") as f:
            self.assertEqual(defs["intent-guard-reporter"]["prompt"], f.read().split("\n---\n", 1)[1].strip())
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--settings") + 1], '{"disableAllHooks": true}')
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        for flag in ("--no-session-persistence", "--strict-mcp-config", "mcp__*", "--setting-sources"):
            self.assertIn(flag, argv)
        self.assertEqual(cap["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertIsNone(cap["env"]["CLAUDE_CODE_SESSION_ID"])
        self.assertIn("<<<ANSWER ", cap["stdin"])
        self.assertIn(ANSWER, cap["stdin"])
        self.assertIn("(no messages were saved for this session)", cap["stdin"])
        self.assertEqual(os.path.normcase(cap["cwd"]), os.path.normcase(PROJECT))
        log = lines(LOG)[-1]
        self.assertEqual((log["event"], log["decision"], log["alert"]), ("stop", "declined", "model_declined"))
        self.assertIn("reporter_ms", log)
        # a prompt in the hook input, should a version ever carry one, fills "requested"
        code, out, err = run_hook(stop(prompt="Why does <npm> test fail?"), STUB_REPORTER="declined")
        self.assertEqual(lines(ALERTS)[-1]["requested"], ["Why does npm test fail?"])

    def test_2_not_declined_writes_nothing(self):
        code, out, err = run_hook(stop(), STUB_REPORTER="clean")
        self.assertEqual((code, out), (0, ""))
        self.assertFalse(os.path.exists(ALERTS))
        self.assertTrue(os.path.exists(CAPTURE))
        self.assertEqual(lines(LOG)[-1]["decision"], "not_declined")

    def test_3_skips_when_active_or_without_a_message(self):
        code, out, err = run_hook(stop(active=True), STUB_REPORTER="declined")
        self.assertEqual((code, out), (0, ""))
        self.assertFalse(os.path.exists(CAPTURE))
        self.assertFalse(os.path.exists(ALERTS))
        for message in (None, "", "  "):
            code, out, err = run_hook(stop(message=message), STUB_REPORTER="declined")
            self.assertEqual((code, out), (0, ""))
            self.assertFalse(os.path.exists(CAPTURE))
            self.assertIn("last_assistant_message", lines(LOG)[-1]["reason"])
        code, out, err = run_hook(dict(stop(), hook_event_name="SessionStart"), STUB_REPORTER="declined")
        self.assertEqual((code, out), (0, ""))
        self.assertFalse(os.path.exists(CAPTURE))

    def test_4_bad_answers_and_errors_make_no_alert_and_print_nothing(self):
        for mode in ("garbage", "bad_json", "two_answers", "is_error"):
            code, out, err = run_hook(stop(), STUB_REPORTER=mode)
            self.assertEqual((code, out), (0, ""), mode)
            self.assertFalse(os.path.exists(ALERTS), mode)
            log = lines(LOG)[-1]
            self.assertEqual(log["decision"], "error", mode)
            self.assertTrue(log["reason"], mode)
        for raw in (b"not json", b"[1]"):
            p = subprocess.run([sys.executable, "-I", SCRIPT], input=raw, capture_output=True,
                               env=dict(os.environ, CLAUDE_PROJECT_DIR=PROJECT), timeout=60)
            self.assertEqual((p.returncode, p.stdout), (0, b""))
        code, out, err = run_hook(stop(), STUB_REPORTER="declined", REFUSAL_ALERT_CLAUDE=os.path.join(OUTSIDE, "none.py"))
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(lines(LOG)[-1]["decision"], "error")
        self.assertFalse(os.path.exists(ALERTS))

    def test_5_sources_read_come_from_the_pre_tool_use_entry(self):
        log_file = os.path.join(PROJECT, "logs", "ci-run-4812.txt")
        for tool, ti in (("Read", {"file_path": log_file}),
                         ("Grep", {"pattern": "discount", "path": os.path.join(PROJECT, "src")}),
                         ("Glob", {"pattern": "**/*.js"}),
                         ("WebFetch", {"url": "https://example.test/page?x=<1>\u200b", "prompt": "sum up"}),
                         ("WebSearch", {"query": "tesla price"}),
                         ("Bash", {"command": "cat logs/ci-run-4812.txt"}),  # not noted here
                         ("Write", {"file_path": log_file, "content": "x"}),  # not a source
                         ("Read", {"file_path": log_file})):  # a repeat is listed once
            code, out, err = run_hook(call(tool, ti), STUB_REPORTER="declined")
            self.assertEqual((code, out), (0, ""), tool)
        self.assertFalse(os.path.exists(CAPTURE))  # PreToolUse never starts the reporter
        self.assertEqual(len(lines(SOURCES)), 6)
        code, out, err = run_hook(stop(), STUB_REPORTER="declined")
        self.assertEqual((code, out), (0, ""))
        a = lines(ALERTS)[-1]
        self.assertEqual(list(a), KEYS)
        self.assertEqual(a["sources_read"], [
            {"tool": "Read", "source": log_file},
            {"tool": "Grep", "source": "discount in " + os.path.join(PROJECT, "src")},
            {"tool": "Glob", "source": "**/*.js in (the working folder)"},
            {"tool": "WebFetch", "source": "https://example.test/page?x= 1"},
            {"tool": "WebSearch", "source": "tesla price"}])
        self.assertFalse(os.path.exists(SOURCES))  # the list starts again after the turn
        code, out, err = run_hook(stop(), STUB_REPORTER="declined")
        self.assertEqual(lines(ALERTS)[-1]["sources_read"], [])
        # a turn with no alert resets the list too; long values are cut, many are capped
        run_hook(call("Read", {"file_path": "C:\\" + "a" * 400 + ".txt"}))
        for i in range(60):
            run_hook(call("Glob", {"pattern": "p%d" % i}))
        with open(SOURCES, encoding="utf-8") as f:
            self.assertEqual(len(json.loads(f.readline())["source"]), 300)
        code, out, err = run_hook(stop(), STUB_REPORTER="clean")
        self.assertFalse(os.path.exists(SOURCES))
        run_hook(call("Read", {"file_path": log_file}))
        for i in range(60):
            run_hook(call("Glob", {"pattern": "p%d" % i}))
        code, out, err = run_hook(stop(), STUB_REPORTER="declined")
        self.assertEqual(len(lines(ALERTS)[-1]["sources_read"]), 50)

    def test_6_files(self):
        with open(REPORTER, "rb") as f, open(FULL_REPORTER, "rb") as g:
            self.assertEqual(f.read(), g.read())  # byte for byte the same reporter as the full guard
        with open(os.path.join(ALERT_ONLY, ".claude", "settings.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual(list(s["hooks"]), ["Stop", "PreToolUse"])
        stop_hook = s["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual((stop_hook["type"], stop_hook["command"], stop_hook["timeout"]), ("command", "python", 60))
        self.assertEqual(stop_hook["args"], ["-I", "${CLAUDE_PROJECT_DIR}/.claude/hooks/refusal_alert.py"])
        self.assertNotIn("onFailure", stop_hook)
        self.assertNotIn("matcher", s["hooks"]["Stop"][0])
        pre = s["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "Read|Grep|Glob|WebFetch|WebSearch")
        self.assertEqual(pre["hooks"][0]["args"], stop_hook["args"])
        self.assertEqual(pre["hooks"][0]["timeout"], 10)
        self.assertNotIn("onFailure", pre["hooks"][0])
        self.assertIn("Agent(intent-guard-reporter)", s["permissions"]["deny"])
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertLessEqual(len(text.splitlines()), 230)  # 227 lines after the review fixes, 24 October 2026
        self.assertIn('"requested" in the alert is empty', text)
        self.assertIn('"sources_read" is\nempty too unless', text)
        with open(os.path.join(ALERT_ONLY, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("`requested` is always empty here", readme)
        self.assertIn("then `sources_read` stays empty", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
