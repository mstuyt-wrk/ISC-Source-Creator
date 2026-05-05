"""
Tests for ISCClient — authentication and HTTP helpers.

All HTTP calls are intercepted with unittest.mock so no real network
traffic is generated.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

import sys
import os

# Make sure the package root is on the path when running tests directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from isc_client import ISCAPIError, ISCAuthError, ISCClient


def _make_client() -> ISCClient:
    return ISCClient(
        tenant="test-tenant",
        client_id="test-client-id",
        client_secret="test-client-secret",
    )


def _mock_token_response(expires_in: int = 750) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "fake-jwt-token",
        "token_type": "bearer",
        "expires_in": expires_in,
    }
    return resp


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication(unittest.TestCase):
    def test_fetch_token_success(self):
        client = _make_client()
        with patch.object(client._session, "post", return_value=_mock_token_response()):
            token = client.ensure_token()
        self.assertEqual(token, "fake-jwt-token")
        self.assertIsNotNone(client._access_token)

    def test_fetch_token_failure_raises_auth_error(self):
        client = _make_client()
        bad_resp = MagicMock()
        bad_resp.status_code = 401
        bad_resp.text = "Unauthorized"
        with patch.object(client._session, "post", return_value=bad_resp):
            with self.assertRaises(ISCAuthError):
                client.ensure_token()

    def test_token_is_cached(self):
        client = _make_client()
        mock_post = MagicMock(return_value=_mock_token_response())
        with patch.object(client._session, "post", mock_post):
            client.ensure_token()
            client.ensure_token()
        # Should only have called the token endpoint once
        self.assertEqual(mock_post.call_count, 1)

    def test_expired_token_is_refreshed(self):
        client = _make_client()
        mock_post = MagicMock(return_value=_mock_token_response())
        with patch.object(client._session, "post", mock_post):
            client.ensure_token()
            # Force expiry
            client._token_expires_at = time.monotonic() - 1
            client.ensure_token()
        self.assertEqual(mock_post.call_count, 2)

    def test_auth_header_contains_bearer(self):
        client = _make_client()
        with patch.object(client._session, "post", return_value=_mock_token_response()):
            headers = client._auth_headers()
        self.assertEqual(headers["Authorization"], "Bearer fake-jwt-token")

    def test_default_domain_is_identitynow(self):
        client = ISCClient(tenant="acme", client_id="cid", client_secret="csec")
        self.assertIn("identitynow.com", client._base_url)
        self.assertNotIn("identitynow-demo.com", client._base_url)
        self.assertTrue(client._api_base.endswith("/v3"))

    def test_demo_domain_builds_correct_url(self):
        client = ISCClient(
            tenant="acme-demo",
            client_id="cid",
            client_secret="csec",
            domain="identitynow-demo.com",
        )
        self.assertEqual(
            client._base_url,
            "https://acme-demo.api.identitynow-demo.com",
        )
        self.assertEqual(
            client._token_url,
            "https://acme-demo.api.identitynow-demo.com/oauth/token",
        )
        self.assertEqual(
            client._api_base,
            "https://acme-demo.api.identitynow-demo.com/v3",
        )

    def test_custom_domain_builds_correct_url(self):
        client = ISCClient(
            tenant="myorg",
            client_id="cid",
            client_secret="csec",
            domain="custom.example.com",
        )
        self.assertIn("custom.example.com", client._base_url)


# ---------------------------------------------------------------------------
# HTTP helpers — _raise_for_status
# ---------------------------------------------------------------------------


class TestRaiseForStatus(unittest.TestCase):
    def test_2xx_does_not_raise(self):
        for code in (200, 201, 202, 204):
            resp = MagicMock()
            resp.status_code = code
            ISCClient._raise_for_status(resp)  # should not raise

    def test_4xx_raises_api_error(self):
        resp = MagicMock()
        resp.status_code = 400
        resp.url = "https://example.com/v2026/sources"
        resp.json.return_value = {"detailCode": "400.1 Bad Request"}
        with self.assertRaises(ISCAPIError) as ctx:
            ISCClient._raise_for_status(resp)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_5xx_raises_api_error(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.url = "https://example.com/v2026/sources"
        resp.json.side_effect = ValueError("not json")
        resp.text = "Internal Server Error"
        with self.assertRaises(ISCAPIError) as ctx:
            ISCClient._raise_for_status(resp)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.body, "Internal Server Error")


# ---------------------------------------------------------------------------
# ISCAPIError.detail()
# ---------------------------------------------------------------------------


class TestISCAPIErrorDetail(unittest.TestCase):
    def test_detail_with_messages_key(self):
        err = ISCAPIError(400, "bad", body={"messages": "Name is required"})
        self.assertIn("Name is required", err.detail())

    def test_detail_with_message_key(self):
        err = ISCAPIError(404, "not found", body={"message": "Source not found"})
        self.assertIn("Source not found", err.detail())

    def test_detail_with_detail_code(self):
        err = ISCAPIError(400, "bad", body={"detailCode": "400.1 Bad Request"})
        self.assertIn("400.1 Bad Request", err.detail())

    def test_detail_with_plain_text_body(self):
        err = ISCAPIError(500, "server error", body="Internal Server Error")
        self.assertIn("500", err.detail())
        self.assertIn("Internal Server Error", err.detail())

    def test_detail_with_no_body(self):
        err = ISCAPIError(403, "forbidden", body=None)
        self.assertIn("403", err.detail())

    def test_detail_with_locale_list_prefers_en_us(self):
        """The 404 locale-message list format ISC returns should be readable."""
        body = [
            {"locale": "und", "localeOrigin": "REQUEST",
             "text": "The server did not find a current representation."},
            {"locale": "en-US", "localeOrigin": "DEFAULT",
             "text": "The server did not find a current representation."},
        ]
        err = ISCAPIError(404, "not found", body=body)
        detail = err.detail()
        self.assertIn("404", detail)
        self.assertIn("The server did not find", detail)
        # Must NOT contain raw Python list/dict repr
        self.assertNotIn("'locale'", detail)
        self.assertNotIn("localeOrigin", detail)

    def test_detail_with_messages_as_locale_list(self):
        """messages field can itself be a locale list."""
        body = {
            "messages": [
                {"locale": "en-US", "localeOrigin": "DEFAULT", "text": "Name already exists"},
            ]
        }
        err = ISCAPIError(400, "bad", body=body)
        self.assertIn("Name already exists", err.detail())
        self.assertNotIn("localeOrigin", err.detail())


class TestExtractMessage(unittest.TestCase):
    def setUp(self):
        from isc_client import _extract_message
        self.extract = _extract_message

    def test_plain_string(self):
        self.assertEqual(self.extract("hello"), "hello")

    def test_list_prefers_en_us(self):
        msgs = [
            {"locale": "und", "localeOrigin": "REQUEST", "text": "Und text"},
            {"locale": "en-US", "localeOrigin": "DEFAULT", "text": "English text"},
        ]
        self.assertEqual(self.extract(msgs), "English text")

    def test_list_falls_back_to_default_origin(self):
        msgs = [
            {"locale": "und", "localeOrigin": "REQUEST", "text": "Und text"},
            {"locale": "fr-FR", "localeOrigin": "DEFAULT", "text": "French default"},
        ]
        self.assertEqual(self.extract(msgs), "French default")

    def test_list_falls_back_to_first_entry(self):
        msgs = [
            {"locale": "und", "localeOrigin": "REQUEST", "text": "First text"},
        ]
        self.assertEqual(self.extract(msgs), "First text")

    def test_empty_list_returns_string(self):
        result = self.extract([])
        self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# Sources API methods
# ---------------------------------------------------------------------------


class TestSourcesMethods(unittest.TestCase):
    def setUp(self):
        self.client = _make_client()
        # Pre-load a fake token so we don't need to mock the token endpoint
        self.client._access_token = "fake-jwt-token"
        self.client._token_expires_at = time.monotonic() + 600

    def _mock_get(self, return_value):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = return_value
        return patch.object(self.client._session, "get", return_value=resp)

    def _mock_post(self, return_value, status_code=201):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = return_value
        return patch.object(self.client._session, "post", return_value=resp)

    def _mock_delete(self, return_value, status_code=202):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = return_value
        return patch.object(self.client._session, "delete", return_value=resp)

    # create_source
    def test_create_source_returns_created_object(self):
        payload = {"name": "Test Source", "connector": "active-directory"}
        created = {"id": "abc123", **payload}
        with self._mock_post(created):
            result = self.client.create_source(payload)
        self.assertEqual(result["id"], "abc123")

    def test_create_source_csv_adds_query_param(self):
        payload = {"name": "CSV Source", "connector": "delimited-file"}
        created = {"id": "csv001", **payload}
        with patch.object(self.client._session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = created
            mock_post.return_value = mock_resp
            self.client.create_source(payload, provision_as_csv=True)
            call_kwargs = mock_post.call_args
            params = call_kwargs.kwargs.get("params") or {}
            self.assertEqual(params.get("provisionAsCsv"), "true")

    def test_create_source_raises_on_api_error(self):
        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.url = "https://test-tenant.api.identitynow.com/v2026/sources"
        bad_resp.json.return_value = {"messages": "Name already exists"}
        with patch.object(self.client._session, "post", return_value=bad_resp):
            with self.assertRaises(ISCAPIError) as ctx:
                self.client.create_source({"name": "Dup"})
        self.assertEqual(ctx.exception.status_code, 400)

    # get_source
    def test_get_source_returns_source(self):
        source = {"id": "abc123", "name": "My Source"}
        with self._mock_get(source):
            result = self.client.get_source("abc123")
        self.assertEqual(result["name"], "My Source")

    # list_sources
    def test_list_sources_returns_list(self):
        sources = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
        with self._mock_get(sources):
            result = self.client.list_sources()
        self.assertEqual(len(result), 2)

    def test_list_sources_passes_filters(self):
        with patch.object(self.client._session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = []
            mock_get.return_value = mock_resp
            self.client.list_sources(filters='name co "HR"', sorters="name")
            call_kwargs = mock_get.call_args
            params = call_kwargs.kwargs.get("params") or {}
            self.assertEqual(params["filters"], 'name co "HR"')
            self.assertEqual(params["sorters"], "name")

    # iter_sources
    def test_iter_sources_paginates(self):
        page1 = [{"id": str(i)} for i in range(3)]
        page2 = [{"id": str(i)} for i in range(3, 5)]

        call_count = 0

        def fake_list(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("offset", 0) == 0:
                return page1
            return page2

        with patch.object(self.client, "list_sources", side_effect=lambda **kw: fake_list(**kw)):
            results = list(self.client.iter_sources(page_size=3))

        self.assertEqual(len(results), 5)

    def test_iter_sources_stops_on_empty_page(self):
        with patch.object(self.client, "list_sources", return_value=[]):
            results = list(self.client.iter_sources())
        self.assertEqual(results, [])

    # delete_source
    def test_delete_source_returns_task(self):
        task = {"id": "task-001", "type": "DELETE_SOURCE"}
        with self._mock_delete(task):
            result = self.client.delete_source("abc123")
        self.assertEqual(result["id"], "task-001")

    def test_delete_source_raises_on_404(self):
        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.url = "https://test-tenant.api.identitynow.com/v2026/sources/nope"
        bad_resp.json.return_value = {"message": "Source not found"}
        with patch.object(self.client._session, "delete", return_value=bad_resp):
            with self.assertRaises(ISCAPIError) as ctx:
                self.client.delete_source("nope")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
