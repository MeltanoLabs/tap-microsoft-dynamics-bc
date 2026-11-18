"""Behavioural tests against the mocked Business Central environment."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from tap_microsoft_dynamics_bc.client import BusinessCentralStream
from tap_microsoft_dynamics_bc.streams import CompanyEntityStream
from tap_microsoft_dynamics_bc.tap import TapMicrosoftDynamicsBC, snake_case
from tests.conftest import COMPANY_ID, DRAFT_PASS_STATUS, ONPREM_BASE, SAMPLE_CONFIG

if TYPE_CHECKING:
    from collections.abc import Generator

    import responses


def _messages(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]


def _records(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        if message["type"] == "RECORD":
            out[message["stream"]].append(message["record"])
    return out


@pytest.fixture
def tap() -> TapMicrosoftDynamicsBC:
    return TapMicrosoftDynamicsBC(config=SAMPLE_CONFIG, parse_env_config=False)


def test_snake_case() -> None:
    assert snake_case("salesInvoiceLines") == "sales_invoice_lines"
    assert snake_case("Customer_Ledger_Entries") == "customer_ledger_entries"
    assert snake_case("generalLedgerEntries") == "general_ledger_entries"
    assert snake_case("BankAccountLedgerEntries") == "bank_account_ledger_entries"


def test_discovery_builds_streams_from_metadata(tap: TapMicrosoftDynamicsBC) -> None:
    names = set(tap.streams)
    assert {
        "companies",
        "customers",
        "sales_invoices",
        "sales_invoice_lines",
        "sales_orders",
        "sales_order_lines",
        "trial_balances",
        "company_information",
        "reports_finance_customer_ledger_entries",
        "web_service_bank_account_ledger_entries",
    } <= names
    # Excluded or not listable company-wide.
    assert "dimension_set_lines" not in names
    assert "pdf_document" not in names
    # The analytics routes return 404 in the mock (Power BI apps not installed).
    assert not any(n.startswith("analytics_") for n in names)


def test_keys_and_replication(tap: TapMicrosoftDynamicsBC) -> None:
    s = tap.streams
    assert s["customers"].primary_keys == ["company_id", "id"]
    assert s["customers"].replication_key == "lastModifiedDateTime"
    # Working documents are full-table so deletions propagate.
    assert s["sales_orders"].replication_key is None
    assert s["sales_orders"].replication_method == "FULL_TABLE"
    # Query endpoints declare no key; the entry number identifies the row.
    ledger = s["reports_finance_customer_ledger_entries"]
    assert ledger.primary_keys == ["company_id", "entryNumber"]
    assert ledger.replication_key == "lastModifiedDateTime"
    web = s["web_service_bank_account_ledger_entries"]
    assert web.primary_keys == ["company_id", "Entry_No"]
    assert web.replication_key == "SystemModifiedAt"
    assert s["trial_balances"].primary_keys == ["company_id", "number"]


def test_parent_child_wiring(tap: TapMicrosoftDynamicsBC) -> None:
    s = tap.streams
    assert s["sales_invoice_lines"] in s["sales_invoices"].child_streams
    assert s["sales_invoices"] in s["companies"].child_streams
    assert s["sales_invoice_lines"].parent_stream_type is not None


def test_schema_types_and_extension_fields(tap: TapMicrosoftDynamicsBC) -> None:
    customers = tap.streams["customers"].schema["properties"]
    assert customers["company_id"]["type"] == ["string"]
    assert customers["type"]["type"] == ["string", "null"]  # enum
    assert customers["balanceDue"]["type"] == ["number", "null"]
    assert customers["lastModifiedDateTime"]["format"] == "date-time"
    # A field added by an extension appears because schemas come from $metadata.
    assert "xbankRiskRating" in customers
    # Binary (Edm.Stream) properties are never extracted.
    assert "picture" not in tap.streams["company_information"].schema["properties"]
    lines = tap.streams["sales_invoice_lines"].schema["properties"]
    assert lines["itemDetails"]["type"] == ["object", "null"]
    # Field descriptions come from Microsoft's AL captions in endpoints.json.
    assert tap.streams["customers"].schema["properties"]["number"].get("description")


def test_sync(
    tap: TapMicrosoftDynamicsBC,
    capsys: pytest.CaptureFixture[str],
    bc_api: responses.RequestsMock,
) -> None:
    bc_api.calls.reset()
    tap.sync_all()
    messages = _messages(capsys)
    records = _records(messages)

    # Server-driven paging is followed.
    assert [r["number"] for r in records["customers"]] == ["10000", "20000"]
    # OData annotations are dropped and the owning company is stamped.
    assert "@odata.etag" not in records["customers"][0]
    assert {r["company_id"] for r in records["customers"]} == {COMPANY_ID}

    # Lines arrive through $expand on the header, without a request per document.
    # The second invoice is a draft that did not change in this window: it is
    # found by the re-read of open documents, so edits to its lines are not missed.
    assert [r["number"] for r in records["sales_invoices"]] == ["103001", ""]
    assert [r["description"] for r in records["sales_invoice_lines"]] == [
        "Bicycle",
        "Bicycle bell, edited",
    ]
    assert records["sales_invoice_lines"][0]["company_id"] == COMPANY_ID
    assert "salesInvoiceLines" not in records["sales_invoices"][0]
    # A truncated expansion falls back to the line endpoint for that document.
    assert len(records["sales_order_lines"]) == 3

    assert len(records["reports_finance_customer_ledger_entries"]) == 1
    assert len(records["web_service_bank_account_ledger_entries"]) == 1
    # 403 on an endpoint skips it instead of failing the run.
    assert records["trial_balances"] == []

    # Full-table streams emit ACTIVATE_VERSION so deletes propagate: once per run,
    # not once per company or per document.
    activated = [m["stream"] for m in messages if m["type"] == "ACTIVATE_VERSION"]
    assert len(activated) == len(set(activated))
    assert "sales_orders" in activated
    assert "sales_order_lines" in activated
    assert "customers" not in activated
    # Lines of an incremental header only cover changed documents; activating a
    # version would delete the lines of every unchanged invoice downstream.
    assert "sales_invoice_lines" not in activated
    # A skipped endpoint (403) keeps the rows already loaded.
    assert "trial_balances" not in activated
    # The version is activated before any of its records.
    first_order = next(
        i for i, m in enumerate(messages) if m["type"] == "RECORD" and m["stream"] == "sales_orders"
    )
    activate_orders = next(
        i
        for i, m in enumerate(messages)
        if m["type"] == "ACTIVATE_VERSION" and m["stream"] == "sales_orders"
    )
    assert activate_orders < first_order

    urls = [urlsplit(str(c.request.url)) for c in bc_api.calls]
    customers_first = next(
        u for u in urls if unquote(u.path).endswith("/customers") and "skiptoken" not in u.query
    )
    assert parse_qs(customers_first.query)["$filter"] == [
        "lastModifiedDateTime ge 2026-01-01T00:00:00Z"
    ]
    invoice_calls = [u for u in urls if unquote(u.path).endswith("/salesInvoices")]
    assert [parse_qs(u.query)["$filter"] for u in invoice_calls] == [
        ["lastModifiedDateTime ge 2026-01-01T00:00:00Z"],
        ["status eq 'Draft' and lastModifiedDateTime lt 2026-01-01T00:00:00Z"],
    ]
    assert {parse_qs(u.query)["$expand"][0] for u in invoice_calls} == {"salesInvoiceLines"}
    # Only invoices and credit memos are re-read; orders are full-table anyway.
    assert sum(unquote(u.path).endswith("/salesOrders") for u in urls) == 1
    web = next(u for u in urls if "BankAccountLedgerEntries" in u.path)
    assert "Company('CRONUS%20UK%20Ltd.')" in web.path
    assert not any(unquote(u.path).endswith("/salesInvoiceLines") for u in urls)

    # Incremental streams emit per-company bookmarks.
    states = [m["value"] for m in messages if m["type"] == "STATE"]
    customer_state = states[-1]["bookmarks"]["customers"]["partitions"][0]
    assert customer_state["context"] == {"company_id": COMPANY_ID}
    assert customer_state["replication_key_value"] == "2026-03-01T10:00:00Z"


def test_open_documents_reread_failure_is_not_fatal(
    tap: TapMicrosoftDynamicsBC,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(DRAFT_PASS_STATUS, "value", 400)
    tap.sync_all()
    records = _records(_messages(capsys))
    assert [r["number"] for r in records["sales_invoices"]] == ["103001"]
    assert len(records["sales_invoice_lines"]) == 1
    assert "Could not re-read open documents of 'sales_invoices'" in caplog.text


def test_open_documents_not_reread_without_window_start(
    capsys: pytest.CaptureFixture[str],
    bc_api: responses.RequestsMock,
) -> None:
    # No bookmark and no start date: the first pass already reads every document.
    config = {k: v for k, v in SAMPLE_CONFIG.items() if k != "start_date"}
    tap = TapMicrosoftDynamicsBC(config=config, parse_env_config=False)
    bc_api.calls.reset()
    tap.sync_all()
    capsys.readouterr()
    paths = [unquote(urlsplit(str(c.request.url)).path) for c in bc_api.calls]
    assert sum(p.endswith("/salesInvoices") for p in paths) == 1


def test_open_documents_filter_only_on_draft_documents(tap: TapMicrosoftDynamicsBC) -> None:
    streams = tap.streams
    invoices = streams["sales_invoices"]
    assert isinstance(invoices, CompanyEntityStream)
    assert invoices.open_documents_filter == "status eq 'Draft'"
    orders = streams["sales_orders"]
    assert isinstance(orders, CompanyEntityStream)
    assert orders.open_documents_filter is None


def test_full_table_override_propagates_deletes(
    tap: TapMicrosoftDynamicsBC,
    capsys: pytest.CaptureFixture[str],
    bc_api: responses.RequestsMock,
) -> None:
    """``replication-method: FULL_TABLE`` re-reads a stream and activates a version.

    This is how deletions of master data (customers, items...) reach targets.
    Meltano's ``metadata`` extra writes the key both on the stream (underscored)
    and in its stream-level metadata; the catalog is built the same way here.
    """
    catalog = tap.catalog_dict
    entry = next(s for s in catalog["streams"] if s["tap_stream_id"] == "customers")
    entry["replication_method"] = "FULL_TABLE"
    stream_metadata = next(m for m in entry["metadata"] if m["breadcrumb"] == [])
    stream_metadata["metadata"]["replication-method"] = "FULL_TABLE"
    overridden = TapMicrosoftDynamicsBC(
        config=SAMPLE_CONFIG, catalog=catalog, parse_env_config=False
    )
    assert overridden.streams["customers"].replication_method == "FULL_TABLE"

    bc_api.calls.reset()
    overridden.sync_all()
    messages = _messages(capsys)
    activated = [m["stream"] for m in messages if m["type"] == "ACTIVATE_VERSION"]
    assert activated.count("customers") == 1
    first_page = next(
        urlsplit(str(c.request.url))
        for c in bc_api.calls
        if unquote(urlsplit(str(c.request.url)).path).endswith("/customers")
    )
    assert "$filter" not in parse_qs(first_page.query)
    assert len(_records(messages)["customers"]) == 2


def test_on_premises_base_url(bc_api: responses.RequestsMock) -> None:
    bc_api.calls.reset()
    tap = TapMicrosoftDynamicsBC(
        config={**SAMPLE_CONFIG, "base_url": f"{ONPREM_BASE}/"},
        parse_env_config=False,
    )
    assert tap.environment_url == ONPREM_BASE
    assert "customers" in tap.streams
    hosts = {urlsplit(str(c.request.url)).netloc for c in bc_api.calls if c.request.method == "GET"}
    assert hosts == {urlsplit(ONPREM_BASE).netloc}
    # On-premises servers accept the same Entra token as Business Central online.
    assert tap.authenticator.oauth_scopes == "https://api.businesscentral.dynamics.com/.default"


def test_time_registration_entries_are_reread_for_every_employee(
    tap: TapMicrosoftDynamicsBC,
) -> None:
    # Time registration entries can only be listed per employee and change without
    # the employee changing, so employees are full-table.
    endpoints = tap.endpoints["routes"]["v2.0"]["endpoints"]
    assert endpoints["timeRegistrationEntries"]["parent"]["entity_set"] == "employees"
    assert endpoints["employees"]["full_table_default"] is True


def test_company_filter(capsys: pytest.CaptureFixture[str]) -> None:
    tap = TapMicrosoftDynamicsBC(
        config={**SAMPLE_CONFIG, "company_ids": ["Some Other Company"]},
        parse_env_config=False,
    )
    tap.sync_all()
    records = _records(_messages(capsys))
    assert records["companies"] == []
    assert records["customers"] == []


def test_retry_after_is_honoured(tap: TapMicrosoftDynamicsBC) -> None:
    stream = tap.streams["customers"]
    assert isinstance(stream, BusinessCentralStream)
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "7"
    exc = type("Throttled", (Exception,), {"response": response})()
    waits = cast("Generator[float, Any, None]", stream.backoff_wait_generator())
    next(waits)
    assert waits.send(exc) == 7
