"""Offline auth/HTTP tests; opt-in native keyring smoke test uses a dummy secret."""
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import io
import json
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import uuid

SOURCE = (Path(__file__).resolve().parents[1] / "proxy.py").read_text(encoding="utf-8")
DEVICE = {"device_code": "device-secret", "user_code": "ABCD-1234",
          "verification_uri": "https://github.com/login/device", "interval": 5, "expires_in": 900}
COPILOT = {"token": "api-secret", "expires_at": 3000,
           "endpoints": {"api": "https://copilot.example"}}


class Response(io.BytesIO):
    def __init__(self, body, status=200, content_type="application/json"):
        super().__init__(json.dumps(body).encode() if isinstance(body, dict) else body)
        self.status = status
        self.headers = {"Content-Type": content_type}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.p = types.ModuleType("proxy_test")
        with patch.dict(os.environ, {}, clear=True), patch.object(builtins, "open", side_effect=FileNotFoundError):
            exec(compile(SOURCE, "proxy.py", "exec"), self.p.__dict__)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stderr, self.stdout = io.StringIO(), io.StringIO()
        self.mock(sys, "stderr", self.stderr)
        self.mock(sys, "stdout", self.stdout)
        self.network = self.mock(self.p.urllib.request, "urlopen")
        self.network.side_effect = AssertionError("Unexpected network request")

    def mock(self, obj, name, *args, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, *args, **kwargs))

    def device(self, replies):
        self.now = 0
        def sleep(seconds):
            self.now += seconds
        self.sleep = self.mock(self.p.time, "sleep", side_effect=sleep)
        self.mock(self.p.time, "monotonic", side_effect=lambda: self.now)
        self.network.side_effect = [Response(DEVICE)] + [Response(r) for r in replies]

    def test_device_flow_pending_slowdown_and_approval(self):
        self.device([{"error": "authorization_pending"}, {"error": "slow_down", "interval": 15},
                     {"access_token": "oauth-secret"}])
        self.assertEqual(self.p.device_login(), "oauth-secret")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [5, 5, 15])
        requests = [c.args[0] for c in self.network.call_args_list]
        self.assertEqual(requests[0].full_url, "https://github.com/login/device/code")
        initial = urllib.parse.parse_qs(requests[0].data.decode())
        self.assertEqual(initial, {"client_id": [self.p.CLIENT_ID], "scope": ["read:user"]})
        poll = requests[-1]
        self.assertEqual(poll.full_url, "https://github.com/login/oauth/access_token")
        self.assertEqual(urllib.parse.parse_qs(poll.data.decode())["device_code"], ["device-secret"])
        self.assertEqual(poll.get_header("Accept"), "application/json")
        self.assertIn("ABCD-1234", self.stderr.getvalue())
        self.assertNotIn("oauth-secret", self.stderr.getvalue())
        self.assertNotIn("device-secret", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_device_slowdown_without_interval_adds_five(self):
        self.device([{"error": "slow_down"}, {"access_token": "oauth-secret"}])
        self.p.device_login()
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [5, 10])

    def test_device_denial_expiry_and_unknown_error(self):
        for error, message in [("access_denied", "denied"), ("expired_token", "expired"),
                               ("unexpected-secret", "device login failed")]:
            with self.subTest(error=error):
                self.device([{"error": error}])
                with self.assertRaisesRegex(self.p.AuthError, message) as result:
                    self.p.device_login()
                self.assertNotIn("unexpected-secret", str(result.exception))

    def test_device_deadline_does_not_poll_after_expiry(self):
        self.device([])
        self.network.side_effect = [Response(dict(DEVICE, expires_in=12)),
                                    Response({"error": "authorization_pending"}),
                                    Response({"error": "authorization_pending"})]
        with self.assertRaisesRegex(self.p.AuthError, "expired"):
            self.p.device_login()
        self.assertEqual(self.network.call_count, 3)
        self.assertEqual([c.kwargs["timeout"] for c in self.network.call_args_list[1:]], [7, 2])

    def test_bad_device_response(self):
        self.network.side_effect = [Response({"error": "bad_client"})]
        with self.assertRaisesRegex(self.p.AuthError, "Could not start"):
            self.p.device_login()

    def test_account_check_uses_same_enterprise_host(self):
        self.p.DOMAIN, self.p.ACCOUNT = "tenant.ghe.com", "octocat"
        self.device([{"access_token": "oauth-secret"}, {"login": "Octocat"}])
        self.assertEqual(self.p.device_login(), "oauth-secret")
        requests = [c.args[0] for c in self.network.call_args_list]
        self.assertEqual(requests[0].full_url, "https://tenant.ghe.com/login/device/code")
        self.assertEqual(requests[1].full_url, "https://tenant.ghe.com/login/oauth/access_token")
        self.assertEqual(requests[2].full_url, "https://api.tenant.ghe.com/user")
        self.assertEqual(requests[2].get_header("Authorization"), "token oauth-secret")

    def test_account_mismatch_is_not_saved(self):
        store = self.mock(self.p, "stored_token")
        self.device([{"access_token": "oauth-secret"}, {"login": "someone-else"}])
        self.assertEqual(self.p.main(["login", "--account", "octocat"]), 1)
        self.assertEqual([c.args[0] for c in store.call_args_list], ["get"])
        self.assertIn("nothing saved", self.stderr.getvalue())

    def test_oauth_error_in_http_400(self):
        self.network.side_effect = urllib.error.HTTPError("https://github.com", 400, "Bad Request", {},
                                                        Response({"error": "authorization_pending"}))
        self.assertEqual(self.p.github("/login/oauth/access_token", data={}), {"error": "authorization_pending"})

    def test_auth_http_errors_are_actionable_and_redacted(self):
        for code, message in [(401, "Run login again"), (403, "Check Copilot access"), (500, "HTTP 500")]:
            with self.subTest(code=code):
                self.network.side_effect = urllib.error.HTTPError(
                    "https://api.github.com", code, "oauth-secret", {}, Response(b"oauth-secret"))
                with self.assertRaisesRegex(self.p.AuthError, message) as result:
                    self.p.github("/copilot_internal/v2/token", token="oauth-secret")
                self.assertNotIn("oauth-secret", str(result.exception))

    def test_network_failure_and_invalid_json(self):
        for failure in [urllib.error.URLError("oauth-secret"), Response(b"oauth-secret"), Response(b"[]")]:
            with self.subTest(failure=type(failure).__name__):
                self.network.side_effect = [failure]
                with self.assertRaisesRegex(self.p.AuthError, "network error or invalid JSON"):
                    self.p.github("/login/device/code", data={})

    def test_native_backend_selection_and_host_account_namespace(self):
        for platform, module, name in [("linux", "SecretService", "Keyring"),
                                       ("darwin", "macOS", "Keyring"),
                                       ("win32", "Windows", "WinVaultKeyring")]:
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                store = Mock()
                backend = types.SimpleNamespace(**{name: Mock(return_value=store)})
                with patch.object(self.p.importlib, "import_module", return_value=backend) as load:
                    self.p.DOMAIN, self.p.ACCOUNT = "tenant.ghe.com", "octocat"
                    self.p.stored_token("set", "oauth-secret")
                    self.p.stored_token("get")
                    self.p.stored_token("delete")
                    load.assert_called_with("keyring.backends." + module)
                    store.set_password.assert_called_once_with("copilot-proxy:tenant.ghe.com", "octocat", "oauth-secret")
                    store.get_password.assert_called_with("copilot-proxy:tenant.ghe.com", "octocat")
                    store.delete_password.assert_called_once_with("copilot-proxy:tenant.ghe.com", "octocat")

    def test_keyring_failures_are_distinguished_and_have_no_fallback(self):
        store = Mock()
        store.get_password.side_effect = RuntimeError("secret error details")
        backend = types.SimpleNamespace(Keyring=Mock(return_value=store), WinVaultKeyring=Mock(return_value=store))
        # A MISSING LIBRARY AND A LOCKED STORE ARE DIFFERENT PROBLEMS. Saying
        # "locked" for the first sends the reader to unlock a keyring that was
        # never at fault -- measured 2026-09-21 against an interpreter without
        # keyring whose Secret Service was answering secret-tool at the time.
        for failure, expected in [(ImportError("missing keyring"), "not importable"),
                                  (backend, "unavailable or locked")]:
            with self.subTest(expected=expected), patch.object(
                self.p.importlib, "import_module", side_effect=[failure]
            ), patch.object(builtins, "open") as files:
                with self.assertRaisesRegex(self.p.AuthError, expected) as result:
                    self.p.stored_token("get")
                self.assertNotIn("secret error details", str(result.exception))
                files.assert_not_called()
        # And the missing-library message must not also claim "locked".
        with patch.object(self.p.importlib, "import_module",
                          side_effect=[ImportError("missing keyring")]):
            with self.assertRaises(self.p.AuthError) as result:
                self.p.stored_token("get")
        self.assertNotIn("unavailable or locked", str(result.exception))
        self.network.assert_not_called()

    def test_logout_missing_credential_is_idempotent(self):
        store = Mock(get_password=Mock(return_value=None))
        backend = types.SimpleNamespace(Keyring=Mock(return_value=store), WinVaultKeyring=Mock(return_value=store))
        self.mock(self.p.importlib, "import_module", return_value=backend)
        self.assertEqual(self.p.main(["logout", "--account", "octocat"]), 0)
        store.delete_password.assert_not_called()
        self.assertIn("Stop running proxies", self.stderr.getvalue())
        self.network.assert_not_called()

    def test_login_saves_and_normalizes_identity(self):
        store = self.mock(self.p, "stored_token")
        self.device([{"access_token": "oauth-secret"}, {"login": "Octocat"}])
        self.assertEqual(self.p.main(["login", "--account", "Octocat", "--domain", "GitHub.COM"]), 0)
        store.assert_called_with("set", "oauth-secret")
        self.assertEqual((self.p.DOMAIN, self.p.ACCOUNT), ("github.com", "octocat"))
        self.assertIsNone(self.p._oauth_token)
        self.assertNotIn("oauth-secret", self.stderr.getvalue())

    def test_login_checks_store_before_device_flow(self):
        self.mock(self.p, "stored_token", side_effect=self.p.AuthError("store locked"))
        self.assertEqual(self.p.main(["login", "--account", "octocat"]), 1)
        self.network.assert_not_called()

    def test_missing_login_fails_without_implicit_device_flow(self):
        self.mock(self.p, "stored_token", return_value=None)
        self.assertEqual(self.p.main(["--account", "octocat"]), 1)
        self.assertIn("No saved login", self.stderr.getvalue())
        self.network.assert_not_called()

    def test_memory_cli_never_touches_keyring_or_disk_and_clears_on_exit(self):
        # stored_token is now a dispatcher that --auth memory legitimately uses,
        # so assert on the two PERSISTENT backings instead: neither may be
        # reached, and nothing may survive the server.
        keyring = self.mock(self.p, "_keyring_store", side_effect=AssertionError("keyring touched"))
        disk = self.mock(self.p, "_file_store", side_effect=AssertionError("disk touched"))
        self.mock(self.p, "device_login", return_value="oauth-secret")
        self.network.side_effect = [Response(dict(COPILOT, expires_at=9999999999))]
        server = Mock(server_address=("127.0.0.1", 8787))
        factory = self.mock(self.p, "ThreadingHTTPServer")
        factory.return_value.__enter__.return_value = server
        server.serve_forever.side_effect = lambda: self.assertEqual(self.p._oauth(), "oauth-secret")
        self.assertEqual(self.p.main(["--auth", "memory"]), 0)
        keyring.assert_not_called()
        disk.assert_not_called()
        self.assertIsNone(self.p._oauth_token)
        self.assertIsNone(self.p._token["value"])

    def test_cli_validation_precedes_network(self):
        for args in [[], ["login", "--auth", "memory"], ["logout", "--auth", "memory"],
                     ["--auth", "memory", "--domain", "https://github.com"], ["--auth", "memory", "--port", "70000"]]:
            with self.subTest(args=args), self.assertRaises(SystemExit) as result:
                self.p.main(args)
            self.assertEqual(result.exception.code, 2)
        self.network.assert_not_called()

    def test_ctrl_c_is_a_clean_exit(self):
        self.mock(self.p, "device_login", side_effect=KeyboardInterrupt)
        self.assertEqual(self.p.main(["--auth", "memory"]), 130)
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_cached_token_and_renewal(self):
        self.p.AUTH, self.p._oauth_token = "memory", "oauth-secret"
        store = self.mock(self.p, "_keyring_store")
        clock = self.mock(self.p.time, "time", return_value=1000)
        self.network.side_effect = [Response(COPILOT), Response(dict(COPILOT, token="new-secret", expires_at=6000))]
        self.assertEqual(self.p.copilot_token()[0], "api-secret")
        self.assertEqual(self.p.copilot_token()[0], "api-secret")
        self.assertEqual(self.network.call_count, 1)
        clock.return_value = 2700
        self.assertEqual(self.p.copilot_token()[0], "new-secret")
        self.assertEqual(self.network.call_count, 2)
        store.assert_not_called()

    def test_concurrent_requests_mint_once(self):
        self.p.AUTH, self.p._oauth_token = "memory", "oauth-secret"
        self.mock(self.p.time, "time", return_value=1000)
        self.network.side_effect = [Response(COPILOT)]
        with ThreadPoolExecutor(max_workers=12) as workers:
            results = list(workers.map(lambda _: self.p.copilot_token(), range(24)))
        self.assertEqual(len(results), 24)
        self.assertTrue(all(token == "api-secret" for token, _ in results))
        self.assertEqual(self.network.call_count, 1)

    def test_logout_is_detected_at_next_renewal(self):
        self.mock(self.p, "stored_token", side_effect=["oauth-secret", None])
        clock = self.mock(self.p.time, "time", return_value=1000)
        self.network.side_effect = [Response(COPILOT)]
        self.p.copilot_token()
        clock.return_value = 2700
        with self.assertRaisesRegex(self.p.AuthError, "No saved login"):
            self.p.copilot_token()
        self.assertEqual(self.network.call_count, 1)

    def test_malformed_token_response_does_not_partially_update_cache(self):
        self.p.AUTH, self.p._oauth_token = "memory", "oauth-secret"
        self.network.side_effect = [Response({"token": "secret-with-no-endpoint"})]
        with self.assertRaisesRegex(self.p.AuthError, "invalid Copilot token response"):
            self.p.copilot_token()
        self.assertIsNone(self.p._token["value"])

    def http_server(self):
        server = self.p.ThreadingHTTPServer(("127.0.0.1", 0), self.p.Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        client = http.client.HTTPConnection(*server.server_address, timeout=5)
        self.addCleanup(client.close)
        return client

    def test_memory_login_to_http_inference_and_streaming(self):
        self.p.AUTH = "memory"
        self.device([{"access_token": "oauth-secret"}])
        self.p._oauth_token = self.p.device_login()
        self.mock(self.p.time, "time", return_value=1000)
        self.network.reset_mock()
        sse = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        self.network.side_effect = [Response(COPILOT), Response({"data": [{"id": "claude-sonnet-4.6"}]}),
                                    Response({"content": [{"type": "text", "text": "hello"}]}),
                                    Response(sse, content_type="text/event-stream")]
        client = self.http_server()
        for streaming in [False, True]:
            client.request("POST", "/v1/messages", json.dumps({"model": "claude-sonnet-4-6", "stream": streaming}),
                           {"x-api-key": "client-secret", "Authorization": "Bearer client-secret"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            result = response.read()
            if streaming:
                self.assertEqual(result, sse)
                self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
            else:
                self.assertEqual(json.loads(result)["content"][0]["text"], "hello")
        self.assertEqual(self.network.call_count, 4)
        forwarded = self.network.call_args.args[0]
        self.assertEqual(forwarded.full_url, "https://copilot.example/v1/messages")
        self.assertEqual(forwarded.get_header("Authorization"), "Bearer api-secret")
        self.assertIsNone(forwarded.get_header("X-api-key"))
        self.assertEqual(json.loads(forwarded.data)["model"], "claude-sonnet-4.6")
        self.assertNotIn("oauth-secret", self.stderr.getvalue())

    def test_api_401_invalidates_cache_without_replaying_post(self):
        self.p._token.update(value="old-secret", exp=9999999999, api="https://copilot.example")
        self.p._models_cache.update(ids=["model"], at=self.p.time.time())
        self.network.side_effect = urllib.error.HTTPError("https://copilot.example", 401, "Unauthorized", {},
                                                        Response({"error": "Unauthorized"}))
        client = self.http_server()
        client.request("POST", "/v1/messages", '{"model":"model"}')
        response = client.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        self.assertIsNone(self.p._token["value"])
        self.assertEqual(self.network.call_count, 1)

    def test_request_auth_failure_is_actionable_without_login_prompt(self):
        self.mock(self.p, "stored_token", return_value=None)
        client = self.http_server()
        client.request("POST", "/v1/messages", '{"model":"model"}')
        response = client.getresponse()
        self.assertEqual(response.status, 503)
        self.assertIn("Run login", json.loads(response.read())["error"]["message"])
        # A health probe that reports ok while the proxy cannot serve a single
        # request is a lying probe: unauthenticated is 503, with the reason.
        client.request("GET", "/health")
        health = client.getresponse()
        self.assertEqual(health.status, 503)
        body = json.loads(health.read())
        self.assertFalse(body["ok"])
        self.assertIn("Run login", body["error"])
        self.network.assert_not_called()

    @unittest.skipUnless(os.environ.get("COPILOT_PROXY_TEST_KEYRING") == "1", "opt-in native keyring smoke test")
    def test_native_keyring_roundtrip(self):
        self.p.APP = "copilot-proxy-test-" + uuid.uuid4().hex
        self.p.ACCOUNT = "dummy-account"
        try:
            self.p.stored_token("set", "dummy-secret-not-a-token")
            self.assertEqual(self.p.stored_token("get"), "dummy-secret-not-a-token")
            self.p.stored_token("delete")
            self.assertIsNone(self.p.stored_token("get"))
            self.p.stored_token("delete")
        finally:
            self.p.stored_token("delete")


if __name__ == "__main__":
    unittest.main()
