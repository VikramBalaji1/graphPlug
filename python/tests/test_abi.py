"""The ABI layer (ARCHITECTURE.md 13).

The layer most likely to fail catastrophically and least likely to fail visibly: its failure mode
is a dead interpreter, not a red assertion. These run against the built native library and skip
when it is absent, so a clean checkout on a machine that cannot build it still runs green.

    docker build -f build/Dockerfile -t msgraph-core-build .
    docker run --rm -v "$PWD/python/msgraph_simple/_lib:/dest" msgraph-core-build \\
           cp /out/libMicrosoftGraph.so /dest/
    python -m unittest discover -s python/tests
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from msgraph_simple import _native
from msgraph_simple._native import GraphError


try:
    # POSIX only. Absent on Windows, where the linux-x64 library cannot be loaded anyway.
    import resource
except ImportError:  # pragma: no cover - platform dependent
    resource = None  # type: ignore[assignment]


def _library_available() -> bool:
    try:
        _native._library_path()
    except GraphError:
        return False
    return True


requires_library = unittest.skipUnless(
    _library_available(), "the native library has not been built into msgraph_simple/_lib"
)

#: Enough to construct a session without reaching Entra. Nothing here is a real credential.
FAKE_CREDENTIALS = json.dumps({
    "type": "clientSecret",
    "tenantId": "11111111-1111-1111-1111-111111111111",
    "clientId": "22222222-2222-2222-2222-222222222222",
    "clientSecret": "not-a-real-secret",
})


def raw(name: str, *args: object) -> dict:
    """Call an export and decode its envelope without the raising wrapper."""
    library = _native.core()
    encoded = [a.encode("utf-8") if isinstance(a, str) else a for a in args]

    pointer = getattr(library, name)(*encoded)
    if not pointer:
        raise AssertionError(f"{name} returned a null pointer")

    try:
        return json.loads(ctypes.string_at(pointer).decode("utf-8"))
    finally:
        library.graph_free(pointer)


@requires_library
class NoManagedExceptionEscapes(unittest.TestCase):
    """The most important tests in the suite (6.2).

    A managed exception crossing into native frames terminates the process, so every one of these
    must come back as an error envelope. If any of them fails, it fails by killing the interpreter.
    """

    def assert_error(self, envelope: dict, code: str | None = None) -> None:
        self.assertFalse(envelope.get("ok", True), envelope)
        self.assertIn("error", envelope)
        if code is not None:
            self.assertEqual(envelope["error"]["code"], code)

    def test_malformed_json_returns_an_error_envelope(self) -> None:
        self.assert_error(raw("graph_client_create", "{ this is not json"), "invalidRequest")

    def test_a_null_pointer_returns_an_error_envelope(self) -> None:
        self.assert_error(raw("graph_client_create", None), "invalidRequest")

    def test_an_empty_string_returns_an_error_envelope(self) -> None:
        self.assert_error(raw("graph_client_create", ""), "invalidRequest")

    def test_an_unknown_credential_type_returns_an_error_envelope(self) -> None:
        self.assert_error(
            raw("graph_client_create", json.dumps({"type": "smokeSignals"})),
            "unsupportedCredentialType",
        )

    def test_an_unknown_handle_returns_an_error_envelope(self) -> None:
        self.assert_error(
            raw("graph_request", 999_999, json.dumps({"method": "GET", "path": "/me"})),
            "invalidHandle",
        )

    def test_an_unknown_flow_id_returns_an_error_envelope(self) -> None:
        self.assert_error(raw("graph_auth_complete", 999_999, "{}"), "invalidHandle")
        self.assert_error(raw("graph_auth_cancel", 999_999), "invalidHandle")

    def test_closing_an_unknown_handle_returns_an_error_envelope(self) -> None:
        self.assert_error(raw("graph_client_close", 999_999), "invalidHandle")

    def test_a_closed_handle_returns_an_error_envelope(self) -> None:
        created = raw("graph_client_create", FAKE_CREDENTIALS)
        self.assertTrue(created["ok"], created)
        handle = created["handle"]

        self.assertTrue(raw("graph_client_close", handle)["ok"])

        self.assert_error(
            raw("graph_request", handle, json.dumps({"method": "GET", "path": "/me"})),
            "invalidHandle",
        )

    def test_a_download_without_a_destination_returns_an_error_envelope(self) -> None:
        created = raw("graph_client_create", FAKE_CREDENTIALS)
        try:
            self.assert_error(
                raw("graph_download", created["handle"], json.dumps({"path": "/me/photo/$value"})),
                "invalidRequest",
            )
        finally:
            raw("graph_client_close", created["handle"])

    def test_an_upload_of_a_missing_file_returns_an_error_envelope(self) -> None:
        created = raw("graph_client_create", FAKE_CREDENTIALS)
        try:
            self.assert_error(
                raw("graph_upload", created["handle"],
                    json.dumps({"path": "/me/drive/root:/x:/content", "sourcePath": "/nope/x"})),
                "invalidRequest",
            )
        finally:
            raw("graph_client_close", created["handle"])


@requires_library
class HandleLifecycle(unittest.TestCase):
    def test_create_close_round_trips_and_reports_the_core_version(self) -> None:
        created = raw("graph_client_create", FAKE_CREDENTIALS)

        self.assertTrue(created["ok"], created)
        self.assertIsInstance(created["handle"], int)
        self.assertEqual(created["coreVersion"], _native.WHEEL_VERSION)

        self.assertTrue(raw("graph_client_close", created["handle"])["ok"])

    def test_handles_are_distinct(self) -> None:
        first = raw("graph_client_create", FAKE_CREDENTIALS)
        second = raw("graph_client_create", FAKE_CREDENTIALS)
        try:
            self.assertNotEqual(first["handle"], second["handle"])
        finally:
            raw("graph_client_close", first["handle"])
            raw("graph_client_close", second["handle"])

    def test_a_delegated_flow_can_be_begun_and_cancelled(self) -> None:
        begun = raw("graph_auth_begin", json.dumps({
            "type": "authorizationCode",
            "tenantId": "11111111-1111-1111-1111-111111111111",
            "clientId": "22222222-2222-2222-2222-222222222222",
            "scopes": ["User.Read"],
            "redirectUri": "http://localhost:8400",
        }))

        self.assertTrue(begun["ok"], begun)
        self.assertIn("authorizeUrl", begun)
        self.assertIn("state", begun)
        # The verifier is credential material and stays inside the core (7.5).
        self.assertNotIn("verifier", json.dumps(begun))

        self.assertTrue(raw("graph_auth_cancel", begun["flowId"])["ok"])

        # Cancelling twice must report, not crash.
        self.assertFalse(raw("graph_auth_cancel", begun["flowId"])["ok"])

    def test_a_delegated_envelope_without_scopes_is_rejected_at_the_boundary(self) -> None:
        begun = raw("graph_auth_begin", json.dumps({
            "type": "deviceCode",
            "tenantId": "11111111-1111-1111-1111-111111111111",
            "clientId": "22222222-2222-2222-2222-222222222222",
        }))

        self.assertFalse(begun["ok"], begun)
        self.assertEqual(begun["error"]["code"], "invalidRequest")


@requires_library
@unittest.skipIf(resource is None, "RSS measurement needs the POSIX resource module")
class MemoryOwnership(unittest.TestCase):
    """Checks that graph_free is wired correctly (6.4)."""

    @staticmethod
    def _rss_kib() -> int:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def test_a_repeated_create_and_close_loop_does_not_grow_rss(self) -> None:
        # Warm up, so one-off allocations are not mistaken for a leak.
        for _ in range(50):
            created = raw("graph_client_create", FAKE_CREDENTIALS)
            raw("graph_client_close", created["handle"])

        before = self._rss_kib()

        for _ in range(2_000):
            created = raw("graph_client_create", FAKE_CREDENTIALS)
            raw("graph_client_close", created["handle"])

        growth = self._rss_kib() - before
        self.assertLess(growth, 32 * 1024, f"RSS grew by {growth} KiB across 2000 round-trips")

    def test_error_envelopes_are_freed_too(self) -> None:
        before = self._rss_kib()

        for _ in range(2_000):
            raw("graph_request", 999_999, json.dumps({"method": "GET", "path": "/me"}))

        growth = self._rss_kib() - before
        self.assertLess(growth, 32 * 1024, f"RSS grew by {growth} KiB across 2000 failures")

    def test_freeing_null_is_a_no_op(self) -> None:
        _native.core().graph_free(None)


@requires_library
class CoreVersionAgreement(unittest.TestCase):
    def test_a_rejected_call_still_reports_the_version(self) -> None:
        # This is what the load-time check relies on: no session, no network, still a version.
        envelope = raw("graph_client_create", "")

        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["coreVersion"], _native.WHEEL_VERSION)

    def test_loading_the_library_verifies_the_pair(self) -> None:
        # core() runs the check on first use; reaching here means it agreed.
        self.assertIsNotNone(_native.core())

    def test_the_library_and_the_wheel_agree(self) -> None:
        created = raw("graph_client_create", FAKE_CREDENTIALS)
        try:
            self.assertEqual(
                created["coreVersion"],
                _native.WHEEL_VERSION,
                "the .so and the Python code ship as one unit and must match (6.6)",
            )
        finally:
            raw("graph_client_close", created["handle"])


@requires_library
@unittest.skipUnless(
    os.environ.get("AZURE_TENANT_ID"), "live tests need a tenant; set AZURE_TENANT_ID to run them"
)
class LiveGraph(unittest.TestCase):
    """Real Graph, real tenant. Skipped unless the environment supplies credentials (13)."""

    def test_app_only_can_list_users(self) -> None:
        from msgraph_simple import GraphClient

        with GraphClient.from_env() as client:
            users = list(client.paged("/users", select="id,mail", top=5))

        self.assertIsInstance(users, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
