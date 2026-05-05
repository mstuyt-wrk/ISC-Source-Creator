#!/usr/bin/env python3
"""
SailPoint ISC Source Creator — CLI entry point.

Commands
--------
create   Create sources from a JSON definition file.
list     List existing sources in ISC.
get      Get a single source by ID.
delete   Delete a source by ID.

Examples
--------
    python main.py create --file examples/sources.json
    python main.py create --file examples/sources.json --dry-run
    python main.py list
    python main.py list --filter 'name co "HR"' --output json
    python main.py get 2c9180835d191a86015d28455b4a2329
    python main.py delete 2c9180835d191a86015d28455b4a2329
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

from isc_client import ISCAPIError, ISCAuthError, ISCClient
from provisioner import provision_sources
from source_creator import create_sources, load_sources_file

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config() -> tuple[str, str, str, str]:
    """
    Load ISC credentials from environment variables (or a .env file).

    Returns:
        (tenant, client_id, client_secret, domain)

    Raises:
        SystemExit: If any required variable is missing.
    """
    load_dotenv()

    tenant = os.getenv("ISC_TENANT", "").strip()
    client_id = os.getenv("ISC_CLIENT_ID", "").strip()
    client_secret = os.getenv("ISC_CLIENT_SECRET", "").strip()
    domain = os.getenv("ISC_DOMAIN", "identitynow.com").strip()

    missing = [
        name
        for name, val in [
            ("ISC_TENANT", tenant),
            ("ISC_CLIENT_ID", client_id),
            ("ISC_CLIENT_SECRET", client_secret),
        ]
        if not val
    ]

    if missing:
        logger.error(
            "Missing required environment variable(s): %s\n"
            "Copy .env.example to .env and fill in your credentials.",
            ", ".join(missing),
        )
        sys.exit(1)

    return tenant, client_id, client_secret, domain


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def _print_sources_table(sources: list[dict]) -> None:
    if not sources:
        print("No sources found.")
        return

    print(f"\nFound {len(sources)} source(s):\n")
    id_w, name_w, conn_w, status_w = 36, 40, 30, 20
    header = (
        f"  {'ID':<{id_w}}  {'Name':<{name_w}}  "
        f"{'Connector':<{conn_w}}  {'Status':<{status_w}}"
    )
    print(header)
    print(f"  {'-'*id_w}  {'-'*name_w}  {'-'*conn_w}  {'-'*status_w}")
    for src in sources:
        connector = src.get("connectorName") or src.get("connector") or ""
        status = src.get("status") or ""
        print(
            f"  {src.get('id', ''):<{id_w}}  "
            f"{src.get('name', ''):<{name_w}}  "
            f"{connector:<{conn_w}}  "
            f"{status:<{status_w}}"
        )
    print()


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------


def cmd_provision(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'provision' sub-command."""
    # Resolve owner name if not supplied — failure here is non-fatal,
    # we fall back to using the ID as the display name
    owner_name = args.owner_name
    if not owner_name:
        try:
            ident = client.get_identity(args.owner_id)
            owner_name = (
                ident.get("displayName")
                or ident.get("name")
                or args.owner_id
            )
        except ISCAPIError:
            # Some tenants don't support GET /v3/identities/{id} — try a
            # filter search instead
            try:
                results = client.list_identities(
                    filters=f'id eq "{args.owner_id}"', limit=1
                )
                if results:
                    ident = results[0]
                    owner_name = (
                        ident.get("displayName")
                        or ident.get("name")
                        or args.owner_id
                    )
                else:
                    owner_name = args.owner_id
            except ISCAPIError:
                owner_name = args.owner_id

        if owner_name == args.owner_id:
            logger.warning(
                "Could not resolve display name for owner '%s' — using ID as name.",
                args.owner_id,
            )
        else:
            logger.info("Resolved owner: %s (%s)", owner_name, args.owner_id)

    logger.info(
        "Provisioning %d source(s) with base name '%s', owner: %s (%s)",
        args.count, args.name, owner_name, args.owner_id,
    )

    if args.dry_run:
        logger.info("DRY-RUN mode — no API calls will be made.")

    summary = provision_sources(
        client=client,
        count=args.count,
        base_name=args.name,
        owner_id=args.owner_id,
        owner_name=owner_name,
        exclude_alias=args.exclude_alias,
        users_file=args.users_file,
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.output == "json":
        _print_json([
            {
                "name": r.name,
                "success": r.success,
                "source_id": r.source_id,
                "accounts_loaded": r.accounts_loaded,
                "entitlements_loaded": r.entitlements_loaded,
                "error": r.error,
            }
            for r in summary.results
        ])
    else:
        summary.print_report()

    return 0 if not summary.failed else 1


def cmd_create(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'create' sub-command."""
    try:
        sources = load_sources_file(args.file)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Loaded %d source definition(s) from '%s'", len(sources), args.file
    )

    if args.dry_run:
        logger.info("Running in DRY-RUN mode — no API calls will be made.")

    summary = create_sources(client, sources, dry_run=args.dry_run)

    if args.output == "json":
        _print_json(
            [
                {
                    "name": r.name,
                    "success": r.success,
                    "id": r.source_id,
                    "error": r.error,
                }
                for r in summary.results
            ]
        )
    else:
        summary.print_report()

    return 0 if not summary.failed else 1


def cmd_list(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'list' sub-command."""
    try:
        if args.all:
            sources = list(
                client.iter_sources(filters=args.filter, sorters=args.sorters)
            )
        else:
            sources = client.list_sources(
                filters=args.filter,
                sorters=args.sorters,
                limit=args.limit,
                offset=args.offset,
            )
    except ISCAPIError as exc:
        logger.error("Failed to list sources: %s", exc.detail())
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list sources: %s", exc)
        return 1

    if args.output == "json":
        _print_json(sources)
    else:
        _print_sources_table(sources)

    return 0


def cmd_get(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'get' sub-command."""
    try:
        source = client.get_source(args.id)
    except ISCAPIError as exc:
        logger.error("Failed to get source '%s': %s", args.id, exc.detail())
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get source '%s': %s", args.id, exc)
        return 1

    if args.output == "json":
        _print_json(source)
    else:
        # Pretty human-readable summary
        print(f"\nSource: {source.get('name')}")
        print(f"  ID          : {source.get('id')}")
        print(f"  Connector   : {source.get('connectorName') or source.get('connector')}")
        print(f"  Type        : {source.get('type')}")
        print(f"  Status      : {source.get('status')}")
        print(f"  Healthy     : {source.get('healthy')}")
        print(f"  Authoritative: {source.get('authoritative')}")
        owner = source.get("owner") or {}
        print(f"  Owner       : {owner.get('name')} ({owner.get('id')})")
        print(f"  Description : {source.get('description')}")
        print(f"  Created     : {source.get('created')}")
        print(f"  Modified    : {source.get('modified')}")
        print()

    return 0


def cmd_list_connectors(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'list-connectors' sub-command."""
    try:
        connectors = client.list_connectors()
    except ISCAPIError as exc:
        logger.error("Failed to list connectors: %s", exc.detail())
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list connectors: %s", exc)
        return 1

    if not connectors:
        print("No connectors found.")
        return 0

    if args.filter:
        term = args.filter.lower()
        connectors = [
            c for c in connectors
            if term in (c.get("name") or "").lower()
            or term in (c.get("scriptName") or "").lower()
            or term in (c.get("type") or "").lower()
        ]

    if args.output == "json":
        _print_json(connectors)
        return 0

    print(f"\nFound {len(connectors)} connector(s):\n")
    name_w, script_w, type_w = 40, 35, 15
    print(
        f"  {'Name':<{name_w}}  {'connector value (scriptName)':<{script_w}}  {'Type':<{type_w}}"
    )
    print(f"  {'-'*name_w}  {'-'*script_w}  {'-'*type_w}")
    for c in sorted(connectors, key=lambda x: (x.get("name") or "").lower()):
        print(
            f"  {(c.get('name') or ''):<{name_w}}  "
            f"{(c.get('scriptName') or ''):<{script_w}}  "
            f"{(c.get('type') or ''):<{type_w}}"
        )
    print(
        "\nUse the 'connector value' column as the 'connector' field "
        "in your sources JSON file.\n"
    )
    return 0


def cmd_find_owner(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'find-owner' sub-command."""
    try:
        identities = client.search_identities(query=args.query, limit=args.limit)
    except ISCAPIError as exc:
        logger.error("Identity search failed: %s", exc.detail())
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Identity search failed: %s", exc)
        return 1

    if not identities:
        print(f"\nNo identities found matching '{args.query}'.")
        print("Try a shorter term, a different name, or '*' to list all identities.\n")
        return 0

    if args.output == "json":
        _print_json(identities)
        return 0

    print(f"\nFound {len(identities)} identity/identities matching '{args.query}':\n")
    id_w, name_w, alias_w = 36, 40, 30
    print(f"  {'ID':<{id_w}}  {'Name':<{name_w}}  {'Alias':<{alias_w}}  Email")
    print(f"  {'-'*id_w}  {'-'*name_w}  {'-'*alias_w}  {'-'*30}")
    for ident in identities:
        print(
            f"  {ident.get('id', ''):<{id_w}}  "
            f"{ident.get('displayName', ident.get('name', '')):<{name_w}}  "
            f"{ident.get('alias', ''):<{alias_w}}  "
            f"{ident.get('email', ident.get('emailAddress', ''))}"
        )
    print(
        "\nCopy the ID of the identity you want to use as source owner "
        "into your sources JSON file under owner.id\n"
    )
    return 0


def cmd_delete(args: argparse.Namespace, client: ISCClient) -> int:
    """Handle the 'delete' sub-command."""
    source_id = args.id

    # Fetch the source name for a friendlier confirmation prompt
    source_name: Optional[str] = None
    if not args.yes:
        try:
            src = client.get_source(source_id)
            source_name = src.get("name", source_id)
        except ISCAPIError as exc:
            if exc.status_code == 404:
                logger.error("Source '%s' not found.", source_id)
                return 1
            logger.warning("Could not fetch source details: %s", exc.detail())
            source_name = source_id

        confirm = input(
            f"\nAre you sure you want to delete source '{source_name}' ({source_id})?\n"
            "This will remove ALL accounts on the source. Type 'yes' to confirm: "
        ).strip()
        if confirm.lower() != "yes":
            print("Aborted.")
            return 0

    try:
        result = client.delete_source(source_id)
        logger.info(
            "Delete task submitted for source '%s'. Task id: %s",
            source_name or source_id,
            result.get("id", "<unknown>"),
        )
        if args.output == "json":
            _print_json(result)
        else:
            print(
                f"\nDelete task accepted for source '{source_name or source_id}'.\n"
                f"Task id: {result.get('id', 'n/a')}\n"
                "The source and its accounts will be removed asynchronously.\n"
            )
    except ISCAPIError as exc:
        logger.error("Failed to delete source '%s': %s", source_id, exc.detail())
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete source '%s': %s", source_id, exc)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_output_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "-o",
        choices=["table", "json"],
        default="table",
        help="Output format: 'table' (default) or 'json'.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sailpoint-source-creator",
        description=(
            "Create and manage sources in SailPoint Identity Security Cloud "
            "using the v2026 API."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------ provision
    prov_p = subparsers.add_parser(
        "provision",
        help="Create N Delimited File sources and load account + entitlement data.",
        description=(
            "Creates a set of Delimited File sources, samples 10 random tenant "
            "identities per source as account data, generates entitlements "
            "(read, write, update, audit_view), randomly assigns them to accounts, "
            "then triggers account and entitlement aggregations."
        ),
    )
    prov_p.add_argument(
        "--count",
        "-n",
        type=int,
        required=True,
        metavar="N",
        help="Number of sources to create.",
    )
    prov_p.add_argument(
        "--name",
        required=True,
        metavar="BASE_NAME",
        help=(
            "Base name for the sources. Sources will be named "
            "'<BASE_NAME> 1', '<BASE_NAME> 2', etc."
        ),
    )
    prov_p.add_argument(
        "--owner-id",
        required=True,
        metavar="IDENTITY_ID",
        help="Identity ID of the source owner (use 'find-owner' to look this up).",
    )
    prov_p.add_argument(
        "--owner-name",
        default=None,
        metavar="NAME",
        help="Display name of the owner (looked up automatically if omitted).",
    )
    prov_p.add_argument(
        "--users-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to a plain-text file of usernames/aliases, one per line. "
            "When provided, these users are added as accounts on every source "
            "(instead of randomly sampling from the tenant), each receiving "
            "all 4 entitlements assigned randomly. "
            "Blank lines and lines starting with # are ignored."
        ),
    )
    prov_p.add_argument(
        "--exclude-alias",
        default=None,
        metavar="ALIAS",
        help=(
            "Alias of the admin account to exclude from account data "
            "(e.g. the alias of the PAT owner). Defaults to none."
        ),
    )
    prov_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Delete any existing source with the same name before creating. "
            "Use this to re-run provision with the same --name."
        ),
    )
    prov_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate CSVs and log what would happen without making any API calls.",
    )
    _add_output_arg(prov_p)

    # ------------------------------------------------------------------ create
    create_p = subparsers.add_parser(
        "create",
        help="Create sources from a JSON definition file.",
        description=(
            "Reads an array of source definitions from a JSON file and creates "
            "each one via the ISC v2026 API. All definitions are validated before "
            "any API calls are made."
        ),
    )
    create_p.add_argument(
        "--file",
        "-f",
        required=True,
        metavar="PATH",
        help="Path to a JSON file containing an array of source definitions.",
    )
    create_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate definitions without making any API calls.",
    )
    _add_output_arg(create_p)

    # ------------------------------------------------------------------ list
    list_p = subparsers.add_parser(
        "list",
        help="List existing sources in ISC.",
    )
    list_p.add_argument(
        "--filter",
        metavar="EXPR",
        default=None,
        help=(
            "ISC filter expression, e.g. 'name co \"HR\"' or "
            "'connectorName eq \"Active Directory\"'."
        ),
    )
    list_p.add_argument(
        "--sorters",
        metavar="FIELD",
        default="name",
        help="Sort field (default: name). Prefix with '-' for descending.",
    )
    list_p.add_argument(
        "--limit",
        type=int,
        default=250,
        metavar="N",
        help="Maximum number of sources to return per page (default: 250).",
    )
    list_p.add_argument(
        "--offset",
        type=int,
        default=0,
        metavar="N",
        help="Pagination offset (default: 0).",
    )
    list_p.add_argument(
        "--all",
        action="store_true",
        help="Fetch all pages automatically (ignores --limit and --offset).",
    )
    _add_output_arg(list_p)

    # ------------------------------------------------------------------ get
    get_p = subparsers.add_parser(
        "get",
        help="Get a single source by ID.",
    )
    get_p.add_argument("id", metavar="SOURCE_ID", help="The source ID.")
    _add_output_arg(get_p)

    # ------------------------------------------------------------------ list-connectors
    conn_p = subparsers.add_parser(
        "list-connectors",
        help="List all connectors available on this tenant.",
        description=(
            "Lists every connector available on the tenant. "
            "Use the 'connector value' column as the 'connector' field "
            "in your sources JSON file."
        ),
    )
    conn_p.add_argument(
        "--filter",
        metavar="TERM",
        default=None,
        help="Case-insensitive substring filter on name or connector value.",
    )
    _add_output_arg(conn_p)

    # ------------------------------------------------------------------ find-owner
    find_owner_p = subparsers.add_parser(
        "find-owner",
        help="Search for an identity to use as a source owner.",
        description=(
            "Searches ISC identities by name, alias, or email and prints their IDs. "
            "Use this to find a valid owner.id before creating sources."
        ),
    )
    find_owner_p.add_argument(
        "query",
        metavar="QUERY",
        help="Name, alias, or email fragment to search for (e.g. 'jane' or 'admin').",
    )
    find_owner_p.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of results to return (default: 10).",
    )
    _add_output_arg(find_owner_p)

    # ------------------------------------------------------------------ delete
    delete_p = subparsers.add_parser(
        "delete",
        help="Delete a source by ID (removes all accounts first).",
    )
    delete_p.add_argument("id", metavar="SOURCE_ID", help="The source ID to delete.")
    delete_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    _add_output_arg(delete_p)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    tenant, client_id, client_secret, domain = load_config()

    logger.info("Connecting to ISC tenant: %s (domain: %s)", tenant, domain)
    client = ISCClient(
        tenant=tenant,
        client_id=client_id,
        client_secret=client_secret,
        domain=domain,
    )

    # Verify credentials before doing any real work
    try:
        client.ensure_token()
        logger.info("Authentication successful.")
    except ISCAuthError as exc:
        logger.error("Authentication failed: %s", exc)
        sys.exit(1)

    dispatch = {
        "provision": cmd_provision,
        "create": cmd_create,
        "list": cmd_list,
        "get": cmd_get,
        "list-connectors": cmd_list_connectors,
        "find-owner": cmd_find_owner,
        "delete": cmd_delete,
    }

    exit_code = dispatch[args.command](args, client)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
