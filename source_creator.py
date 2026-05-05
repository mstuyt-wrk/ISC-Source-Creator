"""
Source creation logic.

Reads a list of source definitions from a JSON file, validates them,
and creates each one via the ISC API.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from isc_client import ISCAPIError, ISCClient

logger = logging.getLogger(__name__)

# Minimum required fields for a source definition
_REQUIRED_FIELDS = ("name", "owner", "connector")

# Connectors that must be created with provisionAsCsv=true.
# Matched as a substring so both "delimited-file" and
# "delimited-file-angularsc" are handled correctly.
_CSV_CONNECTOR_SUBSTRING = "delimited-file"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CreationResult:
    """Outcome of a single source creation attempt."""

    name: str
    success: bool
    source_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CreationSummary:
    """Aggregated results from a bulk creation run."""

    results: list[CreationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[CreationResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[CreationResult]:
        return [r for r in self.results if not r.success]

    def print_report(self) -> None:
        total = len(self.results)
        ok = len(self.succeeded)

        print(f"\n{'='*60}")
        print(f"  Source Creation Summary: {ok}/{total} succeeded")
        print(f"{'='*60}")

        if self.succeeded:
            print("\n✓ Created successfully:")
            for r in self.succeeded:
                print(f"    {r.name}  (id: {r.source_id})")

        if self.failed:
            print("\n✗ Failed:")
            for r in self.failed:
                print(f"    {r.name}")
                print(f"      → {r.error}")

        print()


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def load_sources_file(path: str) -> list[dict]:
    """
    Load and parse a JSON file containing an array of source definitions.

    Args:
        path: Path to the JSON file.

    Returns:
        List of source definition dicts.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not valid JSON or not a JSON array.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Sources file not found: {path}")

    with file_path.open("r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in '{path}': {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(
            f"Expected a JSON array in '{path}', got {type(data).__name__}"
        )

    if len(data) == 0:
        raise ValueError(f"Sources file '{path}' contains an empty array")

    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_source(source: dict, index: int) -> list[str]:
    """
    Validate a single source definition.

    Returns a list of validation error messages (empty list means valid).
    """
    errors: list[str] = []
    label = f"sources[{index}] (name={source.get('name', '<missing>')})"

    # Required top-level fields
    for fname in _REQUIRED_FIELDS:
        if not source.get(fname):
            errors.append(f"{label}: missing required field '{fname}'")

    # owner sub-object
    owner = source.get("owner")
    if isinstance(owner, dict):
        if not owner.get("id"):
            errors.append(f"{label}: owner.id is required")
        if owner.get("type") not in (None, "IDENTITY"):
            errors.append(
                f"{label}: owner.type must be 'IDENTITY', got '{owner.get('type')}'"
            )
        if not owner.get("type"):
            errors.append(f"{label}: owner.type is required (use 'IDENTITY')")
    elif owner is not None:
        errors.append(f"{label}: 'owner' must be an object with id/type/name")

    # deleteThreshold sanity check
    threshold = source.get("deleteThreshold")
    if threshold is not None:
        if not isinstance(threshold, int) or not (0 <= threshold <= 100):
            errors.append(
                f"{label}: deleteThreshold must be an integer between 0 and 100"
            )

    return errors


def validate_all(sources: list[dict]) -> list[str]:
    """Validate every source in the list and return all errors."""
    errors: list[str] = []
    for i, source in enumerate(sources):
        errors.extend(validate_source(source, i))
    return errors


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_sources(
    client: ISCClient,
    sources: list[dict],
    dry_run: bool = False,
) -> CreationSummary:
    """
    Create a list of sources via the ISC API.

    Validates all definitions first; if any fail validation the entire run is
    aborted before any API calls are made.

    Args:
        client:  Authenticated ISCClient instance.
        sources: List of source definition dicts.
        dry_run: If True, validate and log but do not call the API.

    Returns:
        CreationSummary with per-source results.
    """
    summary = CreationSummary()

    # --- Validate all sources before touching the API ---
    all_errors = validate_all(sources)
    if all_errors:
        logger.error("Validation failed with %d error(s):", len(all_errors))
        for err in all_errors:
            logger.error("  %s", err)
        for source in sources:
            summary.results.append(
                CreationResult(
                    name=source.get("name", "<unknown>"),
                    success=False,
                    error="Validation failed (see log for details)",
                )
            )
        return summary

    if dry_run:
        logger.info(
            "Dry-run mode: skipping API calls for %d source(s)", len(sources)
        )
        for source in sources:
            name = source.get("name", "<unknown>")
            logger.info("  [DRY RUN] Would create source: %s", name)
            summary.results.append(
                CreationResult(name=name, success=True, source_id="<dry-run>")
            )
        return summary

    # --- Create each source ---
    for source in sources:
        name = source.get("name", "<unknown>")
        is_csv = _CSV_CONNECTOR_SUBSTRING in source.get("connector", "").lower()

        try:
            logger.info("Creating source: %s", name)
            created = client.create_source(source, provision_as_csv=is_csv)
            source_id = created.get("id", "<unknown>")
            logger.info("  ✓ Created '%s'  id=%s", name, source_id)
            summary.results.append(
                CreationResult(name=name, success=True, source_id=source_id)
            )
        except ISCAPIError as exc:
            detail = exc.detail()
            logger.error("  ✗ Failed to create '%s': %s", name, detail)
            summary.results.append(
                CreationResult(name=name, success=False, error=detail)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("  ✗ Unexpected error creating '%s': %s", name, exc)
            summary.results.append(
                CreationResult(name=name, success=False, error=str(exc))
            )

    return summary
