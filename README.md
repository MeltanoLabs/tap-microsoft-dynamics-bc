# tap-microsoft-dynamics-bc

Singer tap for [Microsoft Dynamics 365 Business Central](https://dynamics.microsoft.com/business-central/) online,
built with the [Meltano Singer SDK](https://sdk.meltano.com).

Business Central is Microsoft's ERP for small and mid-sized organisations: finance, sales,
purchasing, inventory, projects and manufacturing. This tap extracts that data through
Business Central's REST APIs using service-to-service (client ID + secret) authentication,
so no user has to sign in and nothing breaks when an employee leaves.

What it extracts:

- **Every standard API v2.0 entity**: customers, vendors, items, G/L accounts and entries,
  sales and purchase documents with their lines, journals, dimensions, fixed assets,
  projects, employees and the financial statements.
- **Ledger entries the standard API does not expose**: customer, vendor and detailed
  ledger entries, G/L budgets and dimension set entries, from Microsoft's
  *API Reports – Finance* app.
- **Item, value, warehouse, job and manufacturing ledgers** from Microsoft's analytics API,
  installed with the Power BI Reports apps. These streams are opt-in.
- **Your own data**: fields added by extensions are picked up automatically, and custom
  API pages or published OData web services can be added in configuration.

See [STREAMS.md](STREAMS.md) for every stream with its replication method and description.

## How it works

- **Schemas come from your environment.** At discovery the tap reads the OData
  `$metadata` of each API, so the catalog matches your Business Central version,
  localisation and installed extensions exactly.
- **Multi-company.** Every company in the environment is extracted (or the ones you
  list). Each record carries `company_id`, which is part of every primary key.
- **Incremental where Business Central allows it.** Streams with a `lastModifiedDateTime`
  field are filtered on it, with a bookmark per company.
- **Deletes are propagated.** Working documents that Business Central deletes when they
  are posted or cancelled (orders, quotes, journals, transfer orders...) and all streams
  without a modification timestamp are extracted in full. They emit `ACTIVATE_VERSION`,
  so targets that support it remove rows deleted in Business Central.
- **Lines come with their documents.** Business Central only returns sales/purchase
  document lines for a given document, so they are fetched with `$expand` on the header
  request instead of one request per document.
- **Gentle on production.** Requests carry `Data-Access-Intent: ReadOnly` (served from a
  read-only replica where available), honour `Retry-After` when throttled, and page with
  `Prefer: odata.maxpagesize`.
- **Resilient.** An endpoint the app has no permission for, or whose extension is not
  installed, is skipped with a warning instead of failing the whole run.

## Setup

Setup has two parts: register an application in **Microsoft Entra ID**, then grant that
application access **in Business Central**. Both are done by the customer's administrators.
This is Microsoft's standard
[service-to-service authentication](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/automation-apis-using-s2s-authentication)
flow.

### Prerequisites

- Business Central **online**, version 18.3 or later.
- A Microsoft Entra administrator who can register applications and grant admin consent
  (Application Administrator, Cloud Application Administrator or Global Administrator).
- A Business Central user with permission to manage Microsoft Entra applications
  (for example `SUPER` or `SECURITY`).

### 1. Register the application in Microsoft Entra ID

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com) and open
   **App registrations** › **New registration**.
2. Name it (for example `Meltano – Business Central`), choose
   **Accounts in this organizational directory only**, leave **Redirect URI** empty and
   select **Register**.
3. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the
   overview page.
4. Open **Certificates & secrets** › **New client secret**, choose an expiry, and copy the
   secret **Value** immediately (it is shown only once). Note the expiry date: the
   connection stops working when the secret expires.
5. Open **API permissions** › **Add a permission** › **Dynamics 365 Business Central** ›
   **Application permissions**, tick **API.ReadWrite.All**, and select
   **Add permissions**.
6. Select **Grant admin consent for _your organisation_**.

> **Why "ReadWrite"?** Microsoft offers no read-only application permission for Business
> Central. What the application can actually do is decided by the Business Central
> permission sets in the next step, so grant read-only sets there. The tap itself only
> ever sends `GET` requests.

### 2. Grant the application access in Business Central

1. In Business Central, search for **Microsoft Entra Applications** and select **New**.
2. Enter the **Client ID** from step 1, a **Description** (for example `Meltano data
   extraction`), and set **State** to **Enabled**.
3. Under **User Permission Sets**, add:
   - **`D365 READ`** (*Dyn. 365 Read access all*): read access to all business data.
     This covers the standard API and the analytics API.
   - For the **Finance reports** streams (`reports_finance_*`, which include customer and
     vendor ledger entries): Microsoft grants access to these endpoints through the
     **`D365 BASIC ISV`** permission set. Add it if those streams are needed and are
     skipped with an HTTP 403 warning.

   Leave **Company** blank to grant the permission sets in all companies, or add one line
   per company to restrict access.

Applications cannot be given the `SUPER` permission set.

### 3. Optional: more data

- **Analytics API.** The `analytics_*` streams need Microsoft's *Power BI Reports* app
  installed (check **Extension Management** in Business Central). They are not selected by
  default.
- **Extension APIs.** If an extension publishes its own API, for example
  `api/contoso/banking/v1.0`, add it to `custom_apis`. Every entity becomes a stream.
- **Pages and queries that have no API.** In Business Central, open **Web Services**,
  publish the page or query (for example page 25 *Customer Ledger Entries*) and add its
  **Service Name** to `web_services`. If the page shows `SystemModifiedAt`, set it as the
  `replication_key` to extract incrementally.

## Settings

| Setting | Required | Default | Description |
|:--|:--:|:--|:--|
| `tenant_id` | ✅ | | Microsoft Entra tenant ID (GUID) or primary domain. |
| `client_id` | ✅ | | Application (client) ID of the Entra app registration. |
| `client_secret` | ✅ | | Client secret value of the Entra app registration. |
| `environment_name` | | `production` | Business Central environment name, as shown in the admin center. |
| `company_ids` | | all | Companies to extract, by ID or name. |
| `start_date` | | all history | Earliest modification time for incremental streams, ISO-8601 date-time, e.g. `2026-01-01T00:00:00Z`. |
| `include_reports_finance_api` | | `true` | Discover the Finance reports API streams. |
| `include_analytics_api` | | `true` | Discover the analytics API streams (not selected by default). |
| `custom_apis` | | | Extra API routes: list of `{publisher, group, version, stream_prefix}`. |
| `web_services` | | | OData web services: list of `{name, replication_key}`. |
| `emit_activate_version_messages` | | `true` | Emit `ACTIVATE_VERSION` for full-table streams so deletes propagate. |
| `page_size` | | `1000` | Records per response. |
| `api_url` / `auth_url` | | public cloud | Change only for sovereign clouds. |

Run `tap-microsoft-dynamics-bc --about` for the full list, including the SDK's stream maps
and flattening settings.

### Example

```yaml
plugins:
  extractors:
  - name: tap-microsoft-dynamics-bc
    config:
      tenant_id: 00000000-0000-0000-0000-000000000000
      environment_name: production
      start_date: "2024-01-01T00:00:00Z"
      web_services:
      - name: BankAccountLedgerEntries
        replication_key: SystemModifiedAt
    select:
    - "*.*"
```

Set `client_id` and `client_secret` through your secrets store or environment variables
(`TAP_MICROSOFT_DYNAMICS_BC_CLIENT_ID`, `TAP_MICROSOFT_DYNAMICS_BC_CLIENT_SECRET`).

## Data recovery and backfill

- **Re-extract everything:** clear the pipeline state (in Meltano:
  `meltano state clear <state-id>`, or run with `--full-refresh`). Incremental streams then
  start from `start_date` again; full-table streams are always complete.
- **Backfill a period:** set `start_date` to the start of the period and run once with a
  full refresh. Records are upserted by primary key, so overlapping runs do not create
  duplicates.
- **One stream only:** with Meltano, `meltano el tap-microsoft-dynamics-bc <target>
  --select <stream> --full-refresh`.
- **Deleted records:** full-table streams remove them downstream through
  `ACTIVATE_VERSION`. For incremental streams (where Business Central keeps no record of
  deletions), a periodic full refresh removes them.

## Known limitations

- **Deletions in incremental streams** are not visible until a full refresh. Posted
  ledger entries and posted documents cannot be deleted in Business Central, so in
  practice this affects master data (customers, items...) that is deleted rather than
  blocked.
- **Document lines** follow their header: if a line changes without the header's
  `lastModifiedDateTime` changing, it is picked up at the next change to that document or
  the next full refresh.
- **On-premises** Business Central is not supported yet. It needs Microsoft Entra
  authentication configured on the server and the `api_url` pointed at the server.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run tap-microsoft-dynamics-bc --about
```

Tests run against a mocked Business Central environment (`tests/conftest.py`) and need no
credentials.

`tap_microsoft_dynamics_bc/endpoints.json` (stream descriptions, field descriptions, which
lines belong to which documents) is generated from Microsoft's open-source AL code in
[microsoft/BCApps](https://github.com/microsoft/BCApps). To refresh it for a new Business
Central release:

```bash
uv run python scripts/generate_endpoints.py --commit <BCApps commit SHA>
```

This also regenerates [STREAMS.md](STREAMS.md).

## License

Apache 2.0
