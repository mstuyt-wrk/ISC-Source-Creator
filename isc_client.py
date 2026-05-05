"""
SailPoint ISC API client.

Handles OAuth 2.0 authentication (client credentials grant) and provides
methods for the v2026 Sources API.

Reference: https://developer.sailpoint.com/docs/api/v2026/sources
"""

import logging
import time
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

# Token is considered expired this many seconds before its actual expiry,
# to avoid using a token that expires mid-request.
_TOKEN_EXPIRY_BUFFER_SECONDS = 30

# Default page size for paginated list calls
_DEFAULT_PAGE_SIZE = 250


def _extract_message(messages: Any) -> str:
    """
    Normalise ISC error message payloads into a plain string.

    ISC can return messages as:
      - a plain string
      - a list of locale objects: [{"locale": "en-US", "text": "..."}, ...]

    We prefer the en-US locale, falling back to the first entry's text,
    and finally to a raw string representation.
    """
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        # Prefer en-US, then any DEFAULT origin, then first entry
        for entry in messages:
            if isinstance(entry, dict) and entry.get("locale") == "en-US":
                return entry.get("text", str(entry))
        for entry in messages:
            if isinstance(entry, dict) and entry.get("localeOrigin") == "DEFAULT":
                return entry.get("text", str(entry))
        if messages and isinstance(messages[0], dict):
            return messages[0].get("text", str(messages[0]))
    return str(messages)


class ISCAuthError(Exception):
    """Raised when authentication fails."""


