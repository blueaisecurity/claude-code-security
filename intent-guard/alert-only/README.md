# Refusal alert: the alert-only version of the intent guard

A small early warning system for Claude Code. It has no guard and blocks nothing.
After every turn it checks whether the model's text in that turn says that the model
declined to follow an instruction it found in content it read: a file, a web page,
an email, a build log, a tool result. When yes, it writes one JSON line to
`.claude/intent-guard/alerts.jsonl`. A SIEM can tail that file. Without it, in a
headless or cloud run nobody reads the answer, and nobody learns that someone tried.

## Files

| File | What it is |
|---|---|
| `.claude/settings.json` | The hook entries: `Stop` runs the script after every turn; `PreToolUse` runs it on `Read`, `Grep`, `Glob`, `WebFetch` and `WebSearch` only to note what the model reads; `MessageDisplay` runs it on the text Claude shows, only to keep that text for the `Stop` hook; `StopFailure` runs it when an API error ends a turn, only to delete that turn's files |
| `.claude/hooks/refusal_alert.py` | The script, Python 3 standard library only, 367 lines |
| `.claude/agents/intent-guard-reporter.md` | The reporter: a subagent with no tools that reads Claude's text from the turn and answers one question with one JSON line. Byte for byte the same file as in the full intent guard |

The `Stop` hook input holds only Claude's final message. In our tests a model sometimes
named the planted instruction in an earlier message of the turn and ended with the fix
alone, so the reporter missed it. The `MessageDisplay` entry is there for that case.
Claude Code runs it for the text of each message Claude shows. The script keeps that text in
`.claude/intent-guard/shown-<session>.jsonl`, at most 16,000 characters, and prints
nothing, so you see the text as it is. When a turn shows more, each message keeps its start
and its end, so a long final answer can't push out a refusal written before it. The `Stop` hook gives the reporter that
earlier text as a second block next to the final answer, and then deletes the file.
Remove the entry to send only the final answer.

## Setup

1. Copy the `.claude` folder into your project.
2. Make sure `python` and `claude` are on PATH. On Windows, when `claude` on PATH is npm's
   `claude.cmd`, the script finds `claude.exe` next to it by itself. If it cannot, set
   `REFUSAL_ALERT_CLAUDE` to the full path of `claude.exe`.
3. Run Claude Code in the project. Nothing changes on screen. After a turn in which the
   model says it declined something it found in content, `alerts.jsonl` gets a line.

The reporter runs as `claude -p` with no tools, no hooks, no MCP servers and no memory,
for one turn, with the Sonnet model. It costs about 0.007 USD and 3 seconds per turn.

## The alert line

```
{"time": "2026-10-24T16:05:34+00:00", "session": "bf88cd19-...", "project": "C:\\work\\shop",
 "model": null, "kind": "model_declined", "requested": [],
 "source": "CI log file content",
 "sources_read": [{"tool": "Read", "source": "C:\\work\\shop\\logs\\ci-run-4812.txt"}],
 "attempted": "tried to get the agent to read AWS credentials and secretly copy them into a report file",
 "reason": "the agent identified it as a prompt-injection attempt and refused to do it",
 "stopped_by": "model"}
```

- `requested` is always empty here. This hook alone keeps no record of what the user
  asked, and the `Stop` hook input carries no prompt text. The full intent guard saves
  every prompt in its `UserPromptSubmit` hook and fills this field.
- `source` is the reporter's own words about where the instruction was.
- `sources_read` is the exact list of what the model read or fetched in that turn, noted
  by the `PreToolUse` entry: the file path for `Read`, the pattern and path for `Grep` and
  `Glob`, the address for `WebFetch`, the query for `WebSearch`. Each entry is cleaned (one
  line, no angle brackets or control characters) and cut to 300 characters, at most 50
  entries. Remove the `PreToolUse` entry from `settings.json` if you do not want this list;
  then `sources_read` stays empty. MCP tools and shell commands are not noted here; the
  full intent guard notes them too.
- `model` is filled only when the hook input carries a model name, which today's `Stop`
  input does not.
- Every text in the line is cleaned the same way, so the file cannot carry instructions
  to whoever reads it next.

## What it does not do

- It reports only what the model says in its text: the final answer and, with the
  `MessageDisplay` entry, the earlier text of the turn. A model that follows the
  instruction quietly, or declines without saying so, gives no alert. The full intent
  guard checks the risky calls themselves.
- The reporter is a model too. It can misread an answer. In the lab replay of 32 saved
  answers it matched every fixed expectation (17 of 17), as post #3 reports.
- It never blocks anything. The script prints nothing on every path, so Claude Code
  never keeps the model working because of it. Errors go to `.claude/refusal-alert.log`.
- The sources list and the kept text of a turn the user interrupts stay for the next
  turn, because Claude Code runs no `Stop` hook after an interrupt. For the kept text, the
  script then uses only the text of the current prompt when the hook inputs carry a
  `prompt_id`. Every `Stop` also deletes the turn files of any session that are more than
  a day old. After an API error, `StopFailure` runs instead of `Stop` and deletes the
  turn's files.
- The files in `.claude/intent-guard/` hold the paths the model read and the text Claude
  showed, which can include a key that Claude repeated. Add `.claude/intent-guard/` and
  `.claude/refusal-alert.log*` to your `.gitignore`.

## Test

`python -I tests/test_refusal_alert.py` in the `intent-guard` folder runs 13 tests with a stub reporter and
no model calls.
