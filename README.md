# tap-microsoft-dynamics-bc

Singer tap for [Microsoft Dynamics 365 Business Central](https://dynamics.microsoft.com/business-central/)
(online and on-premises), built with the [Meltano Singer SDK](https://sdk.meltano.com).

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
  are posted or cancelled (orders, quotes, journals, transfer orders...), employees, and
  all streams without a modification timestamp are extracted in full. They emit
  `ACTIVATE_VERSION` once per run, so targets that support it (for example the MeltanoLabs
  `target-postgres` and `target-snowflake`) remove rows deleted in Business Central.
  Targets that ignore `ACTIVATE_VERSION` keep deleted rows.
- **Lines come with their documents.** Business Central only returns sales/purchase
  document lines for a given document, so they are fetched with `$expand` on the header
  request instead of one request per document. Lines of incremental documents (invoices,
  credit memos, shipments, receipts) never emit `ACTIVATE_VERSION`, because a run only
  sees the lines of documents that changed.
- **Draft invoices and credit memos are re-read every run.** Business Central only updates
  a document's `lastModifiedDateTime` for line changes that affect its totals, so an edit
  such as a new description on an item line would otherwise be missed. Lines can only be
  edited on drafts, and drafts are few, so the tap reads them again with
  `$filter=status eq 'Draft'` on every incremental run.
- **Gentle on production.** Requests carry `Data-Access-Intent: ReadOnly` (served from a
  read-only replica where available), honour `Retry-After` when throttled, and page with
  `Prefer: odata.maxpagesize`.
- **Resilient.** An endpoint the app has no permission for, or whose extension is not
  installed, or that is not set up in a company (HTTP 400 `Internal_DataNotFoundFilter`,
  e.g. `journals` with no journal template), is skipped with a warning instead of
  failing the whole run. Other 400s still fail.

## Setup

Setup has two parts: register an application in **Microsoft Entra ID**, then grant that
application access **in Business Central**. Both are done by the customer's administrators.
This is Microsoft's standard
[service-to-service authentication](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/automation-apis-using-s2s-authentication)
flow.

### Prerequisites

- Business Central **online**, version 18.3 or later, or Business Central
  **on-premises**, version 18.11, 19.5 or later, configured for
  [Microsoft Entra authentication with OpenID Connect](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/authenticating-users-with-azure-ad-openid-connect)
  with `https://api.businesscentral.dynamics.com` in the server's `ValidAudiences`.
  Set `base_url` to the server instance URL. Steps 1 and 2 below are the same as
  online.
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

Do these steps in this order: Business Central only accepts permission sets while the
application is **Disabled** and its user exists.

1. In Business Central, search for **Microsoft Entra Applications** and select **New**.
2. Enter the **Client ID** from step 1 and a **Description** (for example `Meltano data
   extraction`). Leave **State** as **Disabled**.
3. Select *Click here to create the application user* in the notification at the top of
   the page (*"You must initialize the user before you can add permission sets"*).
4. Under **User Permission Sets**, add **`D365 READ`** (*Dyn. 365 Read access all*). It
   gives read access to all business data and covers every stream: the standard API, the
   Finance reports (`reports_finance_*`, including customer and vendor ledger entries)
   and the analytics API. Leave **Company** blank to grant it in all companies, or add one
   line per company to restrict access.
5. Set **State** to **Enabled**.

You don't need the card's **Grant Consent** action when admin consent was granted in
step 1. The **Client ID** of a card can't be changed: if it's wrong, delete the card and
create a new one. To change permission sets later, set **State** back to **Disabled**
first. Applications cannot be given the `SUPER` permission set.

If Business Central answers **401** `Authentication_InvalidCredentials` although a token
is issued, there is no **Enabled** card for that client ID in that environment.

### 3. Optional: more data

- **Analytics API.** The `analytics_*` streams need Microsoft's *Power BI Reports* app
  installed (check **Extension Management** in Business Central). The tap marks them as
  not selected by default, but Meltano selects every stream unless the project has
  `select` rules. To leave them out, use `select: ['*.*', '!analytics_*.*']`.
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
| `base_url` | | | On-premises only: server instance URL, e.g. `https://bc.example.com:7048/BC`. |
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
  `ACTIVATE_VERSION`. Business Central keeps no record of deletions for incremental
  streams. Posted entries and documents cannot be deleted, so this only matters for
  master data. Choose per stream:
  - **Extract it in full.** Master data is small, so re-reading it every run is cheap.
    With Meltano:

    ```yaml
    plugins:
      extractors:
      - name: tap-microsoft-dynamics-bc
        metadata:
          customers:
            replication-method: FULL_TABLE
          vendors:
            replication-method: FULL_TABLE
          items:
            replication-method: FULL_TABLE
    ```

  - **Run a periodic full refresh** (see above).
  - **Keep an audit trail.** In Business Central, enable the **Change Log** and log
    deletions for the tables that matter. Then publish page 595 *Change Log Entries*
    on **Web Services** and extract it with
    `web_services: [{name: <ServiceName>, replication_key: Entry_No}]`. Each deletion
    arrives as an entry with the table number and the record's primary key.

  The target must honour `ACTIVATE_VERSION`. Targets built on the Meltano Singer SDK
  (for example MeltanoLabs `target-postgres`, `target-snowflake` and `target-mssql`)
  need **`add_record_metadata: true`**: without it records carry no table version, and
  nothing is ever deleted. By default they then *soft*-delete, setting
  `_sdc_deleted_at` on rows that are gone, so filter on `_sdc_deleted_at IS NULL`
  downstream, or set `hard_delete: true` to remove the rows. Some loaders ignore
  `ACTIVATE_VERSION` altogether (for example the pipelinewise `target-postgres`).

## Known limitations

- **Deletions in incremental streams** reach the target only with one of the options
  under *Deleted records* above.
- **Lines deleted from a draft** invoice or credit memo stay in the target until a full
  refresh. Lines that are added or changed are picked up by the re-read of drafts.
- **SQL Server targets:** MeltanoLabs `target-mssql` creates `varchar` columns, so text
  outside the database collation's code page (for example Polish, Greek or Japanese
  names under the default `SQL_Latin1_General_CP1_CI_AS`) is silently stored as `?`.
  Load into a database with a UTF-8 collation such as
  `Latin1_General_100_CI_AS_SC_UTF8` (SQL Server 2019 or later, Azure SQL).
- **Multi-tenant on-premises servers** (which need `?tenant=` on every request) are not
  supported yet. Neither is on-premises authentication without Microsoft Entra
  (Windows or `NavUserPassword`).

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
