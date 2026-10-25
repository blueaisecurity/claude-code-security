#!/usr/bin/env python3
"""A fake SIEM webhook for tests. Listens on 127.0.0.1 only, random port.

As a module:  r = fake_receiver.start(fail_first=0)
              r.url, r.bodies (accepted bodies), r.requests (everything seen), r.stop()
              fail_first=N answers the first N POSTs with 500, then accepts.
As a script:  python fake_receiver.py   prints its URL and every body it gets.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        server = self.server
        server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if server.fail_first > 0:
            server.fail_first -= 1
            self.send_response(500)
            self.end_headers()
            return
        server.bodies.append(body)
        if server.echo:
            print(body.decode("utf-8", "replace"), flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # keep test output quiet
        pass


class Receiver(HTTPServer):
    def __init__(self, fail_first=0, echo=False):
        super().__init__(("127.0.0.1", 0), Handler)
        self.fail_first = fail_first
        self.echo = echo
        self.bodies = []
        self.requests = []
        self.url = "http://127.0.0.1:%d/hook" % self.server_address[1]
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def stop(self):
        self.shutdown()
        self.server_close()


def start(fail_first=0):
    receiver = Receiver(fail_first)
    receiver.thread.start()
    return receiver


if __name__ == "__main__":
    receiver = Receiver(echo=True)
    print("fake receiver listening on", receiver.url, flush=True)
    try:
        receiver.serve_forever()
    except KeyboardInterrupt:
        receiver.server_close()
