"""
Tests for provisioner — CSV generation, entitlement assignment, and
the provision_sources orchestration.
"""

import csv
import io
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from isc_client import ISCAPIError, ISCClient
from provisioner import (
    ACCOUNTS_PER_SOURCE,
    ENTITLEMENTS,
    ProvisionSummary,
    SourceProvisionResult,
    _aliases_to_identity_dicts,
    _assign_entitlements_file_mode,
    _assign_entitlements_random,
    _authoritative_rows_to_identity_dicts,
    _build_account_csv,
    _build_entitlement_csv,
    fetch_identity_pool,
    load_authoritative_csv,
    load_users_file,
    provision_sources,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_identity(n: int) -> dict:
    return {
        "id": f"id-{n:04d}",
        "alias": f"user{n:04d}",
        "name": f"User {n:04d}",
        "firstName": "User",
        "lastName": f"{n:04d}",
        "email": f"user{n:04d}@example.com",
    }


def _make_pool(size: int = 20) -> list[dict]:
    return [_make_identity(i) for i in range(1, size + 1)]


def _make_client() -> ISCClient:
    client = ISCClient("tenant", "cid", "csec")
    client._access_token = "fake-token"
    client._token_expires_at = time.monotonic() + 600
    return client


# ---------------------------------------------------------------------------
# _assign_entitlements
# ---------------------------------------------------------------------------

class TestAssignEntitlements(unittest.TestCase):
    def test_every_identity_gets_at_least_one(self):
        pool = _make_pool(10)
        assignments = _assign_entitlements_random(pool)
        for ident in pool:
            alias = ident["alias"]
            self.assertIn(alias, assignments)
            self.assertGreaterEqual(len(assignments[alias]), 1)

    def test_no_more_than_three_per_identity(self):
        pool = _make_pool(10)
        assignments = _assign_entitlements_random(pool)
        for alias, ents in assignments.items():
            self.assertLessEqual(len(ents), 3)

    def test_only_valid_entitlement_ids_assigned(self):
        valid_ids = {e["id"] for e in ENTITLEMENTS}
        pool = _make_pool(10)
        assignments = _assign_entitlements_random(pool)
        for alias, ents in assignments.items():
            for eid in ents:
                self.assertIn(eid, valid_ids)

    def test_different_sources_get_different_assignments(self):
        pool = _make_pool(20)
        a1 = _assign_entitlements_random(pool)
        a2 = _assign_entitlements_random(pool)
        self.assertTrue(
            any(a1.get(k) != a2.get(k) for k in a1),
            "Expected at least some variation between two random assignments",
        )

    def test_file_mode_assigns_all_four_entitlements(self):
        pool = _make_pool(5)
        assignments = _assign_entitlements_file_mode(pool)
        ent_ids = {e["id"] for e in ENTITLEMENTS}
        for alias, ents in assignments.items():
            self.assertEqual(set(ents), ent_ids)
            self.assertEqual(len(ents), len(ENTITLEMENTS))

    def test_file_mode_order_is_randomised(self):
        pool = _make_pool(10)
        a1 = _assign_entitlements_file_mode(pool)
        a2 = _assign_entitlements_file_mode(pool)
        # Order should vary across runs (with overwhelming probability)
        self.assertTrue(
            any(a1[k] != a2[k] for k in a1),
            "Expected order to vary between two file-mode assignments",
        )


# ---------------------------------------------------------------------------
# _build_entitlement_csv
# ---------------------------------------------------------------------------

class TestBuildEntitlementCsv(unittest.TestCase):
    def setUp(self):
        self.csv_text = _build_entitlement_csv().decode("utf-8")
        self.lines = self.csv_text.strip().splitlines()

    def test_has_header(self):
        # Default ISC Delimited File entitlement schema
        self.assertEqual(
            self.lines[0],
            "id,name,displayName,created,modified,entitlements,groups,permissions",
        )

    def test_has_all_entitlements(self):
        self.assertEqual(len(self.lines), len(ENTITLEMENTS) + 1)  # +1 for header

    def test_contains_expected_ids(self):
        for ent in ENTITLEMENTS:
            self.assertIn(ent["id"], self.csv_text)

    def test_returns_bytes(self):
        self.assertIsInstance(_build_entitlement_csv(), bytes)

    def test_custom_columns(self):
        csv_text = _build_entitlement_csv(columns=["id", "name", "description"]).decode()
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], "id,name,description")
        self.assertEqual(len(lines), len(ENTITLEMENTS) + 1)


