"""Tests for what convergence added: the credential-store selector, generic
/v1 forwarding across methods, and the request log.

Companion to test_proxy.py, which still owns the auth/device-flow surface."""
import builtins
from contextlib import ExitStack
import http.client
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

SOURCE = (Path(__file__).resolve().parents[1] / "proxy.py").read_text(encoding="utf-8")
COPILOT = {"token": "api-secret", "expires_at": 3000,
           "endpoints": {"api": "https://copilot.example"}}


class Response(io.BytesIO):
    def __init__(self, body, status=200, content_type="application/json"):
        super().__init__(json.dumps(body).encode() if isinstance(body, dict) else body)
        self.status = status
        self.headers = {"Content-Type": content_type}


class Base(unittest.TestCase):
    def setUp(self):
        self.p = types.ModuleType("proxy_test")
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(builtins, "open", side_effect=FileNotFoundError):
            exec(compile(SOURCE, "proxy.py", "exec"), self.p.__dict__)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stderr = io.StringIO()
        self.mock(sys, "stderr", self.stderr)
        self.network = self.mock(self.p.urllib.request, "urlopen")
        self.network.side_effect = AssertionError("Unexpected network request")

    def mock(self, obj, name, *args, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, *args, **kwargs))

    def await_log(self, timeout=5.0):
        """The handler appends to the request log after it writes the response,
        so a client that reads immediately can lose the race."""
        deadline = time.monotonic() + timeout
        path = Path(self.p.REQUEST_LOG)
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return path.read_text(encoding="utf-8")
            time.sleep(0.01)
        self.fail("request log %s was never written (stderr: %s)"
                  % (path, self.stderr.getvalue()))

    def serving(self):
        """A live proxy with a valid cached Copilot token and a known model list."""
        self.p.AUTH, self.p._oauth_token = "memory", "oauth-secret"
        self.mock(self.p.time, "time", return_value=1000)
        self.p._token.update(value="api-secret", exp=9999.0, api="https://copilot.example")
        self.p._models_cache.update(ids=["claude-sonnet-4.6"], at=1000)
        server = self.p.ThreadingHTTPServer(("127.0.0.1", 0), self.p.Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        client = http.client.HTTPConnection(*server.server_address, timeout=5)
        self.addCleanup(client.close)
        return client


class StoreTests(Base):
    def test_file_store_roundtrip_is_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.p.AUTH = "file"
            self.p.OAUTH_FILE = os.path.join(d, "nested", "oauth.txt")
            self.assertIsNone(self.p.stored_token("get"))
            self.p.stored_token("set", "oauth-secret")
            self.assertEqual(self.p.stored_token("get"), "oauth-secret")
            if os.name == "posix":
                mode = stat.S_IMODE(os.stat(self.p.OAUTH_FILE).st_mode)
                self.assertEqual(mode, 0o600, "credential file must be owner-only")
            self.p.stored_token("delete")
            self.assertIsNone(self.p.stored_token("get"))
            self.p.stored_token("delete")  # idempotent

    def test_each_mode_uses_only_its_own_backing(self):
        """No fallback chain: a locked keyring must never silently become a file."""
        for mode, expected in [("keyring", "_keyring_store"),
                               ("file", "_file_store"),
                               ("memory", "_memory_store")]:
            with self.subTest(mode=mode):
                calls = []
                for name in ("_keyring_store", "_file_store", "_memory_store"):
                    self.stack.enter_context(patch.object(
                        self.p, name,
                        side_effect=lambda a, v=None, n=name: calls.append(n)))
                self.p.AUTH = mode
                self.p.stored_token("get")
                self.assertEqual(calls, [expected])
                self.stack.close()
                self.stack = ExitStack()

    def test_memory_store_never_reaches_disk_or_keyring(self):
        self.p.AUTH = "memory"
        self.mock(self.p, "_keyring_store", side_effect=AssertionError("keyring touched"))
        self.mock(self.p, "_file_store", side_effect=AssertionError("disk touched"))
        self.p.stored_token("set", "oauth-secret")
        self.assertEqual(self.p.stored_token("get"), "oauth-secret")
        self.p.stored_token("delete")
        self.assertIsNone(self.p.stored_token("get"))

    def test_config_dir_follows_the_platform(self):
        cases = [("win32", {"APPDATA": os.path.join("C:", "Users", "u", "AppData", "Roaming")}),
                 ("linux", {"XDG_CONFIG_HOME": "/home/u/.config"})]
        for platform, env in cases:
            with self.subTest(platform=platform), \
                 patch.object(self.p.sys, "platform", platform), \
                 patch.dict(os.environ, env, clear=True):
                got = self.p.config_dir()
                self.assertTrue(got.endswith(self.p.APP), got)
                self.assertTrue(got.startswith(list(env.values())[0]), got)


class ForwardingTests(Base):
    def test_every_v1_path_is_forwarded_untouched(self):
        """The point of convergence: a Copilot endpoint this file has never
        heard of must need no code change here."""
        client = self.serving()
        paths = ["/v1/messages", "/v1/responses", "/v1/chat/completions",
                 "/v1/embeddings", "/v1/some/future/endpoint"]
        self.network.side_effect = [Response({"ok": True}) for _ in paths]
        for path in paths:
            with self.subTest(path=path):
                client.request("POST", path, json.dumps({"model": "claude-sonnet-4.6"}))
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                forwarded = self.network.call_args.args[0]
                self.assertEqual(forwarded.full_url, "https://copilot.example" + path)
                self.assertEqual(forwarded.get_method(), "POST")
        self.assertEqual(self.network.call_count, len(paths))

    def test_get_and_delete_are_forwarded(self):
        client = self.serving()
        self.network.side_effect = [Response({"data": []}), Response({"deleted": True})]
        client.request("GET", "/v1/models")
        self.assertEqual(client.getresponse().read(), b'{"data": []}')
        self.assertEqual(self.network.call_args.args[0].get_method(), "GET")
        self.assertIsNone(self.network.call_args.args[0].data)
        client.request("DELETE", "/v1/messages/batches/abc")
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.network.call_args.args[0].get_method(), "DELETE")

    def test_root_level_paths_are_forwarded_too(self):
        """Copilot serves /v1/messages but /models and /chat/completions at the
        root, so an allowlist of /v1/ would have hidden half the API."""
        client = self.serving()
        paths = ["/models", "/chat/completions", "/responses", "/embeddings"]
        self.network.side_effect = [Response({"ok": True}) for _ in paths]
        for path in paths:
            with self.subTest(path=path):
                client.request("POST", path, json.dumps({"model": "claude-sonnet-4.6"}))
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                self.assertEqual(self.network.call_args.args[0].full_url,
                                 "https://copilot.example" + path)

    def test_only_the_local_paths_are_answered_locally(self):
        client = self.serving()
        self.network.side_effect = [Response({"forwarded": True})]
        client.request("GET", "/healthz")
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("ok", json.loads(response.read()))
        self.network.assert_not_called()
        # Anything else, however unlikely, goes upstream rather than being
        # guessed at locally.
        client.request("GET", "/some/unknown/thing")
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"forwarded": True})
        self.assertEqual(self.network.call_args.args[0].full_url,
                         "https://copilot.example/some/unknown/thing")

    def test_payload_without_a_model_key_is_not_rewritten(self):
        """A model rewrite on a body that names no model used to be harmless
        only by luck; /v1/files and friends carry no model at all."""
        client = self.serving()
        self.network.side_effect = [Response({"ok": True})]
        body = json.dumps({"input": "no model here"})
        client.request("POST", "/v1/embeddings", body)
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.network.call_args.args[0].data, body.encode())

    def test_health_reports_the_live_credential_state(self):
        client = self.serving()
        client.request("GET", "/health")
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        body = json.loads(response.read())
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["api"], "https://copilot.example")
        self.assertEqual(body["auth"], "memory")
        self.network.assert_not_called()


