from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


def load_checker():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "check-telegram-token.py"
    spec = importlib.util.spec_from_file_location("check_telegram_token", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TokenProblemTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()

    def test_accepts_bot_api_token_shape(self):
        token = "8406112058:" + "AAdGh-xyz_9" * 4  # 40-char secret
        self.assertIsNone(self.checker.token_problem(token))

    def test_rejects_empty(self):
        self.assertIsNotNone(self.checker.token_problem(""))

    def test_rejects_masked_token_copied_from_logs(self):
        problem = self.checker.token_problem("8406112058:***")
        self.assertIsNotNone(problem)
        self.assertIn("***", problem)

    def test_rejects_placeholder_values(self):
        for value in ("your-token", "changeme", "paste-token-here"):
            self.assertIsNotNone(self.checker.token_problem(value), value)

    def test_rejects_wrong_shape(self):
        for value in ("8406112058", "123:x", "not a token at all"):
            self.assertIsNotNone(self.checker.token_problem(value), value)


class ClassifyHttpTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()

    def test_success_ranges_are_ok(self):
        for status in (200, 201, 204):
            self.assertEqual(self.checker.classify_http(status), "ok")

    def test_unauthorized_and_not_found_are_authoritative_rejections(self):
        for status in (401, 404):
            self.assertEqual(self.checker.classify_http(status), "rejected")

    def test_rate_limit_and_server_errors_are_transient(self):
        for status in (403, 408, 429, 500, 502, 503):
            self.assertEqual(self.checker.classify_http(status), "transient")

    def test_no_http_answer_is_transient(self):
        self.assertEqual(self.checker.classify_http(None), "transient")


class _FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status


def _http_error(status: int, body: bytes):
    import urllib.error

    return urllib.error.HTTPError(
        url="https://api.telegram.org/bot<redacted>/getMe",
        code=status,
        msg="HTTP Error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _respond(status: int, body: bytes | None = None):
    def fake_urlopen(request, timeout):
        if status < 300:
            return _FakeResponse(status, body or b"{}")
        raise _http_error(status, body or b"{}")

    return fake_urlopen


class GetmeTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()

    def test_ok_passes_token_through_and_returns_status(self):
        token = "8406112058:" + "AAdGh-xyz_9" * 4
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            return _FakeResponse(200, b'{"ok": true}')

        with mock.patch.object(
            self.checker.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            status, description = self.checker.getme(token)

        self.assertEqual(status, 200)
        self.assertIsNone(description)
        self.assertIn(token, seen["url"])

    def test_http_error_surfaces_status_and_description_without_url(self):
        body = b'{"ok": false, "error_code": 401, "description": "Unauthorized"}'
        with mock.patch.object(
            self.checker.urllib.request,
            "urlopen",
            side_effect=_respond(401, body),
        ):
            status, description = self.checker.getme("8406112058:" + "AAdGh-xyz_9" * 4)

        self.assertEqual(status, 401)
        self.assertEqual(description, "Unauthorized")

    def test_transport_failure_reports_no_status(self):
        with mock.patch.object(
            self.checker.urllib.request,
            "urlopen",
            side_effect=OSError("DNS failure for api.telegram.org"),
        ):
            status, description = self.checker.getme("8406112058:" + "AAdGh-xyz_9" * 4)

        self.assertIsNone(status)
        self.assertIn("OSError", description)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_checker()
        self.token = "8406112058:" + "AAdGh-xyz_9" * 4

    def _run(self, env_token: str | None):
        env = {} if env_token is None else {"TELEGRAM_BOT_TOKEN": env_token}
        stderr, stdout = io.StringIO(), io.StringIO()
        with mock.patch.dict(self.checker.os.environ, env, clear=True):
            with redirect_stderr(stderr), redirect_stdout(stdout):
                code = self.checker.main()
        return code, stderr.getvalue(), stdout.getvalue()

    def test_no_token_configured_is_a_no_op(self):
        code, _, stdout = self._run(None)
        self.assertEqual(code, self.checker.EXIT_OK)
        self.assertEqual(stdout, "")

    def test_blank_token_is_a_no_op(self):
        code, _, _ = self._run("   ")
        self.assertEqual(code, self.checker.EXIT_OK)

    def test_ok_token_exits_zero_without_stdout(self):
        with mock.patch.object(
            self.checker.urllib.request, "urlopen", side_effect=_respond(200)
        ):
            code, _, stdout = self._run(self.token)
        self.assertEqual(code, self.checker.EXIT_OK)
        self.assertEqual(stdout, "")

    def test_rejected_token_exits_three_with_remediation(self):
        body = b'{"ok": false, "error_code": 401, "description": "Unauthorized"}'
        with mock.patch.object(
            self.checker.urllib.request, "urlopen", side_effect=_respond(401, body)
        ):
            code, stderr, stdout = self._run(self.token)
        self.assertEqual(code, self.checker.EXIT_REJECTED)
        self.assertEqual(stdout, "")
        self.assertIn("BotFather", stderr)
        self.assertIn("Environment", stderr)
        # The secret half must never be logged.
        self.assertNotIn(self.token.split(":", 1)[1], stderr)

    def test_transient_failure_exits_zero_and_keeps_quiet(self):
        with mock.patch.object(
            self.checker.urllib.request, "urlopen", side_effect=OSError("timeout")
        ):
            code, stderr, _ = self._run(self.token)
        self.assertEqual(code, self.checker.EXIT_OK)
        self.assertIn("inconclusive", stderr)

    def test_whitespace_wrapped_token_returns_trimmed_value_on_stdout(self):
        with mock.patch.object(
            self.checker.urllib.request, "urlopen", side_effect=_respond(200)
        ):
            code, _, stdout = self._run(f"  {self.token}\n")
        self.assertEqual(code, self.checker.EXIT_TRIMMED)
        self.assertEqual(stdout.strip(), self.token)

    def test_masked_token_is_rejected_without_network_call(self):
        with mock.patch.object(
            self.checker.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must not be touched"),
        ):
            code, stderr, _ = self._run("8406112058:***")
        self.assertEqual(code, self.checker.EXIT_REJECTED)
        self.assertIn("***", stderr)


if __name__ == "__main__":
    unittest.main()
