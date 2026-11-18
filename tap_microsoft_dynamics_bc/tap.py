"""Microsoft Dynamics 365 Business Central tap class."""

from __future__ import annotations

import json
import re
import sys
from functools import cached_property
from importlib import resources
from typing import TYPE_CHECKING, Any, TypeVar, cast

import requests
from singer_sdk import Tap
from singer_sdk import typing as th

from tap_microsoft_dynamics_bc.auth import (
    DEFAULT_API_URL,
    DEFAULT_AUTH_URL,
    BusinessCentralAuthenticator,
)
from tap_microsoft_dynamics_bc.client import BusinessCentralStream
from tap_microsoft_dynamics_bc.edmx import entity_schema, parse_metadata
from tap_microsoft_dynamics_bc.streams import (
    COMPANY_ID,
    CompaniesStream,
    CompanyEntityStream,
    ExpandedLineStream,
    WebServiceStream,
)

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from tap_microsoft_dynamics_bc.edmx import Metadata

STANDARD_ROUTE = "v2.0"
METADATA_TIMEOUT_SECONDS = 300
# Columns that identify a ledger/query row when the endpoint declares no key.
QUERY_KEY_CANDIDATES = ("id", "systemId", "entryNumber", "entryNo")
REPLICATION_KEY_CANDIDATES = ("lastModifiedDateTime", "systemModifiedAt", "SystemModifiedAt")
# Incremental documents whose lines can change without the header's
# lastModifiedDateTime changing: Business Central only updates the header for
# line changes that affect its totals. Lines can only be edited on drafts, so
# drafts are re-read every run. Posted documents cannot change.
OPEN_DOCUMENT_FILTERS = {
    "salesInvoices": "status eq 'Draft'",
    "salesCreditMemos": "status eq 'Draft'",
    "purchaseInvoices": "status eq 'Draft'",
    "purchaseCreditMemos": "status eq 'Draft'",
}


_S = TypeVar("_S", bound=BusinessCentralStream)


def bind_parent(cls: type[_S], parent: BusinessCentralStream, name: str) -> type[_S]:
    """Return a per-stream subclass of ``cls`` whose parent is ``parent``'s class.

    The SDK links child streams to parents by class (``parent_stream_type``), so
    every dynamically built stream gets a class of its own.
    """
    subclass = type(
        f"{cls.__name__}__{name}",
        (cls,),
        {"parent_stream_type": type(parent), "__module__": cls.__module__},
    )
    return cast("type[_S]", subclass)


def snake_case(name: str) -> str:
    """Convert an OData name (``salesInvoiceLines``, ``Cust_Ledger``) to snake_case."""
    name = re.sub(r"[^0-9A-Za-z]+", "_", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"_+", "_", name).strip("_").lower()