class RequestLogTests(Base):
    def test_log_records_shape_and_usage_but_never_content(self):
        with tempfile.TemporaryDirectory() as d:
            self.p.REQUEST_LOG = os.path.join(d, "nested", "requests.ndjson")
            client = self.serving()
            self.network.side_effect = [Response(
                {"content": [{"type": "text", "text": "hello"}],
                 "usage": {"input_tokens": 11, "output_tokens": 3,
                           "cache_read_input_tokens": 7}})]
            client.request("POST", "/v1/messages", json.dumps(
                {"model": "claude-sonnet-4.6",
                 "system": "SECRET-SYSTEM-PROMPT",
                 "messages": [{"role": "user", "content": "SECRET-USER-TEXT"}]}))
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

            raw = self.await_log()
            self.assertEqual(len(raw.strip().splitlines()), 1, raw)
            rec = json.loads(raw)
        self.assertEqual(rec["path"], "/v1/messages")
        self.assertEqual(rec["method"], "POST")
        self.assertEqual(rec["wire"], "anthropic")
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["model"], "claude-sonnet-4.6")
        self.assertEqual(rec["n_messages"], 1)
        self.assertEqual(rec["system_len"], len("SECRET-SYSTEM-PROMPT"))
        self.assertEqual(rec["input_tokens"], 11)
        self.assertEqual(rec["cache_read_input_tokens"], 7)
        # The whole reason this log is safe to leave on.
        self.assertNotIn("SECRET-SYSTEM-PROMPT", raw)
        self.assertNotIn("SECRET-USER-TEXT", raw)
        self.assertNotIn("api-secret", raw)
        self.assertNotIn("oauth-secret", raw)

    def test_log_is_off_unless_configured(self):
        self.p.REQUEST_LOG = None
        client = self.serving()
        self.network.side_effect = [Response({"ok": True})]
        client.request("POST", "/v1/messages", '{"model":"claude-sonnet-4.6"}')
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        response.read()

    def test_wire_classification(self):
        for path, wire in [("/v1/messages", "anthropic"),
                           ("/v1/responses", "openai-responses"),
                           ("/v1/chat/completions", "openai-chat"),
                           ("/v1/embeddings", "other")]:
            self.assertEqual(self.p.wire_of(path), wire)

    def test_normalized_cache_fields_span_both_wires(self):
        """The regression this exists for: on 2026-09-21 a query asked for the
        Anthropic field name on OpenAI records, got nothing back, and reported
        458,717 cache-read tokens as "caching does not work here"."""
        anthropic = ('{"usage":{"input_tokens":2,"output_tokens":227,'
                     '"cache_read_input_tokens":42636,'
                     '"cache_creation_input_tokens":11}}')
        openai = ('{"usage":{"input_tokens":30512,"output_tokens":516,'
                  '"cached_tokens":27274,"cache_write_tokens":3163}}')

        a = self.p._usage_facts(anthropic)
        o = self.p._usage_facts(openai)

        # Same normalized names on both wires.
        self.assertEqual(a["cache_read"], 42636)
        self.assertEqual(o["cache_read"], 27274)
        self.assertEqual(a["cache_write"], 11)
        self.assertEqual(o["cache_write"], 3163)

        # And the raw provider fields are still there, unrenamed.
        self.assertEqual(a["cache_read_input_tokens"], 42636)
        self.assertEqual(o["cached_tokens"], 27274)

        # prompt_total must account for the wires COUNTING differently:
        # Anthropic's input_tokens excludes the cached part, OpenAI's includes
        # it. Without this, a hit rate is right on one wire and absurd on the
        # other -- 42636/2 = 21318x on the Anthropic record.
        self.assertEqual(a["prompt_total"], 2 + 42636)
        self.assertEqual(o["prompt_total"], 30512)
        for label, facts in (("anthropic", a), ("openai", o)):
            with self.subTest(wire=label):
                rate = facts["cache_read"] / facts["prompt_total"]
                self.assertTrue(0.0 <= rate <= 1.0, "%s rate %r" % (label, rate))

    def test_usage_without_cache_fields_still_reports_prompt_total(self):
        facts = self.p._usage_facts('{"usage":{"input_tokens":15,"output_tokens":9}}')
        self.assertEqual(facts["prompt_total"], 15)
        self.assertNotIn("cache_read", facts)

    def test_log_names_the_caller(self):
        """Without this the only answer to "who sent this burst?" is a guess."""
        with tempfile.TemporaryDirectory() as d:
            self.p.REQUEST_LOG = os.path.join(d, "requests.ndjson")
            client = self.serving()
            self.network.side_effect = [Response({"ok": True})]
            client.request("POST", "/v1/messages", '{"model":"claude-sonnet-4.6"}',
                           {"User-Agent": "cowork-cli/2.0.27"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            rec = json.loads(self.await_log())
        self.assertEqual(rec["client"], "cowork-cli/2.0.27")
        self.assertEqual(rec["client_addr"], "127.0.0.1")

    def test_caller_identity_is_bounded_and_optional(self):
        with tempfile.TemporaryDirectory() as d:
            self.p.REQUEST_LOG = os.path.join(d, "requests.ndjson")
            client = self.serving()
            self.network.side_effect = [Response({"ok": True})]
            # http.client always sends a User-Agent unless told otherwise; an
            # absurd one must not be able to bloat the log line.
            client.request("POST", "/v1/messages", "{}", {"User-Agent": "x" * 5000})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            rec = json.loads(self.await_log())
        self.assertEqual(len(rec["client"]), 200)

    def test_last_usage_object_wins_in_a_stream(self):
        """An SSE body emits several usage objects and only the terminal one
        is complete; a first-match read reports the wrong numbers."""
        sse = ('data: {"usage":{"input_tokens":5,"output_tokens":0}}\n\n'
               'data: {"usage":{"input_tokens":5,"output_tokens":42}}\n\n')
        facts = self.p._usage_facts(sse)
        self.assertEqual(facts["output_tokens"], 42, "took a non-terminal usage object")
        self.assertEqual(facts["input_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
