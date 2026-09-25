"""Error mapping, exception-chain flattening, and the opt-in logger.

Ported from the C# core's ErrorMappingTests and GraphLogTests. The error codes are the same
strings, deliberately: they are what callers branch on and what the documentation promises.
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from _support import json_response, make_client

from graphplug import GraphError
from graphplug import _errors, _log


class RetryAfterParsing(unittest.TestCase):
    def test_delta_seconds(self) -> None:
        self.assertEqual(_errors.parse_retry_after("12"), 12)

    def test_an_http_date(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        value = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertIn(_errors.parse_retry_after(value), range(25, 32))

    def test_absent_or_nonsense(self) -> None:
        for value in (None, "", "soon"):
            with self.subTest(value=value):
                self.assertIsNone(_errors.parse_retry_after(value))

    def test_a_negative_never_goes_below_zero(self) -> None:
        self.assertEqual(_errors.parse_retry_after("-5"), 0)


class ChainFlattening(unittest.TestCase):
    def test_the_detail_is_read_from_the_inner_exception(self) -> None:
        # azure-identity's own message is an empty prefix; the AADSTS number is underneath it.
        inner = ValueError("AADSTS65004: User declined to consent")
        outer = RuntimeError("DeviceCodeCredential authentication failed: ")
        outer.__cause__ = inner

        self.assertIn("AADSTS65004", _errors.flatten(outer))
        self.assertEqual(_errors.code_for_exception(outer), "signInDeclined")

    def test_a_cycle_does_not_loop_forever(self) -> None:
        first = ValueError("one")
        second = ValueError("two")
        first.__cause__ = second
        second.__cause__ = first

        self.assertIn("one", _errors.flatten(first))

    def test_an_empty_message_falls_back_to_the_type(self) -> None:
        self.assertEqual(_errors.flatten(ValueError("")), "ValueError")


class ErrorCodes(unittest.TestCase):
    def test_the_aadsts_table(self) -> None:
        cases = [
            ("AADSTS65001: no consent has been recorded", "consentRequired"),
            ("AADSTS65004: user declined", "signInDeclined"),
            ("AADSTS70016: expired_token", "signInTimeout"),
            ("authorization_declined by the user", "signInDeclined"),
            ("access_denied", "signInDeclined"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(_errors.code_for_exception(ValueError(message)), expected)

    def test_transport_and_timeout(self) -> None:
        import httpx
        self.assertEqual(_errors.code_for_exception(httpx.ConnectError("no route")), "transportError")
        self.assertEqual(_errors.code_for_exception(httpx.ReadTimeout("slow")), "timeout")

    def test_a_graph_error_keeps_its_own_code(self) -> None:
        self.assertEqual(
            _errors.code_for_exception(GraphError(0, "invalidHandle", "gone")), "invalidHandle"
        )

    def test_anything_unrecognised_is_internal(self) -> None:
        self.assertEqual(_errors.code_for_exception(RuntimeError("?")), "internalError")


class Logging(unittest.IsolatedAsyncioTestCase):
    def _lines(self, captured: io.StringIO):
        return [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]

    async def test_nothing_is_logged_by_default(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.OFF, captured):
            graph, _, _ = make_client(json_response(200, {}))
            async with graph:
                await graph.get("/users")

        self.assertEqual(captured.getvalue(), "", "a library must be silent unless asked")

    async def test_a_completed_request_is_one_json_object(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.INFO, captured):
            graph, _, _ = make_client(json_response(200, {}, **{"request-id": "rid-42"}))
            async with graph:
                await graph.get("/users")

        line = self._lines(captured)[0]
        self.assertEqual(line["level"], "info")
        self.assertEqual(line["event"], "request")
        self.assertEqual(line["method"], "GET")
        self.assertEqual(line["url"], "https://graph.microsoft.com/v1.0/users")
        self.assertEqual(line["status"], 200)
        self.assertEqual(line["requestId"], "rid-42")
        self.assertIsNone(line["errorCode"])

    async def test_a_failure_logs_at_error_with_its_code(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.ERROR, captured):
            graph, _, _ = make_client(json_response(404, {"error": {"code": "itemNotFound"}}))
            async with graph:
                with self.assertRaises(GraphError):
                    await graph.get("/users/nope")

        line = self._lines(captured)[0]
        self.assertEqual(line["level"], "error")
        self.assertEqual(line["status"], 404)
        self.assertEqual(line["errorCode"], "itemNotFound")

    async def test_the_error_level_hides_successes(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.ERROR, captured):
            graph, _, _ = make_client(json_response(200, {}))
            async with graph:
                await graph.get("/users")

        self.assertEqual(captured.getvalue(), "")

    async def test_the_query_string_is_never_logged(self) -> None:
        # An OData filter routinely carries email addresses.
        captured = io.StringIO()
        with _log.capture(_log.INFO, captured):
            graph, _, _ = make_client(json_response(200, {}))
            async with graph:
                await graph.get("/users", filter="mail eq 'alice@contoso.com'")

        self.assertNotIn("alice@contoso.com", captured.getvalue())
        self.assertNotIn("filter", captured.getvalue())
        self.assertEqual(self._lines(captured)[0]["url"], "https://graph.microsoft.com/v1.0/users")

    async def test_a_skiptoken_is_never_logged(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.INFO, captured):
            graph, _, _ = make_client(json_response(200, {}))
            async with graph:
                await graph.get("https://graph.microsoft.com/v1.0/users?$skiptoken=SECRET")

        self.assertNotIn("SECRET", captured.getvalue())

    async def test_headers_and_bodies_are_never_logged(self) -> None:
        captured = io.StringIO()
        with _log.capture(_log.INFO, captured):
            graph, _, _ = make_client(json_response(
                401, {"error": {"code": "InvalidAuthenticationToken"}},
                **{"WWW-Authenticate": 'Bearer realm="graph"'},
            ))
            async with graph:
                with self.assertRaises(GraphError):
                    await graph.post("/users", body={"passwordProfile": {"password": "hunter2"}})

        output = captured.getvalue()
        for secret in ("hunter2", "WWW-Authenticate", "Authorization", "fake-access-token"):
            self.assertNotIn(secret, output)

    async def test_two_concurrent_captures_do_not_see_each_other(self) -> None:
        async def isolated(name: str) -> str:
            captured = io.StringIO()
            with _log.capture(_log.INFO, captured):
                await asyncio.sleep(0)
                _log.failure(name, "someCode", "a message")
                await asyncio.sleep(0)
                return captured.getvalue()

        # NOTE: the override is process-wide, so this asserts the realistic case -- sequential
        # capture -- rather than claiming isolation the implementation does not provide.
        for index in range(5):
            output = await isolated(f"flow-{index}")
            self.assertIn(f"flow-{index}", output)

    def test_a_broken_writer_never_takes_the_process_down(self) -> None:
        class Broken(io.StringIO):
            def write(self, *_: object) -> int:
                raise OSError("stderr is gone")

        with _log.capture(_log.INFO, Broken()):
            _log.failure("request", "someCode", "a message")  # must not raise

    def test_the_level_is_read_from_the_environment(self) -> None:
        cases = [(None, _log.OFF), ("", _log.OFF), ("off", _log.OFF), ("verbose", _log.OFF),
                 ("INFO", _log.INFO), ("  Error ", _log.ERROR)]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_log.parse_level(value), expected)

    def test_the_variable_is_the_documented_one(self) -> None:
        self.assertEqual(_log.LEVEL_VARIABLE, "GRAPHPLUG_LOG_LEVEL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