class ISCAPIError(Exception):
    """Raised when an API call returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def detail(self) -> str:
        """Return a human-readable error detail string."""
        body = self.body
        if isinstance(body, dict):
            # ISC error responses use 'messages', 'message', or 'detailCode'
            messages = (
                body.get("messages")
                or body.get("message")
                or body.get("detailCode")
            )
            msg = f"HTTP {self.status_code}: {_extract_message(messages)}" if messages else f"HTTP {self.status_code}: {body}"

            # Append any field-level causes ISC provides
            causes = body.get("causes")
            if causes:
                cause_texts = [
                    _extract_message(c.get("messages") or c.get("text") or c)
                    for c in causes
                    if c
                ]
                if cause_texts:
                    msg += " — causes: " + "; ".join(cause_texts)
            return msg
        if isinstance(body, list):
            return f"HTTP {self.status_code}: {_extract_message(body)}"
        return f"HTTP {self.status_code}: {body}"


class ISCClient:
    """
    Thin wrapper around the SailPoint ISC v2026 REST API.

    Authentication uses the OAuth 2.0 client credentials grant flow.
    The access token is cached and refreshed automatically when it expires.

    Usage::

        client = ISCClient(tenant="acme", client_id="...", client_secret="...")
        source = client.create_source({...})
        sources = list(client.iter_sources())
    """

    def __init__(
        self,
        tenant: str,
        client_id: str,
        client_secret: str,
        domain: str = "identitynow.com",
    ):
        """
        Args:
            tenant:        ISC tenant name (e.g. "acme" for acme.identitynow.com).
            client_id:     OAuth client ID (from a PAT or API client).
            client_secret: OAuth client secret.
            domain:        Base domain for the tenant. Defaults to "identitynow.com".
                           Use "identitynow-demo.com" for demo tenants.
        """
        self._tenant = tenant.strip()
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._domain = domain.strip()

        self._base_url = f"https://{self._tenant}.api.{self._domain}"
        self._token_url = f"{self._base_url}/oauth/token"
        # The v2026 label is the documentation version. The actual REST path
        # served by ISC tenants is /v3 for stable endpoints.
        self._api_base = f"{self._base_url}/v3"

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _fetch_token(self) -> None:
        """Request a new access token using the client credentials grant."""
        logger.debug("Requesting new access token for tenant '%s'", self._tenant)
        response = self._session.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            raise ISCAuthError(
                f"Failed to obtain access token (HTTP {response.status_code}): "
                f"{response.text}"
            )

        data = response.json()
        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 750))
        self._token_expires_at = (
            time.monotonic() + expires_in - _TOKEN_EXPIRY_BUFFER_SECONDS
        )
        logger.debug("Access token obtained, expires in %d seconds", expires_in)

    def ensure_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token is None or time.monotonic() >= self._token_expires_at:
            self._fetch_token()
        return self._access_token  # type: ignore[return-value]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.ensure_token()}"}

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self._api_base}{path}"
        logger.debug("GET %s  params=%s", url, params)
        response = self._session.get(url, headers=self._auth_headers(), params=params)
        self._raise_for_status(response)
        return response.json()

    def _post(self, path: str, body: dict, params: Optional[dict] = None) -> Any:
        url = f"{self._api_base}{path}"
        logger.debug("POST %s  params=%s", url, params)
        response = self._session.post(
            url, headers=self._auth_headers(), json=body, params=params
        )
        self._raise_for_status(response)
        return response.json()

    def _delete(self, path: str) -> requests.Response:
        url = f"{self._api_base}{path}"
        logger.debug("DELETE %s", url)
        response = self._session.delete(url, headers=self._auth_headers())
        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = response.text
            logger.debug(
                "API error %s %s — full body: %s",
                response.status_code,
                response.url,
                body,
            )
            raise ISCAPIError(
                status_code=response.status_code,
                message=f"API error {response.status_code}: {response.url}",
                body=body,
            )

    # ------------------------------------------------------------------
    # Connectors API
    # ------------------------------------------------------------------

    def list_connectors(self, limit: int = 250) -> list[dict]:
        """
        List all connectors available on this tenant.

        ``GET /v3/connectors``

        Returns connector objects with at least ``name``, ``scriptName``
        (the value to use as ``connector`` in a source definition), and
        ``type``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        logger.debug("Listing connectors")
        return self._get("/connectors", params=params)

    # ------------------------------------------------------------------
    # Sources API
    # ------------------------------------------------------------------

    def create_source(
        self, source_payload: dict, provision_as_csv: bool = False
    ) -> dict:
        """
        Create a source in ISC.

        ``POST /v2026/sources``

        Args:
            source_payload:   Full source JSON object.
            provision_as_csv: Set True to create a Delimited File (CSV) source.
                              This sets the source type to DelimitedFile automatically.
                              You must use this flag instead of setting ``type``
                              directly in the payload.

        Returns:
            The created source object returned by the API.
        """
        params: dict[str, str] = {}
        if provision_as_csv:
            params["provisionAsCsv"] = "true"

        logger.debug("Creating source: %s", source_payload.get("name"))
        return self._post("/sources", source_payload, params=params or None)

    def get_source(self, source_id: str) -> dict:
        """
        Get a source by ID.

        ``GET /v2026/sources/{id}``
        """
        return self._get(f"/sources/{source_id}")

    def list_sources(
        self,
        filters: Optional[str] = None,
        sorters: Optional[str] = None,
        limit: int = _DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """
        Return one page of sources.

        ``GET /v2026/sources``

        Args:
            filters: ISC filter expression, e.g. ``name co "HR"``.
            sorters: Sort field(s), e.g. ``name`` or ``-created``.
            limit:   Page size (max 250).
            offset:  Zero-based record offset.

        Returns:
            List of source objects for the requested page.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if filters:
            params["filters"] = filters
        if sorters:
            params["sorters"] = sorters

        logger.debug("Listing sources (limit=%d, offset=%d)", limit, offset)
        return self._get("/sources", params=params)

    def iter_sources(
        self,
        filters: Optional[str] = None,
        sorters: Optional[str] = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict]:
        """
        Iterate over *all* sources, handling pagination automatically.

        ``GET /v2026/sources``  (paginated)

        Args:
            filters:   ISC filter expression.
            sorters:   Sort field(s).
            page_size: Records per API call (default 250).

        Yields:
            Individual source objects.
        """
        offset = 0
        while True:
            page = self.list_sources(
                filters=filters,
                sorters=sorters,
                limit=page_size,
                offset=offset,
            )
            if not page:
                break
            yield from page
            if len(page) < page_size:
                # Last page — no need for another round-trip
                break
            offset += page_size

    def delete_source(self, source_id: str) -> dict:
        """
        Delete a source by ID.

        ``DELETE /v3/sources/{id}``

        The API removes all accounts on the source first, then deletes it.
        Returns the task result DTO (202 Accepted).
        """
        url = f"{self._api_base}/sources/{source_id}"
        logger.debug("DELETE %s", url)
        response = self._session.delete(url, headers=self._auth_headers())
        # DELETE /sources returns 202 Accepted with a task body
        if response.status_code not in (200, 202, 204):
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise ISCAPIError(
                status_code=response.status_code,
                message=f"API error {response.status_code}: {url}",
                body=body,
            )
        try:
            return response.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Identities API  (used to look up valid owner IDs)
    # ------------------------------------------------------------------

    def search_identities(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        """
        Search for identities by name or alias.

        Tries two strategies in order:
        1. ``GET /v3/identities`` with a ``filters`` expression — fast and
           reliable for exact/partial name or alias matches.
        2. ``POST /v3/search`` (Elasticsearch) — broader full-text search,
           used as a fallback if the filter returns nothing.

        Args:
            query: Name, alias, or email fragment to search for.
            limit: Max results to return (default 10).

        Returns:
            List of identity objects.
        """
        # Strategy 1: identities list endpoint with filter
        # Supports: name co "x", alias co "x", email co "x"
        results = self._identities_filter_search(query, limit)
        if results:
            return results

        logger.debug("Filter search returned nothing, trying full-text search")

        # Strategy 2: Search API (Elasticsearch)
        results = self._search_api_identities(query, limit)
        return results

    def _identities_filter_search(self, query: str, limit: int) -> list[dict]:
        """
        Search identities via GET /v3/identities using ISC filter syntax.

        Tries name, alias, and email contains filters combined with OR logic
        by running three separate requests and deduplicating.
        """
        seen: dict[str, dict] = {}
        for field in ("name", "alias", "email"):
            try:
                page = self._get(
                    "/identities",
                    params={
                        "filters": f'{field} co "{query}"',
                        "limit": limit,
                        "offset": 0,
                    },
                )
                for identity in page:
                    seen[identity["id"]] = identity
                    if len(seen) >= limit:
                        break
            except ISCAPIError as exc:
                # Some tenants don't support filtering on all fields — skip
                logger.debug(
                    "Filter search on field '%s' failed (%s), skipping",
                    field,
                    exc.status_code,
                )
            if len(seen) >= limit:
                break
        return list(seen.values())[:limit]

    def _search_api_identities(self, query: str, limit: int) -> list[dict]:
        """
        Search identities via POST /v3/search (Elasticsearch).

        Uses a wildcard query so partial terms like 'adm' match 'admin'.
        The Search API accepts up to 250 results per request.
        """
        # Wrap the query in wildcards if it doesn't already contain them
        q = query if ("*" in query or "?" in query) else f"*{query}*"
        body = {
            "indices": ["identities"],
            "query": {"query": q},
            "sort": ["name"],
        }
        # Search API max is 250 per page
        page_limit = min(limit, 250)
        params: dict[str, Any] = {"limit": page_limit, "offset": 0}
        logger.debug("Search API query=%r limit=%d", q, page_limit)
        try:
            url = f"{self._base_url}/v3/search"
            response = self._session.post(
                url, headers=self._auth_headers(), json=body, params=params
            )
            self._raise_for_status(response)
            return response.json()
        except ISCAPIError as exc:
            logger.debug("Search API failed: %s", exc.detail())
            return []

    def get_identity(self, identity_id: str) -> dict:
        """
        Get a single identity by ID.

        ``GET /v3/identities/{id}``
        """
        return self._get(f"/identities/{identity_id}")

    def list_identities(
        self,
        limit: int = _DEFAULT_PAGE_SIZE,
        offset: int = 0,
        filters: Optional[str] = None,
    ) -> list[dict]:
        """
        List identities on the tenant.

        ``GET /v3/identities``

        Args:
            limit:   Page size (max 250).
            offset:  Zero-based record offset.
            filters: Optional ISC filter expression.

        Returns:
            List of identity objects.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if filters:
            params["filters"] = filters
        logger.debug("Listing identities (limit=%d, offset=%d)", limit, offset)
        return self._get("/identities", params=params)

    def list_source_schemas(self, source_id: str) -> list[dict]:
        """
        List schemas defined on a source.

        ``GET /v3/sources/{sourceId}/schemas``

        Returns:
            List of schema objects. Each has ``name``, ``attributes``, etc.
        """
        return self._get(f"/sources/{source_id}/schemas")

    # ------------------------------------------------------------------
    # Aggregation API
    # ------------------------------------------------------------------

    def import_accounts(
        self,
        source_id: str,
        csv_bytes: bytes,
        filename: str = "accounts.csv",
        disable_optimization: bool = False,
    ) -> dict:
        """
        Upload a CSV file and trigger an account aggregation on a source.

        ``POST /beta/sources/{id}/load-accounts``

        Args:
            source_id:            The source ID.
            csv_bytes:            Raw CSV content as bytes.
            filename:             Filename to use in the multipart upload.
            disable_optimization: If True, reprocesses every account even if
                                  the data has not changed.

        Returns:
            Task result dict.
        """
        url = f"{self._base_url}/beta/sources/{source_id}/load-accounts"
        logger.debug("POST %s (account aggregation, %d bytes)", url, len(csv_bytes))

        # Build multipart form — the file field MUST be named "file".
        # Do not set Content-Type manually; requests sets the correct
        # multipart/form-data boundary automatically when files= is used.
        # We must NOT let the session-level "Content-Type: application/json"
        # header bleed in, so we override it to None here.
        headers = {**self._auth_headers(), "Content-Type": None}

        files = {"file": (filename, csv_bytes, "text/csv")}
        form_data = {}
        if disable_optimization:
            form_data["disableOptimization"] = "true"

        response = self._session.post(
            url,
            headers=headers,
            files=files,
            data=form_data if form_data else None,
        )
        self._raise_for_status(response)
        return response.json()

    def import_entitlements(
        self,
        source_id: str,
        csv_bytes: bytes,
        filename: str = "entitlements.csv",
    ) -> dict:
        """
        Upload a CSV file and trigger an entitlement aggregation on a source.

        ``POST /beta/sources/{sourceId}/load-entitlements``

        Args:
            source_id:  The source ID.
            csv_bytes:  Raw CSV content as bytes.
            filename:   Filename to use in the multipart upload.

        Returns:
            Task result dict.
        """
        url = f"{self._base_url}/beta/sources/{source_id}/load-entitlements"
        logger.debug("POST %s (entitlement aggregation, %d bytes)", url, len(csv_bytes))

        headers = {**self._auth_headers(), "Content-Type": None}
        files = {"file": (filename, csv_bytes, "text/csv")}

        response = self._session.post(url, headers=headers, files=files)
        self._raise_for_status(response)
        return response.json()
