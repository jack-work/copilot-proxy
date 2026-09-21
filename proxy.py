#!/usr/bin/env python3
"""copilot-anthropic-proxy -- speak the Anthropic Messages API to GitHub Copilot.

Copilot's chat endpoint serves POST /v1/messages in NATIVE Anthropic wire
format, including SSE streaming, thinking blocks and tool_use, and it also
serves the OpenAI shapes (/v1/responses, /v1/chat/completions). So this is not
a translating shim: it forwards the request body byte-for-byte and only

  1. swaps auth -- callers send `x-api-key`, Copilot wants
     `Authorization: Bearer <short-lived copilot token>` plus editor headers;
  2. mints and refreshes that short-lived token from a long-lived OAuth token;
  3. rewrites the model id -- callers tend to use dashes
     (claude-sonnet-4-6) where Copilot uses dots (claude-sonnet-4.6).

Every /v1/ path is forwarded, so a Copilot endpoint this file has never heard
of needs no code change here.

AUTHENTICATION. GitHub's device login is built in; there is no external
identity library and nothing to paste. `--auth` picks where the resulting
long-lived OAuth token is kept:

  keyring  (default)  the OS credential store -- Windows Credential Manager,
                      macOS Keychain, or Secret Service on Linux. `login`
                      writes it, `logout` removes it. Survives a reboot, so a
                      service can start unattended.
  memory              device login at startup, held in this process only and
                      wiped on exit. Nothing is ever written to disk.
  file                a plaintext file, mode 0600. For an unattended service
                      on a host with no unlocked keyring, which is the only
                      situation where it beats `keyring`.

MSAL IS NOT USABLE HERE and this is not an oversight: it is an Entra ID client
and rejects a GitHub authority outright (`ValueError: ... should consist of an
https url with ... e.g. https://login.microsoftonline.com/{tenant}`). The
credential in play is a GitHub App user-to-server token (`ghu_...`) issued by
GitHub's own device flow and exchanged at /copilot_internal/v2/token. Different
issuer, different protocol. The flow below is 40 lines of stdlib and needs no
dependency at all.

CONFIGURATION, highest precedence first:

  1. command-line flags
  2. environment variables
  3. a .env file (./.env, or $COPILOT_PROXY_ENV)
  4. config.json in the user config dir (see config_dir below)
  5. the defaults here

Keys: domain, account, auth, oauth_file, port, bind, dump_dir, request_log.
As environment variables: COPILOT_DOMAIN, COPILOT_ACCOUNT, COPILOT_AUTH,
COPILOT_OAUTH_FILE, PORT, BIND, PROXY_DUMP_DIR, PROXY_REQUEST_LOG.

To point this at a GitHub Enterprise tenant, set the domain to that host, e.g.
`COPILOT_DOMAIN=your-tenant.ghe.com`. Everything else is derived from it. The
device flow works against a GHE tenant: verified against microsoft.ghe.com,
which returns a device_code and a verification_uri of
https://<tenant>/login/device.

Runs on Windows and Linux (and macOS). On WSL, run it inside the distro so
containers reach it at host.docker.internal.
"""

import argparse
import base64
import importlib
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "copilot-proxy"
CLIENT_ID = "Iv1.b507a08c87ecfe98"  # Public VS Code Copilot OAuth client.

# Everything except our own endpoints is forwarded upstream, verbatim. This is
# deliberately a denylist rather than an allowlist of /v1/: Copilot's api base
# serves the Anthropic shape under /v1/ (/v1/messages) but the OpenAI-ish ones
# at the root (/models, /chat/completions, /responses), and measuring that
# beats guessing it. A path this file has never heard of reaches the upstream
# and gets the upstream's own answer, which is the honest result.
LOCAL_PATHS = ("/health", "/healthz")


def log(msg):
    sys.stderr.write("[proxy] %s\n" % msg)
    sys.stderr.flush()


def config_dir():
    """Per-user config directory. %APPDATA% on Windows, XDG elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming"
        )
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, APP)


def state_dir():
    """Per-user state directory, for the request log."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "state"
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
AUTH = setting("auth", "COPILOT_AUTH", "keyring")
OAUTH_FILE = setting("oauth_file", "COPILOT_OAUTH_FILE",
                     os.path.join(config_dir(), "oauth.txt"))
PORT = setting("port", "PORT", "8787")
# Loopback by default: this proxy holds a credential and has NO authentication
# of its own, so it must not be reachable off the machine without a decision.
# Containers that need it (Docker's host.docker.internal) require "0.0.0.0".
BIND = setting("bind", "BIND", "127.0.0.1")
# Optional debug tap; see the README before enabling. Writes full request
# bodies, including whatever prompt content you send, to disk in the clear.
DUMP_DIR = setting("dump_dir", "PROXY_DUMP_DIR", None)
# Shape-and-counts log; safe to leave on. See request_log below.
REQUEST_LOG = setting("request_log", "PROXY_REQUEST_LOG", None)