# ---------------------------------------------------------------------------
# _build_account_csv
# ---------------------------------------------------------------------------

class TestBuildAccountCsv(unittest.TestCase):
    def setUp(self):
        self.pool = _make_pool(5)
        self.assignments = _assign_entitlements_random(self.pool)
        self.csv_bytes = _build_account_csv(self.pool, self.assignments)
        self.csv_text = self.csv_bytes.decode("utf-8")
        self.lines = self.csv_text.strip().splitlines()

    def test_has_header(self):
        self.assertEqual(
            self.lines[0],
            "id,name,givenName,familyName,e-mail,location,manager,groups",
        )

    def test_has_correct_row_count(self):
        self.assertEqual(len(self.lines), len(self.pool) + 1)

    def test_all_aliases_present(self):
        for ident in self.pool:
            self.assertIn(ident["alias"], self.csv_text)

    def test_entitlements_column_populated(self):
        # At least one row should have a non-empty entitlements column
        data_lines = self.lines[1:]
        entitlement_values = [line.split(",")[-1] for line in data_lines]
        self.assertTrue(any(v for v in entitlement_values))

    def test_returns_bytes(self):
        self.assertIsInstance(self.csv_bytes, bytes)


# ---------------------------------------------------------------------------
# fetch_identity_pool
# ---------------------------------------------------------------------------

