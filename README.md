# copilot-anthropic-proxy

A small local shim that lets any client speaking the **Anthropic Messages API**
do inference through **GitHub Copilot** instead of an Anthropic API key.

It is ~270 lines of standard-library Python with no dependencies.

## Why this is so small

Copilot's chat endpoint serves `POST /v1/messages` in **native Anthropic wire
format**, including SSE streaming, thinking blocks and `tool_use`. So this is
not a translating shim. It forwards the request body byte-for-byte and only:

1. **swaps auth** — callers send `x-api-key`; Copilot wants
   `Authorization: Bearer <short-lived copilot token>` plus editor headers;
2. **mints and refreshes** that short-lived token from a long-lived OAuth token,
   five minutes before expiry;
3. **rewrites the model id** — callers tend to use dashes
   (`claude-sonnet-4-6`) where Copilot uses dots (`claude-sonnet-4.6`). An exact
   match always wins, so this only fires when it has to.

If Copilot's format ever diverges from Anthropic's, this approach stops working
and you would need a real translation layer. Today it does not need one.

## Requirements

- Python 3.8+ (standard library only)
- A GitHub account with Copilot access

## Getting a credential

Copilot uses a two-step exchange: a long-lived OAuth token obtained through the
device flow, which is then exchanged for a short-lived API token. This proxy
does the second step for you, every time. You do the first step once.

The device flow uses the **public VS Code Copilot client id**,
`Iv1.b507a08c87ecfe98`. That is not a secret; it is the same id every editor
integration uses.

```
device code   POST https://<domain>/login/device/code
              client_id=Iv1.b507a08c87ecfe98&scope=read:user

poll          POST https://<domain>/login/oauth/access_token
              client_id=Iv1.b507a08c87ecfe98&device_code=<code>
              &grant_type=urn:ietf:params:oauth:grant-type:device_code

exchange      GET  https://api.<domain>/copilot_internal/v2/token
              Authorization: token <the OAuth token>
              -> endpoints.api = the base URL this proxy forwards to
```

Approve at `https://<domain>/login/device`. **Codes expire quickly**, so do not
leave the approval sitting.

> **Use one host consistently.** If you authorise against `github.com`, exchange
> against `api.github.com`. If you authorise against an Enterprise tenant,
> exchange against that tenant's `api.` host. Crossing them fails in confusing
> ways: the wrong pairing returns 403 or 404 rather than a useful error.

Write the resulting OAuth token to the file the proxy reads, and keep it
private:

```sh
mkdir -p ~/.config/copilot-proxy
printf '%s' "<the-oauth-token>" > ~/.config/copilot-proxy/oauth.txt
chmod 600 ~/.config/copilot-proxy/oauth.txt
```

## Running it

```sh
python3 proxy.py
```

Point your client at `http://127.0.0.1:8787`. It answers `GET /health` and
forwards `POST /v1/messages`.

## Configuration

Highest precedence first:

1. environment variables
2. a `.env` file (`./.env`, or `$COPILOT_PROXY_ENV`)
3. `config.json` in `$XDG_CONFIG_HOME/copilot-proxy` (default `~/.config/copilot-proxy`)
4. built-in defaults

| config.json | env var | default | meaning |
|---|---|---|---|
| `domain` | `COPILOT_DOMAIN` | `github.com` | Host to authenticate and exchange against |
| `oauth_file` | `COPILOT_OAUTH_FILE` | `<config dir>/oauth.txt` | Long-lived OAuth token |
| `port` | `PORT` | `8787` | Listen port |
| `bind` | `BIND` | `127.0.0.1` | Listen address |
| `dump_dir` | `PROXY_DUMP_DIR` | unset | Debug tap, see below |

Copy `config.example.json` or `.env.example` to get started.

### GitHub Enterprise

Set the domain to your tenant host. Everything else is derived from it:

```sh
COPILOT_DOMAIN=your-tenant.ghe.com python3 proxy.py
```

Whether that is permitted is a question for whoever administers your tenant.
See the note at the bottom.

## Two things to read before you widen anything

**`bind` is loopback by default, deliberately.** This proxy holds a credential
and has **no authentication of its own**. Anything that can reach the port can
spend your Copilot quota. Containers that need it (Docker's
`host.docker.internal`) require `BIND=0.0.0.0`, which makes it reachable from
your network. Make that choice knowingly; the proxy logs a warning when you do.

**`dump_dir` writes prompt content to disk in the clear.** It exists because
TLS makes a packet capture useless and it is the only way to see the exact body
sent upstream. It is off unless you set it. Turn it off again when you are done,
and remember the files are plain JSON containing whatever you sent.

## Running it as a service

`copilot-proxy.service` is a systemd **user** unit. Adjust `WorkingDirectory`,
then:

```sh
cp copilot-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now copilot-proxy
```

`loginctl enable-linger $USER` keeps it running when you are not logged in.

## Status and caveats

- **This is a local development shim, not a product, a service, or a sanctioned
  integration.** It is published so it can be read and reviewed rather than
  described second-hand.
- **Whether routing an application's inference through Copilot is permitted by
  your Copilot licence or your organisation's policy is a question for you and
  them, not something this repository can answer.** Check before you rely on it.
- The editor headers and the client id it sends are the standard public Copilot
  ones. This does not attempt to disguise itself as anything else.
- Streaming is passed through as chunked transfer. Non-streaming responses are
  buffered and sent with a Content-Length.
- There is no retry logic, no rate limiting and no request queue. Upstream
  errors are forwarded to the caller with their original status code.
