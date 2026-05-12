# SailPoint ISC Source Creator

A Python CLI tool for creating and managing sources in **SailPoint Identity Security Cloud (ISC)** using the [v2026 API](https://developer.sailpoint.com/docs/api/v2026/sources).

## Features

| Feature | Detail |
|---|---|
| **Provision** | Create N Delimited File sources, auto-populate with real tenant identities, and aggregate account + entitlement data in one command |
| **Authoritative sources** | Provision sources as authoritative and automatically create a linked identity profile with attribute mappings |
| **Owner by alias** | Supply an identity alias instead of an ID — the tool resolves it automatically |
| **Create** | Bulk-create sources from a JSON definition file |
| **List** | List sources with filtering, sorting, and auto-pagination |
| **Get** | Fetch a single source by ID |
| **Delete** | Delete a source (with confirmation prompt) |
| **Find owner** | Search tenant identities to find a valid owner ID or alias |
| **List connectors** | List all connectors available on the tenant |
| **Dry-run** | Validate or preview any operation without touching the API |
| **JSON output** | `--output json` on every command for scripting |
| **Auth** | OAuth 2.0 client credentials — PAT or API client |
| **Token caching** | Access token is reused and refreshed automatically |
| **Multi-domain** | Supports `identitynow.com` and `identitynow-demo.com` tenants |

## Project layout

```
sailpoint-source-creator/
├── main.py              # CLI entry point
├── isc_client.py        # ISC API client (auth + HTTP)
├── source_creator.py    # Validation and bulk-creation logic
├── provisioner.py       # End-to-end demo data provisioning
├── requirements.txt     # Runtime dependencies
├── requirements-dev.txt # Dev/test dependencies
├── .env.example         # Environment variable template
├── examples/
│   ├── sources.json           # Template for the create command
│   └── employees.csv          # Sample authoritative CSV
└── tests/
    ├── test_isc_client.py     # Unit tests for the API client
    ├── test_source_creator.py # Unit tests for validation & creation
    └── test_provisioner.py    # Unit tests for the provisioner
```

## Prerequisites

- Python 3.10+
- A SailPoint ISC tenant (production or demo)
- A **Personal Access Token (PAT)** or OAuth API client with `ORG_ADMIN` or `SOURCE_ADMIN` authority

> Creating authoritative sources and identity profiles requires `ORG_ADMIN` authority.

## Setup

```bash
cd ISC-Source-Creator

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# Install runtime dependencies
pip install -r requirements.txt
```

> The `.venv` directory is machine-specific and should not be copied between machines. On each new machine, recreate it with the two commands above. If you see `ModuleNotFoundError`, the most likely cause is that the venv isn't activated — run `source .venv/bin/activate` first.

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your tenant details:

```dotenv
# Your tenant subdomain — the part before .identitynow.com (or .identitynow-demo.com)
ISC_TENANT=acme

# OAuth 2.0 credentials from a Personal Access Token or API client
ISC_CLIENT_ID=your-client-id
ISC_CLIENT_SECRET=your-client-secret

# Base domain — defaults to identitynow.com if not set.
# Uncomment and change this for demo tenants.
# ISC_DOMAIN=identitynow-demo.com
```

### Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `ISC_TENANT` | Yes | — | Tenant subdomain (e.g. `acme`) |
| `ISC_CLIENT_ID` | Yes | — | OAuth client ID |
| `ISC_CLIENT_SECRET` | Yes | — | OAuth client secret |
| `ISC_DOMAIN` | No | `identitynow.com` | Base domain for the tenant |

### Tenant domain

The `ISC_DOMAIN` variable controls which base domain is used to build all API URLs:

| Tenant type | `ISC_DOMAIN` | Resulting API base |
|---|---|---|
| Production | `identitynow.com` (default) | `https://{tenant}.api.identitynow.com` |
| Demo | `identitynow-demo.com` | `https://{tenant}.api.identitynow-demo.com` |

### Authentication

The tool uses the **OAuth 2.0 client credentials grant flow**, which is the recommended approach for scripts and automation. No browser or user interaction is required.

The flow works like this:

1. On the first API call, the tool posts your `client_id` and `client_secret` to `https://{tenant}.api.{domain}/oauth/token`.
2. ISC returns a short-lived JWT access token.
3. Every subsequent API request includes that token as `Authorization: Bearer <token>`.
4. The token is cached in memory and automatically refreshed 30 seconds before it expires.

Credentials are supplied via a **Personal Access Token (PAT)**. To generate one:

1. Log in to your ISC tenant.
2. Go to **Preferences → Personal Access Tokens**.
3. Click **New Token**, give it a name, and click **Create Token**.
4. Copy both the **Client ID** and **Client Secret** — the secret is only shown once.

> The authority of the PAT matches the authority of the user who created it. You need at least `SOURCE_ADMIN` to create sources, `ORG_ADMIN` to delete them, and `ORG_ADMIN` to create identity profiles.

---

## Usage

### Provision command (end-to-end demo setup)

The `provision` command is the fastest way to populate a tenant with realistic demo data. It creates N Delimited File sources and automatically loads account and entitlement data into each one.

There are three account population modes:

| Mode | Flag | Behaviour |
|---|---|---|
| **Random** (default) | _(no flag)_ | Fetches up to 250 tenant identities, randomly samples 10 per source, assigns 1–3 entitlements to each |
| **File** | `--users-file PATH` | Reads aliases from a plain-text file — those users appear on **every** source, each receiving all 4 entitlements in a random order |
| **Authoritative** | `--authoritative --users-file PATH` | Sources are marked authoritative and an identity profile is created automatically. The file must be a CSV with identity fields (see below). Limited to a single source (`--count 1`). |

#### Entitlement catalogue

All modes use the same four entitlements:

| ID | Name | Description |
|---|---|---|
| `read` | Read | Read access |
| `write` | Write | Write access |
| `update` | Update | Update access |
| `audit_view` | Audit View | Audit view access |

#### What it does per source (all modes)

1. Creates the Delimited File source
2. Reads the source's account and entitlement schemas from ISC to discover the exact column names required
3. Builds account and entitlement CSVs that match those schemas exactly
4. Uploads and triggers the **account aggregation**
5. Uploads and triggers the **entitlement aggregation**

In authoritative mode, an additional step runs between source creation and aggregation:

- Creates an **identity profile** linked to the source, with attribute transforms mapping source account columns to ISC identity attributes (see [Identity profile attribute mappings](#identity-profile-attribute-mappings))
- Profile priority is resolved automatically — the tool queries existing profiles and assigns the next available value

---

#### Step 1 — Identify the source owner

Every source requires an owner that references a real identity in your tenant. You can supply the owner in two ways:

**Option A — by alias (recommended):** pass `--owner-alias` with the identity's username. The tool resolves the ID automatically using an exact alias lookup, with a Search API fallback.

```bash
python main.py provision \
  --count 5 \
  --name "Demo Source" \
  --owner-alias jsmith
```

**Option B — by ID:** pass `--owner-id` with the full identity ID. Use `find-owner` to look it up first if needed.

```bash
# Search by name or alias fragment
python main.py find-owner "jane"
python main.py find-owner "*"   # list first 10 identities

python main.py provision \
  --count 5 \
  --name "Demo Source" \
  --owner-id 2c9180a46f3b1234567890abcdef1234
```

Exactly one of `--owner-alias` or `--owner-id` is required. Supplying both is an error.

---

#### Step 2 — Run provision

**Random mode** — 10 different randomly sampled identities per source:

```bash
python main.py provision \
  --count 5 \
  --name "Demo Source" \
  --owner-alias spadmin \
  --exclude-alias spadmin
```

**File mode** — specific users on every source, all 4 entitlements each:

First create a `users.txt` file with one alias per line:

```
jsmith
adoe
bjones
mwilliams
# blank lines and comments are ignored
```

Then run:

```bash
python main.py provision \
  --count 5 \
  --name "Demo Source" \
  --owner-alias jsmith \
  --users-file users.txt
```

This creates **Demo Source 1** through **Demo Source 5**. All five sources contain the same users, each with all 4 entitlements assigned in a random order per user.

**Authoritative mode** — creates a single authoritative source with a linked identity profile:

First create an `employees.csv` file with the required columns:

```csv
firstName,lastName,fullName,email
Jane,Doe,Jane Doe,jane.doe@acme.com
John,Smith,John Smith,john.smith@acme.com
Alice,Jones,Alice Jones,alice.jones@acme.com
```

Then run:

```bash
python main.py provision \
  --count 1 \
  --name "HR Source" \
  --owner-alias jsmith \
  --users-file employees.csv \
  --authoritative
```

---

#### Authoritative CSV format

When using `--authoritative`, `--users-file` must point to a CSV file. Plain-text alias files are not accepted in this mode.

| Column | Required | Description |
|---|---|---|
| `firstName` | Yes | User's first name |
| `lastName` | Yes | User's last name |
| `fullName` | Yes | User's display name |
| `email` | Yes | User's email address — also used to derive the account alias |

Column headers are case-insensitive (`FIRSTNAME`, `firstname`, and `firstName` all work). Additional columns are allowed and ignored.

The account alias is derived from the email local-part (the part before `@`). For example, `jane.doe@acme.com` becomes the alias `jane.doe`.

A sample file is provided at `examples/employees.csv`.

#### Identity profile attribute mappings

The identity profile created in authoritative mode maps source account attributes to ISC identity attributes as follows:

| ISC identity attribute | Source account attribute | Populated from CSV column |
|---|---|---|
| `uid` | `name` | `fullName` |
| `firstname` | `givenName` | `firstName` |
| `lastname` | `familyName` | `lastName` |
| `displayName` | `name` | `fullName` |
| `email` | `e-mail` | `email` |

The profile priority is set automatically to one higher than the current maximum across all existing identity profiles on the tenant.

---

#### Provision options

| Flag | Required | Default | Description |
|---|---|---|---|
| `--count N` | Yes | — | Number of sources to create. Must be `1` when `--authoritative` is set. |
| `--name BASE_NAME` | Yes | — | Name prefix — sources are named `<BASE_NAME> 1`, `<BASE_NAME> 2`, etc. |
| `--owner-alias ALIAS` | One of | — | Alias (username) of the source owner — ID is resolved automatically. |
| `--owner-id ID` | One of | — | Identity ID of the source owner. Use `find-owner` to look this up. |
| `--owner-name NAME` | No | auto | Display name of the owner (resolved automatically if omitted). |
| `--users-file PATH` | No* | — | Users file. Plain-text aliases for file mode; CSV with identity fields for authoritative mode. *Required when `--authoritative` is set. |
| `--authoritative` | No | false | Create an authoritative source and generate a linked identity profile. Requires `--users-file` (CSV) and `--count 1`. Requires `ORG_ADMIN`. |
| `--exclude-alias ALIAS` | No | — | Alias to exclude from the random identity pool (random mode only). |
| `--force` | No | false | Delete any existing source with the same name before creating. |
| `--dry-run` | No | false | Preview CSVs and log what would happen without making any API calls. |
| `--output json` | No | table | Output results as JSON instead of a table. |

---

#### Re-running with the same name

If a source with the same name already exists on the tenant, the create step will fail with a conflict error. Use `--force` to delete and recreate it:

```bash
python main.py provision \
  --count 1 \
  --name "HR Source" \
  --owner-alias jsmith \
  --users-file employees.csv \
  --authoritative \
  --force
```

---

#### Dry run

Preview exactly what would be created and what the CSVs would contain, without touching the API:

```bash
python main.py provision \
  --count 1 \
  --name "HR Source" \
  --owner-alias jsmith \
  --users-file employees.csv \
  --authoritative \
  --dry-run
```

---

#### Example output (authoritative mode)

```
Resolved alias 'jsmith' → id=2c9180a46f3b1234567890abcdef1234 name=John Smith
────────────────────────────────────────────────────────────
Provisioning source 1/1: HR Source 1
  ✓ Source created  id=34db381d97944bdc89fa3eed326f6f1  authoritative=True
  ✓ Identity profile created  id=7ac2190f3e8b4d12a09f1cc234de5678
  ✓ Account aggregation started  task=task-acct-001
  ✓ Entitlement aggregation started  task=task-ent-001

======================================================================
  Provision Summary: 1/1 sources fully provisioned
======================================================================

✓ Provisioned successfully:
    HR Source 1                               id=34db381d97944bdc89fa3eed326f6f1  accounts=3  entitlements=4
```

#### Example output (random / file mode)

```
Resolved alias 'jsmith' → id=2c9180a46f3b1234567890abcdef1234 name=John Smith
────────────────────────────────────────────────────────────
Provisioning source 1/5: Demo Source 1
  ✓ Source created  id=34db381d97944bdc89fa3eed326f6f1  authoritative=False
  ✓ Account aggregation started  task=task-acct-001
  ✓ Entitlement aggregation started  task=task-ent-001
...

======================================================================
  Provision Summary: 5/5 sources fully provisioned
======================================================================

✓ Provisioned successfully:
    Demo Source 1                             id=34db381d97944bdc89fa3eed326f6f1  accounts=10  entitlements=4
    Demo Source 2                             id=f4bd61c6ad7b49e68da22c9fde4f47b1  accounts=10  entitlements=4
    Demo Source 3                             id=3e42d84d94dd4303a77ad7211bed01a1  accounts=10  entitlements=4
    Demo Source 4                             id=5b1134312f0c4894ba27c0030aeccffc  accounts=10  entitlements=4
    Demo Source 5                             id=79b35fb5982d46b5a0daaed5259fb0d5  accounts=10  entitlements=4
```

> Aggregation tasks run asynchronously in ISC. The task IDs are logged for reference but you don't need to wait — ISC processes them in the background. You can verify the results by checking the source in the ISC UI under **Connections → Sources**.

---

### Create sources from a JSON file

For more control over source configuration, define sources in a JSON file and use the `create` command.

#### Step 1 — Find a valid owner ID

```bash
python main.py find-owner "admin"
python main.py find-owner "*"   # list first 10 identities
```

#### Step 2 — Find the right connector value

Connector names vary by tenant. Use `list-connectors` to find the exact value for your tenant:

```bash
python main.py list-connectors
python main.py list-connectors --filter delimited
python main.py list-connectors --filter active
```

#### Step 3 — Create sources

```bash
# Create sources defined in a JSON file
python main.py create --file examples/sources.json

# Validate definitions without making any API calls
python main.py create --file examples/sources.json --dry-run

# Output results as JSON
python main.py create --file examples/sources.json --output json
```

---

### Other commands

#### List sources

```bash
# List all sources (table view)
python main.py list

# Filter by name
python main.py list --filter 'name co "HR"'

# Filter by connector type
python main.py list --filter 'connectorName eq "Active Directory"'

# Sort descending by creation date
python main.py list --sorters -created

# Fetch all pages automatically (ignores --limit / --offset)
python main.py list --all

# Output as JSON
python main.py list --output json
```

#### Get a source

```bash
# Human-readable summary
python main.py get 2c9180835d191a86015d28455b4a2329

# Full JSON response
python main.py get 2c9180835d191a86015d28455b4a2329 --output json
```

#### Delete a source

```bash
# Prompts "Type 'yes' to confirm" before proceeding
python main.py delete 2c9180835d191a86015d28455b4a2329

# Skip the confirmation prompt (for scripts)
python main.py delete 2c9180835d191a86015d28455b4a2329 --yes
```

> Deleting a source removes **all accounts** on it first, then deletes the source itself. The operation is asynchronous — the CLI prints the task ID you can use to track progress.

#### Global flags

```bash
# Enable DEBUG logging (shows every HTTP request URL and parameters)
python main.py --verbose provision --count 1 --name "Test" --owner-alias jsmith
```

---

## Source definition file

Sources are defined as a JSON array. Each object maps directly to the ISC
[Source schema](https://developer.sailpoint.com/docs/api/v2026/create-source).

### Required fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Unique source name within the tenant |
| `owner` | object | Identity that owns the source — must include `id`, `name`, and `type: "IDENTITY"` |
| `connector` | string | Connector identifier — use `list-connectors` to find the right value for your tenant |

### Common optional fields

| Field | Type | Description |
|---|---|---|
| `description` | string | Human-readable description |
| `connectorName` | string | Display name shown in the ISC UI |
| `connectionType` | string | `"direct"` or `"file"` |
| `authoritative` | boolean | Whether this is an authoritative source for identity data |
| `deleteThreshold` | integer | 0–100 — max % of accounts that can be deleted in one aggregation run |
| `cluster` | object | VA cluster to use for direct connections (`id`, `name`, `type: "CLUSTER"`) |
| `features` | array | Connector features, e.g. `["PROVISIONING", "PASSWORD", "AUTHENTICATE"]` |
| `connectorAttributes` | object | Connector-specific settings (host, port, credentials, etc.) |

### Connector reference

Connector `scriptName` values vary by tenant. Always verify with `list-connectors`. Common values:

| Connector | Typical `connector` value | `connectionType` |
|---|---|---|
| Active Directory | `active-directory-angularsc` | `direct` |
| OpenLDAP | `openldap-angularsc` | `direct` |
| Delimited File (CSV) | `delimited-file-angularsc` | `file` |
| JDBC | `jdbc-angularsc` | `direct` |
| ServiceNow | `servicenow-saas` | `direct` |
| Workday | `workday` | `direct` |
| GitHub | `github-saas` | `direct` |

> **Note:** Delimited File sources are automatically created with `provisionAsCsv=true` as required by the ISC API. You do not need to set this yourself.

### Minimal example

```json
[
  {
    "name": "My HR System",
    "owner": {
      "id": "2c9180a46f3b1234567890abcdef1234",
      "name": "Jane Admin",
      "type": "IDENTITY"
    },
    "connector": "delimited-file-angularsc"
  }
]
```

See `examples/sources.json` for a template covering three common connector types. Replace the `REPLACE_WITH_*` placeholders with real values before running `create`.

---

## Running tests

```bash
pip install -r requirements-dev.txt

# Run all tests
python -m pytest tests/ -v

# Run with coverage report
python -m pytest tests/ -v --cov=. --cov-report=term-missing
```

---

## API reference

- [v2026 Sources API](https://developer.sailpoint.com/docs/api/v2026/sources)
- [v2024 Identity Profiles API](https://developer.sailpoint.com/docs/api/v2024/identity-profiles)
- [Authentication](https://developer.sailpoint.com/docs/api/authentication)
- [ISC API Standard Collection Parameters](https://developer.sailpoint.com/idn/api/standard-collection-parameters)