AUTH_MODES = ("keyring", "memory", "file")

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
_oauth_token = None  # Populated only in --auth memory.


class AuthError(Exception):
    pass


# --- CREDENTIAL STORE --------------------------------------------------------
# One entry point, three backings, chosen by AUTH. No fallback chain: a silent
# downgrade from the keyring to a plaintext file would be exactly the kind of
# thing nobody notices until the file leaks.

def _keyring_store(action, value=None):
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
            "(Linux needs Secret Service and a session D-Bus), or use "
            "--auth memory, or --auth file for an unattended service. "
            "Install this package with uv/uvx to include keyring."
        ) from None


def _file_store(action, value=None):
    """A plaintext file, created 0600. Chosen only by an explicit --auth file."""
    try:
        if action == "get":
            try:
                with open(OAUTH_FILE, encoding="utf-8") as f:
                    return f.read().strip() or None
            except FileNotFoundError:
                return None
        if action == "set":
            parent = os.path.dirname(OAUTH_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Create with 0600 rather than chmod afterwards: a token must never
            # exist, even momentarily, in a world-readable file.
            fd = os.open(OAUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value + "\n")
        elif action == "delete":
            try:
                os.remove(OAUTH_FILE)
            except FileNotFoundError:
                pass
    except AuthError:
        raise
    except OSError as e:
        raise AuthError("Credential file %s: %s" % (OAUTH_FILE, e.strerror)) from None


def _memory_store(action, value=None):
    """No persistence at all: the token lives in this process or nowhere."""
    global _oauth_token
    if action == "get":
        return _oauth_token
    if action == "set":
        _oauth_token = value
    elif action == "delete":
        _oauth_token = None


def stored_token(action, value=None):
    """Read, write or remove the long-lived OAuth token in the chosen store."""
    return {"keyring": _keyring_store,
            "file": _file_store,
            "memory": _memory_store}[AUTH](action, value)


# --- GITHUB OAUTH ------------------------------------------------------------

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
                                "with the same --domain/--account, or restart --auth memory.") from None
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
    """RFC 8628 device flow against DOMAIN. Blocks until the user approves."""
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
            raise AuthError("GitHub login was denied. Run login or --auth memory again to retry.")
        elif error != "authorization_pending":
            raise AuthError("GitHub device login failed. Request a new code and try again.")
    raise AuthError("GitHub device code expired. Run login or --auth memory again for a new code.")


def _oauth():
    value = stored_token("get")
    if not value:
        raise AuthError(
            "No saved login for %s on %s (--auth %s). Run login with the same "
            "--domain/--account, or use --auth memory."
            % (ACCOUNT or "<any account>", DOMAIN, AUTH)
        )
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


# --- PAYLOAD FIX-UPS ---------------------------------------------------------

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


def _flatten_document(block):
    """Return a text block for a text-ish document, or None to leave it alone."""
    src = block.get("source")
    if not isinstance(src, dict):
        return None
    if src.get("media_type") == "application/pdf":
        return None
    if src.get("type") not in ("text", "base64"):
        return None
    data = src.get("data")
    if not isinstance(data, str):
        return None
    if src.get("type") == "base64":
        try:
            data = base64.b64decode(data).decode("utf-8", "replace")
        except Exception:
            return None
    title = block.get("title") or "document"
    return {"type": "text", "text": "<%s>\n%s\n</%s>" % (title, data, title)}


def fix_document_sources(node, _depth=0):
    """Flatten text-source document blocks anywhere in the payload.

    The Copilot upstream serves documents ONLY as PDFs and refuses in two
    stages: first ``source type must be base64 or url, got "text"``, then
    ``media_type must be application/pdf, got text/plain``. So there is no text
    document on this path at all, and flattening to a text block preserves what
    the model sees while dropping only framing the upstream cannot serve.

    MUST RECURSE. App generation puts the document inside a tool_result, at
    ``messages[5].content[0].content[0]``, two levels below the message
    content. A one-level walk finds nothing and reports success while every
    turn still dies at the model call.

    Returns the number of blocks rewritten.
    """
    fixed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            fixed += fix_document_sources(value, _depth + 1)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, dict) and value.get("type") == "document":
                replacement = _flatten_document(value)
                if replacement is not None:
                    node[i] = replacement
                    fixed += 1
                    continue
            fixed += fix_document_sources(value, _depth + 1)
    return fixed


# --- REQUEST LOG -------------------------------------------------------------
# One JSON object per request, appended to REQUEST_LOG. This is the only vantage
# point that sees BOTH the body actually dispatched to the model and the usage
# the provider returns, so it is what a prompt-cache change is validated from.
# It records SHAPE and COUNTS only: never the token, never prompt text.

