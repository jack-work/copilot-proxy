#!/usr/bin/env python3
"""copilot-anthropic-proxy -- speak the Anthropic Messages API to GitHub Copilot.

Copilot's chat endpoint serves POST /v1/messages in NATIVE Anthropic wire
format, including SSE streaming, thinking blocks and tool_use. So this is not a
translating shim: it forwards the request body byte-for-byte and only

  1. swaps auth -- callers send `x-api-key`, Copilot wants
     `Authorization: Bearer <short-lived copilot token>` plus editor headers;
  2. mints and refreshes that short-lived token from a long-lived OAuth token;
  3. rewrites the model id -- callers tend to use dashes
     (claude-sonnet-4-6) where Copilot uses dots (claude-sonnet-4.6).

That is the whole program. It is standard-library Python with no dependencies.

CONFIGURATION, highest precedence first:

  1. environment variables
  2. a .env file (./.env, or $COPILOT_PROXY_ENV)
  3. config.json in $XDG_CONFIG_HOME/copilot-proxy (default ~/.config/...)
  4. the defaults below

Keys: domain, oauth_file, port, bind, dump_dir. As environment variables they
are COPILOT_DOMAIN, COPILOT_OAUTH_FILE, PORT, BIND, PROXY_DUMP_DIR.

To point this at a GitHub Enterprise tenant, set the domain to that host, e.g.
`COPILOT_DOMAIN=your-tenant.ghe.com`. Everything else is derived from it.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "copilot-proxy"


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, APP)


def _load_config_file():
    """config.json from the standard config dir. Missing or invalid is fine."""
    path = os.path.join(config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log("ignoring %s: %s" % (path, e))
        return {}


def _load_env_file():
    """A .env of KEY=value lines. Quotes stripped, # comments ignored."""
    path = os.environ.get("COPILOT_PROXY_ENV") or ".env"
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log("ignoring %s: %s" % (path, e))
    return out


def log(msg):
    sys.stderr.write("[proxy] %s\n" % msg)
    sys.stderr.flush()


_FILE_CFG = _load_config_file()
_ENV_FILE = _load_env_file()


def setting(key, env_var, default=None):
    """env var > .env > config.json > default."""
    if env_var in os.environ:
        return os.environ[env_var]
    if env_var in _ENV_FILE:
        return _ENV_FILE[env_var]
    if key in _FILE_CFG:
        return _FILE_CFG[key]
    return default


# github.com is the default. Set the domain to a GitHub Enterprise host to use
# that tenant instead; the API and OAuth URLs are derived from it.
DOMAIN = setting("domain", "COPILOT_DOMAIN", "github.com")
OAUTH_FILE = setting(
    "oauth_file", "COPILOT_OAUTH_FILE", os.path.join(config_dir(), "oauth.txt")
)
PORT = int(setting("port", "PORT", "8787"))
# Loopback by default: this proxy holds a credential and has NO authentication
# of its own, so it must not be reachable off the machine without a decision.
# Containers that need it (Docker's host.docker.internal) require "0.0.0.0".
BIND = setting("bind", "BIND", "127.0.0.1")
# Optional debug tap; see the README before enabling. Writes full request
# bodies, including whatever prompt content you send, to disk in the clear.
DUMP_DIR = setting("dump_dir", "PROXY_DUMP_DIR", None)

EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.99.0",
    "Editor-Plugin-Version": "copilot-chat/0.26.0",
    "User-Agent": "GitHubCopilotChat/0.26.0",
    "Copilot-Integration-Id": "vscode-chat",
}

# Headers we must NOT forward from the caller: auth (we replace it), hop-by-hop,
# and Host/Content-Length (recomputed by urllib).
STRIP = {
    "x-api-key", "authorization", "host", "content-length", "connection",
    "accept-encoding", "user-agent", "editor-version", "editor-plugin-version",
    "copilot-integration-id",
}

_lock = threading.Lock()
_token = {"value": None, "exp": 0.0, "api": None}
_models_cache = {"ids": None, "at": 0.0}


