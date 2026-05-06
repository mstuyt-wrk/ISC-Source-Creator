"""
Provisioner — end-to-end demo data setup for Delimited File sources.

Two account-population modes
-----------------------------
Random mode (default)
    Fetches up to 250 identities from the tenant, randomly samples 10 per
    source, and randomly assigns 1–3 entitlements to each account.

    python main.py provision --count 5 --name "Demo" --owner-id <id>

File mode  (--users-file)
    Reads a plain-text file of usernames/aliases (one per line).  Every user
    in the file is added as an account on *every* source, each receiving all
    4 entitlements assigned randomly (1–4 per user).

    python main.py provision --count 5 --name "Demo" --owner-id <id> \\
        --users-file users.txt

users.txt format
    One alias per line.  Blank lines and lines starting with # are ignored.
    Example:
        jsmith
        adoe
        # this is a comment
        bjones

Authoritative sources  (--authoritative)
-----------------------------------------
When --authoritative is set the source is created with ``authoritative=True``
and an identity profile is automatically created and linked to it.

The CSV supplied via --users-file (required in this mode) must contain the
following columns:

    firstName, lastName, fullName, email

These are mapped to ISC identity attributes via the identity profile's
attribute-transform configuration:

    ISC attribute   ←  source account attribute
    firstname       ←  givenName   (populated from CSV firstName column)
    lastname        ←  familyName  (populated from CSV lastName column)
    displayName     ←  name        (populated from CSV fullName column)
    email           ←  e-mail      (populated from CSV email column)

CSV schemas
-----------
Account columns (auto-discovered from source schema, falls back to ISC defaults):
    id, name, givenName, familyName, e-mail, location, manager, groups

Entitlement columns (auto-discovered, falls back to ISC defaults):
    id, name, displayName, created, modified, entitlements, groups, permissions
"""

import csv
import io
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from isc_client import ISCAPIError, ISCClient

logger = logging.getLogger(__name__)

# Fixed entitlement catalogue — same for every source
ENTITLEMENTS = [
    {"id": "read",       "name": "Read",       "description": "Read access",       "type": "entitlement"},
    {"id": "write",      "name": "Write",       "description": "Write access",      "type": "entitlement"},
    {"id": "update",     "name": "Update",      "description": "Update access",     "type": "entitlement"},
    {"id": "audit_view", "name": "Audit View",  "description": "Audit view access", "type": "entitlement"},
]

ACCOUNTS_PER_SOURCE = 10
IDENTITY_POOL_SIZE = 250


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class SourceProvisionResult:
    """Outcome of provisioning a single source."""
    name: str
    success: bool
    source_id: Optional[str] = None
    accounts_loaded: int = 0
    entitlements_loaded: int = 0
    error: Optional[str] = None


