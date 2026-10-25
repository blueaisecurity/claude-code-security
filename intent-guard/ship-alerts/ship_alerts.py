#!/usr/bin/env python3
"""Ship intent guard alerts to a SIEM webhook, one POST per alert line.

Reads the lines added to .claude/intent-guard/alerts.jsonl since the last run (the byte offset
is kept in alerts.jsonl.offset) and POSTs each one unchanged as JSON to ALERT_WEBHOOK_URL, with
ALERT_WEBHOOK_TOKEN as a bearer token when set. Standard library only.
Usage: python ship_alerts.py [path/to/alerts.jsonl]
"""
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_FILE = os.path.join(".claude", "intent-guard", "alerts.jsonl")
TIMEOUT = 10  # seconds per request
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: urllib would resend the POST as a GET without the alert,
    and it would carry the bearer token to the new address, even over plain http."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def checked_url(url):
    """Return the URL, or stop: plain http is only allowed to this machine."""
    if not url:
        sys.exit("ALERT_WEBHOOK_URL is not set")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        return url
    sys.exit("refusing %s://%s: use https, or http only to 127.0.0.1 or localhost" % (parts.scheme, parts.hostname))


def read_offset(state_file):
    try:
        with open(state_file) as f:
            return max(0, int(f.read().strip() or 0))
    except (OSError, ValueError):
        return 0


def save_offset(state_file, offset):
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(offset))
    os.replace(tmp, state_file)


def post(url, body, token):
    """POST one alert, trying twice. "sent", "rejected" (the receiver will never take
    this line) or "failed" (try again next run)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with OPENER.open(req, timeout=TIMEOUT) as resp:
                return "sent" if 200 <= resp.status < 300 else "failed"
        except urllib.error.HTTPError as err:
            if 400 <= err.code < 500 and err.code not in (401, 403, 408, 429):
                print("line rejected with HTTP %d" % err.code, file=sys.stderr)
                return "rejected"
            print("attempt %d failed: HTTP %d" % (attempt, err.code), file=sys.stderr)
        except (OSError, http.client.HTTPException, ValueError) as err:  # never print a ValueError: it can hold a header
            print("attempt %d failed: %s" % (attempt, type(err).__name__), file=sys.stderr)
        if attempt == 1:
            time.sleep(1)
    return "failed"


def main():
    url = checked_url(os.environ.get("ALERT_WEBHOOK_URL", "").strip())
    token = os.environ.get("ALERT_WEBHOOK_TOKEN", "").strip()
    if any(c in token for c in "\r\n"):
        sys.exit("ALERT_WEBHOOK_TOKEN contains a line break")
    alerts_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    if not os.path.exists(alerts_file):
        print("ship_alerts: no alerts file yet")
        sys.exit(0)
    state_file = alerts_file + ".offset"
    offset = read_offset(state_file)
    if os.path.getsize(alerts_file) < offset:
        offset = 0  # truncated or rotated: start over
    sent = failed = skipped = rejected = 0
    with open(alerts_file, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break  # a half written last line, pick it up next run
            try:
                if not isinstance(json.loads(line), dict):
                    raise ValueError("not a JSON object")
            except ValueError:
                skipped += 1
            else:
                result = post(url, line.strip(), token)
                if result == "failed":
                    failed += 1
                    break  # keep the offset here, so this line goes out next time
                if result == "rejected":
                    with open(alerts_file + ".rejected", "ab") as r:
                        r.write(line)
                    rejected += 1
                else:
                    sent += 1
            offset += len(line)
            save_offset(state_file, offset)
    save_offset(state_file, offset)
    print("ship_alerts: sent %d, failed %d, skipped %d, rejected %d" % (sent, failed, skipped, rejected))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