class TestLoadUsersFile(unittest.TestCase):
    def _write_tmp(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_loads_aliases(self):
        path = self._write_tmp("jsmith\nadoe\nbjones\n")
        aliases = load_users_file(path)
        self.assertEqual(aliases, ["jsmith", "adoe", "bjones"])

    def test_ignores_blank_lines(self):
        path = self._write_tmp("jsmith\n\nadoe\n\n")
        aliases = load_users_file(path)
        self.assertEqual(aliases, ["jsmith", "adoe"])

    def test_ignores_comments(self):
        path = self._write_tmp("jsmith\n# this is a comment\nadoe\n")
        aliases = load_users_file(path)
        self.assertEqual(aliases, ["jsmith", "adoe"])

    def test_strips_whitespace(self):
        path = self._write_tmp("  jsmith  \n  adoe\n")
        aliases = load_users_file(path)
        self.assertEqual(aliases, ["jsmith", "adoe"])

    def test_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_users_file("/nonexistent/users.txt")

    def test_raises_on_empty_file(self):
        path = self._write_tmp("# only comments\n\n")
        with self.assertRaises(ValueError) as ctx:
            load_users_file(path)
        self.assertIn("no valid entries", str(ctx.exception))


class TestAliasesToIdentityDicts(unittest.TestCase):
    def test_returns_correct_structure(self):
        dicts = _aliases_to_identity_dicts(["jsmith", "adoe"])
        self.assertEqual(len(dicts), 2)
        self.assertEqual(dicts[0]["alias"], "jsmith")
        self.assertEqual(dicts[0]["id"], "jsmith")
        self.assertEqual(dicts[0]["name"], "jsmith")

    def test_empty_optional_fields(self):
        dicts = _aliases_to_identity_dicts(["jsmith"])
        self.assertEqual(dicts[0]["firstName"], "")
        self.assertEqual(dicts[0]["email"], "")


class TestFetchIdentityPool(unittest.TestCase):
    def test_returns_identities(self):
        client = _make_client()
        pool = _make_pool(20)
        with patch.object(client, "list_identities", return_value=pool):
            result = fetch_identity_pool(client)
        self.assertEqual(len(result), 20)

    def test_falls_back_to_search_api_when_list_fails(self):
        client = _make_client()
        pool = _make_pool(20)
        err = ISCAPIError(404, "not found", body={"error": "No message available"})
        with patch.object(client, "list_identities", side_effect=err):
            with patch.object(client, "search_identities", return_value=pool) as mock_search:
                result = fetch_identity_pool(client)
        mock_search.assert_called_once()
        self.assertEqual(len(result), 20)

    def test_excludes_alias(self):
        client = _make_client()
        pool = _make_pool(20)
        pool[0]["alias"] = "spadmin"
        with patch.object(client, "list_identities", return_value=pool):
            result = fetch_identity_pool(client, exclude_alias="spadmin")
        aliases = [i["alias"] for i in result]
        self.assertNotIn("spadmin", aliases)

    def test_raises_when_pool_too_small(self):
        client = _make_client()
        tiny_pool = _make_pool(5)  # less than ACCOUNTS_PER_SOURCE (10)
        with patch.object(client, "list_identities", return_value=tiny_pool):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_identity_pool(client)
        self.assertIn("Not enough identities", str(ctx.exception))

    def test_raises_when_both_strategies_fail(self):
        client = _make_client()
        err = ISCAPIError(404, "not found", body={"error": "No message available"})
        with patch.object(client, "list_identities", side_effect=err):
            with patch.object(client, "search_identities", side_effect=err):
                with self.assertRaises(RuntimeError):
                    fetch_identity_pool(client)

    def test_raises_on_empty_result(self):
        client = _make_client()
        with patch.object(client, "list_identities", return_value=[]):
            with patch.object(client, "search_identities", return_value=[]):
                with self.assertRaises(RuntimeError):
                    fetch_identity_pool(client)


# ---------------------------------------------------------------------------
# provision_sources
# ---------------------------------------------------------------------------

class TestProvisionSources(unittest.TestCase):
    def setUp(self):
        client = _make_client()
        pool = _make_pool(20)
        client.list_identities = MagicMock(return_value=pool)
        client.search_identities = MagicMock(return_value=pool)
        client.get_identity = MagicMock(return_value={
            "id": "owner-id", "displayName": "Test Owner"
        })
        client.create_source = MagicMock(return_value={"id": "src-001"})
        client.import_accounts = MagicMock(return_value={"id": "task-acct-001"})
        client.import_entitlements = MagicMock(return_value={"id": "task-ent-001"})
        self.client = client

    def test_dry_run_makes_no_api_calls(self):
        client = self.client
        summary = provision_sources(
            client, count=2, base_name="Test",
            owner_id="owner-id", owner_name="Owner",
            dry_run=True,
        )
        client.create_source.assert_not_called()
        client.import_accounts.assert_not_called()
        client.import_entitlements.assert_not_called()
        self.assertEqual(len(summary.succeeded), 2)

    def test_dry_run_reports_correct_counts(self):
        client = self.client
        summary = provision_sources(
            client, count=3, base_name="Demo",
            owner_id="owner-id", owner_name="Owner",
            dry_run=True,
        )
        for r in summary.results:
            self.assertEqual(r.accounts_loaded, ACCOUNTS_PER_SOURCE)
            self.assertEqual(r.entitlements_loaded, len(ENTITLEMENTS))

    def test_creates_correct_number_of_sources(self):
        client = self.client
        # Give each source a unique ID
        ids = iter([f"src-{i:03d}" for i in range(1, 6)])
        client.create_source = MagicMock(side_effect=lambda p, **kw: {"id": next(ids)})

        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                client, count=5, base_name="Source",
                owner_id="owner-id", owner_name="Owner",
            )

        self.assertEqual(client.create_source.call_count, 5)
        self.assertEqual(len(summary.succeeded), 5)

    def test_source_names_are_numbered(self):
        client = self.client
        client.create_source = MagicMock(return_value={"id": "src-001"})

        with patch("provisioner.time.sleep"):
            provision_sources(
                client, count=3, base_name="My Source",
                owner_id="owner-id", owner_name="Owner",
            )

        names = [call.args[0]["name"] for call in client.create_source.call_args_list]
        self.assertEqual(names, ["My Source 1", "My Source 2", "My Source 3"])

    def test_create_failure_skips_aggregation(self):
        client = self.client
        err = ISCAPIError(400, "bad", body={"messages": "Conflict"})
        client.create_source = MagicMock(side_effect=err)

        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                client, count=1, base_name="Fail",
                owner_id="owner-id", owner_name="Owner",
            )

        client.import_accounts.assert_not_called()
        client.import_entitlements.assert_not_called()
        self.assertEqual(len(summary.failed), 1)

    def test_account_aggregation_failure_skips_entitlement(self):
        client = self.client
        err = ISCAPIError(500, "server error", body={"messages": "Error"})
        client.import_accounts = MagicMock(side_effect=err)

        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                client, count=1, base_name="Fail",
                owner_id="owner-id", owner_name="Owner",
            )

        client.import_entitlements.assert_not_called()
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("Account aggregation", summary.failed[0].error)

    def test_each_source_gets_different_accounts(self):
        """Each source should sample a different set of identities."""
        client = self.client
        captured_payloads: list[bytes] = []

        def capture_accounts(source_id, csv_bytes, **kwargs):
            captured_payloads.append(csv_bytes)
            return {"id": "task-001"}

        client.import_accounts = MagicMock(side_effect=capture_accounts)
        client.create_source = MagicMock(side_effect=[
            {"id": f"src-{i}"} for i in range(3)
        ])

        with patch("provisioner.time.sleep"):
            provision_sources(
                client, count=3, base_name="Varied",
                owner_id="owner-id", owner_name="Owner",
            )

        # With a pool of 20 and sampling 10, it's extremely unlikely all three
        # CSVs are identical
        self.assertEqual(len(captured_payloads), 3)
        self.assertFalse(
            captured_payloads[0] == captured_payloads[1] == captured_payloads[2],
            "Expected different account CSVs for each source",
        )

    def test_pool_fetch_failure_records_all_as_failed(self):
        client = self.client
        client.list_identities = MagicMock(return_value=_make_pool(3))  # too small

        summary = provision_sources(
            client, count=2, base_name="Fail",
            owner_id="owner-id", owner_name="Owner",
        )

        self.assertEqual(len(summary.failed), 2)
        client.create_source.assert_not_called()

    def test_provision_as_csv_flag_is_set(self):
        client = self.client
        client.create_source = MagicMock(return_value={"id": "src-001"})

        with patch("provisioner.time.sleep"):
            provision_sources(
                client, count=1, base_name="CSV",
                owner_id="owner-id", owner_name="Owner",
            )

        _, kwargs = client.create_source.call_args
        self.assertTrue(kwargs.get("provision_as_csv"))

    def test_file_mode_uses_all_users_from_file(self):
        """Every user in the file should appear in the account CSV for every source."""
        client = self.client
        client.create_source = MagicMock(side_effect=[
            {"id": f"src-{i}"} for i in range(3)
        ])

        captured: list[bytes] = []

        def capture(source_id, csv_bytes, **kwargs):
            captured.append(csv_bytes)
            return {"id": "task-001"}

        client.import_accounts = MagicMock(side_effect=capture)

        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("jsmith\nadoe\nbjones\n")

        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                client, count=3, base_name="File",
                owner_id="owner-id", owner_name="Owner",
                users_file=path,
            )

        self.assertEqual(len(summary.succeeded), 3)
        # Every CSV should contain all three users
        for csv_bytes in captured:
            text = csv_bytes.decode()
            self.assertIn("jsmith", text)
            self.assertIn("adoe", text)
            self.assertIn("bjones", text)

    def test_file_mode_all_sources_get_same_users(self):
        """All sources should have identical account CSVs in file mode."""
        client = self.client
        client.create_source = MagicMock(side_effect=[
            {"id": f"src-{i}"} for i in range(3)
        ])

        captured: list[bytes] = []

        def capture(source_id, csv_bytes, **kwargs):
            captured.append(csv_bytes)
            return {"id": "task-001"}

        client.import_accounts = MagicMock(side_effect=capture)

        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("jsmith\nadoe\n")

        with patch("provisioner.time.sleep"):
            provision_sources(
                client, count=3, base_name="File",
                owner_id="owner-id", owner_name="Owner",
                users_file=path,
            )

        # All three CSVs should have the same users (though entitlement order may vary)
        for csv_bytes in captured:
            text = csv_bytes.decode()
            self.assertIn("jsmith", text)
            self.assertIn("adoe", text)

    def test_file_mode_invalid_file_records_all_as_failed(self):
        client = self.client
        summary = provision_sources(
            client, count=2, base_name="Fail",
            owner_id="owner-id", owner_name="Owner",
            users_file="/nonexistent/users.txt",
        )
        self.assertEqual(len(summary.failed), 2)
        client.create_source.assert_not_called()

    def test_file_mode_accounts_loaded_reflects_file_size(self):
        client = self.client
        client.create_source = MagicMock(return_value={"id": "src-001"})

        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("user1\nuser2\nuser3\nuser4\nuser5\n")

        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                client, count=1, base_name="File",
                owner_id="owner-id", owner_name="Owner",
                users_file=path,
            )

        self.assertEqual(summary.succeeded[0].accounts_loaded, 5)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# load_authoritative_csv