@dataclass
class ProvisionSummary:
    results: list[SourceProvisionResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SourceProvisionResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[SourceProvisionResult]:
        return [r for r in self.results if not r.success]

    def print_report(self) -> None:
        total = len(self.results)
        ok = len(self.succeeded)
        print(f"\n{'='*70}")
        print(f"  Provision Summary: {ok}/{total} sources fully provisioned")
        print(f"{'='*70}")

        if self.succeeded:
            print("\n✓ Provisioned successfully:")
            for r in self.succeeded:
                print(
                    f"    {r.name:<40}  id={r.source_id}  "
                    f"accounts={r.accounts_loaded}  entitlements={r.entitlements_loaded}"
                )

        if self.failed:
            print("\n✗ Failed:")
            for r in self.failed:
                print(f"    {r.name}")
                print(f"      → {r.error}")
        print()


# ---------------------------------------------------------------------------
# Users file loading
# ---------------------------------------------------------------------------

def load_users_file(path: str) -> list[str]:
    """
    Load a plain-text file of usernames/aliases, one per line.

    Blank lines and lines starting with ``#`` are ignored.
    Leading/trailing whitespace is stripped from each entry.

    Args:
        path: Path to the users file.

    Returns:
        List of alias strings.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file contains no valid entries.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Users file not found: {path}")

    aliases: list[str] = []
    with file_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                aliases.append(stripped)

    if not aliases:
        raise ValueError(
            f"Users file '{path}' contains no valid entries. "
            "Add one alias per line; blank lines and # comments are ignored."
        )

    return aliases


# Required columns for the authoritative CSV (case-insensitive header match)
_AUTHORITATIVE_REQUIRED_COLUMNS = {"firstname", "lastname", "fullname", "email"}


def load_authoritative_csv(path: str) -> list[dict]:
    """
    Load a CSV file of user records for authoritative source provisioning.

    The CSV must contain at minimum the following columns (case-insensitive):

        firstName, lastName, fullName, email

    Additional columns are allowed and passed through unchanged.

    Args:
        path: Path to the CSV file.

    Returns:
        List of row dicts with normalised keys (original case preserved).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If required columns are missing or the file has no data rows.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Authoritative CSV file not found: {path}")

    with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file '{path}' appears to be empty.")

        # Validate required columns (case-insensitive)
        header_lower = {col.strip().lower() for col in reader.fieldnames}
        missing = _AUTHORITATIVE_REQUIRED_COLUMNS - header_lower
        if missing:
            raise ValueError(
                f"Authoritative CSV '{path}' is missing required column(s): "
                f"{', '.join(sorted(missing))}. "
                f"Required: firstName, lastName, fullName, email."
            )

        rows = [
            {k.strip(): v.strip() if v else "" for k, v in row.items()}
            for row in reader
            if any(v and v.strip() for v in row.values())  # skip blank rows
        ]

    if not rows:
        raise ValueError(
            f"Authoritative CSV '{path}' contains no data rows."
        )

    return rows


def _authoritative_rows_to_identity_dicts(rows: list[dict]) -> list[dict]:
    """
    Normalise authoritative CSV rows into identity dicts compatible with
    ``_build_account_csv``.

    Performs case-insensitive column lookup so ``FirstName``, ``firstname``,
    and ``FIRSTNAME`` all work.
    """
    def _get(row: dict, *keys: str) -> str:
        """Case-insensitive key lookup across multiple candidate names."""
        row_lower = {k.lower(): v for k, v in row.items()}
        for key in keys:
            val = row_lower.get(key.lower(), "")
            if val:
                return val
        return ""

    result = []
    for row in rows:
        first = _get(row, "firstName", "firstname")
        last  = _get(row, "lastName",  "lastname")
        full  = _get(row, "fullName",  "fullname")
        email = _get(row, "email")

        # Derive a stable alias: use email local-part, fall back to fullName
        alias = email.split("@")[0] if email else (full.replace(" ", ".").lower() or "unknown")

        result.append({
            "id":        alias,
            "alias":     alias,
            "name":      full,
            "firstName": first,
            "lastName":  last,
            "email":     email,
        })
    return result


def _aliases_to_identity_dicts(aliases: list[str]) -> list[dict]:
    """
    Convert a list of alias strings into minimal identity dicts compatible
    with ``_build_account_csv``.

    Since we only have the alias, all other fields are left empty.
    ISC will correlate the account to the identity via the alias/id match.
    """
    return [
        {
            "id":    alias,
            "alias": alias,
            "name":  alias,
            "firstName": "",
            "lastName":  "",
            "email":     "",
        }
        for alias in aliases
    ]


# ---------------------------------------------------------------------------
# Identity pool (random mode)
# ---------------------------------------------------------------------------

def fetch_identity_pool(
    client: ISCClient,
    exclude_alias: Optional[str] = None,
    pool_size: int = IDENTITY_POOL_SIZE,
) -> list[dict]:
    """
    Fetch up to ``pool_size`` non-admin identities from the tenant.

    Tries two strategies:
    1. GET /v3/identities (list endpoint)
    2. POST /v3/search with a wildcard query (fallback)

    Args:
        client:        Authenticated ISCClient.
        exclude_alias: Alias of the PAT owner to exclude (e.g. "spadmin").
        pool_size:     Maximum number of identities to fetch.

    Returns:
        List of identity dicts.
    """
    logger.info("Fetching identity pool (up to %d identities)...", pool_size)

    identities: list[dict] = []

    try:
        identities = client.list_identities(limit=pool_size)
        logger.debug("Identity pool fetched via list endpoint (%d)", len(identities))
    except ISCAPIError as exc:
        logger.debug("List endpoint failed (%s), falling back to Search API", exc.status_code)

    if not identities:
        try:
            identities = client.search_identities(query="*", limit=pool_size)
            logger.debug("Identity pool fetched via Search API (%d)", len(identities))
        except ISCAPIError as exc:
            raise RuntimeError(f"Failed to fetch identities: {exc.detail()}") from exc

    if not identities:
        raise RuntimeError(
            "No identities returned from the tenant. "
            "Ensure the PAT has at least SOURCE_ADMIN authority."
        )

    before = len(identities)
    if exclude_alias:
        identities = [
            i for i in identities
            if (i.get("alias") or "").lower() != exclude_alias.lower()
            and (i.get("name") or "").lower() != exclude_alias.lower()
        ]
        excluded = before - len(identities)
        if excluded:
            logger.info(
                "  Excluded %d identity/identities matching alias '%s'",
                excluded, exclude_alias,
            )

    logger.info("  Identity pool: %d identities available", len(identities))
    if len(identities) < ACCOUNTS_PER_SOURCE:
        raise RuntimeError(
            f"Not enough identities in the pool: need {ACCOUNTS_PER_SOURCE}, "
            f"found {len(identities)}. Try a larger tenant or reduce ACCOUNTS_PER_SOURCE."
        )
    return identities


# ---------------------------------------------------------------------------
# Entitlement assignment
# ---------------------------------------------------------------------------

def _assign_entitlements_random(identities: list[dict]) -> dict[str, list[str]]:
    """
    Randomly assign 1–3 entitlements from the catalogue to each identity.

    Used in random mode.

    Returns a dict mapping alias → list of entitlement IDs.
    """
    ent_ids = [e["id"] for e in ENTITLEMENTS]
    assignments: dict[str, list[str]] = {}
    for ident in identities:
        alias = ident.get("alias") or ident.get("name") or ident["id"]
        count = random.randint(1, min(3, len(ent_ids)))
        assignments[alias] = random.sample(ent_ids, count)
    return assignments


def _assign_entitlements_file_mode(identities: list[dict]) -> dict[str, list[str]]:
    """
    Assign all 4 entitlements to every identity, in a random order.

    Used in file mode — every listed user gets all entitlements on every source,
    with the assignment order randomised per user.

    Returns a dict mapping alias → list of all entitlement IDs (shuffled).
    """
    ent_ids = [e["id"] for e in ENTITLEMENTS]
    assignments: dict[str, list[str]] = {}
    for ident in identities:
        alias = ident.get("alias") or ident.get("name") or ident["id"]
        shuffled = ent_ids[:]
        random.shuffle(shuffled)
        assignments[alias] = shuffled
    return assignments


# ---------------------------------------------------------------------------
# Schema discovery
# ---------------------------------------------------------------------------

def _get_account_schema_columns(client: ISCClient, source_id: str) -> list[str]:
    """
    Fetch the account schema for a source and return the column names in order.

    Falls back to the ISC Delimited File default schema on any error.
    """
    default_columns = ["id", "name", "givenName", "familyName", "e-mail", "location", "manager", "groups"]

    try:
        schemas = client.list_source_schemas(source_id)
        account_schema = next(
            (s for s in schemas if (s.get("name") or "").lower() == "account"),
            None,
        )
        if not account_schema:
            return default_columns

        attributes = account_schema.get("attributes") or []
        if not attributes:
            return default_columns

        columns = [a["name"] for a in attributes if a.get("name")]
        for required in ("name", "id"):
            if required in columns:
                columns.remove(required)
                columns.insert(0, required)

        logger.debug("  Account schema columns: %s", columns)
        return columns

    except Exception as exc:  # noqa: BLE001
        logger.debug("  Account schema fetch failed (%s), using defaults", exc)
        return default_columns


def _get_entitlement_schema_columns(client: ISCClient, source_id: str) -> list[str]:
    """
    Fetch the entitlement (group) schema for a source and return column names.

    Falls back to the ISC Delimited File default entitlement schema on any error.
    """
    default_columns = [
        "id", "name", "displayName", "created", "modified",
        "entitlements", "groups", "permissions",
    ]

    try:
        schemas = client.list_source_schemas(source_id)
        group_schema = next(
            (s for s in schemas if (s.get("name") or "").lower() != "account"),
            None,
        )
        if not group_schema:
            return default_columns

        attributes = group_schema.get("attributes") or []
        if not attributes:
            return default_columns

        columns = [a["name"] for a in attributes if a.get("name")]
        for required in ("name", "id"):
            if required in columns:
                columns.remove(required)
                columns.insert(0, required)

        logger.debug("  Entitlement schema columns: %s", columns)
        return columns

    except Exception as exc:  # noqa: BLE001
        logger.debug("  Entitlement schema fetch failed (%s), using defaults", exc)
        return default_columns


# ---------------------------------------------------------------------------
# CSV builders
# ---------------------------------------------------------------------------

def _build_account_csv(
    identities: list[dict],
    entitlement_assignments: dict[str, list[str]],
    columns: Optional[list[str]] = None,
) -> bytes:
    """
    Build an account CSV for the given identities.

    Column names are taken from ``columns`` (discovered from the source schema).
    Falls back to the ISC Delimited File defaults if not provided.

    Identity fields are mapped to schema columns by best-effort name matching.
    Unknown columns receive an empty string.
    """
    if columns is None:
        columns = ["id", "name", "givenName", "familyName", "e-mail", "location", "manager", "groups"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()

    for ident in identities:
        alias = ident.get("alias") or ident.get("name") or ident["id"]
        assigned = entitlement_assignments.get(alias, [])
        entitlement_str = ",".join(assigned)

        value_map: dict[str, str] = {
            "id":           alias,
            "name":         alias,
            "givenName":    ident.get("firstName") or ident.get("givenName") or "",
            "familyName":   ident.get("lastName")  or ident.get("familyName") or "",
            "email":        ident.get("email") or ident.get("emailAddress") or "",
            "e-mail":       ident.get("email") or ident.get("emailAddress") or "",
            "emailAddress": ident.get("email") or ident.get("emailAddress") or "",
            "location":     "",
            "manager":      "",
            "groups":       entitlement_str,
            "entitlements": entitlement_str,
        }

        row = {col: value_map.get(col, "") for col in columns}
        writer.writerow(row)

    return buf.getvalue().encode("utf-8")


def _build_entitlement_csv(columns: Optional[list[str]] = None) -> bytes:
    """
    Build the entitlement CSV.

    Column names are taken from ``columns`` (discovered from the source schema).
    Falls back to the ISC Delimited File default entitlement schema if not provided.
    """
    if columns is None:
        columns = [
            "id", "name", "displayName", "created", "modified",
            "entitlements", "groups", "permissions",
        ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()

    for ent in ENTITLEMENTS:
        value_map: dict[str, str] = {
            "id":           ent["id"],
            "name":         ent["name"],
            "displayName":  ent["name"],
            "description":  ent["description"],
            "type":         ent.get("type", ""),
            "created":      "",
            "modified":     "",
            "entitlements": "",
            "groups":       "",
            "permissions":  "",
        }
        row = {col: value_map.get(col, "") for col in columns}
        writer.writerow(row)

    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Main provision function
# ---------------------------------------------------------------------------

def provision_sources(
    client: ISCClient,
    count: int,
    base_name: str,
    owner_id: str,
    owner_name: str,
    exclude_alias: Optional[str] = None,
    users_file: Optional[str] = None,
    authoritative: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> ProvisionSummary:
    """
    Create ``count`` Delimited File sources and aggregate account + entitlement
    data into each one.

    Account population mode is determined by ``users_file``:

    - **Random mode** (``users_file=None``): fetches up to 250 tenant identities,
      randomly samples 10 per source, assigns 1–3 random entitlements each.

    - **File mode** (``users_file`` provided): reads aliases from the file,
      uses those same users on every source, assigns all 4 entitlements to
      each user (in a random order per user).

    When ``authoritative=True``:

    - The source is created with ``authoritative=True``.
    - ``users_file`` is **required** and must be a CSV with columns:
      ``firstName``, ``lastName``, ``fullName``, ``email``.
    - An identity profile is automatically created and linked to the source
      after it is provisioned.

    Args:
        client:        Authenticated ISCClient.
        count:         Number of sources to create.
        base_name:     Name prefix — sources will be named "<base_name> 1", etc.
        owner_id:      Identity ID of the source owner.
        owner_name:    Display name of the source owner.
        exclude_alias: Alias to exclude from the random pool (random mode only).
        users_file:    Path to a CSV file (authoritative mode) or plain-text
                       aliases file (file mode).
        authoritative: If True, create an authoritative source and identity
                       profile.  Requires ``users_file`` to be a CSV with
                       firstName, lastName, fullName, email columns.
        dry_run:       If True, generate CSVs and log but make no API calls.
        force:         If True, delete any existing source with the same name
                       before creating it.

    Returns:
        ProvisionSummary with per-source results.
    """
    summary = ProvisionSummary()

    # --- Validate authoritative mode requirements ---
    if authoritative and not users_file:
        err = (
            "--authoritative requires --users-file pointing to a CSV with "
            "columns: firstName, lastName, fullName, email."
        )
        logger.error(err)
        for i in range(1, count + 1):
            summary.results.append(SourceProvisionResult(
                name=f"{base_name} {i}", success=False, error=err
            ))
        return summary

    if authoritative and count != 1:
        err = (
            "Authoritative mode only supports count=1. "
            "An authoritative source represents a single system of record."
        )
        logger.error(err)
        for i in range(1, count + 1):
            summary.results.append(SourceProvisionResult(
                name=f"{base_name} {i}", success=False, error=err
            ))
        return summary

    # --- Resolve the account list ---
    if users_file and authoritative:
        # Authoritative mode — load structured CSV rows
        try:
            csv_rows = load_authoritative_csv(users_file)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("%s", exc)
            for i in range(1, count + 1):
                summary.results.append(SourceProvisionResult(
                    name=f"{base_name} {i}", success=False, error=str(exc)
                ))
            return summary

        file_identities = _authoritative_rows_to_identity_dicts(csv_rows)
        logger.info(
            "Authoritative mode: %d user(s) loaded from '%s' — each will appear "
            "on every source with all 4 entitlements assigned randomly.",
            len(file_identities), users_file,
        )
        pool: Optional[list[dict]] = None

    elif users_file:
        # Regular file mode — plain-text aliases
        try:
            aliases = load_users_file(users_file)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("%s", exc)
            for i in range(1, count + 1):
                summary.results.append(SourceProvisionResult(
                    name=f"{base_name} {i}", success=False, error=str(exc)
                ))
            return summary

        file_identities = _aliases_to_identity_dicts(aliases)
        logger.info(
            "File mode: %d user(s) loaded from '%s' — each will appear on every source "
            "with all 4 entitlements assigned randomly.",
            len(file_identities), users_file,
        )
        pool = None

    else:
        # Random mode — fetch identity pool once, sample per source
        file_identities = []
        try:
            pool = fetch_identity_pool(client, exclude_alias=exclude_alias)
        except RuntimeError as exc:
            logger.error("%s", exc)
            for i in range(1, count + 1):
                summary.results.append(SourceProvisionResult(
                    name=f"{base_name} {i}", success=False, error=str(exc)
                ))
            return summary

    for i in range(1, count + 1):
        source_name = f"{base_name} {i}"
        logger.info("─" * 60)
        logger.info("Provisioning source %d/%d: %s", i, count, source_name)

        # --- Build account sample and entitlement assignments ---
        if users_file:
            sample = file_identities
            assignments = _assign_entitlements_file_mode(sample)
        else:
            sample = random.sample(pool, ACCOUNTS_PER_SOURCE)  # type: ignore[arg-type]
            assignments = _assign_entitlements_random(sample)

        account_csv = _build_account_csv(sample, assignments)

        if dry_run:
            logger.info("  [DRY RUN] Would create source '%s' (authoritative=%s)", source_name, authoritative)
            logger.info(
                "  [DRY RUN] Mode: %s",
                f"authoritative CSV ({len(sample)} users from '{users_file}')" if authoritative
                else f"file ({len(sample)} users from '{users_file}')" if users_file
                else f"random ({len(sample)} sampled from pool)",
            )
            logger.info("  [DRY RUN] Account CSV (%d bytes):", len(account_csv))
            for line in account_csv.decode().splitlines():
                logger.info("    %s", line)
            dry_ent_csv = _build_entitlement_csv()
            logger.info("  [DRY RUN] Entitlement CSV (%d bytes):", len(dry_ent_csv))
            for line in dry_ent_csv.decode().splitlines():
                logger.info("    %s", line)
            if authoritative:
                logger.info(
                    "  [DRY RUN] Would create identity profile '%s' linked to source",
                    source_name,
                )
            summary.results.append(SourceProvisionResult(
                name=source_name,
                success=True,
                source_id="<dry-run>",
                accounts_loaded=len(sample),
                entitlements_loaded=len(ENTITLEMENTS),
            ))
            continue

        # --- Optional: delete existing source with the same name ---
        if force:
            _delete_existing_source(client, source_name)

        # --- Step 1: Create the source ---
        source_payload = {
            "name": source_name,
            "description": f"Demo Delimited File source — {source_name}",
            "owner": {"id": owner_id, "name": owner_name, "type": "IDENTITY"},
            "connector": "delimited-file-angularsc",
            "connectorName": "Delimited File",
            "connectionType": "file",
            "authoritative": authoritative,
            "deleteThreshold": 10,
        }

        try:
            created = client.create_source(source_payload, provision_as_csv=True)
            source_id = created["id"]
            logger.info("  ✓ Source created  id=%s  authoritative=%s", source_id, authoritative)
        except ISCAPIError as exc:
            detail = exc.detail()
            if "already exists" in detail.lower():
                detail += " — use --force to delete and recreate, or choose a different --name"
            logger.error("  ✗ Failed to create source: %s", detail)
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, error=detail
            ))
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error("  ✗ Unexpected error creating source: %s", exc)
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, error=str(exc)
            ))
            continue

        # Brief pause — let ISC finish initialising the new source
        time.sleep(2)

        # --- Step 2 (authoritative only): Create identity profile ---
        if authoritative:
            try:
                profile = client.create_identity_profile(
                    name=source_name,
                    authoritative_source_id=source_id,
                    authoritative_source_name=source_name,
                    owner_id=owner_id,
                    owner_name=owner_name,
                )
                logger.info(
                    "  ✓ Identity profile created  id=%s",
                    profile.get("id", "<unknown>"),
                )
            except ISCAPIError as exc:
                logger.error("  ✗ Identity profile creation failed: %s", exc.detail())
                summary.results.append(SourceProvisionResult(
                    name=source_name, success=False, source_id=source_id,
                    error=f"Identity profile creation failed: {exc.detail()}",
                ))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error("  ✗ Identity profile creation error: %s", exc)
                summary.results.append(SourceProvisionResult(
                    name=source_name, success=False, source_id=source_id,
                    error=f"Identity profile creation error: {exc}",
                ))
                continue

        # --- Discover schemas ---
        account_columns = _get_account_schema_columns(client, source_id)
        entitlement_columns = _get_entitlement_schema_columns(client, source_id)

        # Rebuild CSVs with the correct schema columns
        account_csv = _build_account_csv(sample, assignments, account_columns)
        source_entitlement_csv = _build_entitlement_csv(entitlement_columns)

        # --- Step 3: Account aggregation ---
        try:
            task = client.import_accounts(
                source_id, account_csv,
                filename=f"{source_name}_accounts.csv",
            )
            logger.info(
                "  ✓ Account aggregation started  task=%s",
                task.get("id") or task.get("taskId"),
            )
        except ISCAPIError as exc:
            logger.error("  ✗ Account aggregation failed: %s", exc.detail())
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, source_id=source_id,
                error=f"Account aggregation failed: {exc.detail()}",
            ))
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error("  ✗ Account aggregation error: %s", exc)
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, source_id=source_id,
                error=f"Account aggregation error: {exc}",
            ))
            continue

        # --- Step 4: Entitlement aggregation ---
        try:
            task = client.import_entitlements(
                source_id, source_entitlement_csv,
                filename=f"{source_name}_entitlements.csv",
            )
            logger.info(
                "  ✓ Entitlement aggregation started  task=%s",
                task.get("id") or task.get("taskId"),
            )
        except ISCAPIError as exc:
            logger.error("  ✗ Entitlement aggregation failed: %s", exc.detail())
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, source_id=source_id,
                accounts_loaded=len(sample),
                error=f"Entitlement aggregation failed: {exc.detail()}",
            ))
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error("  ✗ Entitlement aggregation error: %s", exc)
            summary.results.append(SourceProvisionResult(
                name=source_name, success=False, source_id=source_id,
                accounts_loaded=len(sample),
                error=f"Entitlement aggregation error: {exc}",
            ))
            continue

        summary.results.append(SourceProvisionResult(
            name=source_name,
            success=True,
            source_id=source_id,
            accounts_loaded=len(sample),
            entitlements_loaded=len(ENTITLEMENTS),
        ))

    return summary


def _delete_existing_source(client: ISCClient, source_name: str) -> None:
    """
    Find and delete a source by name if it exists.
    Logs but does not raise so the provision loop can continue.
    """
    try:
        existing = client.list_sources(filters=f'name eq "{source_name}"', limit=1)
        if not existing:
            return
        source_id = existing[0]["id"]
        logger.info(
            "  --force: deleting existing source '%s' (id=%s)",
            source_name, source_id,
        )
        client.delete_source(source_id)
        time.sleep(3)
    except ISCAPIError as exc:
        logger.warning("  --force: could not delete '%s': %s", source_name, exc.detail())
    except Exception as exc:  # noqa: BLE001
        logger.warning("  --force: could not delete '%s': %s", source_name, exc)