def _oauth():
    try:
        with open(OAUTH_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise SystemExit(
            "No OAuth token at %s.\nSee the README: obtain one with the device "
            "flow, then write it there with mode 0600." % OAUTH_FILE
        )


def copilot_token():
    """Return (token, api_base), minting a fresh one when within 5 min of expiry."""
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 300:
            return _token["value"], _token["api"]
        h = dict(EDITOR_HEADERS)
        h["Authorization"] = "token " + _oauth()
        req = urllib.request.Request(
            "https://api.%s/copilot_internal/v2/token" % DOMAIN, headers=h
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
        _token["value"] = body["token"]
        _token["exp"] = float(body.get("expires_at", time.time() + 1500))
        _token["api"] = body["endpoints"]["api"]
        log("minted copilot token, expires in %ds" % int(_token["exp"] - time.time()))
        return _token["value"], _token["api"]


def model_ids(api_base, token):
    """Cached /models id list, refreshed every 10 minutes."""
    if _models_cache["ids"] and time.time() - _models_cache["at"] < 600:
        return _models_cache["ids"]
    h = dict(EDITOR_HEADERS)
    h["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(api_base + "/models", headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        _models_cache["ids"] = ids
        _models_cache["at"] = time.time()
    except Exception as e:
        log("model list failed (%s); using last known" % e)
        ids = _models_cache["ids"] or []
    return ids


def map_model(name, ids):
    """claude-sonnet-4-6 -> claude-sonnet-4.6. Exact match always wins."""
    if not name or name in ids:
        return name
    # Convert the trailing -<digits>-<digits> version into dotted form.
    cand = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", name)
    if cand in ids:
        return cand
    cand2 = re.sub(r"-(\d+)$", r".\1", name)
    if cand2 in ids:
        return cand2
    return name


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _fail(self, code, msg):
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._fail(404, "not found")

    def do_POST(self):
        try:
            token, api_base = copilot_token()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

            streaming = False
            try:
                payload = json.loads(raw or b"{}")
                original = payload.get("model")
                mapped = map_model(original, model_ids(api_base, token))
                if mapped != original:
                    payload["model"] = mapped
                    raw = json.dumps(payload).encode()
                    log("model %s -> %s" % (original, mapped))
                streaming = bool(payload.get("stream"))
            except Exception:
                streaming = b'"stream":true' in raw.replace(b" ", b"")

            # Optional debug tap: dump the exact body sent upstream. This is the
            # only place the dispatched prompt can be read, since TLS makes a
            # packet capture useless. Off unless dump_dir is set, because it
            # writes prompt content to disk in the clear.
            if DUMP_DIR:
                try:
                    os.makedirs(DUMP_DIR, exist_ok=True)
                    fn = os.path.join(DUMP_DIR, "req-%d.json" % int(time.time() * 1000))
                    with open(fn, "wb") as f:
                        f.write(raw)
                except Exception as e:
                    log("dump failed: %s" % e)

            headers = {k: v for k, v in self.headers.items() if k.lower() not in STRIP}
            headers.update(EDITOR_HEADERS)
            headers["Authorization"] = "Bearer " + token
            headers.setdefault("Content-Type", "application/json")

            url = api_base + self.path
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")

            try:
                upstream = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                body = e.read()
                log("upstream %s on %s: %s" % (e.code, self.path, body[:200]))
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(upstream.status)
            ctype = upstream.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            if streaming or "event-stream" in ctype:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = upstream.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                body = upstream.read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            log("handler error: %r" % e)
            try:
                self._fail(502, "proxy failure: %s" % e)
            except Exception:
                pass


def main():
    log("domain %s | oauth %s" % (DOMAIN, OAUTH_FILE))
    if DUMP_DIR:
        log("REQUEST DUMPING IS ON -> %s (prompt content written in the clear)" % DUMP_DIR)
    token, api = copilot_token()
    log("upstream %s" % api)
    log("listening on http://%s:%d  (POST /v1/messages)" % (BIND, PORT))
    if BIND not in ("127.0.0.1", "localhost", "::1"):
        log("NOTE: bound to %s -- reachable off this machine, and this proxy "
            "has no auth of its own." % BIND)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
