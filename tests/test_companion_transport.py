import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from companion.auth import OAuthConfig, Verifier
from companion.mcp_server import create_server
from companion.model_client import ModelClient, ModelConfig, generate_reviewed_item
from companion.store import Store


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.config = OAuthConfig(
            "https://auth.example.com/",
            "https://study.example.com/mcp",
            "https://auth.example.com/jwks",
            "learner-one",
        )

        class JWKS:
            def get_signing_key_from_jwt(inner, token):
                class Key:
                    key = self.key.public_key()

                return Key()

        self.verifier = Verifier(self.config, JWKS())

    def token(self, **changes):
        claims = {
            "iss": self.config.issuer,
            "aud": self.config.audience,
            "sub": "learner-one",
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "scope": "study:read study:write",
        }
        return jwt.encode({**claims, **changes}, self.key, algorithm="RS256")

    def test_oauth_subject_issuer_audience_expiry_and_signature(self):
        self.assertIsNotNone(asyncio.run(self.verifier.verify_token(self.token())))
        for changes in (
            {"sub": "another-learner"},
            {"aud": "wrong-resource"},
            {"iss": "https://other.example.com"},
            {"exp": 1},
        ):
            self.assertIsNone(
                asyncio.run(self.verifier.verify_token(self.token(**changes)))
            )
        self.assertIsNone(asyncio.run(self.verifier.verify_token("invalid-token")))

    def test_http_mcp_read_and_write_scopes_and_sync_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("companion.mcp_server.Verifier", return_value=self.verifier):
                server = create_server(
                    Path(tmp) / "study.sqlite",
                    oauth=self.config,
                    collector_token="x" * 32,
                )
            with TestClient(
                server.streamable_http_app(), base_url="https://study.example.com"
            ) as client:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                }
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "study_get_status", "arguments": {}},
                }
                unauthorized = client.post("/mcp", json=request, headers=headers)
                self.assertEqual(unauthorized.status_code, 401)
                self.assertIn(
                    "resource_metadata", unauthorized.headers["www-authenticate"]
                )
                headers["Authorization"] = "Bearer " + self.token()
                result = client.post("/mcp", json=request, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertFalse(result.json()["result"].get("isError"), result.text)
                request["params"] = {
                    "name": "study_set_preferences",
                    "arguments": {"request_id": "setup", "values": {"timezone": "UTC"}},
                }
                headers["Authorization"] = "Bearer " + self.token(scope="study:read")
                readonly = client.post("/mcp", json=request, headers=headers)
                self.assertTrue(readonly.json()["result"]["isError"])
                headers["Authorization"] = "Bearer " + self.token()
                saved = client.post("/mcp", json=request, headers=headers)
                self.assertFalse(saved.json()["result"].get("isError"), saved.text)
                self.assertEqual(client.get("/sync/changes").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/sync/changes", headers={"Authorization": "Bearer " + "x" * 32}
                    ).status_code,
                    200,
                )
                request["method"] = "tools/list"
                request.pop("params")
                listed = client.post("/mcp", json=request, headers=headers).json()[
                    "result"
                ]["tools"]
                self.assertGreaterEqual(len(listed), 15)
                self.assertFalse(any("sql" in t["name"] for t in listed))


class GenerationBudgetTests(unittest.TestCase):
    def test_cap_prevents_network_call_and_unverified_claims_need_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "store.sqlite")
            try:

                class NoNetwork:
                    def post(self, *a, **k):
                        raise AssertionError("No network allowed")

                config = ModelConfig(
                    "explicit-test-model",
                    "1",
                    "2",
                    "0.000001",
                    "0.000001",
                    pricing_checked_at="2026-11-01",
                )
                client = ModelClient(store, config, "synthetic-key", NoNetwork())
                with self.assertRaisesRegex(ValueError, "cap"):
                    client.call("test", "Return JSON", {})
                self.assertEqual(
                    generate_reviewed_item(client, {})["status"], "needs_research"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