class TapMicrosoftDynamicsBC(Tap):
    """Singer tap for Microsoft Dynamics 365 Business Central."""

    name = "tap-microsoft-dynamics-bc"
    dynamic_catalog = True

    config_jsonschema = th.PropertiesList(
        th.Property(
            "tenant_id",
            th.StringType(nullable=False),
            required=True,
            title="Tenant ID",
            description=(
                "Microsoft Entra tenant ID (GUID) or primary domain of the organisation "
                "that owns the Business Central environment."
            ),
        ),
        th.Property(
            "client_id",
            th.StringType(nullable=False),
            required=True,
            title="Client ID",
            description="Application (client) ID of the Microsoft Entra app registration.",
        ),
        th.Property(
            "client_secret",
            th.StringType(nullable=False),
            required=True,
            secret=True,
            title="Client Secret",
            description="Client secret value of the Microsoft Entra app registration.",
        ),
        th.Property(
            "environment_name",
            th.StringType(nullable=False),
            default="production",
            title="Environment Name",
            description=(
                "Business Central environment to extract from, as shown in the Business "
                "Central admin center (for example `production` or `sandbox`)."
            ),
        ),
        th.Property(
            "company_ids",
            th.ArrayType(th.StringType(nullable=False)),
            title="Companies",
            description=(
                "Companies to extract, by ID or name. Leave empty to extract every "
                "company in the environment."
            ),
        ),
        th.Property(
            "start_date",
            th.DateTimeType(nullable=True),
            title="Start Date",
            description=(
                "Earliest modification timestamp to extract for incremental streams, as "
                "an ISO-8601 date-time (for example `2026-01-01T00:00:00Z`). Leave empty "
                "to extract all history."
            ),
        ),
        th.Property(
            "include_reports_finance_api",
            th.BooleanType(nullable=False),
            default=True,
            title="Include Finance Reports API",
            description=(
                "Discover streams from Microsoft's `reportsFinance` API, which adds "
                "customer, vendor and detailed ledger entries, G/L budgets and dimension "
                "set entries."
            ),
        ),
        th.Property(
            "include_analytics_api",
            th.BooleanType(nullable=False),
            default=True,
            title="Include Analytics API",
            description=(
                "Discover streams from Microsoft's `analytics` API (installed with the "
                "Power BI reports apps), which adds item, value, warehouse, job and "
                "manufacturing ledgers. These streams are not selected by default."
            ),
        ),
        th.Property(
            "custom_apis",
            th.ArrayType(
                th.ObjectType(
                    th.Property("publisher", th.StringType(nullable=False), required=True),
                    th.Property("group", th.StringType(nullable=False), required=True),
                    th.Property("version", th.StringType(nullable=False), required=True),
                    th.Property("stream_prefix", th.StringType),
                ),
            ),
            title="Custom APIs",
            description=(
                "Additional API routes published by extensions, as "
                "`api/{publisher}/{group}/{version}`. Every entity in the route becomes "
                "a stream named `{stream_prefix}_{entity}` (prefix defaults to the group)."
            ),
        ),
        th.Property(
            "web_services",
            th.ArrayType(
                th.ObjectType(
                    th.Property("name", th.StringType(nullable=False), required=True),
                    th.Property("replication_key", th.StringType),
                ),
            ),
            title="OData Web Services",
            description=(
                "Pages or queries published on the Business Central *Web Services* page "
                "to extract through the `ODataV4` endpoint, for data the APIs do not "
                "expose. `replication_key` (for example `SystemModifiedAt`) enables "
                "incremental extraction."
            ),
        ),
        th.Property(
            "emit_activate_version_messages",
            th.BooleanType(nullable=False),
            default=True,
            title="Propagate Deletes",
            description=(
                "Emit `ACTIVATE_VERSION` messages for full-table streams so that targets "
                "which support them (with `hard_delete`, or soft-delete via "
                "`_sdc_deleted_at`) remove rows deleted in Business Central."
            ),
        ),
        th.Property(
            "page_size",
            th.IntegerType(nullable=False),
            default=1000,
            title="Page Size",
            description="Maximum records per API response (`Prefer: odata.maxpagesize`).",
        ),
        th.Property(
            "base_url",
            th.StringType,
            title="Server URL (on-premises)",
            description=(
                "For Business Central on-premises: the root URL of the server instance, "
                "for example `https://bc.example.com:7048/BC`. Requests go to "
                "`{base_url}/api/v2.0/...` and `environment_name` is not used. The "
                "server must use Microsoft Entra authentication and accept tokens for "
                "`https://api.businesscentral.dynamics.com` (in `ValidAudiences`). "
                "Leave empty for Business Central online."
            ),
        ),
        th.Property(
            "api_url",
            th.StringType(nullable=False),
            default=DEFAULT_API_URL,
            title="API URL",
            description="Business Central API host. Change only for sovereign clouds.",
        ),
        th.Property(
            "auth_url",
            th.StringType(nullable=False),
            default=DEFAULT_AUTH_URL,
            title="Authority URL",
            description="Microsoft Entra authority host. Change only for sovereign clouds.",
        ),
    ).to_dict()

    @cached_property
    def environment_url(self) -> str:
        """Return the environment root URL (the server instance URL on-premises)."""
        if base_url := self.config.get("base_url"):
            return str(base_url).rstrip("/")
        api_url = (self.config.get("api_url") or DEFAULT_API_URL).rstrip("/")
        environment = self.config.get("environment_name") or "production"
        return f"{api_url}/v2.0/{self.config['tenant_id']}/{environment}"

    @cached_property
    def authenticator(self) -> BusinessCentralAuthenticator:
        """Return the authenticator shared by every stream."""
        return BusinessCentralAuthenticator(
            tenant_id=self.config["tenant_id"],
            client_id=self.config["client_id"],
            client_secret=self.config["client_secret"],
            auth_url=self.config.get("auth_url") or DEFAULT_AUTH_URL,
            api_url=self.config.get("api_url") or DEFAULT_API_URL,
        )

    @cached_property
    def endpoints(self) -> dict[str, Any]:
        """Return the packaged endpoint registry (descriptions, keys, line links)."""
        text = resources.files("tap_microsoft_dynamics_bc").joinpath("endpoints.json").read_text()
        return json.loads(text)

    def _get_metadata_response(self, path: str) -> requests.Response:
        return requests.get(
            f"{self.environment_url}/{path}/$metadata",
            auth=self.authenticator,
            headers={"Accept": "application/xml"},
            timeout=METADATA_TIMEOUT_SECONDS,
        )

    def get_metadata(self, path: str) -> Metadata:
        """Fetch and parse ``{environment}/{path}/$metadata``, failing on any error."""
        response = self._get_metadata_response(path)
        response.raise_for_status()
        return parse_metadata(response.content)

    def get_optional_metadata(self, path: str) -> Metadata | None:
        """Like :meth:`get_metadata`, but return ``None`` if the route is unavailable."""
        response = self._get_metadata_response(path)
        if response.status_code in {403, 404}:
            self.logger.warning(
                "Skipping '%s': HTTP %s. The extension that publishes it may not be "
                "installed, or the application lacks permission.",
                path,
                response.status_code,
            )
            return None
        response.raise_for_status()
        return parse_metadata(response.content)

    def _route_streams(  # noqa: C901, PLR0913
        self,
        companies: CompaniesStream,
        *,
        route_id: str,
        path: str,
        prefix: str,
        metadata: Metadata,
        known: dict[str, Any],
        excluded: set[str],
        selected_by_default: bool,
    ) -> list[BusinessCentralStream]:
        streams: list[BusinessCentralStream] = []
        headers: dict[str, CompanyEntityStream] = {}
        lines: list[tuple[str, str, dict[str, Any]]] = []

        for entity_set, type_name in sorted(metadata.company_scoped_sets().items()):
            if entity_set in excluded:
                continue
            info = known.get(entity_set)
            entity = metadata.entity_type(type_name)
            if entity is None:
                continue
            if info and info.get("parent"):
                lines.append((entity_set, type_name, info))
                continue
            if info is None and route_id == STANDARD_ROUTE and entity_set.endswith("Lines"):
                # Lines unknown to the registry usually need a document filter.
                continue

            schema = entity_schema(entity, metadata, (info or {}).get("field_descriptions"))
            props = schema["properties"]
            schema["properties"] = {"company_id": COMPANY_ID, **props}

            if info and info.get("kind") == "query":
                keys = [next((k for k in QUERY_KEY_CANDIDATES if k in props), "")]
            else:
                keys = entity.keys or (["id"] if "id" in props else [])
            keys = [k for k in keys if k]

            replication_key = None
            if info is not None:
                if info.get("incremental") and not info.get("full_table_default"):
                    replication_key = (
                        "lastModifiedDateTime" if "lastModifiedDateTime" in props else None
                    )
            else:
                replication_key = next((k for k in REPLICATION_KEY_CANDIDATES if k in props), None)

            open_documents_filter = (
                OPEN_DOCUMENT_FILTERS.get(entity_set)
                if route_id == STANDARD_ROUTE and replication_key and "status" in props
                else None
            )

            name = f"{prefix}{snake_case(entity_set)}"
            stream = bind_parent(CompanyEntityStream, companies, name)(
                self,
                name=name,
                schema=schema,
                path=f"/{path}/companies({{company_id}})/{entity_set}",
                primary_keys=["company_id", *keys] if keys else [],
                replication_key=replication_key,
                description=(info or {}).get("description"),
                open_documents_filter=open_documents_filter,
            )
            stream.parent_stream = companies
            stream.selected_by_default = selected_by_default and (
                info is not None or route_id not in self.endpoints["routes"]
            )
            headers[entity_set] = stream
            streams.append(stream)

        for entity_set, type_name, info in lines:
            parent_set = info["parent"]["entity_set"]
            nav = info["parent"]["navigation_property"]
            header = headers.get(parent_set)
            entity = metadata.entity_type(type_name)
            if header is None or entity is None:
                continue
            schema = entity_schema(entity, metadata, info.get("field_descriptions"))
            schema["properties"] = {"company_id": COMPANY_ID, **schema["properties"]}
            keys = entity.keys or (["id"] if "id" in schema["properties"] else [])
            name = f"{prefix}{snake_case(entity_set)}"
            line_stream = bind_parent(ExpandedLineStream, header, name)(
                self,
                name=name,
                schema=schema,
                path=f"/{path}/companies({{company_id}})/{parent_set}({{parent_id}})/{nav}",
                primary_keys=["company_id", *keys] if keys else [],
                replication_key=None,
                description=info.get("description"),
                navigation_property=nav,
            )
            line_stream.parent_stream = header
            line_stream.selected_by_default = header.selected_by_default
            streams.append(line_stream)
        return streams

    def _web_service_streams(self, companies: CompaniesStream) -> list[BusinessCentralStream]:
        services = self.config.get("web_services") or []
        if not services:
            return []
        metadata = self.get_metadata("ODataV4")
        streams: list[BusinessCentralStream] = []
        for service in services:
            name = service["name"]
            type_name = metadata.entity_sets.get(name)
            entity = metadata.entity_type(type_name) if type_name else None
            if entity is None:
                self.logger.warning(
                    "Web service '%s' is not published in this environment; skipping.",
                    name,
                )
                continue
            schema = entity_schema(entity, metadata)
            schema["properties"] = {"company_id": COMPANY_ID, **schema["properties"]}
            replication_key = service.get("replication_key")
            if replication_key and replication_key not in schema["properties"]:
                self.logger.warning(
                    "Web service '%s' has no field '%s'; extracting it in full.",
                    name,
                    replication_key,
                )
                replication_key = None
            stream_name = f"web_service_{snake_case(name)}"
            stream = bind_parent(WebServiceStream, companies, stream_name)(
                self,
                name=stream_name,
                schema=schema,
                path=f"/ODataV4/Company('{{company_name}}')/{name}",
                primary_keys=["company_id", *entity.keys],
                replication_key=replication_key,
                description=f"OData web service '{name}'.",
            )
            stream.parent_stream = companies
            streams.append(stream)
        return streams

    @override
    def discover_streams(self) -> list[BusinessCentralStream]:
        """Discover streams from the environment's OData ``$metadata``."""
        routes = self.endpoints["routes"]
        excluded = {k: set(v) for k, v in self.endpoints["excluded"].items()}

        standard = self.get_metadata(routes[STANDARD_ROUTE]["path"])
        company_type = standard.entity_type("company")
        companies_schema = (
            entity_schema(company_type, standard)
            if company_type
            else {"type": "object", "properties": {"id": {"type": "string"}}}
        )
        companies = CompaniesStream(
            self,
            name="companies",
            schema=companies_schema,
            path=f"/{routes[STANDARD_ROUTE]['path']}/companies",
            primary_keys=["id"],
            description="Companies in the Business Central environment.",
        )
        streams: list[BusinessCentralStream] = [companies]

        plan: list[tuple[str, dict[str, Any], bool]] = [
            (STANDARD_ROUTE, routes[STANDARD_ROUTE], True)
        ]
        if self.config.get("include_reports_finance_api", True):
            plan.append(("reports_finance", routes["reports_finance"], True))
        if self.config.get("include_analytics_api", True):
            plan.append(("analytics", routes["analytics"], False))
            plan.append(("analytics_v1", routes["analytics_v1"], False))
        for api in self.config.get("custom_apis") or []:
            route_id = f"{api['publisher']}/{api['group']}/{api['version']}"
            prefix = snake_case(api.get("stream_prefix") or api["group"])
            plan.append(
                (
                    route_id,
                    {"path": f"api/{route_id}", "stream_prefix": f"{prefix}_", "endpoints": {}},
                    True,
                ),
            )

        for route_id, route, selected_by_default in plan:
            metadata = (
                standard
                if route_id == STANDARD_ROUTE
                else self.get_optional_metadata(route["path"])
            )
            if metadata is None:
                continue
            streams.extend(
                self._route_streams(
                    companies,
                    route_id=route_id,
                    path=route["path"],
                    prefix=route["stream_prefix"],
                    metadata=metadata,
                    known=route["endpoints"],
                    excluded=excluded.get(route_id, set()),
                    selected_by_default=selected_by_default,
                ),
            )

        streams.extend(self._web_service_streams(companies))

        unique: dict[str, BusinessCentralStream] = {}
        for stream in streams:
            if stream.name in unique:
                self.logger.warning("Duplicate stream name '%s'; keeping the first.", stream.name)
                continue
            unique[stream.name] = stream
        return list(unique.values())


if __name__ == "__main__":
    TapMicrosoftDynamicsBC.cli()
