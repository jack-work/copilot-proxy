# copilot-anthropic-proxy

A small local proxy: speak the **Anthropic Messages API** — and the OpenAI
shapes Copilot also serves — to **GitHub Copilot**. One Python module;
`keyring` is its only direct dependency, and only for `--auth keyring`.
Python 3.10+, Windows and Linux (and macOS).

## Run without installing globally

With [uv](https://docs.astral.sh/uv/), from this checkout, on Linux, Windows or macOS:

```sh
uvx --from . copilot-proxy --auth memory
```

Open the printed GitHub URL, enter the code and approve. The proxy then listens
on **http://127.0.0.1:8787**. Both credentials stay in this process; restarting
requires approval again. No token file, keyring access or browser automation.
`uvx` caches the package/dependencies, **not your credentials**.

Memory mode also works with just Python: `python proxy.py --auth memory`.

Once these changes are pushed, running directly from Git works too:

```sh
uvx --from git+https://github.com/jack-work/copilot-proxy.git copilot-proxy --auth memory
```

No PyPI publication is required. Pin the Git URL to a commit (`.git@<commit>`)
for reproducibility. `uvx copilot-anthropic-proxy` alone is not supported unless
the package is published, and the executable name is `copilot-proxy`.

## Remember a login instead

Replace `YOUR_GITHUB_LOGIN` with your GitHub username (not an email address):

```sh
uvx --from . copilot-proxy login --account YOUR_GITHUB_LOGIN
uvx --from . copilot-proxy --account YOUR_GITHUB_LOGIN
```

Login verifies the approved account before saving the OAuth token. Storage is
namespaced by GitHub host and account:

| OS | Credential store |
|---|---|
| Windows | Windows Credential Manager |
| macOS | macOS Keychain |
| Linux / WSL | Secret Service (e.g. GNOME Keyring), with session D-Bus |

The store must be available and unlocked (it may show an OS unlock prompt).
There is **no plaintext or alternate-backend fallback**: a locked keyring fails
loudly instead of silently downgrading. Use `--auth memory` if you do not have
a usable store, or `--auth file` for an unattended service. Windows and WSL use
**separate** credential stores.

Stop running proxies with Ctrl+C, then remove the saved login:

```sh
uvx --from . copilot-proxy logout --account YOUR_GITHUB_LOGIN
```

Logout removes the local credential, not GitHub's authorization grant. It
cannot erase another process's cached token; a running keyring-backed proxy
notices removal at its next renewal. Revoke the app on GitHub if needed.

## Configuration

Flags override environment variables, then `.env` (`./.env` or
`$COPILOT_PROXY_ENV`), then `$XDG_CONFIG_HOME/copilot-proxy/config.json`
(default `~/.config/copilot-proxy/config.json`), then defaults.

| Flag | Environment | JSON key | Default |
|---|---|---|---|
| `--domain` | `COPILOT_DOMAIN` | `domain` | `github.com` |
| `--account` | `COPILOT_ACCOUNT` | `account` | required for `--auth keyring` |
| `--auth` | `COPILOT_AUTH` | `auth` | `keyring` |
| `--oauth-file` | `COPILOT_OAUTH_FILE` | `oauth_file` | `<config dir>/oauth.txt` |
| `--port` | `PORT` | `port` | `8787` |
| `--bind` | `BIND` | `bind` | `127.0.0.1` |
| `--dump-dir` | `PROXY_DUMP_DIR` | `dump_dir` | off |
| `--request-log` | `PROXY_REQUEST_LOG` | `request_log` | off |

The config dir is `%APPDATA%\copilot-proxy` on Windows and
`$XDG_CONFIG_HOME/copilot-proxy` (default `~/.config/copilot-proxy`) elsewhere.

`serve` is the default command; `login` and `logout` manage saved credentials.
`--help` lists the options.

### Where the OAuth token lives: `--auth`

| Mode | Stored in | Survives a reboot | Use for |
|---|---|---|---|
| `keyring` (default) | OS credential store | yes | interactive use |
| `memory` | this process only | no | a one-off; nothing touches disk |
| `file` | a 0600 file | yes | an unattended service |

There is **no fallback chain between them**: a locked keyring fails loudly
rather than silently becoming a plaintext file. Device login is built in for
all three — `login` runs it and saves, `--auth memory` runs it at startup and
keeps the result in memory.

> **Why not MSAL?** It is an Entra ID client and rejects a GitHub authority
> outright. The credential here is a GitHub App user-to-server token (`ghu_`)
> from GitHub's own device flow, exchanged at `/copilot_internal/v2/token`.
> Different issuer, different protocol. The built-in flow is stdlib only.

For GitHub Enterprise Cloud, add `--domain your-tenant.ghe.com` to **both**
login and serve/logout. OAuth goes to that host and API requests to its `api.`
host; credentials cannot be mixed across hosts. Other GitHub Enterprise
Server URL layouts are not implemented.

## Check the flow

From a second terminal on the same OS, with the proxy running:

```sh
uv run python tests/smoke.py
```

Or use any Anthropic-compatible client with base URL `http://127.0.0.1:8787`;
the caller's API key is ignored. `GET /health` mints or reuses a Copilot
token and reports the live credential state, so it returns 503 with a reason
when the proxy could not serve a request. The smoke test makes one real inference
request (uses quota); pass `--model` if you need a different enabled model.
If testing Windows and WSL simultaneously, use `--port 8788` for one proxy
and pass the same port to the smoke test.

## The request log

`--request-log <path>` appends one JSON object per request. It records **shape
and counts only** — never prompt text, never a token — so unlike `--dump-dir`
it is safe to leave on. It self-rotates to one `.1` sibling at 64 MB.

```json
{"ts": 1790022751.6, "method": "POST", "path": "/v1/messages",
 "wire": "anthropic", "client": "Anthropic/JS 0.94.0", "client_addr": "172.22.0.4",
 "model": "claude-opus-5", "status": 200, "stream": true, "duration_ms": 5875,
 "n_messages": 7, "system_len": 1420,
 "input_tokens": 5953, "output_tokens": 611,
 "cache_read": 42632, "cache_write": 3163, "prompt_total": 48585,
 "cache_read_input_tokens": 42632, "cache_creation_input_tokens": 3163}
```

This is the only vantage point that sees both the body actually dispatched to
the model and the usage that came back, which is what a prompt-cache change has
to be validated against.

**Use the normalized fields.** `cache_read`, `cache_write` and `prompt_total`
are emitted next to the raw provider fields, because the two wires disagree
twice over:

| | cache read field | does `input_tokens` include cached? |
|---|---|---|
| Anthropic | `cache_read_input_tokens` | **no** |
| OpenAI | `cached_tokens` | **yes** |

So asking for one wire's name returns *nothing* on the other's records rather
than an error, and `cached / input_tokens` is a hit rate on one wire and
nonsense on the other. `cache_read / prompt_total` is correct on both:

```sh
python3 - <<'EOF'
import json, collections
agg = collections.defaultdict(lambda: [0, 0])
for line in open("requests.ndjson"):
    r = json.loads(line)
    if "prompt_total" in r:
        a = agg[r["wire"]]
        a[0] += r.get("cache_read", 0); a[1] += r["prompt_total"]
for wire, (read, total) in sorted(agg.items()):
    print("%-18s %5.1f%% of %d prompt tokens" % (wire, 100*read/total, total))
EOF
```

## How it works / limits

GitHub device login uses the public VS Code Copilot client ID
`Iv1.b507a08c87ecfe98` and `read:user`. The OAuth token is exchanged at
`/copilot_internal/v2/token` for an API token cached **only in memory**, renewed
on demand within five minutes of expiry. Invalid OAuth credentials require
another explicit login; HTTP handlers never initiate browser approval.

Requests pass through in native Anthropic format (including SSE and tools).
**Every path except `/health` and `/healthz` is forwarded verbatim**, for GET,
POST and DELETE, so `/v1/messages`, `/chat/completions`, `/responses` and
`/models` all work and a Copilot endpoint this proxy has never heard of needs
no code change. Note the asymmetry, which is the upstream's and not ours: the
Anthropic shape is under `/v1/`, the rest sit at the root. The proxy replaces
authorization/editor headers and maps dashed model versions to Copilot's
dotted IDs when — and only when — the body names a model. An upstream 401 invalidates the cache for the **next**
request; POSTs are never automatically replayed. No rate limiting or queueing.

**Keep the loopback binding.** This proxy has no client authentication; anyone
who can reach it can spend your quota. Widening `--bind` is your security decision.
`dump_dir` writes prompts to disk in plaintext; leave it off unless debugging.

This is an **unofficial development integration**, using Copilot's internal
endpoint—not a sanctioned product API. Check your Copilot licence and tenant
policy. Secure storage does not change that support boundary.

## Development

```sh
uv run python -m unittest discover -s tests -v
uv build
```

Tests mock GitHub and exercise real local HTTP forwarding, streaming, token
renewal and failure paths. CI covers Linux, Windows and macOS. Setting
`COPILOT_PROXY_TEST_KEYRING=1` also tests the native store with a unique dummy
credential that is deleted afterward; it needs an unlocked keyring.