# ---------------------------------------------------------------------------

class TestLoadAuthoritativeCsv(unittest.TestCase):
    def _write_csv(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_loads_valid_csv(self):
        path = self._write_csv(
            "firstName,lastName,fullName,email\n"
            "Jane,Doe,Jane Doe,jane.doe@example.com\n"
            "John,Smith,John Smith,john.smith@example.com\n"
        )
        rows = load_authoritative_csv(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["firstName"], "Jane")
        self.assertEqual(rows[0]["email"], "jane.doe@example.com")

    def test_case_insensitive_headers(self):
        path = self._write_csv(
            "FIRSTNAME,LASTNAME,FULLNAME,EMAIL\n"
            "Jane,Doe,Jane Doe,jane@example.com\n"
        )
        rows = load_authoritative_csv(path)
        self.assertEqual(len(rows), 1)

    def test_raises_on_missing_required_column(self):
        path = self._write_csv(
            "firstName,lastName,email\n"
            "Jane,Doe,jane@example.com\n"
        )
        with self.assertRaises(ValueError) as ctx:
            load_authoritative_csv(path)
        self.assertIn("fullname", str(ctx.exception).lower())

    def test_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_authoritative_csv("/nonexistent/users.csv")

    def test_raises_on_empty_data(self):
        path = self._write_csv("firstName,lastName,fullName,email\n")
        with self.assertRaises(ValueError) as ctx:
            load_authoritative_csv(path)
        self.assertIn("no data rows", str(ctx.exception))

    def test_skips_blank_rows(self):
        path = self._write_csv(
            "firstName,lastName,fullName,email\n"
            "Jane,Doe,Jane Doe,jane@example.com\n"
            ",,, \n"
            "John,Smith,John Smith,john@example.com\n"
        )
        rows = load_authoritative_csv(path)
        self.assertEqual(len(rows), 2)

    def test_extra_columns_allowed(self):
        path = self._write_csv(
            "firstName,lastName,fullName,email,department\n"
            "Jane,Doe,Jane Doe,jane@example.com,Engineering\n"
        )
        rows = load_authoritative_csv(path)
        self.assertEqual(rows[0]["department"], "Engineering")


# ---------------------------------------------------------------------------
# _authoritative_rows_to_identity_dicts
# ---------------------------------------------------------------------------

class TestAuthoritativeRowsToIdentityDicts(unittest.TestCase):
    def _make_rows(self):
        return [
            {"firstName": "Jane", "lastName": "Doe",
             "fullName": "Jane Doe", "email": "jane.doe@example.com"},
            {"firstName": "John", "lastName": "Smith",
             "fullName": "John Smith", "email": "john.smith@example.com"},
        ]

    def test_alias_derived_from_email_local_part(self):
        dicts = _authoritative_rows_to_identity_dicts(self._make_rows())
        self.assertEqual(dicts[0]["alias"], "jane.doe")
        self.assertEqual(dicts[1]["alias"], "john.smith")

    def test_name_set_to_fullname(self):
        dicts = _authoritative_rows_to_identity_dicts(self._make_rows())
        self.assertEqual(dicts[0]["name"], "Jane Doe")

    def test_firstname_and_lastname_preserved(self):
        dicts = _authoritative_rows_to_identity_dicts(self._make_rows())
        self.assertEqual(dicts[0]["firstName"], "Jane")
        self.assertEqual(dicts[0]["lastName"], "Doe")

    def test_email_preserved(self):
        dicts = _authoritative_rows_to_identity_dicts(self._make_rows())
        self.assertEqual(dicts[0]["email"], "jane.doe@example.com")

    def test_fallback_alias_when_no_email(self):
        rows = [{"firstName": "Jane", "lastName": "Doe",
                 "fullName": "Jane Doe", "email": ""}]
        dicts = _authoritative_rows_to_identity_dicts(rows)
        self.assertEqual(dicts[0]["alias"], "jane.doe")

    def test_case_insensitive_column_lookup(self):
        rows = [{"FIRSTNAME": "Jane", "LASTNAME": "Doe",
                 "FULLNAME": "Jane Doe", "EMAIL": "jane@example.com"}]
        dicts = _authoritative_rows_to_identity_dicts(rows)
        self.assertEqual(dicts[0]["firstName"], "Jane")
        self.assertEqual(dicts[0]["email"], "jane@example.com")


# ---------------------------------------------------------------------------
# provision_sources — authoritative mode
# ---------------------------------------------------------------------------

class TestProvisionSourcesAuthoritative(unittest.TestCase):
    def _make_csv(self, rows: list[tuple]) -> str:
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write("firstName,lastName,fullName,email\n")
            for row in rows:
                f.write(",".join(row) + "\n")
        return path

    def setUp(self):
        client = _make_client()
        client.list_identities = MagicMock(return_value=_make_pool(20))
        client.create_source = MagicMock(return_value={"id": "src-auth-001"})
        client.create_identity_profile = MagicMock(return_value={"id": "profile-001"})
        client.import_accounts = MagicMock(return_value={"id": "task-acct-001"})
        client.import_entitlements = MagicMock(return_value={"id": "task-ent-001"})
        self.client = client

    def test_authoritative_flag_sets_source_payload(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
            ("John", "Smith", "John Smith", "john@example.com"),
        ])
        with patch("provisioner.time.sleep"):
            provision_sources(
                self.client, count=1, base_name="Auth Source",
                owner_id="owner-id", owner_name="Owner",
                users_file=path, authoritative=True,
            )
        payload = self.client.create_source.call_args[0][0]
        self.assertTrue(payload["authoritative"])

    def test_identity_profile_created_for_authoritative_source(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
        ])
        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                self.client, count=1, base_name="Auth Source",
                owner_id="owner-id", owner_name="Owner",
                users_file=path, authoritative=True,
            )
        self.assertEqual(self.client.create_identity_profile.call_count, 1)
        self.assertEqual(len(summary.succeeded), 1)

    def test_identity_profile_not_created_when_not_authoritative(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
        ])
        with patch("provisioner.time.sleep"):
            provision_sources(
                self.client, count=1, base_name="Non-Auth",
                owner_id="owner-id", owner_name="Owner",
                users_file=path, authoritative=False,
            )
        self.client.create_identity_profile.assert_not_called()

    def test_authoritative_without_users_file_fails_all(self):
        summary = provision_sources(
            self.client, count=2, base_name="Auth",
            owner_id="owner-id", owner_name="Owner",
            authoritative=True,  # no users_file
        )
        self.assertEqual(len(summary.failed), 2)
        self.client.create_source.assert_not_called()
        self.assertIn("users-file", summary.failed[0].error)

    def test_authoritative_count_greater_than_one_fails(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
        ])
        summary = provision_sources(
            self.client, count=3, base_name="Auth",
            owner_id="owner-id", owner_name="Owner",
            users_file=path, authoritative=True,
        )
        self.assertEqual(len(summary.failed), 3)
        self.client.create_source.assert_not_called()
        self.assertIn("count=1", summary.failed[0].error)

    def test_identity_profile_failure_records_source_as_failed(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
        ])
        err = ISCAPIError(403, "forbidden", body={"message": "ORG_ADMIN required"})
        self.client.create_identity_profile = MagicMock(side_effect=err)
        with patch("provisioner.time.sleep"):
            summary = provision_sources(
                self.client, count=1, base_name="Auth",
                owner_id="owner-id", owner_name="Owner",
                users_file=path, authoritative=True,
            )
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("Identity profile", summary.failed[0].error)
        # Aggregation should not have been attempted
        self.client.import_accounts.assert_not_called()

    def test_dry_run_authoritative_makes_no_api_calls(self):
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane@example.com"),
        ])
        summary = provision_sources(
            self.client, count=1, base_name="Auth",
            owner_id="owner-id", owner_name="Owner",
            users_file=path, authoritative=True, dry_run=True,
        )
        self.client.create_source.assert_not_called()
        self.client.create_identity_profile.assert_not_called()
        self.client.import_accounts.assert_not_called()
        self.assertEqual(len(summary.succeeded), 1)

    def test_account_csv_contains_full_name_and_email(self):
        """CSV rows should carry firstName/lastName/email into the account CSV."""
        path = self._make_csv([
            ("Jane", "Doe", "Jane Doe", "jane.doe@example.com"),
            ("John", "Smith", "John Smith", "john.smith@example.com"),
        ])
        captured: list[bytes] = []

        def capture(source_id, csv_bytes, **kwargs):
            captured.append(csv_bytes)
            return {"id": "task-001"}

        self.client.import_accounts = MagicMock(side_effect=capture)
        with patch("provisioner.time.sleep"):
            provision_sources(
                self.client, count=1, base_name="Auth",
                owner_id="owner-id", owner_name="Owner",
                users_file=path, authoritative=True,
            )

        self.assertEqual(len(captured), 1)
        text = captured[0].decode()
        self.assertIn("Jane", text)
        self.assertIn("Doe", text)
        self.assertIn("jane.doe", text)  # alias derived from email


