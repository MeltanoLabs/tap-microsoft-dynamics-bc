"""Regenerate ``tap_microsoft_dynamics_bc/endpoints.json`` from Microsoft's AL source.

Business Central's standard API v2.0, the "API Reports - Finance" app and the
Power BI Reports app are open source in https://github.com/microsoft/BCApps.
This script reads their API pages and queries at a pinned commit and records,
per endpoint: Microsoft's description (``AboutText``), field captions, whether
it exposes a last-modified timestamp, and which header a line collection
belongs to. The tap uses this to name, describe and wire streams; schemas
themselves always come from the live environment's ``$metadata``.

Usage:
    uv run python scripts/generate_endpoints.py --commit <BCApps commit SHA>
    uv run python scripts/generate_endpoints.py --docs-only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tap_microsoft_dynamics_bc.tap import OPEN_DOCUMENT_FILTERS

REPO = "microsoft/BCApps"
APP_DIRS = ("src/Apps/W1/APIV2/", "src/Apps/W1/APIReportsFinance/", "src/Apps/W1/PowerBIReports/")
ROOT = Path(__file__).parent.parent
OUTPUT = ROOT / "tap_microsoft_dynamics_bc" / "endpoints.json"
STREAMS_DOC = ROOT / "STREAMS.md"
ROUTE_TITLES = {
    "v2.0": "Standard API v2.0 (`api/v2.0`)",
    "reports_finance": "Finance reports API (`api/microsoft/reportsFinance/beta`)",
    "analytics": "Analytics API (`api/microsoft/analytics/beta`), Power BI Reports app",
    "analytics_v1": "Analytics API (`api/microsoft/analytics/v1.0`), Power BI Reports app",
}

# (APIGroup, APIVersion) -> (route id, route path, stream prefix)
ROUTES = {
    ("std", "v2.0"): ("v2.0", "api/v2.0", ""),
    ("reportsFinance", "beta"): (
        "reports_finance",
        "api/microsoft/reportsFinance/beta",
        "reports_finance_",
    ),
    ("analytics", "beta"): ("analytics", "api/microsoft/analytics/beta", "analytics_"),
    ("analytics", "v1.0"): ("analytics_v1", "api/microsoft/analytics/v1.0", "analytics_"),
}

# Endpoints that cannot be listed company-wide, are binary, or hold
# configuration/operational data rather than business data.
EXCLUDED = {
    "v2.0": {
        "dimensionSetLines",
        "defaultDimensions",
        "pdfDocument",
        "pictures",
        "attachments",
        "documentAttachments",
        "contactsInformation",
        "customerContacts",
        "applyVendorEntries",
        "jobQueueEntries",
        "jobQueueLogEntries",
        "workflows",
        "workflowSteps",
        "workflowResponseOptions",
        "workflowApprovers",
        "approvalUserSetups",
    },
}

# Working documents that are deleted when posted or cancelled. They default to
# full-table replication so ACTIVATE_VERSION lets targets drop deleted rows.
# `employees` is here for its time registration entries: Business Central only
# lists them per employee, and they change without the employee changing, so
# every employee is re-read (employees are few).
FULL_TABLE_DEFAULT = {
    "employees",
    "salesOrders",
    "salesQuotes",
    "blanketSalesOrders",
    "salesReturnOrders",
    "purchaseOrders",
    "purchaseQuotes",
    "blanketPurchaseOrders",
    "purchaseReturnOrders",
    "transferOrders",
    "assemblyOrders",
    "physicalInventoryOrders",
    "physicalInventoryRecordings",
    "inventoryReceipts",
    "inventoryShipments",
    "journals",
    "customerPaymentJournals",
    "vendorPaymentJournals",
    "salesReturnOrderLines",
    "purchaseReturnOrderLines",
    "purchaseQuoteLines",
    "transferOrderLines",
    "assemblyOrderLines",
    "blanketSalesOrderLines",
    "blanketPurchaseOrderLines",
    "physicalInventoryOrderLines",
    "physicalInventoryRecordingLines",
    "inventoryReceiptLines",
    "inventoryShipmentLines",
}

NOT_LINES = {"dimensionSetLines", "pdfDocument", "attachments", "documentAttachments"}
REQUIRES_PARENT = re.compile(
    r"Error\(\w*(FiltersNotSpecified|ParentNotSpecified|ShouldBeSpecified|"
    r"MissingParent|NotSpecifiedForLines)\w*\)",
)
MODIFIED = re.compile(r"SystemModifiedAt|Last Modified Date Time|Last Date Modified", re.IGNORECASE)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tap-microsoft-dynamics-bc"})  # noqa: S310 - fixed https URLs
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return response.read()


def list_sources(commit: str) -> list[str]:
    """Return the paths of API page/query sources at a commit."""
    tree = json.loads(_get(f"https://api.github.com/repos/{REPO}/git/trees/{commit}?recursive=1"))
    pattern = re.compile(r".*\.(Page|Query)\.al$")
    return [
        item["path"]
        for item in tree["tree"]
        if item["type"] == "blob"
        and item["path"].startswith(APP_DIRS)
        and pattern.match(item["path"])
        and "/_Obsolete/" not in item["path"]
        and "/Embedded/" not in item["path"]
    ]


def download(commit: str, paths: list[str]) -> dict[str, str]:
    """Download source files concurrently."""
    base = f"https://raw.githubusercontent.com/{REPO}/{commit}"
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        bodies = pool.map(lambda p: _get(f"{base}/{quote(p)}").decode("utf-8", "replace"), paths)
        return dict(zip(paths, bodies, strict=True))


def _prop(text: str, name: str) -> str | None:
    match = re.search(rf"^\s*{name}\s*=\s*'((?:[^']|'')*)'\s*(?:,[^;]*)?;", text, re.MULTILINE)
    return match.group(1).replace("''", "'") if match else None


def parse_object(text: str) -> list[dict[str, Any]]:
    """Parse one AL API page or query; one entry per API version it serves."""
    kind = "page" if re.search(r"PageType\s*=\s*API", text) else None
    kind = kind or ("query" if re.search(r"QueryType\s*=\s*API", text) else None)
    if not kind or re.search(r"ObsoleteState\s*=\s*(Pending|Removed)", text):
        return []
    group = _prop(text, "APIGroup") or "std"
    declared = re.search(r"^\s*APIVersion\s*=\s*([^;]+);", text, re.MULTILINE)
    versions = re.findall(r"'([^']+)'", declared.group(1)) if declared else ["beta"]
    name = re.search(r"^\s*(?:page|query)\s+\d+\s+\"?([^\"\n{]+?)\"?\s*$", text, re.MULTILINE)
    fields = re.findall(
        r"^\s*(?:field|column)\((\w+);\s*([^)]*)\)\s*\n\s*\{(.*?)\n\s*\}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    captions = {}
    for field_name, _, body in fields:
        if caption := re.search(r"Caption\s*=\s*'((?:[^']|'')*)'", body):
            captions[field_name] = caption.group(1).replace("''", "'")
    base = {
        "object": name.group(1).strip() if name else None,
        "kind": kind,
        "group": group,
        "entity_set": _prop(text, "EntitySetName"),
        "description": _prop(text, "AboutText"),
        "field_descriptions": captions,
        "modified": any(
            MODIFIED.search(source) or field == "lastModifiedDateTime"
            for field, source, _ in fields
        ),
        "requires_parent": bool(REQUIRES_PARENT.search(text)),
        "parts": {
            nav: page.strip()
            for nav, page in re.findall(r"^\s*part\((\w+);\s*\"?([^\")]+)\"?\)", text, re.MULTILINE)
        },
    }
    return [{**base, "version": version} for version in versions]


def build(objects: list[dict[str, Any]], commit: str) -> dict[str, Any]:
    """Assemble the endpoint registry."""
    objects = [o for o in objects if (o["group"], o["version"]) in ROUTES and o["entity_set"]]
    by_object = {o["object"]: o for o in objects}

    # child entity set -> [(header object, navigation property)]
    headers_of: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for header in objects:
        route_id = ROUTES[header["group"], header["version"]][0]
        for nav, page in header["parts"].items():
            child = by_object.get(page)
            if child and child["entity_set"] not in NOT_LINES:
                headers_of.setdefault((route_id, child["entity_set"]), []).append((header, nav))

    routes: dict[str, Any] = {}
    for obj in objects:
        route_id, path, prefix = ROUTES[obj["group"], obj["version"]]
        route = routes.setdefault(
            route_id, {"path": path, "stream_prefix": prefix, "endpoints": {}}
        )
        entity_set = obj["entity_set"]
        excluded = EXCLUDED.get(route_id, set())
        if entity_set in excluded:
            continue
        headers = [
            (h, nav)
            for h, nav in headers_of.get((route_id, entity_set), [])
            if h["entity_set"] not in excluded
        ]
        is_line = obj["requires_parent"] or (not obj["modified"] and entity_set.endswith("Lines"))
        parent = None
        if obj["kind"] == "page" and headers and is_line:
            header, nav = headers[0]
            if header["modified"] or obj["requires_parent"]:
                parent = {"entity_set": header["entity_set"], "navigation_property": nav}
        elif obj["requires_parent"]:
            continue  # cannot be listed company-wide and has no usable header
        route["endpoints"][entity_set] = {
            "description": obj["description"],
            "kind": obj["kind"],
            "incremental": obj["modified"],
            "full_table_default": entity_set in FULL_TABLE_DEFAULT,
            "parent": parent,
            "field_descriptions": obj["field_descriptions"],
        }

    return {
        "source": {
            "repository": f"https://github.com/{REPO}",
            "commit": commit,
            "note": (
                "Generated by scripts/generate_endpoints.py from the AL source of the "
                "API v2.0, API Reports - Finance and Power BI Reports apps."
            ),
        },
        "excluded": {route: sorted(sets) for route, sets in EXCLUDED.items()},
        "routes": routes,
    }


def _snake(name: str) -> str:
    name = re.sub(r"[^0-9A-Za-z]+", "_", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"_+", "_", name).strip("_").lower()


def streams_doc(registry: dict[str, Any]) -> str:
    """Render the stream reference (STREAMS.md) from the registry."""
    out = [
        "# Streams",
        "",
        "<!-- Generated by scripts/generate_endpoints.py. Do not edit by hand. -->",
        "",
        "Every company-scoped stream has a `company_id` column and a primary key of "
        "`company_id` plus the entity key. Schemas are read from the environment's "
        "`$metadata` at discovery, so fields added by extensions appear automatically.",
        "",
        "Replication: **incremental** streams filter on `lastModifiedDateTime` "
        "(**drafts re-read**: draft documents are also read again every run, so "
        "edits to their lines are not missed); **full table** streams are re-read "
        "each run and emit `ACTIVATE_VERSION` so deleted rows can be removed "
        "downstream; **lines of X** streams are read with their header document "
        "(via `$expand`) and emit `ACTIVATE_VERSION` only when X is full table.",
        "",
        "Analytics streams are not selected by default. Endpoints a Business Central "
        "release adds after this list was generated are discovered too, unselected.",
        "",
        "`companies` (full table): companies in the environment; parent of every stream.",
        "",
    ]
    order = list(ROUTE_TITLES)
    for route_id in sorted(
        registry["routes"], key=lambda r: order.index(r) if r in order else len(order)
    ):
        route = registry["routes"][route_id]
        out += [
            f"## {ROUTE_TITLES.get(route_id, route_id)}",
            "",
            "| Stream | Replication | Description |",
            "|---|---|---|",
        ]
        for entity_set, info in sorted(route["endpoints"].items()):
            if info["parent"]:
                parent = route["stream_prefix"] + _snake(info["parent"]["entity_set"])
                replication = f"lines of `{parent}`"
            elif info["incremental"] and not info["full_table_default"]:
                replication = "incremental"
                if route_id == "v2.0" and entity_set in OPEN_DOCUMENT_FILTERS:
                    replication = "incremental, drafts re-read"
            else:
                replication = "full table"
            description = (info["description"] or "").replace("|", "\\|")
            stream = route["stream_prefix"] + _snake(entity_set)
            out.append(f"| `{stream}` | {replication} | {description} |")
        out.append("")
    excluded = ", ".join(f"`{e}`" for e in registry["excluded"].get("v2.0", []))
    out += [
        "## Not extracted",
        "",
        f"From the standard API: {excluded}. These are binary content, configuration, "
        "or endpoints that can only be read for a single parent record.",
        "",
    ]
    return "\n".join(out)


def main() -> None:
    """Regenerate the registry and the stream reference."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", help="BCApps commit SHA to read")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Only re-render STREAMS.md from the existing endpoints.json",
    )
    args = parser.parse_args()

    if args.docs_only:
        registry = json.loads(args.output.read_text())
        STREAMS_DOC.write_text(streams_doc(registry))
        return
    if not args.commit:
        parser.error("--commit is required unless --docs-only is given")

    sources = download(args.commit, list_sources(args.commit))
    objects = [entry for text in sources.values() for entry in parse_object(text)]
    registry = build(objects, args.commit)
    args.output.write_text(json.dumps(registry, indent=1, sort_keys=True) + "\n")
    STREAMS_DOC.write_text(streams_doc(registry))
    for route_id, route in registry["routes"].items():
        endpoints = route["endpoints"].values()
        print(  # noqa: T201 - CLI summary
            f"{route_id}: {len(endpoints)} endpoints, "
            f"{sum(1 for e in endpoints if e['incremental'])} incremental, "
            f"{sum(1 for e in endpoints if e['parent'])} lines",
        )


if __name__ == "__main__":
    main()