_reqlog_lock = threading.Lock()


def _req_facts(raw):
    """Shape of the dispatched body. Wire-agnostic: Anthropic uses `system` +
    `messages`, OpenAI Responses uses `instructions` + `input`."""
    out = {}
    try:
        b = json.loads(raw)
    except Exception:
        return out
    if not isinstance(b, dict):
        return out
    out["model"] = b.get("model")
    out["prompt_cache_key"] = b.get("prompt_cache_key")
    instr = b.get("instructions")
    if isinstance(instr, str):
        out["instr_len"] = len(instr)
    sysb = b.get("system")
    if isinstance(sysb, str):
        out["system_len"] = len(sysb)
    elif isinstance(sysb, list):
        out["system_blocks"] = len(sysb)
    for key, field in (("input", "n_input_items"), ("messages", "n_messages")):
        v = b.get(key)
        if isinstance(v, list):
            out[field] = len(v)
    text = raw.decode("utf-8", "replace")
    out["has_prompt_cache_breakpoint"] = "prompt_cache_breakpoint" in text
    out["has_cache_control"] = "cache_control" in text
    out["has_aether_sentinel"] = "AETHER_VAR" in text
    return out


_USAGE_RE = re.compile(r'"usage"\s*:\s*\{')
_INT_RE = {
    "input_tokens": re.compile(r'"input_tokens"\s*:\s*(\d+)'),
    "output_tokens": re.compile(r'"output_tokens"\s*:\s*(\d+)'),
    "cached_tokens": re.compile(r'"cached_tokens"\s*:\s*(\d+)'),
    "cache_write_tokens": re.compile(r'"cache_write_tokens"\s*:\s*(\d+)'),
    "cache_read_input_tokens": re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)'),
    "cache_creation_input_tokens": re.compile(r'"cache_creation_input_tokens"\s*:\s*(\d+)'),
}


def _usage_facts(text):
    """LAST usage object wins: an SSE stream emits several and only the terminal
    one is complete. Regex rather than a parser because the SSE body is not JSON."""
    out = {}
    last = None
    for m in _USAGE_RE.finditer(text):
        last = m.start()
    if last is None:
        return out
    seg = text[last:last + 800]
    for name, rx in _INT_RE.items():
        hit = rx.search(seg)
        if hit:
            out[name] = int(hit.group(1))
    return out


def request_log(rec):
    if not REQUEST_LOG:
        return
    try:
        with _reqlog_lock:
            parent = os.path.dirname(REQUEST_LOG)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Bound it: this runs for days behind a live stack.
            try:
                if os.path.getsize(REQUEST_LOG) > 64 * 1024 * 1024:
                    os.replace(REQUEST_LOG, REQUEST_LOG + ".1")
            except OSError:
                pass
            with open(REQUEST_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception as e:
        log("request_log failed: %s" % e)


def wire_of(path):
    if "messages" in path:
        return "anthropic"
    if "responses" in path:
        return "openai-responses"
    if "chat/completions" in path:
        return "openai-chat"
    return "other"


# --- SERVER ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _fail(self, code, message):
        payload = json.dumps(
            {"type": "error", "error": {"type": "proxy_error", "message": message}}
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _health(self):
        try:
            copilot_token()
            body = json.dumps({"ok": True, "api": _token["api"],
                               "auth": AUTH, "domain": DOMAIN,
                               "expires_in": int(_token["exp"] - time.time())}).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"ok": False, "auth": AUTH, "domain": DOMAIN,
                               "error": str(e)}).encode()
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in LOCAL_PATHS:
            return self._health()
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_DELETE(self):
        self._forward("DELETE")

    def _forward(self, method):
        """Proxy any non-local path upstream, byte-for-byte apart from fix-ups."""
        try:
            # Drain the body BEFORE any decision. Answering a request without
            # reading its body leaves those bytes in the socket, and the next
            # request on a keep-alive connection is parsed starting mid-body:
            # the client then gets 501 on a perfectly good GET.
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

            token, api_base = copilot_token()

            streaming = False
            if raw:
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError()
                    changed = False
                    # Only a payload that names a model gets a model rewrite;
                    # /v1/files and friends carry no model and must pass through.
                    if "model" in payload:
                        original = payload.get("model")
                        mapped = map_model(original, model_ids(api_base, token))
                        if mapped != original:
                            payload["model"] = mapped
                            changed = True
                            log("model %s -> %s" % (original, mapped))
                    docs = fix_document_sources(payload)
                    if docs:
                        changed = True
                        log("rewrote %d text-source document block(s)" % docs)
                    if changed:
                        raw = json.dumps(payload).encode()
                    streaming = bool(payload.get("stream"))
                except Exception:
                    streaming = b'"stream":true' in raw.replace(b" ", b"")

            # Optional debug tap: dump the exact body sent upstream. This is the
            # only place the dispatched prompt can be read, since TLS makes a
            # packet capture useless. Off unless dump_dir is set, because it
            # writes prompt content to disk in the clear.
            if DUMP_DIR and raw:
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
            if raw:
                headers.setdefault("Content-Type", "application/json")

            req = urllib.request.Request(api_base + self.path, data=raw or None,
                                         headers=headers, method=method)

            started = time.time()
            rec = {"ts": started, "path": self.path, "method": method,
                   "wire": wire_of(self.path)}
            rec.update(_req_facts(raw))

            try:
                upstream = urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    with _lock:
                        if _token["value"] == token:
                            _token["value"] = None  # Renew next request; never replay.
                body = e.read()
                log("upstream %s on %s: %s" % (e.code, self.path, body[:200]))
                rec["status"] = e.code
                rec["duration_ms"] = int((time.time() - started) * 1000)
                request_log(rec)
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(upstream.status)
            ctype = upstream.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            rec["status"] = upstream.status
            if streaming or "event-stream" in ctype:
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                acc = bytearray()
                while True:
                    chunk = upstream.read(1024)
                    if not chunk:
                        break
                    acc += chunk
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                rec["stream"] = True
                rec.update(_usage_facts(acc.decode("utf-8", "replace")))
            else:
                body = upstream.read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                rec["stream"] = False
                rec.update(_usage_facts(body.decode("utf-8", "replace")))
            rec["duration_ms"] = int((time.time() - started) * 1000)
            request_log(rec)
        except Exception as e:
            log("handler error: %r" % e)
            try:
                self._fail(503 if isinstance(e, AuthError) else 502, "proxy failure: %s" % e)
            except Exception:
                pass


