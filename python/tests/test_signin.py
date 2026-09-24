"""The two-phase sign-in orchestration, and the PKCE pieces.

The credential itself is azure-identity's and not ours to test. What *is* ours is the
orchestration around it: returning the code without waiting for the human, not hanging when the
sign-in fails before a code is ever issued, cancelling cleanly, and keeping the PKCE verifier
inside the process. A stub credential stands in for Entra so all of that can be exercised.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from _support import json_response, make_client

from msgraph_simple import GraphClient, GraphError
from msgraph_simple import _auth

import threading


class StubDeviceCodeCredential:
    """Stands in for azure.identity.DeviceCodeCredential.

    Mirrors its real contract: prompt_callback fires with (verification_uri, user_code,
    expires_on) as soon as a code exists, and authenticate() blocks until the person finishes.
    """

    #: Set to an exception to make authenticate() fail.
    fails_with: BaseException = None
    #: Set False to fail *before* the callback, the way a bad tenant does.
    issues_code: bool = True
    #: Blocks authenticate() until released, standing in for the human.
    gate = None

    def __init__(self, client_id=None, tenant_id=None, prompt_callback=None, **kwargs):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self._callback = prompt_callback
        self.authenticate_calls = 0
        self.closed = False

    def authenticate(self, scopes=None, **kwargs):
        self.authenticate_calls += 1
        if type(self).issues_code and self._callback:
            self._callback("https://microsoft.com/devicelogin", "FJKLMNPQ", None)
        if type(self).gate is not None:
            type(self).gate.wait(timeout=10)
        if type(self).fails_with is not None:
            raise type(self).fails_with
        return {"username": "alice@contoso.com"}

    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken
        import time
        return AccessToken("stub-delegated-token", int(time.time()) + 3600)

    def close(self):
        self.closed = True


class DeviceCodeOrchestration(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        StubDeviceCodeCredential.fails_with = None
        StubDeviceCodeCredential.issues_code = True
        StubDeviceCodeCredential.gate = None
        self._real = _auth._DeviceCodeSignIn.credential_class
        _auth._DeviceCodeSignIn.credential_class = StubDeviceCodeCredential

    def tearDown(self) -> None:
        _auth._DeviceCodeSignIn.credential_class = self._real
        StubDeviceCodeCredential.gate = None

    async def test_begin_returns_the_code_without_waiting_for_the_person(self) -> None:
        # The gate is never released, so authenticate() is still blocked when begin() returns.
        StubDeviceCodeCredential.gate = threading.Event()

        flow, begun = await asyncio.wait_for(
            _auth.device_code_begin("t", "c", ["User.Read"]), timeout=5
        )

        self.assertEqual(begun["userCode"], "FJKLMNPQ")
        self.assertEqual(begun["verificationUri"], "https://microsoft.com/devicelogin")
        self.assertIn("FJKLMNPQ", begun["message"])
        self.assertFalse(flow._signed_in.done(), "begin() must not wait for the sign-in")

        StubDeviceCodeCredential.gate.set()
        await flow.complete()

    async def test_complete_yields_a_usable_credential(self) -> None:
        flow, _ = await _auth.device_code_begin("t", "c", ["User.Read"])
        credential = await asyncio.wait_for(flow.complete(), timeout=5)

        token = await credential.get_token("User.Read")
        self.assertEqual(token.token, "stub-delegated-token")

    async def test_a_sign_in_that_fails_before_issuing_a_code_does_not_hang(self) -> None:
        # A bad tenant never reaches the callback. Waiting on the code alone would hang until the
        # caller's timeout; begin() must surface the real cause instead.
        StubDeviceCodeCredential.issues_code = False
        StubDeviceCodeCredential.fails_with = ValueError("AADSTS90002: tenant not found")

        with self.assertRaises(Exception) as raised:
            await asyncio.wait_for(_auth.device_code_begin("t", "c", ["User.Read"]), timeout=5)

        self.assertIn("AADSTS90002", str(raised.exception))

    async def test_a_declined_sign_in_maps_to_a_defined_code(self) -> None:
        StubDeviceCodeCredential.gate = threading.Event()
        flow, _ = await _auth.device_code_begin("t", "c", ["User.Read"])

        StubDeviceCodeCredential.fails_with = ValueError("AADSTS65004: user declined")
        StubDeviceCodeCredential.gate.set()

        with self.assertRaises(GraphError) as raised:
            await asyncio.wait_for(flow.complete(), timeout=5)
        self.assertEqual(raised.exception.code, "signInDeclined")

    async def test_cancelling_releases_the_pending_sign_in(self) -> None:
        StubDeviceCodeCredential.gate = threading.Event()
        flow, _ = await _auth.device_code_begin("t", "c", ["User.Read"])

        await asyncio.wait_for(flow.cancel(), timeout=5)
        self.assertTrue(flow._signed_in.done())
        StubDeviceCodeCredential.gate.set()

    async def test_completing_a_flow_that_was_never_begun_is_refused(self) -> None:
        flow = _auth._DeviceCodeSignIn("t", "c", ["User.Read"])
        with self.assertRaises(GraphError) as raised:
            await flow.complete()
        self.assertEqual(raised.exception.code, "invalidRequest")

    async def test_scopes_are_required(self) -> None:
        for scopes in (None, [], ()):
            with self.subTest(scopes=scopes):
                with self.assertRaises(GraphError) as raised:
                    _auth._DeviceCodeSignIn("t", "c", scopes)
                self.assertEqual(raised.exception.code, "invalidRequest")

    async def test_the_client_wires_the_flow_through_to_a_session(self) -> None:
        pending = await GraphClient.begin_device_code("t", "c", ["User.Read"])
        self.assertEqual(pending.user_code, "FJKLMNPQ")
        self.assertIsNone(pending.authorize_url, "that belongs to the browser flow")

        graph = await asyncio.wait_for(pending.complete(), timeout=5)
        self.assertIsInstance(graph, GraphClient)
        self.assertIsNotNone(graph.mail)
        await graph.aclose()


class Pkce(unittest.TestCase):
    def test_the_verifier_meets_rfc_7636(self) -> None:
        verifier, _ = _auth.pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertRegex(verifier, r"^[A-Za-z0-9._~-]+$")

    def test_the_challenge_is_the_s256_transform(self) -> None:
        verifier, challenge = _auth.pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)

    def test_every_verifier_and_state_is_fresh(self) -> None:
        self.assertEqual(len({_auth.pkce_pair()[0] for _ in range(50)}), 50)
        self.assertEqual(len({_auth.new_state() for _ in range(50)}), 50)

    def test_the_authorize_url_carries_the_challenge_not_the_verifier(self) -> None:
        verifier, challenge = _auth.pkce_pair()
        state = _auth.new_state()
        url = _auth.authorization_url(
            "tenant-1", "client-1", ["User.Read"], "http://localhost:8400", challenge, state
        )

        parts = urlsplit(url)
        query = parse_qs(parts.query)

        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/authorize")
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(query["state"], [state])
        self.assertEqual(query["redirect_uri"], ["http://localhost:8400"])
        self.assertNotIn(verifier, url, "the verifier must never leave the process")

    def test_offline_access_is_requested_so_the_session_can_refresh(self) -> None:
        url = _auth.authorization_url("t", "c", ["User.Read"], "http://localhost:8400", "ch", "st")
        scope = parse_qs(urlsplit(url).query)["scope"][0]
        self.assertEqual(scope, "User.Read offline_access")

    def test_the_redirect_uri_is_passed_through_character_for_character(self) -> None:
        # Entra matches it exactly; a normalised trailing slash produces AADSTS50011.
        for redirect in ("http://localhost:8400", "http://127.0.0.1:8400/callback"):
            with self.subTest(redirect=redirect):
                url = _auth.authorization_url("t", "c", ["User.Read"], redirect, "ch", "st")
                self.assertEqual(parse_qs(urlsplit(url).query)["redirect_uri"], [redirect])


class BrowserFlowGuards(unittest.IsolatedAsyncioTestCase):
    async def test_a_mismatched_state_is_refused_before_any_exchange(self) -> None:
        async def redirect(_uri, _timeout):
            return {"code": "the-code", "state": "not-the-one-issued"}

        original = _auth.wait_for_redirect
        _auth.wait_for_redirect = redirect
        _auth.open_browser = lambda _url: True
        try:
            with self.assertRaises(GraphError) as raised:
                await GraphClient.interactive("t", "c", ["User.Read"])
        finally:
            _auth.wait_for_redirect = original

        self.assertEqual(raised.exception.code, "stateMismatch")

    async def test_an_error_in_the_redirect_is_reported(self) -> None:
        async def redirect(_uri, _timeout):
            return {"error": "access_denied", "error_description": "user said no"}

        original = _auth.wait_for_redirect
        _auth.wait_for_redirect = redirect
        _auth.open_browser = lambda _url: True
        try:
            with self.assertRaises(GraphError) as raised:
                await GraphClient.interactive("t", "c", ["User.Read"])
        finally:
            _auth.wait_for_redirect = original

        self.assertEqual(raised.exception.code, "signInDeclined")
        self.assertIn("user said no", raised.exception.message)

    async def test_delegated_scopes_are_required_for_the_browser_flow(self) -> None:
        with self.assertRaises(GraphError) as raised:
            await GraphClient.interactive("t", "c", [])
        self.assertEqual(raised.exception.code, "invalidRequest")
        self.assertIn("scopes", raised.exception.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
