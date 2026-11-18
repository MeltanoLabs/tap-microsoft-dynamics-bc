"""Behavioural tests against the mocked Business Central environment."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from tap_microsoft_dynamics_bc.client import BusinessCentralStream
from tap_microsoft_dynamics_bc.tap import TapMicrosoftDynamicsBC, snake_case
from tests.conftest import COMPANY_ID, SAMPLE_CONFIG

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
    assert len(records["sales_invoice_lines"]) == 1
    assert records["sales_invoice_lines"][0]["company_id"] == COMPANY_ID
    assert "salesInvoiceLines" not in records["sales_invoices"][0]
    # A truncated expansion falls back to the line endpoint for that document.
    assert len(records["sales_order_lines"]) == 2

    assert len(records["reports_finance_customer_ledger_entries"]) == 1
    assert len(records["web_service_bank_account_ledger_entries"]) == 1
    # 403 on an endpoint skips it instead of failing the run.
    assert records["trial_balances"] == []

    # Full-table streams emit ACTIVATE_VERSION so deletes propagate.
    activated = {m["stream"] for m in messages if m["type"] == "ACTIVATE_VERSION"}
    assert "sales_orders" in activated
    assert "customers" not in activated

    urls = [urlsplit(str(c.request.url)) for c in bc_api.calls]
    customers_first = next(
        u for u in urls if unquote(u.path).endswith("/customers") and "skiptoken" not in u.query
    )
    assert parse_qs(customers_first.query)["$filter"] == [
        "lastModifiedDateTime ge 2026-01-01T00:00:00Z"
    ]
    invoices = next(u for u in urls if unquote(u.path).endswith("/salesInvoices"))
    assert parse_qs(invoices.query)["$expand"] == ["salesInvoiceLines"]
    web = next(u for u in urls if "BankAccountLedgerEntries" in u.path)
    assert "Company('CRONUS%20UK%20Ltd.')" in web.path
    assert not any(unquote(u.path).endswith("/salesInvoiceLines") for u in urls)

    # Incremental streams emit per-company bookmarks.
    states = [m["value"] for m in messages if m["type"] == "STATE"]
    customer_state = states[-1]["bookmarks"]["customers"]["partitions"][0]
    assert customer_state["context"] == {"company_id": COMPANY_ID}
    assert customer_state["replication_key_value"] == "2026-03-01T10:00:00Z"


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
