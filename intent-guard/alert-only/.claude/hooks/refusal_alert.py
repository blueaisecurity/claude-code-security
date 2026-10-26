#!/usr/bin/env python3
"""Refusal alert: a Stop hook for Claude Code that works without the rest of the
intent guard, as an early warning system. After every turn it asks a small
subagent with no tools (.claude/agents/intent-guard-reporter.md) one question:
does Claude's text in this turn say that the model declined to follow an
instruction it found in content it read (a file, a page, a tool result) rather
than from the user? When yes, one JSON line goes to .claude/intent-guard/alerts.jsonl
for a SIEM. The hook prints nothing, so it never keeps the model working. Errors go
to .claude/refusal-alert.log. Python 3 standard library only.

"requested" in the alert is empty: this hook alone keeps no record of what the
user asked, and the Stop hook input carries no prompt text. "sources_read" is
empty too unless settings.json also runs this script on PreToolUse, where it
notes each file, pattern, address or query the model reads (not MCP tools). The
full intent guard records both. The Stop input holds only Claude's final
message. When settings.json also runs this script on MessageDisplay, it keeps
the text Claude shows during the turn, at most KEEP characters, each message
keeping its start and end, so the reporter also reads a refusal written before
the final message. The turn files are deleted at every Stop, and on StopFailure,
which runs instead of Stop when an API error ends the turn; every Stop also
deletes turn files a day old, such as those of a turn the user interrupted.
REFUSAL_ALERT_CLAUDE: full path of claude.exe, needed when the claude on PATH
is npm's .cmd shim. REFUSAL_ALERT_FILE: another place for alerts.jsonl."""
import contextlib, json, os, re, secrets, shutil, stat, subprocess, sys, time, unicodedata
from datetime import datetime, timezone

TIMEOUT = 45  # seconds for the reporter; the hook's own timeout in settings.json is 60
KEEP = 16000  # characters of the text Claude shows in a turn that are kept and given to the reporter
CUT = " [cut] "
REPORTER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "agents", "intent-guard-reporter.md")
SOURCE_KEYS = {"Read": "file_path", "Grep": "pattern", "Glob": "pattern", "WebFetch": "url", "WebSearch": "query"}
QUESTION = ("Decide whether the %s says that the agent declined to follow an instruction that "
            "came from content it read.\n\n## The user's messages in this session, oldest first\n\n%s\n\n"
            "%s## The final answer\n\n<<<ANSWER %s>>>\n%s\n<<<END ANSWER %s>>>\n\n"
            "Answer with one line of JSON, as your instructions say.")
EARLIER = ("## Earlier text from the agent in this turn, oldest first\n\n"
           "<<<EARLIER %s>>>\n%s\n<<<END EARLIER %s>>>\n\n")


def clean(text, limit=300):
    """One line, no angle brackets or control characters, at most limit chars."""
    text = "".join(ch for ch in str(text)[:limit * 4]
                   if unicodedata.category(ch) not in ("Cf", "Cc", "Cs") or ch in "\n\t")
    return re.sub(r"\s+", " ", re.sub(r"[<>]", " ", text)).strip()[:limit]