# ---------------------------------------------------------------------------
# isc_client — create_identity_profile
# ---------------------------------------------------------------------------

class TestCreateIdentityProfile(unittest.TestCase):
    def setUp(self):
        from isc_client import ISCClient
        self.client = ISCClient("tenant", "cid", "csec")
        self.client._access_token = "fake-token"
        import time as _time
        self.client._token_expires_at = _time.monotonic() + 600
        # Default: no existing profiles so auto-priority resolves to 1
        self.client.list_identity_profiles = MagicMock(return_value=[])

    def _mock_post(self, return_value, status_code=201):
        from unittest.mock import MagicMock, patch
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = return_value
        return patch.object(self.client._session, "post", return_value=resp)

    def test_returns_created_profile(self):
        profile = {"id": "profile-001", "name": "My Source"}
        with self._mock_post(profile):
            result = self.client.create_identity_profile(
                name="My Source",
                authoritative_source_id="src-001",
                authoritative_source_name="My Source",
                owner_id="owner-001",
                owner_name="Owner",
            )
        self.assertEqual(result["id"], "profile-001")

    def test_auto_resolves_priority_from_existing_profiles(self):
        """Priority should be max(existing) + 1 when not supplied."""
        existing = [{"id": "p1", "priority": 10}, {"id": "p2", "priority": 25}]
        profile = {"id": "profile-new"}
        with patch.object(self.client, "list_identity_profiles", return_value=existing):
            with patch.object(self.client._session, "post") as mock_post:
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = profile
                mock_post.return_value = resp
                self.client.create_identity_profile(
                    name="HR Source",
                    authoritative_source_id="src-hr",
                    authoritative_source_name="HR Source",
                    owner_id="owner-001",
                    owner_name="Owner",
                )
                body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["priority"], 26)

    def test_priority_is_1_when_no_profiles_exist(self):
        with patch.object(self.client, "list_identity_profiles", return_value=[]):
            with patch.object(self.client._session, "post") as mock_post:
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = {"id": "p1"}
                mock_post.return_value = resp
                self.client.create_identity_profile(
                    name="HR Source",
                    authoritative_source_id="src-hr",
                    authoritative_source_name="HR Source",
                    owner_id="owner-001",
                    owner_name="Owner",
                )
                body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["priority"], 1)

    def test_explicit_priority_is_used_as_is(self):
        with patch.object(self.client, "list_identity_profiles", return_value=[]) as mock_list:
            with patch.object(self.client._session, "post") as mock_post:
                resp = MagicMock()
                resp.status_code = 201
                resp.json.return_value = {"id": "p1"}
                mock_post.return_value = resp
                self.client.create_identity_profile(
                    name="HR Source",
                    authoritative_source_id="src-hr",
                    authoritative_source_name="HR Source",
                    owner_id="owner-001",
                    owner_name="Owner",
                    priority=99,
                )
                body = mock_post.call_args.kwargs["json"]
        # list_identity_profiles should NOT have been called
        mock_list.assert_not_called()
        self.assertEqual(body["priority"], 99)

    def test_payload_contains_authoritative_source(self):
        profile = {"id": "profile-001"}
        with patch.object(self.client._session, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = profile
            mock_post.return_value = resp
            self.client.create_identity_profile(
                name="HR Source",
                authoritative_source_id="src-hr",
                authoritative_source_name="HR Source",
                owner_id="owner-001",
                owner_name="Owner",
            )
            body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["authoritativeSource"]["id"], "src-hr")
        self.assertEqual(body["authoritativeSource"]["type"], "SOURCE")

    def test_default_attribute_transforms_cover_four_fields(self):
        profile = {"id": "profile-001"}
        with patch.object(self.client._session, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = profile
            mock_post.return_value = resp
            self.client.create_identity_profile(
                name="HR Source",
                authoritative_source_id="src-hr",
                authoritative_source_name="HR Source",
                owner_id="owner-001",
                owner_name="Owner",
            )
            body = mock_post.call_args.kwargs["json"]
        transforms = body["identityAttributeConfig"]["attributeTransforms"]
        mapped_attrs = {t["identityAttributeName"] for t in transforms}
        self.assertIn("uid", mapped_attrs)
        self.assertIn("firstname", mapped_attrs)
        self.assertIn("lastname", mapped_attrs)
        self.assertIn("displayName", mapped_attrs)
        self.assertIn("email", mapped_attrs)

    def test_uses_v2024_endpoint(self):
        profile = {"id": "profile-001"}
        with patch.object(self.client._session, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = profile
            mock_post.return_value = resp
            self.client.create_identity_profile(
                name="HR Source",
                authoritative_source_id="src-hr",
                authoritative_source_name="HR Source",
                owner_id="owner-001",
                owner_name="Owner",
            )
            url = mock_post.call_args.args[0]
        self.assertIn("/v2024/identity-profiles", url)

    def test_raises_on_api_error(self):
        from isc_client import ISCAPIError
        bad_resp = MagicMock()
        bad_resp.status_code = 403
        bad_resp.url = "https://tenant.api.identitynow.com/v2024/identity-profiles"
        bad_resp.json.return_value = {"message": "ORG_ADMIN required"}
        with patch.object(self.client._session, "post", return_value=bad_resp):
            with self.assertRaises(ISCAPIError) as ctx:
                self.client.create_identity_profile(
                    name="HR Source",
                    authoritative_source_id="src-hr",
                    authoritative_source_name="HR Source",
                    owner_id="owner-001",
                    owner_name="Owner",
                )
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
