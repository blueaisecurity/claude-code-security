# ship_alerts

A small forwarder that sends intent guard alerts to your SIEM. One Python file,
standard library only, runs on Windows and Linux.

The intent guard hook in Claude Code appends one JSON object per line to
`.claude/intent-guard/alerts.jsonl` in the project folder. `ship_alerts.py`
reads the lines added since its last run and POSTs each one, unchanged, to a
webhook. It does not care which fields the line has, so the hook can change
and the forwarder keeps working.

## What it does

1. Reads `alerts.jsonl` from the byte offset saved in `alerts.jsonl.offset`
   (a small state file next to the alerts file). If the alerts file is smaller
   than the saved offset, it was truncated or rotated, so it starts from 0.
2. Checks each line is a JSON object. Bad lines are skipped and counted.
3. POSTs each good line to `ALERT_WEBHOOK_URL` with
   `Content-Type: application/json`, a 10 second timeout and one retry.
   When `ALERT_WEBHOOK_TOKEN` is set, it adds `Authorization: Bearer <token>`.
4. Stops at the first line that still fails after the retry and keeps the
   offset there, so that line is sent on the next run. A line the receiver
   rejects for good (an HTTP 4xx other than 401, 403, 408 and 429) is set
   aside in `alerts.jsonl.rejected`, so it can't block the lines after it.
   Redirects are treated as errors, so the body is never lost and the token
   never moves to another address. The offset is saved after each line, so a
   crash sends at most one line twice: delivery is at least once.
5. Prints one line, for example `ship_alerts: sent 5, failed 0, skipped 1, rejected 0`,
   and exits with 1 when something failed.

The URL must be `https`. Plain `http` is allowed only to `127.0.0.1` or
`localhost`, so a typo cannot send alerts in clear text across the network.

## Setup

```
python ship_alerts.py                                  # uses .claude/intent-guard/alerts.jsonl in the current folder
python ship_alerts.py C:\work\myproject\.claude\intent-guard\alerts.jsonl
```

Set the two variables once. On Windows, for your user:

```
setx ALERT_WEBHOOK_URL https://siem.example.com/hooks/intent-guard
setx ALERT_WEBHOOK_TOKEN your-token
```

## Run it every minute

Windows Task Scheduler, one line:

```
schtasks /Create /SC MINUTE /MO 1 /TN "Ship intent guard alerts" /TR "python C:\tools\ship_alerts.py C:\work\myproject\.claude\intent-guard\alerts.jsonl"
```

Linux or macOS, add to `crontab -e` (cron reads the variable lines at the top):

```
ALERT_WEBHOOK_URL=https://siem.example.com/hooks/intent-guard
ALERT_WEBHOOK_TOKEN=your-token
* * * * * python3 /opt/ship_alerts/ship_alerts.py /home/me/project/.claude/intent-guard/alerts.jsonl >> /var/log/ship_alerts.log 2>&1
```

One task per project, or point the hook of every project at one shared file.

## Two SIEM rules to start with

1. Alert on every event where `kind` is `model_declined`. The model refused
   on its own, before the guard had to step in. That is rare and it means
   something pushed the session away from what the user asked for. Look at
   `reason`, `attempted` and `source` each time.
2. Page the on call person when one `project` produces 3 or more events of
   any kind within one hour. One blocked action can be noise. Three in an
   hour from the same project is a session being pushed again and again, or
   a prompt injection that keeps retrying. Group by `project`, count over a
   sliding one hour window, page at 3.

## Test

```
python -I test_ship_alerts.py
```

The tests start a fake receiver (`fake_receiver.py`) on 127.0.0.1 and run the
forwarder against it. They cover: five lines sent once, a second run sends
nothing, a bad line is skipped and counted, a receiver that fails once then
works gets the line on the retry, a receiver that is down keeps the line for
the next run, a truncated file restarts from 0, and plain http to a non local
host is refused. To watch the forwarder by hand, run `python fake_receiver.py`
in one window, and in another set `ALERT_WEBHOOK_URL` to the URL it prints and
run `python ship_alerts.py`.
