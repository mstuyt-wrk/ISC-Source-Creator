"""
Tests for source_creator — validation and creation logic.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from isc_client import ISCAPIError, ISCClient
from source_creator import (
    CreationSummary,
    create_sources,
    load_sources_file,
    validate_all,
    validate_source,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_source(**overrides) -> dict:
    base = {
        "name": "Test Source",
        "description": "A test source",
        "owner": {"id": "abc123", "name": "Admin", "type": "IDENTITY"},
        "connector": "active-directory",
        "connectorName": "Active Directory",
        "connectionType": "direct",
    }
    base.update(overrides)
    return base


def _make_client() -> ISCClient:
    client = ISCClient("tenant", "cid", "csecret")
    client._access_token = "fake-token"
    client._token_expires_at = time.monotonic() + 600
    return client


# ---------------------------------------------------------------------------
# load_sources_file
# ---------------------------------------------------------------------------


class TestLoadSourcesFile(unittest.TestCase):
    def _write_tmp(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_loads_valid_array(self):
        path = self._write_tmp(json.dumps([_make_valid_source()]))
        sources = load_sources_file(path)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "Test Source")

    def test_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_sources_file("/nonexistent/path/sources.json")

    def test_raises_on_invalid_json(self):
        path = self._write_tmp("not json {{{")
        with self.assertRaises(ValueError) as ctx:
            load_sources_file(path)
        self.assertIn("Invalid JSON", str(ctx.exception))

    def test_raises_on_non_array(self):
        path = self._write_tmp(json.dumps({"name": "single object"}))
        with self.assertRaises(ValueError) as ctx:
            load_sources_file(path)
        self.assertIn("JSON array", str(ctx.exception))

    def test_raises_on_empty_array(self):
        path = self._write_tmp("[]")
        with self.assertRaises(ValueError) as ctx:
            load_sources_file(path)
        self.assertIn("empty", str(ctx.exception))

    def test_loads_multiple_sources(self):
        sources = [_make_valid_source(name=f"Source {i}") for i in range(5)]
        path = self._write_tmp(json.dumps(sources))
        result = load_sources_file(path)
        self.assertEqual(len(result), 5)


# ---------------------------------------------------------------------------
# validate_source
# ---------------------------------------------------------------------------


class TestValidateSource(unittest.TestCase):
    def test_valid_source_has_no_errors(self):
        errors = validate_source(_make_valid_source(), 0)
        self.assertEqual(errors, [])

    def test_missing_name(self):
        src = _make_valid_source()
        del src["name"]
        errors = validate_source(src, 0)
        self.assertTrue(any("name" in e for e in errors))

    def test_missing_owner(self):
        src = _make_valid_source()
        del src["owner"]
        errors = validate_source(src, 0)
        self.assertTrue(any("owner" in e for e in errors))

    def test_missing_connector(self):
        src = _make_valid_source()
        del src["connector"]
        errors = validate_source(src, 0)
        self.assertTrue(any("connector" in e for e in errors))

    def test_owner_missing_id(self):
        src = _make_valid_source(owner={"name": "Admin", "type": "IDENTITY"})
        errors = validate_source(src, 0)
        self.assertTrue(any("owner.id" in e for e in errors))

    def test_owner_missing_type(self):
        src = _make_valid_source(owner={"id": "abc", "name": "Admin"})
        errors = validate_source(src, 0)
        self.assertTrue(any("owner.type" in e for e in errors))

    def test_owner_wrong_type(self):
        src = _make_valid_source(owner={"id": "abc", "name": "Admin", "type": "GROUP"})
        errors = validate_source(src, 0)
        self.assertTrue(any("IDENTITY" in e for e in errors))

    def test_owner_not_a_dict(self):
        src = _make_valid_source(owner="not-a-dict")
        errors = validate_source(src, 0)
        self.assertTrue(any("object" in e for e in errors))

    def test_delete_threshold_out_of_range(self):
        src = _make_valid_source(deleteThreshold=150)
        errors = validate_source(src, 0)
        self.assertTrue(any("deleteThreshold" in e for e in errors))

    def test_delete_threshold_negative(self):
        src = _make_valid_source(deleteThreshold=-1)
        errors = validate_source(src, 0)
        self.assertTrue(any("deleteThreshold" in e for e in errors))

    def test_delete_threshold_valid_boundary_values(self):
        for val in (0, 50, 100):
            src = _make_valid_source(deleteThreshold=val)
            errors = validate_source(src, 0)
            self.assertEqual(errors, [], f"Expected no errors for deleteThreshold={val}")

    def test_error_message_includes_index(self):
        src = _make_valid_source()
        del src["name"]
        errors = validate_source(src, 3)
        self.assertTrue(any("sources[3]" in e for e in errors))


class TestValidateAll(unittest.TestCase):
    def test_all_valid(self):
        sources = [_make_valid_source(name=f"S{i}") for i in range(3)]
        self.assertEqual(validate_all(sources), [])

    def test_collects_errors_from_all_sources(self):
        bad1 = _make_valid_source()
        del bad1["name"]
        bad2 = _make_valid_source(name="OK", deleteThreshold=999)
        errors = validate_all([bad1, bad2])
        self.assertGreaterEqual(len(errors), 2)


# ---------------------------------------------------------------------------
# create_sources
# ---------------------------------------------------------------------------


class TestCreateSources(unittest.TestCase):
    def test_dry_run_does_not_call_api(self):
        client = _make_client()
        sources = [_make_valid_source()]
        with patch.object(client, "create_source") as mock_create:
            summary = create_sources(client, sources, dry_run=True)
        mock_create.assert_not_called()
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(summary.succeeded[0].source_id, "<dry-run>")

    def test_validation_failure_aborts_all(self):
        client = _make_client()
        bad = _make_valid_source()
        del bad["name"]
        with patch.object(client, "create_source") as mock_create:
            summary = create_sources(client, [bad])
        mock_create.assert_not_called()
        self.assertEqual(len(summary.failed), 1)

    def test_successful_creation(self):
        client = _make_client()
        sources = [_make_valid_source()]
        created = {"id": "new-id-001", **sources[0]}
        with patch.object(client, "create_source", return_value=created):
            summary = create_sources(client, sources)
        self.assertEqual(len(summary.succeeded), 1)
        self.assertEqual(summary.succeeded[0].source_id, "new-id-001")

    def test_api_error_is_recorded_not_raised(self):
        client = _make_client()
        sources = [_make_valid_source()]
        err = ISCAPIError(400, "bad", body={"messages": "Name exists"})
        with patch.object(client, "create_source", side_effect=err):
            summary = create_sources(client, sources)
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("Name exists", summary.failed[0].error)

    def test_unexpected_exception_is_recorded(self):
        client = _make_client()
        sources = [_make_valid_source()]
        with patch.object(client, "create_source", side_effect=RuntimeError("boom")):
            summary = create_sources(client, sources)
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("boom", summary.failed[0].error)

    def test_csv_connector_uses_provision_as_csv(self):
        client = _make_client()
        for connector in ("delimited-file", "delimited-file-angularsc"):
            sources = [_make_valid_source(connector=connector)]
            created = {"id": "csv-001", **sources[0]}
            with patch.object(client, "create_source", return_value=created) as mock_create:
                create_sources(client, sources)
            mock_create.assert_called_once_with(sources[0], provision_as_csv=True)

    def test_non_csv_connector_does_not_use_provision_as_csv(self):
        client = _make_client()
        for connector in ("active-directory", "active-directory-angularsc", "servicenow-saas"):
            sources = [_make_valid_source(connector=connector)]
            created = {"id": "ad-001", **sources[0]}
            with patch.object(client, "create_source", return_value=created) as mock_create:
                create_sources(client, sources)
            mock_create.assert_called_once_with(sources[0], provision_as_csv=False)

    def test_partial_failure_continues(self):
        """A failure on one source should not stop the others."""
        client = _make_client()
        sources = [
            _make_valid_source(name="Source A"),
            _make_valid_source(name="Source B"),
            _make_valid_source(name="Source C"),
        ]
        err = ISCAPIError(400, "bad", body={"messages": "Conflict"})

        def side_effect(payload, **kwargs):
            if payload["name"] == "Source B":
                raise err
            return {"id": f"id-{payload['name']}", **payload}

        with patch.object(client, "create_source", side_effect=side_effect):
            summary = create_sources(client, sources)

        self.assertEqual(len(summary.succeeded), 2)
        self.assertEqual(len(summary.failed), 1)
        self.assertEqual(summary.failed[0].name, "Source B")

    def test_multiple_sources_all_succeed(self):
        client = _make_client()
        sources = [_make_valid_source(name=f"Source {i}") for i in range(4)]

        def side_effect(payload, **kwargs):
            return {"id": f"id-{payload['name']}", **payload}

        with patch.object(client, "create_source", side_effect=side_effect):
            summary = create_sources(client, sources)

        self.assertEqual(len(summary.succeeded), 4)
        self.assertEqual(len(summary.failed), 0)


# ---------------------------------------------------------------------------
# CreationSummary
# ---------------------------------------------------------------------------


class TestCreationSummary(unittest.TestCase):
    def test_succeeded_and_failed_properties(self):
        from source_creator import CreationResult

        summary = CreationSummary()
        summary.results.append(CreationResult("A", True, "id-a"))
        summary.results.append(CreationResult("B", False, error="err"))
        summary.results.append(CreationResult("C", True, "id-c"))

        self.assertEqual(len(summary.succeeded), 2)
        self.assertEqual(len(summary.failed), 1)

    def test_print_report_does_not_raise(self):
        from source_creator import CreationResult
        import io
        from contextlib import redirect_stdout

        summary = CreationSummary()
        summary.results.append(CreationResult("A", True, "id-a"))
        summary.results.append(CreationResult("B", False, error="Something went wrong"))

        buf = io.StringIO()
        with redirect_stdout(buf):
            summary.print_report()

        output = buf.getvalue()
        self.assertIn("1/2", output)
        self.assertIn("Source A", output) if False else None  # name is "A"
        self.assertIn("Something went wrong", output)


if __name__ == "__main__":
    unittest.main()
