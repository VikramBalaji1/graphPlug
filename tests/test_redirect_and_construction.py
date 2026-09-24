"""The parts of sign-in that need no tenant: the loopback listener, the PKCE code exchange, and
building a client.

`test_signin.py` covers the two-phase orchestration. What is left over is code that runs entirely
on this machine and was going untested for no good reason -- the redirect listener is real stdlib
HTTP on 127.0.0.1, so it can simply be run.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import socket
import threading
import time
import unittest

import httpx

from _support import FakeCredential
from test_signin import StubDeviceCodeCredential

from msgraph_simple import GraphClient, GraphError
from msgraph_simple import _auth


def free_port() -> int:
    """An ephemeral port the OS has just released, for the listener to claim."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def get_once(url: str, attempts: int = 100) -> httpx.Response:
    """GET, retrying while the listener is still binding its socket."""
    for _ in range(attempts):
        try:
            async with httpx.AsyncClient() as http:
                return await http.get(url, timeout=2.0)
        except httpx.ConnectError:
            await asyncio.sleep(0.02)
    raise AssertionError(f"nothing ever listened on {url}")


# ── the loopback redirect listener ───────────────────────────────────────────


class RedirectListener(unittest.IsolatedAsyncioTestCase):
    """Real sockets on loopback. No tenant, and no network beyond this machine."""

    async def test_it_returns_the_query_the_browser_arrived_with(self) -> None:
        port = free_port()
        listening = asyncio.create_task(_auth.wait_for_redirect(f"http://localhost:{port}", 10))
        await get_once(f"http://127.0.0.1:{port}/?code=the-code&state=the-state")

        received = await asyncio.wait_for(listening, timeout=5)
        self.assertEqual(received, {"code": "the-code", "state": "the-state"})

    async def test_the_person_gets_a_page_back_rather_than_a_dead_tab(self) -> None:
        port = free_port()
        listening = asyncio.create_task(_auth.wait_for_redirect(f"http://localhost:{port}", 10))
        response = await get_once(f"http://127.0.0.1:{port}/?code=c&state=s")
        await asyncio.wait_for(listening, timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Signed in", response.text)

    async def test_it_binds_loopback_whatever_host_the_uri_names(self) -> None:
        # The redirect URI is Entra's to match. It is not an instruction about what to listen on.
        port = free_port()
        listening = asyncio.create_task(
            _auth.wait_for_redirect(f"http://example.invalid:{port}/callback", 10)
        )
        await get_once(f"http://127.0.0.1:{port}/callback?code=c&state=s")

        self.assertEqual((await asyncio.wait_for(listening, timeout=5))["code"], "c")

    async def test_a_redirect_uri_with_no_port_is_refused(self) -> None:
        with self.assertRaises(GraphError) as raised:
            await _auth.wait_for_redirect("http://localhost/callback", 10)
        self.assertEqual(raised.exception.code, "invalidRequest")

    async def test_nobody_arriving_times_out_rather_than_waiting_for_ever(self) -> None:
        with self.assertRaises(GraphError) as raised:
            await _auth.wait_for_redirect(f"http://localhost:{free_port()}", 0.3)
        self.assertEqual(raised.exception.code, "signInTimeout")

    async def test_an_empty_query_still_settles(self) -> None:
        # A bare GET carries nothing. Whether that is a failure is the caller's call, not the
        # listener's -- it reports what arrived.
        port = free_port()
        listening = asyncio.create_task(_auth.wait_for_redirect(f"http://localhost:{port}", 10))
        await get_once(f"http://127.0.0.1:{port}/")
        self.assertEqual(await asyncio.wait_for(listening, timeout=5), {})

    def test_a_machine_with_no_browser_says_so_instead_of_raising(self) -> None:
        original = _auth.webbrowser.open

        def explode(_url):
            raise RuntimeError("no display")

        _auth.webbrowser.open = explode
        try:
            self.assertFalse(_auth.open_browser("https://login.microsoftonline.com"))
        finally:
            _auth.webbrowser.open = original


# ── the PKCE code exchange ───────────────────────────────────────────────────


class StubMsalApp:
    """Stands in for msal.PublicClientApplication."""

    last = None
    exchange_result: dict = {}
    silent_result = None

    def __init__(self, client_id, authority=None, **_):
        self.client_id = client_id
        self.authority = authority
        self.exchange: dict = {}
        type(self).last = self

    def acquire_token_by_authorization_code(
        self, code, scopes=None, redirect_uri=None, code_verifier=None, **_
    ):
        self.exchange = {
            "code": code,
            "scopes": scopes,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        return dict(type(self).exchange_result)

    def get_accounts(self):
        return [{"username": "alice@contoso.com"}]

    def acquire_token_silent(self, scopes, account, **_):
        result = type(self).silent_result
        return dict(result) if result else None


class CodeExchange(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import msal

        self._real = msal.PublicClientApplication
        msal.PublicClientApplication = StubMsalApp
        StubMsalApp.exchange_result = {
            "access_token": "delegated-token",
            "expires_in": 3600,
            "id_token_claims": {"oid": "1"},
        }
        StubMsalApp.silent_result = {"access_token": "refreshed-token", "expires_in": 3600}

    def tearDown(self) -> None:
        import msal

        msal.PublicClientApplication = self._real

    async def test_the_verifier_is_handed_to_msal_and_the_authority_is_built(self) -> None:
        await _auth.exchange_code(
            "tenant-1", "client-1", ["User.Read"], "http://localhost:8400",
            "the-code", "the-verifier",
        )

        app = StubMsalApp.last
        self.assertEqual(app.authority, "https://login.microsoftonline.com/tenant-1")
        self.assertEqual(app.exchange["code_verifier"], "the-verifier")
        self.assertEqual(app.exchange["redirect_uri"], "http://localhost:8400")
        # offline_access belongs on the authorize call, not here: MSAL rejects reserved scopes.
        self.assertEqual(app.exchange["scopes"], ["User.Read"])

    async def test_the_credential_refreshes_from_the_cache(self) -> None:
        credential = await _auth.exchange_code(
            "t", "c", ["User.Read"], "http://localhost:8400", "code", "verifier"
        )
        token = await credential.get_token("User.Read")

        self.assertEqual(token.token, "refreshed-token")
        self.assertIsNone(await credential.close())

    async def test_a_cache_that_can_no_longer_refresh_says_to_sign_in_again(self) -> None:
        credential = await _auth.exchange_code(
            "t", "c", ["User.Read"], "http://localhost:8400", "code", "verifier"
        )
        StubMsalApp.silent_result = None

        with self.assertRaises(GraphError) as raised:
            await credential.get_token("User.Read")
        self.assertEqual(raised.exception.code, "interactionRequired")

    async def test_a_refused_exchange_maps_onto_a_defined_code(self) -> None:
        StubMsalApp.exchange_result = {
            "error": "invalid_grant",
            "error_description": "AADSTS65004: the user declined the consent prompt",
        }
        with self.assertRaises(GraphError) as raised:
            await _auth.exchange_code(
                "t", "c", ["User.Read"], "http://localhost:8400", "code", "verifier"
            )
        self.assertEqual(raised.exception.code, "signInDeclined")
        self.assertIn("AADSTS65004", raised.exception.message)


# ── building a client ────────────────────────────────────────────────────────

_ENV = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")


class Construction(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._kept = {name: os.environ.pop(name, None) for name in _ENV}

    def tearDown(self) -> None:
        for name, value in self._kept.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    async def test_from_env_names_every_variable_that_is_missing(self) -> None:
        with self.assertRaises(GraphError) as raised:
            GraphClient.from_env()
        for name in _ENV:
            self.assertIn(name, raised.exception.message)

    async def test_from_env_builds_a_client(self) -> None:
        os.environ.update(dict(zip(_ENV, ("t", "c", "s"))))

        graph = GraphClient.from_env()
        self.assertIsInstance(graph, GraphClient)
        self.assertIsNotNone(graph.files)
        await graph.aclose()

    async def test_app_only_requires_each_of_its_three_values(self) -> None:
        for missing, args in (
            ("tenant_id", ("", "c", "s")),
            ("client_id", ("t", "", "s")),
            ("client_secret", ("t", "c", "")),
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(GraphError) as raised:
                    GraphClient.app_only(*args)
                self.assertIn(missing, raised.exception.message)


class BringYourOwnCredential(unittest.IsolatedAsyncioTestCase):
    """from_credential is how managed identity, certificates and OBO reach this package."""

    async def test_an_async_credential_is_used_as_it_is(self) -> None:
        credential = FakeCredential()
        async with GraphClient.from_credential(credential) as graph:
            self.assertIs(graph._transport._credential, credential)

    async def test_a_synchronous_credential_is_adapted(self) -> None:
        # Both spellings exist in azure-identity for every flow and the sync one is easy to reach
        # for by mistake. Unadapted, the failure is an un-awaited coroutine at the first request,
        # which names neither the cause nor the fix.
        class SyncCredential:
            def get_token(self, *_scopes, **_kw):
                from azure.core.credentials import AccessToken

                return AccessToken("sync-token", int(time.time()) + 3600)

        async with GraphClient.from_credential(SyncCredential()) as graph:
            token = await graph._transport._credential.get_token("x")
        self.assertEqual(token.token, "sync-token")

    async def test_something_that_is_not_a_credential_is_refused(self) -> None:
        for bad in (None, object(), "a-raw-token"):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(GraphError) as raised:
                    GraphClient.from_credential(bad)
                self.assertEqual(raised.exception.code, "invalidRequest")

    async def test_the_scope_defaults_to_the_application_level_one(self) -> None:
        async with GraphClient.from_credential(FakeCredential()) as graph:
            self.assertEqual(graph._transport._scopes,
                             ("https://graph.microsoft.com/.default",))

        async with GraphClient.from_credential(FakeCredential(), scopes=["Mail.Send"]) as graph:
            self.assertEqual(graph._transport._scopes, ("Mail.Send",))

    async def test_the_resource_layer_is_attached_however_the_client_was_built(self) -> None:
        async with GraphClient.from_credential(FakeCredential()) as graph:
            for name in ("mail", "calendar", "files", "teams", "users"):
                self.assertIsNotNone(getattr(graph, name), name)


class DeviceCodeConvenience(unittest.IsolatedAsyncioTestCase):
    """The one-call form over begin/complete."""

    def setUp(self) -> None:
        StubDeviceCodeCredential.fails_with = None
        StubDeviceCodeCredential.issues_code = True
        StubDeviceCodeCredential.gate = None
        self._real = _auth._DeviceCodeSignIn.credential_class
        _auth._DeviceCodeSignIn.credential_class = StubDeviceCodeCredential

    def tearDown(self) -> None:
        _auth._DeviceCodeSignIn.credential_class = self._real
        StubDeviceCodeCredential.gate = None

    async def test_it_prints_the_code_and_returns_a_client(self) -> None:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            graph = await asyncio.wait_for(
                GraphClient.device_code("t", "c", ["User.Read"]), timeout=5
            )

        self.assertIn("FJKLMNPQ", printed.getvalue())
        self.assertIsInstance(graph, GraphClient)
        await graph.aclose()

    async def test_a_refusal_after_the_code_surfaces_the_reason(self) -> None:
        gate = threading.Event()
        gate.set()
        StubDeviceCodeCredential.gate = gate
        StubDeviceCodeCredential.fails_with = ValueError("AADSTS65004: user declined")

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(GraphError) as raised:
                await asyncio.wait_for(
                    GraphClient.device_code("t", "c", ["User.Read"]), timeout=5
                )
        self.assertEqual(raised.exception.code, "signInDeclined")


if __name__ == "__main__":
    unittest.main(verbosity=2)
