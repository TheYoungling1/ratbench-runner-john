#!/usr/bin/env python3
"""Local reverse proxy for the Claude Code CLI: forces `thinking: {"type": "disabled"}` on every
/messages request before relaying it to the real Anthropic-compatible upstream.

Why this exists: the CLI's own MAX_THINKING_TOKENS=0 knob only reaches the wire when --model is a
Claude-branded string (sonnet, claude-sonnet-5, ...). For a bare/provider-qualified slug like
deepseek-v4-flash-0731 or deepseek/deepseek-v4-flash-0731 the CLI sends NO `thinking` field at all
(verified live: same behaviour on OpenRouter and Bailian), so the endpoint's reasoning-on default
applies no matter what env var is set. This proxy is the workaround: rewrite the field in transit,
since the CLI itself won't.

ponytail: rewrites only the `thinking` field, no cert pinning or arbitrary header rewriting, and
the response is streamed through unbuffered but not re-chunked (each connection is closed after
one request/response — simplest thing that is still correct for the CLI's one-shot streaming
call). Upgrade path: drop this the day the CLI lets you force `thinking:disabled` for a
non-Claude model directly.

Usage:
    PROXY_UPSTREAM_HOST=openrouter.ai PROXY_PROVIDER=deepseek \\
        python3 tools/anthropic_thinking_proxy.py      # PROXY_PROVIDER: OpenRouter only, optional
    # then point the CLI at:
    ANTHROPIC_BASE_URL=http://host.docker.internal:8931/apps/anthropic   # from inside a container
    ANTHROPIC_BASE_URL=http://127.0.0.1:8931/apps/anthropic              # from the host
"""
import http.client
import http.server
import json
import os
import ssl
import sys

LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "8931"))


def _providers_from_env(value: str) -> list:
    """PROXY_PROVIDER, e.g. "deepseek" or "deepinfra/fp8,baidu/fp8" -> ["deepinfra/fp8", ...]."""
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _force_thinking_disabled(body: bytes, providers: list | None = None) -> bytes:
    """Rewrite a /messages request body to carry `thinking: {"type": "disabled"}`, plus OpenRouter's
    `provider` pin when `providers` is given — the CLI cannot send that field itself, and OpenRouter
    honours it on the Anthropic endpoint (verified 2026-11-10: an unknown provider filters out every
    endpoint). No fallbacks, so a run never silently moves to a provider outside the list.

    Returns `body` UNCHANGED if it doesn't parse as the expected JSON object — a proxy must never
    turn a request it doesn't understand into a corrupted one."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or "model" not in payload:
        return body
    payload["thinking"] = {"type": "disabled"}
    if providers:
        payload["provider"] = {"order": list(providers), "allow_fallbacks": False}
    return json.dumps(payload).encode()


def _make_handler(upstream_host: str, providers: list | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self._relay()

        def do_GET(self):
            self._relay()

        def do_HEAD(self):
            self._relay()

        def _relay(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            if "/messages" in self.path:
                body = _force_thinking_disabled(body, providers)

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection")}
            headers["Content-Length"] = str(len(body))
            headers["Host"] = upstream_host
            headers["Connection"] = "close"

            conn = http.client.HTTPSConnection(upstream_host, timeout=600,
                                               context=ssl.create_default_context())
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            finally:
                conn.close()
            self.close_connection = True

        def log_message(self, fmt, *args):
            sys.stderr.write("[thinking-proxy] " + (fmt % args) + "\n")

    return Handler


def demo():
    """ponytail self-check: the field-injection logic, no network involved."""
    out = _force_thinking_disabled(json.dumps({"model": "deepseek-v4-flash-0731",
                                               "messages": []}).encode())
    assert json.loads(out)["thinking"] == {"type": "disabled"}
    assert _force_thinking_disabled(b"not json") == b"not json"
    assert _force_thinking_disabled(b'{"no_model": true}') == b'{"no_model": true}'
    assert "provider" not in json.loads(out)          # no pin unless one is configured
    pinned = json.loads(_force_thinking_disabled(
        json.dumps({"model": "m", "messages": []}).encode(), providers=["deepseek", "x/fp8"]))
    assert pinned["provider"] == {"order": ["deepseek", "x/fp8"], "allow_fallbacks": False}
    assert pinned["thinking"] == {"type": "disabled"}
    assert _providers_from_env("deepseek, x/fp8 ,") == ["deepseek", "x/fp8"]
    assert _providers_from_env("") == []
    print("ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        demo()
        sys.exit(0)
    UPSTREAM_HOST = os.environ["PROXY_UPSTREAM_HOST"]
    PROVIDERS = _providers_from_env(os.environ.get("PROXY_PROVIDER", ""))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT),
                                             _make_handler(UPSTREAM_HOST, PROVIDERS))
    print(f"[thinking-proxy] :{LISTEN_PORT} -> https://{UPSTREAM_HOST} "
          f"(forcing thinking:disabled on /messages; provider pin: {PROVIDERS or 'none'})",
          file=sys.stderr)
    server.serve_forever()