def cut(text, limit=8000):
    """The text, or its first and last limit / 2 characters when it is longer."""
    return text if len(text) <= limit else text[:limit // 2] + " [cut] " + text[-(limit // 2):]


def as_data(text):
    """Claude's text for the reporter: no control or format characters except line breaks and tabs, and
    <<< and >>> spaced out, so that no line of it can look like one of the marker lines."""
    text = "".join(ch for ch in str(text) if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return text.replace("<<<", "< < <").replace(">>>", "> > >")


def squeeze(text, limit):
    """The text, or its first two thirds and last third around " [cut] ", in at most limit characters."""
    if len(text) <= limit:
        return text
    room = limit - len(CUT)
    if room < 3:
        return text[:max(limit, 0)]
    return text[:room * 2 // 3] + CUT + text[len(text) - (room - room * 2 // 3):]


def fit(texts, limit):
    """The texts cut to add up to at most limit characters: short ones stay whole, and the long ones
    share the rest equally, each keeping its start and end, so one long text can't push out the others."""
    out, room, left = list(texts), limit, len(texts)
    if sum(map(len, out)) > limit:
        for i in sorted(range(len(out)), key=lambda i: len(out[i])):
            out[i] = squeeze(out[i], max(room // left, 0))
            room, left = room - len(out[i]), left - 1
    return out


@contextlib.contextmanager
def locked(path):
    """Holds the lock file next to path, so hook processes that run at the same time take turns."""
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
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def write_line(path, entry):
    """One whole JSON line, also while other hook processes append to the same file."""
    data = (json.dumps(dict({"time": datetime.now(timezone.utc).isoformat(timespec="seconds")}, **entry),
                       ensure_ascii=False) + "\n").encode("utf-8", "replace")
    with locked(path), open(path, "ab") as f:
        f.write(data)


def log(project, **entry):
    try:
        write_line(os.path.join(project, ".claude", "refusal-alert.log"), entry)
    except Exception:
        pass


def turn_path(data, project, kind):
    """This session's file for the current turn: "sources" (what the model read)
    or "shown" (the text Claude showed)."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "no-session"))[:100]
    return os.path.join(project, ".claude", "intent-guard", "%s-%s.jsonl" % (kind, sid))


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # one broken line never hides the rest
            if isinstance(row, dict):
                rows.append(row)
    return rows


def take(path):
    """The rows of a turn file; then the file is deleted, under its lock, so a hook that writes
    at the same moment starts a fresh file. A problem keeps what was read so far."""
    rows = []
    try:
        if os.path.exists(path):
            with locked(path):
                rows = read_rows(path)
                os.remove(path)
    except OSError:
        pass
    return rows


def record_source(data, project):
    """PreToolUse: notes what the model reads, so an alert can name the exact file or
    address. Without this hook entry in settings.json, "sources_read" stays empty."""
    tool, ti = str(data.get("tool_name") or ""), data.get("tool_input") or {}
    if tool in SOURCE_KEYS and isinstance(ti, dict):
        source = str(ti.get(SOURCE_KEYS[tool]) or "")
        if tool in ("Grep", "Glob"):
            source += " in %s" % (ti.get("path") or "(the working folder)")
        write_line(turn_path(data, project, "sources"), {"tool": tool, "source": clean(source)})


def take_sources(data, project):
    """The sources noted since the last turn ended, without repeats; then the list
    starts again. A turn the user interrupts leaves its sources for the next one."""
    found = []
    for row in take(turn_path(data, project, "sources")):
        item = {"tool": clean(row.get("tool"), 100), "source": clean(row.get("source"))}
        if item not in found:
            found.append(item)
    return found[:50]


def keep_text(data, project):
    """MessageDisplay: keeps each batch of the text Claude shows, with its message_id and index, so the
    batches of one message join up again at Stop. Prints nothing, so the original text is shown. When the
    file is larger than KEEP bytes, it is written again with one row per message, KEEP characters in all."""
    delta = data.get("delta")
    if not isinstance(delta, str) or not delta:
        return
    path = turn_path(data, project, "shown")
    row = {"prompt": str(data.get("prompt_id") or ""), "id": str(data.get("message_id") or ""),
           "index": data["index"] if isinstance(data.get("index"), int) else 0, "text": squeeze(delta, KEEP)}
    with locked(path):
        with open(path, "ab") as f:
            f.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8", "replace"))
            size = f.tell()
        if size > KEEP:  # bytes: a character takes at least one byte
            found = messages(read_rows(path))
            rows = [dict(r, text=t) for (r, _), t in zip(found, fit([t for _, t in found], KEEP))]
            with open(path, "wb") as f:
                f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8", "replace"))


def messages(rows):
    """[(row, text)], one per message, oldest first: its batches joined by message_id in index order, and
    its last batch. A row without a message_id is a message of its own."""
    groups = {}
    for n, row in enumerate(rows):
        groups.setdefault(str(row.get("id") or "") or n, []).append(row)
    found = []
    for parts in groups.values():
        parts.sort(key=lambda r: r["index"] if isinstance(r.get("index"), int) else 0)
        found.append((parts[-1], "".join(str(r.get("text") or "") for r in parts)))
    return found


def earlier_text(rows, final, prompt_id):
    """The text Claude showed before its final message, for the reporter: one part per message, oldest
    first, without a message whose every part is already in the final message, KEEP characters in all.
    When the rows and the Stop input carry the same prompt_id, text from an interrupted turn is left out."""
    if prompt_id and any(r.get("prompt") == prompt_id for r in rows):
        rows = [r for r in rows if r.get("prompt") == prompt_id]
    texts = []
    for _, text in messages(rows):
        if text.strip() and not all(part.strip() in final for part in text.split(CUT.strip())):
            texts.append(as_data(text).strip())
    texts = [t for t in texts if t]
    return "\n\n".join(fit(texts, max(KEEP - 2 * (len(texts) - 1), 0)))


def sweep(project):
    """Deletes the turn files of any session, and their lock files, that were last changed more than a
    day ago, such as those of a turn the user interrupted. Plain files only, never through a link."""
    folder = os.path.join(project, ".claude", "intent-guard")
    try:
        top = os.lstat(folder)
        if stat.S_ISLNK(top.st_mode) or getattr(top, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return
        with os.scandir(folder) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if (re.fullmatch(r"(?:shown|sources)-[A-Za-z0-9_-]+\.jsonl(?:\.lock)?", entry.name)
                        and stat.S_ISREG(info.st_mode) and info.st_mtime < time.time() - 86400
                        and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass  # in use, or another hook was first
    except OSError:
        pass  # no folder yet


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


def ask(message, earlier, prompt, project):
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
    nonce = secrets.token_hex(8)  # Claude's text sits between marked lines: it is data
    text = QUESTION % ("final answer or the earlier text" if earlier else "final answer",
                       "[1]\n" + prompt[:4000] if prompt else "(no messages were saved for this session)",
                       EARLIER % (nonce, cut(as_data(earlier), KEEP), nonce) if earlier else "", nonce,
                       cut(as_data(message)), nonce)
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
    event = data.get("hook_event_name")
    if event == "PreToolUse":
        return record_source(data, project)  # optional entry in settings.json; prints nothing
    if event == "MessageDisplay":
        return keep_text(data, project)  # optional entry too; prints nothing, so the text is shown as it is
    if event == "StopFailure":  # an API error ended the turn, and Stop does not run then: start again clean
        take(turn_path(data, project, "shown")), take(turn_path(data, project, "sources"))
        return sweep(project)
    if event != "Stop" or data.get("stop_hook_active"):
        return  # stop_hook_active: another Stop hook already kept the model working
    shown = take(turn_path(data, project, "shown"))  # reset at every Stop: there is no UserPromptSubmit hook
    sweep(project)  # turn files a day old, of any session
    message = data.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return log(project, event="stop", session=data.get("session_id"), decision="skip",
                   reason="no last_assistant_message in the hook input")
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""  # not in today's Stop input
    sources = take_sources(data, project)
    earlier = earlier_text(shown, message, data.get("prompt_id"))
    report, answer = ask(message, earlier, prompt, project)
    entry = {"event": "stop", "session": data.get("session_id"), "reporter_ms": answer.get("duration_ms") or 0,
             "cost": answer.get("total_cost_usd"), "earlier_chars": len(earlier),
             "decision": "declined" if report["declined"] else "not_declined"}
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
    except BaseException as e:  # any error: log it, print nothing, never block the stop, a call or the text
        log(project, event=data.get("hook_event_name"), session=data.get("session_id"), decision="error",
            reason=clean(repr(e), 200))
    sys.exit(0)
