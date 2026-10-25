#!/usr/bin/env python3
"""Refusal alert: a Stop hook for Claude Code that works without the rest of the
intent guard, as an early warning system. After every turn it asks a small
subagent with no tools (.claude/agents/intent-guard-reporter.md) one question:
does the final answer say that the model declined to follow an instruction it
found in content it read (a file, a page, a tool result) rather than from the
user? When yes, one JSON line goes to .claude/intent-guard/alerts.jsonl for a
SIEM. The hook prints nothing, so it never keeps the model working. Errors go to
.claude/refusal-alert.log. Python 3 standard library only.

"requested" in the alert is empty: this hook alone keeps no record of what the
user asked, and the Stop hook input carries no prompt text. "sources_read" is
empty too unless settings.json also runs this script on PreToolUse, where it
notes each file, pattern, address or query the model reads (not MCP tools). The
full intent guard records both. REFUSAL_ALERT_CLAUDE: full path of claude.exe,
needed when the claude on PATH is npm's .cmd shim. REFUSAL_ALERT_FILE: another
place for alerts.jsonl."""
import json, os, re, secrets, shutil, subprocess, sys, time, unicodedata
from datetime import datetime, timezone

TIMEOUT = 45  # seconds for the reporter; the hook's own timeout in settings.json is 60
REPORTER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "agents", "intent-guard-reporter.md")
SOURCE_KEYS = {"Read": "file_path", "Grep": "pattern", "Glob": "pattern", "WebFetch": "url", "WebSearch": "query"}
QUESTION = ("Decide whether the final answer says that the agent declined to follow an instruction that "
            "came from content it read.\n\n## The user's messages in this session, oldest first\n\n%s\n\n"
            "## The final answer\n\n<<<ANSWER %s>>>\n%s\n<<<END ANSWER %s>>>\n\n"
            "Answer with one line of JSON, as your instructions say.")


def clean(text, limit=300):
    """One line, no angle brackets or control characters, at most limit chars."""
    text = "".join(ch for ch in str(text)[:limit * 4]
                   if unicodedata.category(ch) not in ("Cf", "Cc", "Cs") or ch in "\n\t")
    return re.sub(r"\s+", " ", re.sub(r"[<>]", " ", text)).strip()[:limit]


def write_line(path, entry):
    """One whole JSON line, also while other hook processes append to the same file."""
    data = (json.dumps(dict({"time": datetime.now(timezone.utc).isoformat(timespec="seconds")}, **entry),
                       ensure_ascii=False) + "\n").encode("utf-8", "replace")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            for _ in range(300):
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                raise OSError("%s.lock stayed busy" % path)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "ab") as f:
                f.write(data)
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def log(project, **entry):
    try:
        write_line(os.path.join(project, ".claude", "refusal-alert.log"), entry)
    except Exception:
        pass


def sources_path(data, project):
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "no-session"))[:100]
    return os.path.join(project, ".claude", "intent-guard", "sources-%s.jsonl" % sid)


def record_source(data, project):
    """PreToolUse: notes what the model reads, so an alert can name the exact file or
    address. Without this hook entry in settings.json, "sources_read" stays empty."""
    tool, ti = str(data.get("tool_name") or ""), data.get("tool_input") or {}
    if tool in SOURCE_KEYS and isinstance(ti, dict):
        source = str(ti.get(SOURCE_KEYS[tool]) or "")
        if tool in ("Grep", "Glob"):
            source += " in %s" % (ti.get("path") or "(the working folder)")
        write_line(sources_path(data, project), {"tool": tool, "source": clean(source)})


