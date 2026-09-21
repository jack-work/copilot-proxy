# copilot-anthropic-proxy

A small local proxy: speak the **Anthropic Messages API** to **GitHub Copilot**.
One Python module; `keyring` is its only direct dependency. Python 3.10+.

## Run without installing globally

With [uv](https://docs.astral.sh/uv/), from this checkout, on Linux, Windows or macOS:

```sh
uvx --from . copilot-proxy --memory
```

Open the printed GitHub URL, enter the code and approve. The proxy then listens
on **http://127.0.0.1:8787**. Both credentials stay in this process; restarting
requires approval again. No token file, keyring access or browser automation.
`uvx` caches the package/dependencies, **not your credentials**.

Memory mode also works with just Python: `python proxy.py --memory`.

Once these changes are pushed, running directly from Git works too:

```sh
uvx --from git+https://github.com/jack-work/copilot-proxy.git copilot-proxy --memory
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
There is **no plaintext or alternate-backend fallback**; use `--memory` if you
do not have a usable store. This is a foreground, signed-in-user tool, not an
unattended boot service. Windows and WSL use **separate** credential stores.

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
| `--account` | `COPILOT_ACCOUNT` | `account` | required except in memory mode |
| `--port` | `PORT` | `port` | `8787` |
| `--bind` | `BIND` | `bind` | `127.0.0.1` |
| — | `PROXY_DUMP_DIR` | `dump_dir` | off |

`--memory` is an explicit serve-only flag. `serve` is the default command;
`login` and `logout` manage saved credentials. `--help` lists the options.

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
the caller's API key is ignored. `GET /health` checks local liveness only,
not current upstream authorization. The smoke test makes one real inference
request (uses quota); pass `--model` if you need a different enabled model.
If testing Windows and WSL simultaneously, use `--port 8788` for one proxy
and pass the same port to the smoke test.

## How it works / limits

GitHub device login uses the public VS Code Copilot client ID
`Iv1.b507a08c87ecfe98` and `read:user`. The OAuth token is exchanged at
`/copilot_internal/v2/token` for an API token cached **only in memory**, renewed
on demand within five minutes of expiry. Invalid OAuth credentials require
another explicit login; HTTP handlers never initiate browser approval.

Requests pass through in native Anthropic format (including SSE and tools).
The proxy replaces authorization/editor headers and maps dashed model versions
to Copilot's dotted IDs. An upstream 401 invalidates the cache for the **next**
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
