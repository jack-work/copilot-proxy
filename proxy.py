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

GitHub device login is built in. OAuth credentials live in the OS keyring
(the only dependency), or solely in this process with --memory.

CONFIGURATION, highest precedence first:

  1. command-line flags
  2. environment variables
  3. a .env file (./.env, or $COPILOT_PROXY_ENV)
  4. config.json in $XDG_CONFIG_HOME/copilot-proxy (default ~/.config/...)
  5. the defaults below

Keys: domain, account, port, bind, dump_dir. As environment variables they
are COPILOT_DOMAIN, COPILOT_ACCOUNT, PORT, BIND, PROXY_DUMP_DIR.

To point this at a GitHub Enterprise tenant, set the domain to that host, e.g.
`COPILOT_DOMAIN=your-tenant.ghe.com`. Everything else is derived from it.
"""

import argparse
import importlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "copilot-proxy"
CLIENT_ID = "Iv1.b507a08c87ecfe98"  # Public VS Code Copilot OAuth client.


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
ACCOUNT = setting("account", "COPILOT_ACCOUNT")
MEMORY = False
_oauth_token = None
PORT = setting("port", "PORT", "8787")
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


class AuthError(Exception):
    pass


def stored_token(action, value=None):
    """Use only native OS stores, never an auto-selected plaintext backend."""
    module, name = {
        "win32": ("Windows", "WinVaultKeyring"),
        "darwin": ("macOS", "Keyring"),
    }.get(sys.platform, ("SecretService", "Keyring"))
    try:
        store = getattr(importlib.import_module("keyring.backends." + module), name)()
        service = APP + ":" + DOMAIN
        if action == "get":
            return store.get_password(service, ACCOUNT)
        if action == "set":
            store.set_password(service, ACCOUNT, value)
        elif action == "delete" and store.get_password(service, ACCOUNT) is not None:
            store.delete_password(service, ACCOUNT)
    except Exception:
        raise AuthError(
            "OS credential store unavailable or locked. Unlock your keyring "
            "(Linux needs Secret Service and a session D-Bus), or use --memory. "
            "Install this package with uv/uvx to include keyring."
        ) from None


def github(path, token=None, data=None, timeout=30):
    """GitHub JSON requests. Do not expose response bodies or credentials in errors."""
    headers = dict(EDITOR_HEADERS, Accept="application/json")
    if token:
        headers["Authorization"] = "token " + token
    host = "api." + DOMAIN if token else DOMAIN
    raw = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request("https://" + host + path, data=raw, headers=headers)
    try:
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 400 and data is not None:
                response = e  # OAuth errors may be JSON in a 400 response.
            elif e.code == 401:
                raise AuthError("GitHub rejected the OAuth credential. Run login again "
                                "with the same --domain/--account, or restart --memory.") from None
            elif e.code == 403:
                raise AuthError("GitHub denied access. Check Copilot access, tenant/SSO "
                                "policy and the selected --domain/--account.") from None
            else:
                raise AuthError("GitHub request failed (HTTP %d). Try again." % e.code) from None
        with response:
            body = json.load(response)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except (OSError, ValueError):
        raise AuthError("GitHub request failed: network error or invalid JSON. Try again.") from None


def device_login():
    body = github("/login/device/code", data={"client_id": CLIENT_ID, "scope": "read:user"})
    try:
        code = body["device_code"]
        interval = max(1, float(body.get("interval", 5)))
        deadline = time.monotonic() + float(body["expires_in"])
        log("Approve at %s -- code: %s" % (body["verification_uri"], body["user_code"]))
    except (KeyError, TypeError, ValueError):
        raise AuthError("Could not start GitHub device login. Check the domain and try again.") from None
    while time.monotonic() + interval < deadline:
        time.sleep(interval)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        body = github("/login/oauth/access_token", data={
            "client_id": CLIENT_ID, "device_code": code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=min(30, remaining))
        if body.get("access_token"):
            token = body["access_token"]
            if ACCOUNT and github("/user", token=token).get("login", "").lower() != ACCOUNT:
                raise AuthError("Approved account does not match --account; nothing saved. Try again.")
            return token
        error = body.get("error")
        if error == "slow_down":
            interval = max(interval + 5, float(body.get("interval", 0)))
        elif error == "expired_token":
            break
        elif error == "access_denied":
            raise AuthError("GitHub login was denied. Run login or --memory again to retry.")
        elif error != "authorization_pending":
            raise AuthError("GitHub device login failed. Request a new code and try again.")
    raise AuthError("GitHub device code expired. Run login or --memory again for a new code.")


def _oauth():
    value = _oauth_token if MEMORY else stored_token("get")
    if not value:
        raise AuthError("No saved login for %s on %s. Run login with the same "
                        "--domain/--account, or use --memory." % (ACCOUNT, DOMAIN))
    return value


def copilot_token():
    """Return (token, api_base), minting a fresh one when within 5 min of expiry."""
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 300:
            return _token["value"], _token["api"]
        body = github("/copilot_internal/v2/token", token=_oauth())
        try:
            value, api = body["token"], body["endpoints"]["api"]
            exp = float(body.get("expires_at", time.time() + 1500))
            if not value or not api.startswith("https://") or exp <= time.time():
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AuthError("GitHub returned an invalid Copilot token response. Check Copilot access.") from None
        _token.update(value=value, exp=exp, api=api)
        log("minted copilot token, expires in %ds" % int(exp - time.time()))
        return value, api


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
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            token, api_base = copilot_token()

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
                if e.code == 401:
                    with _lock:
                        if _token["value"] == token:
                            _token["value"] = None  # Renew on the next request; never replay a POST.
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
                self._fail(503 if isinstance(e, AuthError) else 502, "proxy failure: %s" % e)
            except Exception:
                pass


def main(argv=None):
    global DOMAIN, ACCOUNT, MEMORY, _oauth_token
    parser = argparse.ArgumentParser(
        prog=APP, description="Anthropic Messages API via GitHub Copilot.",
        epilog="Quick start without saved credentials: copilot-proxy --memory",
    )
    parser.add_argument("command", nargs="?", choices=("serve", "login", "logout"), default="serve",
                        help="serve (default), save a GitHub login, or remove a saved login")
    parser.add_argument("--memory", action="store_true", help="serve with device login; never read/write the keyring")
    parser.add_argument("--domain", default=DOMAIN, help="GitHub host (default: github.com)")
    parser.add_argument("--account", default=ACCOUNT, help="GitHub username; required unless --memory")
    parser.add_argument("--bind", default=BIND, help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=PORT, help="listen port (default: 8787)")
    args = parser.parse_args(argv)
    if args.memory and args.command != "serve":
        parser.error("--memory is for serve: login/logout manage persistent credentials")
    if not args.memory and not args.account:
        parser.error("--account (or COPILOT_ACCOUNT) is required for keyring storage; alternatively use --memory")
    DOMAIN, ACCOUNT, MEMORY = args.domain.lower(), args.account.lower() if args.account else None, args.memory
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", DOMAIN):
        parser.error("--domain must be a hostname, without a scheme, port or path")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        if args.command == "logout":
            stored_token("delete")
            log("Saved credential removed. Stop running proxies to discard their cached tokens.")
            return 0
        if args.command == "login":
            stored_token("get")  # Check store availability before asking for approval.
            stored_token("set", device_login())
            log("Saved login for %s on %s in the OS keyring." % (ACCOUNT, DOMAIN))
            return 0
        if MEMORY:
            _oauth_token = device_login()
        if DUMP_DIR:
            log("REQUEST DUMPING IS ON -> %s (prompt content written in the clear)" % DUMP_DIR)
        _, api = copilot_token()
        log("upstream %s" % api)
        if args.bind not in ("127.0.0.1", "localhost", "::1"):
            log("NOTE: bound to %s -- reachable off this machine, and this proxy has no auth." % args.bind)
        with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
            log("listening on http://%s:%d  (POST /v1/messages)" % server.server_address[:2])
            server.serve_forever()
    except (AuthError, OSError) as e:
        log(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        _oauth_token = None
        _token.update(value=None, exp=0.0, api=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