def main(argv=None):
    global DOMAIN, ACCOUNT, AUTH, OAUTH_FILE, DUMP_DIR, REQUEST_LOG
    parser = argparse.ArgumentParser(
        prog=APP, description="Anthropic and OpenAI APIs via GitHub Copilot.",
        epilog="Quick start without saving anything: %s --auth memory" % APP,
    )
    parser.add_argument("command", nargs="?", choices=("serve", "login", "logout"), default="serve",
                        help="serve (default), save a GitHub login, or remove a saved login")
    parser.add_argument("--auth", choices=AUTH_MODES, default=AUTH,
                        help="where the OAuth token is kept (default: %s)" % AUTH)
    parser.add_argument("--oauth-file", default=OAUTH_FILE,
                        help="credential path for --auth file (default: %(default)s)")
    parser.add_argument("--domain", default=DOMAIN, help="GitHub host (default: %(default)s)")
    parser.add_argument("--account", default=ACCOUNT,
                        help="GitHub username; required for --auth keyring")
    parser.add_argument("--bind", default=BIND, help="listen address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=PORT, help="listen port (default: %(default)s)")
    parser.add_argument("--dump-dir", default=DUMP_DIR,
                        help="debug only: write each dispatched body here, in the clear")
    parser.add_argument("--request-log", default=REQUEST_LOG,
                        help="append one shape-and-counts JSON record per request")
    args = parser.parse_args(argv)

    if args.auth == "memory" and args.command != "serve":
        parser.error("--auth memory is for serve: login/logout manage persistent credentials")
    if args.auth == "keyring" and not args.account:
        parser.error("--account (or COPILOT_ACCOUNT) is required for --auth keyring; "
                     "alternatively use --auth memory or --auth file")

    DOMAIN = args.domain.lower()
    ACCOUNT = args.account.lower() if args.account else None
    AUTH, OAUTH_FILE = args.auth, args.oauth_file
    DUMP_DIR, REQUEST_LOG = args.dump_dir, args.request_log

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
            log("Saved login for %s on %s (--auth %s)." % (ACCOUNT, DOMAIN, AUTH))
            return 0
        if AUTH == "memory":
            stored_token("set", device_login())
        if DUMP_DIR:
            log("REQUEST DUMPING IS ON -> %s (prompt content written in the clear)" % DUMP_DIR)
        _, api = copilot_token()
        log("upstream %s" % api)
        if args.bind not in ("127.0.0.1", "localhost", "::1"):
            log("NOTE: bound to %s -- reachable off this machine, and this proxy has no auth." % args.bind)
        with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
            log("listening on http://%s:%d  (forwarding everything but %s)"
                % (server.server_address[0], server.server_address[1], ", ".join(LOCAL_PATHS)))
            server.serve_forever()
    except (AuthError, OSError) as e:
        log(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        # Unconditional: a no-op outside --auth memory, and the one place that
        # guarantees an in-process credential never outlives the server.
        _memory_store("delete")
        _token.update(value=None, exp=0.0, api=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