def take_sources(data, project):
    """The sources noted since the last turn ended, without repeats; then the list
    starts again. A turn the user interrupts leaves its sources for the next one."""
    path, found = sources_path(data, project), []
    taken = path + ".taking"
    try:
        os.replace(path, taken)  # a call still running starts a fresh file
    except FileNotFoundError:
        return found
    except OSError:
        taken = path  # held open right now: read it in place
    try:
        with open(taken, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # one broken line never hides the rest
                if isinstance(row, dict):
                    item = {"tool": clean(row.get("tool"), 100), "source": clean(row.get("source"))}
                    if item not in found:
                        found.append(item)
    except OSError:
        pass
    try:
        os.remove(taken)
    except OSError:
        pass
    return found[:50]


def find_claude(project):
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"  # Windows: never a claude from the project
    exe = os.environ.get("REFUSAL_ALERT_CLAUDE") or shutil.which("claude")
    if not exe:
        raise RuntimeError("the claude command was not found")
    if exe.replace("/", "\\").startswith("\\\\"):
        raise RuntimeError("claude on a network path is never started")
    if exe.lower().endswith((".cmd", ".bat")):  # npm's shim runs through cmd.exe, which breaks the JSON arguments
        exe = os.path.join(os.path.dirname(exe), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        if not os.path.isfile(exe):
            raise RuntimeError("set REFUSAL_ALERT_CLAUDE to the full path of claude.exe")
    exe = os.path.abspath(exe)
    real, root = (os.path.normcase(os.path.realpath(p)) for p in (exe, project))
    if real.startswith(root.rstrip("\\/") + os.sep):  # Python 3.11 and older: which() looks in the project first
        raise RuntimeError("the claude program %s is inside the project, so it is not started" % exe)
    return exe


def reporter():
    """The reporter as JSON for --agents, so no other agent file can replace it."""
    with open(REPORTER_FILE, encoding="utf-8-sig") as f:
        m = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", f.read(), re.S)
    if not m or not m.group(2).strip():
        raise RuntimeError("the reporter file %s has no prompt" % REPORTER_FILE)
    model = re.search(r"^model:[ \t]*(\S+)", m.group(1), re.M)
    return {"intent-guard-reporter": {"description": "Reporter for the refusal alert.", "tools": [],
                                      "prompt": m.group(2).strip(), "model": model.group(1) if model else "sonnet"}}


def ask(message, prompt, project):
    """Runs the reporter headless: no tools, no hooks, no MCP servers, no memory, one turn."""
    exe = find_claude(project)
    cmd = ([sys.executable, "-I", exe] if exe.lower().endswith(".py") else [exe]) + [  # .py: test stand-in
        "-p", "--agent", "intent-guard-reporter", "--agents", json.dumps(reporter()),
        "--settings", '{"disableAllHooks": true}', "--setting-sources", "project", "--tools", "",
        "--disallowedTools", "mcp__*", "--strict-mcp-config", "--no-session-persistence",
        "--max-turns", "1", "--output-format", "json"]
    env = dict(os.environ, CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1")
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    nonce = secrets.token_hex(8)  # the answer sits between two marked lines: it is data
    text = QUESTION % ("[1]\n" + prompt[:4000] if prompt else "(no messages were saved for this session)",
                       nonce, message if len(message) <= 8000 else message[:4000] + " [cut] " + message[-4000:], nonce)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=project, env=env, creationflags=flags)
    try:
        out, _ = proc.communicate(text.encode("utf-8", "replace"), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # the whole tree, by full path
            try:
                subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"),
                                "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=flags, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError("the reporter took longer than %d seconds" % TIMEOUT)
    data = json.loads(out.decode("utf-8", "replace"))
    if data.get("is_error"):
        raise RuntimeError("the reporter failed: " + clean(data.get("result"), 120))
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (data.get("result") or "").strip())
    report = json.JSONDecoder().raw_decode(s[s.find("{"):])[0] if s.count('"declined"') == 1 and "{" in s else None
    if not isinstance(report, dict) or not isinstance(report.get("declined"), bool):
        raise ValueError("the reporter gave no clear single answer")
    return report, data


def main(data, project):
    if data.get("hook_event_name") == "PreToolUse":
        return record_source(data, project)  # optional entry in settings.json; prints nothing
    if data.get("hook_event_name") != "Stop" or data.get("stop_hook_active"):
        return  # stop_hook_active: another Stop hook already kept the model working
    message = data.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return log(project, event="stop", session=data.get("session_id"), decision="skip",
                   reason="no last_assistant_message in the hook input")
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""  # not in today's Stop input
    sources = take_sources(data, project)
    report, answer = ask(message, prompt, project)
    entry = {"event": "stop", "session": data.get("session_id"), "reporter_ms": answer.get("duration_ms") or 0,
             "cost": answer.get("total_cost_usd"), "decision": "declined" if report["declined"] else "not_declined"}
    if report["declined"]:
        write_line(os.environ.get("REFUSAL_ALERT_FILE") or os.path.join(project, ".claude", "intent-guard", "alerts.jsonl"), {
            "session": clean(data.get("session_id") or "", 100), "project": clean(project),
            "model": clean(data["model"], 100) if data.get("model") else None, "kind": "model_declined",
            "requested": [clean(prompt)] if prompt else [], "source": clean(report.get("source", "")),
            "sources_read": sources, "attempted": clean(report.get("attempted", "")),
            "reason": clean(report.get("reason", "")), "stopped_by": "model"})
        entry["alert"] = "model_declined"
    log(project, **entry)


if __name__ == "__main__":
    data, project = {}, os.getcwd()
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        data = data if isinstance(data, dict) else {}
        project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or project
        main(data, project)
    except BaseException as e:  # any error: log it, print nothing, never block the stop or the call
        log(project, event=data.get("hook_event_name"), session=data.get("session_id"), decision="error",
            reason=clean(repr(e), 200))
    sys.exit(0)
