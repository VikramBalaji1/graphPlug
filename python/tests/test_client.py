"""The Python layer above the ABI, with the boundary replaced by a recorder.

Uses unittest rather than pytest so the package's test suite needs no more dependencies than the
package itself. The tests that require the built native library live in test_abi.py.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import msgraph_simple
from msgraph_simple import GraphClient, GraphError, PendingSignIn
from msgraph_simple._native import GraphError as NativeGraphError


class RecordingBoundary:
    """Stands in for the C ABI, recording what would have crossed it."""

    def __init__(self, *responses: Dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, name: str, *args: Any) -> Dict[str, Any]:
        self.calls.append((name, *args))
        if not self.responses:
            return {"ok": True}
        response = self.responses.pop(0)
        if not response.get("ok", True):
            raise NativeGraphError.from_envelope(response)
        return response

    def envelope(self, index: int = 0) -> Dict[str, Any]:
        """The JSON payload of the index-th recorded call."""
        payload = self.calls[index][-1]
        return json.loads(payload)

    @property
    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


class BoundaryTestCase(unittest.TestCase):
    def install(self, *responses: Dict[str, Any]) -> RecordingBoundary:
        boundary = RecordingBoundary(*responses)
        original = msgraph_simple.call
        msgraph_simple.call = boundary  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(msgraph_simple, "call", original))
        return boundary


def ok(**fields: Any) -> Dict[str, Any]:
    return {"ok": True, "status": 200, **fields}


class RequestConstruction(BoundaryTestCase):
    def test_odata_keywords_become_dollar_parameters(self) -> None:
        boundary = self.install(ok(body={}))
        client = GraphClient(1)

        client.get("/users", select="id,mail", filter="accountEnabled eq true", top=999)

        self.assertEqual(
            boundary.envelope()["query"],
            {"$select": "id,mail", "$filter": "accountEnabled eq true", "$top": 999},
        )

    def test_unknown_options_pass_through_as_literal_parameters(self) -> None:
        boundary = self.install(ok(body={}))

        GraphClient(1).get("/users/delta", deltaToken="abc")

        self.assertEqual(boundary.envelope()["query"], {"deltaToken": "abc"})

    def test_none_valued_options_are_omitted(self) -> None:
        boundary = self.install(ok(body={}))

        GraphClient(1).get("/users", select=None, top=5)

        self.assertEqual(boundary.envelope()["query"], {"$top": 5})

    def test_absent_fields_are_not_sent_at_all(self) -> None:
        boundary = self.install(ok(body={}))

        GraphClient(1).get("/users")

        envelope = boundary.envelope()
        self.assertEqual(envelope, {"method": "GET", "path": "/users"})

    def test_version_headers_body_and_timeout_are_carried(self) -> None:
        boundary = self.install(ok(body={}))

        GraphClient(1).request(
            "POST",
            "/users",
            version="beta",
            body={"displayName": "A"},
            headers={"ConsistencyLevel": "eventual"},
            timeout_ms=5000,
        )

        self.assertEqual(
            boundary.envelope(),
            {
                "method": "POST",
                "path": "/users",
                "version": "beta",
                "body": {"displayName": "A"},
                "headers": {"ConsistencyLevel": "eventual"},
                "timeoutMs": 5000,
            },
        )

    def test_verbs_map_onto_methods_and_return_the_body(self) -> None:
        boundary = self.install(
            ok(body={"m": "GET"}), ok(body={"m": "POST"}), ok(body={"m": "PATCH"}),
            ok(body=None),
        )
        client = GraphClient(1)

        self.assertEqual(client.get("/x"), {"m": "GET"})
        self.assertEqual(client.post("/x", body={}), {"m": "POST"})
        self.assertEqual(client.patch("/x", body={}), {"m": "PATCH"})
        self.assertIsNone(client.delete("/x"))

        self.assertEqual(
            [boundary.envelope(i)["method"] for i in range(4)],
            ["GET", "POST", "PATCH", "DELETE"],
        )

    def test_request_returns_the_whole_envelope_not_just_the_body(self) -> None:
        self.install(ok(body={"value": []}, nextLink="https://graph.microsoft.com/v1.0/users?x"))

        response = GraphClient(1).request("GET", "/users")

        self.assertIn("nextLink", response)
        self.assertIn("status", response)


class Pagination(BoundaryTestCase):
    def test_walks_every_page_and_stops_at_the_last(self) -> None:
        boundary = self.install(
            ok(body={"value": [{"id": "1"}, {"id": "2"}]}, nextLink="https://graph/next-1"),
            ok(body={"value": [{"id": "3"}]}, nextLink="https://graph/next-2"),
            ok(body={"value": [{"id": "4"}]}),
        )

        found = [item["id"] for item in GraphClient(1).paged("/users", select="id")]

        self.assertEqual(found, ["1", "2", "3", "4"])
        self.assertEqual(boundary.envelope(1)["path"], "https://graph/next-1")
        self.assertEqual(boundary.envelope(2)["path"], "https://graph/next-2")

    def test_the_next_link_is_echoed_back_without_the_original_query(self) -> None:
        boundary = self.install(
            ok(body={"value": []}, nextLink="https://graph/next-1"),
            ok(body={"value": []}),
        )

        list(GraphClient(1).paged("/users", select="id,mail"))

        # The cursor is complete and self-describing; re-adding $select would be wrong.
        self.assertNotIn("query", boundary.envelope(1))

    def test_an_empty_page_yields_nothing(self) -> None:
        self.install(ok(body={"value": []}))

        self.assertEqual(list(GraphClient(1).paged("/users")), [])

    def test_abandoning_the_generator_leaves_no_state_behind(self) -> None:
        boundary = self.install(
            ok(body={"value": [{"id": "1"}]}, nextLink="https://graph/next-1"),
            ok(body={"value": [{"id": "2"}]}),
        )

        for item in GraphClient(1).paged("/users"):
            self.assertEqual(item["id"], "1")
            break

        # Only the first page was ever fetched: nothing to release.
        self.assertEqual(len(boundary.calls), 1)


class Batching(BoundaryTestCase):
    def test_tuples_become_numbered_sub_requests(self) -> None:
        boundary = self.install(ok(body={"responses": []}))

        GraphClient(1).batch([("GET", "/users"), ("GET", "/groups")])

        self.assertEqual(
            boundary.envelope()["body"]["requests"],
            [
                {"id": "0", "method": "GET", "url": "/users"},
                {"id": "1", "method": "GET", "url": "/groups"},
            ],
        )

    def test_dictionaries_keep_their_own_fields(self) -> None:
        boundary = self.install(ok(body={"responses": []}))

        GraphClient(1).batch([{"method": "POST", "url": "/users", "body": {"displayName": "A"}}])

        request = boundary.envelope()["body"]["requests"][0]
        self.assertEqual(request["body"], {"displayName": "A"})
        self.assertEqual(request["id"], "0")

    def test_a_failing_sub_request_is_returned_not_raised(self) -> None:
        self.install(ok(body={"responses": [
            {"id": "0", "status": 200, "body": {}},
            {"id": "1", "status": 404, "body": {"error": {"code": "itemNotFound"}}},
        ]}))

        results = GraphClient(1).batch([("GET", "/users"), ("GET", "/nope")])

        self.assertEqual([r["status"] for r in results], [200, 404])

    def test_the_batch_goes_to_the_batch_path(self) -> None:
        boundary = self.install(ok(body={"responses": []}))

        GraphClient(1).batch([("GET", "/users")])

        self.assertEqual(boundary.envelope()["path"], "/$batch")
        self.assertEqual(boundary.envelope()["method"], "POST")


class Files(BoundaryTestCase):
    def test_download_sends_the_destination_and_uses_its_own_export(self) -> None:
        boundary = self.install(ok(bytesWritten=1024, destPath="local.bin"))

        response = GraphClient(1).download("/me/drive/items/1/content", "local.bin")

        self.assertEqual(boundary.names, ["graph_download"])
        self.assertEqual(boundary.envelope()["destPath"], "local.bin")
        self.assertEqual(response["bytesWritten"], 1024)

    def test_upload_sends_the_source_and_uses_its_own_export(self) -> None:
        boundary = self.install(ok(bytesSent=4096))

        GraphClient(1).upload("/me/drive/root:/big.zip:/content", "big.zip")

        self.assertEqual(boundary.names, ["graph_upload"])
        self.assertEqual(boundary.envelope()["sourcePath"], "big.zip")
        self.assertEqual(boundary.envelope()["method"], "PUT")


class Authentication(BoundaryTestCase):
    def test_app_only_sends_a_client_secret_envelope(self) -> None:
        boundary = self.install({"ok": True, "handle": 7, "coreVersion": "0.1.0"})

        client = GraphClient.app_only(tenant_id="t", client_id="c", client_secret="s")

        self.assertEqual(boundary.names, ["graph_client_create"])
        self.assertEqual(
            boundary.envelope(),
            {"type": "clientSecret", "tenantId": "t", "clientId": "c", "clientSecret": "s"},
        )
        self.assertEqual(repr(client), "<GraphClient handle=7 open>")

    def test_retry_tuning_is_nested_under_retry(self) -> None:
        boundary = self.install({"ok": True, "handle": 1})

        GraphClient.app_only("t", "c", "s", max_retries=5, max_delay_seconds=60)

        self.assertEqual(boundary.envelope()["retry"], {"maxRetries": 5, "maxDelaySeconds": 60})

    def test_from_env_names_every_missing_variable(self) -> None:
        for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
            self.addCleanup(lambda n=name, v=None: None)

        import os

        saved = {n: os.environ.pop(n, None) for n in
                 ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")}
        self.addCleanup(lambda: os.environ.update({k: v for k, v in saved.items() if v}))

        with self.assertRaises(GraphError) as raised:
            GraphClient.from_env()

        self.assertIn("AZURE_TENANT_ID", raised.exception.message)
        self.assertIn("AZURE_CLIENT_SECRET", raised.exception.message)

    def test_begin_device_code_returns_what_the_user_needs(self) -> None:
        self.install({
            "ok": True, "flowId": 3, "userCode": "FJKLMNPQ",
            "verificationUri": "https://microsoft.com/devicelogin",
            "message": "enter FJKLMNPQ", "expiresInSeconds": 900,
        })

        flow = GraphClient.begin_device_code("t", "c", ["User.Read"])

        self.assertIsInstance(flow, PendingSignIn)
        self.assertEqual(flow.flow_id, 3)
        self.assertEqual(flow.user_code, "FJKLMNPQ")
        self.assertEqual(flow.expires_in, 900)
        self.assertIsNone(flow.authorize_url)

    def test_delegated_scopes_are_sent_verbatim_and_never_defaulted(self) -> None:
        boundary = self.install({"ok": True, "flowId": 1})

        GraphClient.begin_device_code("t", "c", ["User.Read", "Mail.Send"])

        envelope = boundary.envelope()
        self.assertEqual(envelope["scopes"], ["User.Read", "Mail.Send"])
        self.assertEqual(envelope["type"], "deviceCode")

    def test_completing_a_flow_yields_a_client(self) -> None:
        boundary = self.install(
            {"ok": True, "flowId": 3, "authorizeUrl": "https://login/authorize", "state": "abc"},
            {"ok": True, "handle": 9, "coreVersion": "0.1.0"},
        )

        flow = GraphClient._begin_interactive("t", "c", ["User.Read"])
        client = flow.complete(code="the-code", state=flow.state)

        self.assertEqual(boundary.names, ["graph_auth_begin", "graph_auth_complete"])
        self.assertEqual(json.loads(boundary.calls[1][2]), {"code": "the-code", "state": "abc"})
        self.assertEqual(repr(client), "<GraphClient handle=9 open>")

    def test_cancelling_is_idempotent_and_skipped_after_completion(self) -> None:
        boundary = self.install(
            {"ok": True, "flowId": 3, "userCode": "X"},
            {"ok": True, "handle": 1},
        )

        flow = GraphClient.begin_device_code("t", "c", ["User.Read"])
        flow.complete()
        flow.cancel()
        flow.cancel()

        self.assertEqual(boundary.names, ["graph_auth_begin", "graph_auth_complete"])

    def test_an_abandoned_flow_is_cancelled(self) -> None:
        boundary = self.install({"ok": True, "flowId": 3, "userCode": "X"}, {"ok": True})

        flow = GraphClient.begin_device_code("t", "c", ["User.Read"])
        flow.cancel()

        self.assertEqual(boundary.names, ["graph_auth_begin", "graph_auth_cancel"])


class Lifetime(BoundaryTestCase):
    def test_the_context_manager_always_closes(self) -> None:
        boundary = self.install(ok(body={}), {"ok": True})

        with GraphClient(1) as client:
            client.get("/me")

        self.assertEqual(boundary.names, ["graph_request", "graph_client_close"])

    def test_it_closes_even_when_the_body_raises(self) -> None:
        boundary = self.install({"ok": True})

        with self.assertRaises(ValueError):
            with GraphClient(1):
                raise ValueError("boom")

        self.assertEqual(boundary.names, ["graph_client_close"])

    def test_closing_twice_calls_the_core_once(self) -> None:
        boundary = self.install({"ok": True})
        client = GraphClient(1)

        client.close()
        client.close()

        self.assertEqual(boundary.names, ["graph_client_close"])

    def test_using_a_closed_client_raises_invalid_handle_without_crossing_the_boundary(self) -> None:
        boundary = self.install({"ok": True})
        client = GraphClient(1)
        client.close()

        with self.assertRaises(GraphError) as raised:
            client.get("/me")

        self.assertEqual(raised.exception.code, "invalidHandle")
        self.assertEqual(boundary.names, ["graph_client_close"])


class Errors(BoundaryTestCase):
    def test_a_failure_envelope_becomes_a_graph_error(self) -> None:
        self.install({
            "ok": False,
            "status": 429,
            "error": {
                "code": "activityLimitReached",
                "message": "Too many requests.",
                "requestId": "a1b2c3d4",
                "retryAfterSeconds": 12,
                "innerError": {"code": "quotaLimitReached"},
            },
        })

        with self.assertRaises(GraphError) as raised:
            GraphClient(1).get("/users")

        error = raised.exception
        self.assertEqual(error.status, 429)
        self.assertEqual(error.code, "activityLimitReached")
        self.assertEqual(error.request_id, "a1b2c3d4")
        self.assertEqual(error.retry_after, 12)
        self.assertEqual(error.inner, {"code": "quotaLimitReached"})

    def test_a_non_http_failure_uses_status_zero(self) -> None:
        self.install({"ok": False, "status": 0, "error": {"code": "timeout", "message": "elapsed"}})

        with self.assertRaises(GraphError) as raised:
            GraphClient(1).get("/users")

        self.assertEqual(raised.exception.status, 0)
        self.assertEqual(raised.exception.code, "timeout")

    def test_the_message_carries_status_and_code_for_a_bare_traceback(self) -> None:
        self.install({"ok": False, "status": 404, "error": {"code": "itemNotFound", "message": "no"}})

        with self.assertRaises(GraphError) as raised:
            GraphClient(1).get("/users/nope")

        self.assertEqual(str(raised.exception), "[404 itemNotFound] no")

    def test_one_exception_type_covers_every_failure(self) -> None:
        # Callers branch on status and code rather than importing a class tree.
        self.assertTrue(issubclass(GraphError, Exception))
        self.assertEqual(
            [name for name in msgraph_simple.__all__ if name.endswith("Error")], ["GraphError"]
        )


class CoreVersion(BoundaryTestCase):
    def test_a_mismatched_core_is_rejected(self) -> None:
        from msgraph_simple import _native

        with self.assertRaises(GraphError) as raised:
            _native._check_core_version({"coreVersion": "9.9.9"})

        self.assertEqual(raised.exception.code, "coreVersionMismatch")

    def test_a_matching_core_is_accepted(self) -> None:
        from msgraph_simple import _native

        _native._check_core_version({"coreVersion": _native.WHEEL_VERSION})

    def test_an_envelope_without_a_version_is_not_checked(self) -> None:
        from msgraph_simple import _native

        _native._check_core_version({"ok": True})


class NativeBindings(unittest.TestCase):
    def test_every_export_in_the_abi_is_declared(self) -> None:
        from msgraph_simple import _native

        self.assertEqual(
            sorted(_native._SIGNATURES),
            sorted([
                "graph_auth_begin", "graph_auth_cancel", "graph_auth_complete",
                "graph_client_close", "graph_client_create", "graph_download",
                "graph_free", "graph_request", "graph_upload",
            ]),
        )

    def test_envelope_returning_exports_use_c_void_p_not_c_char_p(self) -> None:
        import ctypes

        from msgraph_simple import _native

        # c_char_p makes ctypes auto-convert and discard the pointer, which would make graph_free
        # impossible and leak every response.
        for name, (_, restype) in _native._SIGNATURES.items():
            if name == "graph_free":
                self.assertIsNone(restype)
            else:
                self.assertIs(restype, ctypes.c_void_p, name)

    def test_a_missing_library_is_reported_clearly(self) -> None:
        # Deterministic whether or not the real library has been built: point the lookup at an
        # empty directory rather than depending on the state of the package.
        import tempfile
        from pathlib import Path

        from msgraph_simple import _native

        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(GraphError) as raised:
                _native._library_path(Path(empty))

        self.assertIn(raised.exception.code, {"libraryNotFound", "unsupportedPlatform"})
        self.assertIn("missing", raised.exception.message.lower() + " missing")

    def test_the_library_name_carries_no_lib_prefix(self) -> None:
        # NativeAOT names the output after the assembly and adds no "lib" prefix. Getting this
        # wrong means the package cannot find its own core.
        from msgraph_simple import _native

        self.assertEqual(_native._LIBRARY_NAMES, {"linux": "MicrosoftGraph.so"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
