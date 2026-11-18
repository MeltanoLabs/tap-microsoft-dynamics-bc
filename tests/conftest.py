"""Shared fixtures: a mocked Business Central environment."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import responses

if TYPE_CHECKING:
    import requests

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = "test-tenant"
ENVIRONMENT = "production"
BASE = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/{ENVIRONMENT}"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
COMPANY_ID = "11111111-1111-1111-1111-111111111111"
COMPANY_NAME = "CRONUS UK Ltd."

SAMPLE_CONFIG: dict[str, Any] = {
    "tenant_id": TENANT,
    "client_id": "test-client",
    "client_secret": "test-secret",
    "environment_name": ENVIRONMENT,
    "start_date": "2026-01-01T00:00:00Z",
    "include_analytics_api": True,
    "web_services": [
        {"name": "BankAccountLedgerEntries", "replication_key": "SystemModifiedAt"},
    ],
}

COMPANY_PATH = f"/api/v2.0/companies({COMPANY_ID})"
FINANCE_PATH = f"/api/microsoft/reportsFinance/beta/companies({COMPANY_ID})"

CUSTOMERS_PAGE_1 = [
    {
        "@odata.etag": 'W/"1"',
        "id": "c0000000-0000-0000-0000-000000000001",
        "number": "10000",
        "displayName": "Adatum Corporation",
        "type": "Company",
        "taxLiable": False,
        "balanceDue": 1499.5,
        "creditLimit": 0,
        "lastModifiedDateTime": "2026-02-01T10:00:00.123Z",
        "xbankRiskRating": "LOW",
    },
]
CUSTOMERS_PAGE_2 = [
    {
        "id": "c0000000-0000-0000-0000-000000000002",
        "number": "20000",
        "displayName": "Trey Research",
        "type": "Person",
        "taxLiable": True,
        "balanceDue": 0,
        "creditLimit": 5000,
        "lastModifiedDateTime": "2026-03-01T10:00:00Z",
        "xbankRiskRating": None,
    },
]
SALES_INVOICES = [
    {
        "id": "a0000000-0000-0000-0000-000000000001",
        "number": "103001",
        "invoiceDate": "2026-02-02",
        "customerId": "c0000000-0000-0000-0000-000000000001",
        "totalAmountIncludingTax": 120.25,
        "status": "Paid",
        "lastModifiedDateTime": "2026-02-02T09:00:00Z",
        "salesInvoiceLines": [
            {
                "id": "b0000000-0000-0000-0000-000000000001",
                "documentId": "a0000000-0000-0000-0000-000000000001",
                "sequence": 10000,
                "description": "Bicycle",
                "quantity": 1,
                "amountIncludingTax": 120.25,
                "itemDetails": {"number": "1000", "displayName": "Bicycle"},
            },
        ],
    },
]
SALES_ORDERS = [
    {
        "id": "d0000000-0000-0000-0000-000000000001",
        "number": "101001",
        "orderDate": "2026-02-03",
        "lastModifiedDateTime": "2026-02-03T09:00:00Z",
        "salesOrderLines": [
            {
                "id": "e0000000-0000-0000-0000-000000000001",
                "documentId": "d0000000-0000-0000-0000-000000000001",
                "sequence": 10000,
                "quantity": 2,
            },
        ],
        "salesOrderLines@odata.nextLink": (
            f"{BASE}{COMPANY_PATH}/salesOrders(d0000000-0000-0000-0000-000000000001)"
            "/salesOrderLines?$skiptoken=x"
        ),
    },
]
SALES_ORDER_LINES_DIRECT = [
    {
        "id": "e0000000-0000-0000-0000-000000000001",
        "documentId": "d0000000-0000-0000-0000-000000000001",
        "sequence": 10000,
        "quantity": 2,
    },
    {
        "id": "e0000000-0000-0000-0000-000000000002",
        "documentId": "d0000000-0000-0000-0000-000000000001",
        "sequence": 20000,
        "quantity": 3,
    },
]
CUSTOMER_LEDGER_ENTRIES = [
    {
        "entryNumber": 1,
        "customerNumber": "10000",
        "postingDate": "2026-02-02",
        "amount": 120.25,
        "open": False,
        "lastModifiedDateTime": "2026-02-05T00:00:00Z",
    },
]
BANK_LEDGER = [
    {
        "@odata.etag": 'W/"x"',
        "Entry_No": 7,
        "Posting_Date": "2026-02-02",
        "Bank_Account_No": "WWB-OPERATING",
        "Amount": -120.25,
        "SystemModifiedAt": "2026-02-02T12:00:00Z",
    },
]


def _json(status: int, body: dict[str, Any]) -> tuple[int, dict[str, str], str]:
    return status, {"Content-Type": "application/json"}, json.dumps(body)


def _xml(name: str) -> tuple[int, dict[str, str], str]:
    return 200, {"Content-Type": "application/xml"}, (FIXTURES / name).read_text()


def _not_found() -> tuple[int, dict[str, str], str]:
    return _json(404, {"error": {"code": "BadRequest_ResourceNotFound", "message": "Not found"}})


def route(request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:  # noqa: C901, PLR0911, PLR0912
    """Answer a request the way Business Central would."""
    url = urlsplit(str(request.url))
    path = unquote(url.path).removeprefix(urlsplit(BASE).path)
    query = parse_qs(url.query)

    if path.endswith("/$metadata"):
        return {
            "/api/v2.0/$metadata": lambda: _xml("api_v2_metadata.xml"),
            "/api/microsoft/reportsFinance/beta/$metadata": lambda: _xml(
                "reports_finance_metadata.xml"
            ),
            "/ODataV4/$metadata": lambda: _xml("odatav4_metadata.xml"),
        }.get(path, _not_found)()

    if path == "/api/v2.0/companies":
        return _json(
            200,
            {
                "value": [
                    {
                        "id": COMPANY_ID,
                        "systemVersion": "27.0",
                        "timestamp": 1,
                        "name": COMPANY_NAME,
                        "displayName": COMPANY_NAME,
                        "businessProfileId": "",
                        "systemCreatedAt": "2025-01-01T00:00:00Z",
                    },
                ],
            },
        )
    if path == f"{COMPANY_PATH}/customers":
        if "$skiptoken" in query:
            return _json(200, {"value": CUSTOMERS_PAGE_2})
        return _json(
            200,
            {
                "value": CUSTOMERS_PAGE_1,
                "@odata.nextLink": f"{BASE}{COMPANY_PATH}/customers?$skiptoken=abc",
            },
        )
    if path == f"{COMPANY_PATH}/salesInvoices":
        invoices = json.loads(json.dumps(SALES_INVOICES))
        if "$expand" not in query:
            for invoice in invoices:
                invoice.pop("salesInvoiceLines")
        return _json(200, {"value": invoices})
    if path == f"{COMPANY_PATH}/salesOrders":
        return _json(200, {"value": SALES_ORDERS})
    if path == f"{COMPANY_PATH}/salesOrders(d0000000-0000-0000-0000-000000000001)/salesOrderLines":
        return _json(200, {"value": SALES_ORDER_LINES_DIRECT})
    if path == f"{COMPANY_PATH}/trialBalances":
        return _json(
            403, {"error": {"code": "Authorization_InsufficientPermissions", "message": "No"}}
        )
    if path == f"{COMPANY_PATH}/companyInformation":
        return _json(
            200,
            {
                "value": [
                    {
                        "id": COMPANY_ID,
                        "displayName": COMPANY_NAME,
                        "lastModifiedDateTime": "2026-01-02T00:00:00Z",
                    }
                ]
            },
        )
    if path == f"{FINANCE_PATH}/customerLedgerEntries":
        return _json(200, {"value": CUSTOMER_LEDGER_ENTRIES})
    if re.fullmatch(r"/ODataV4/Company\('CRONUS UK Ltd\.'\)/BankAccountLedgerEntries", path):
        return _json(200, {"value": BANK_LEDGER})
    return _not_found()


_MOCK = responses.RequestsMock(assert_all_requests_are_fired=False)
_MOCK.add(
    responses.POST,
    TOKEN_URL,
    json={"access_token": "token", "token_type": "Bearer", "expires_in": 3599},
)
_MOCK.add_callback(
    responses.GET,
    re.compile(r"https://api\.businesscentral\.dynamics\.com/.*"),
    callback=route,
)


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Start the fake environment before collection.

    The SDK's standard test class instantiates the tap while tests are being
    collected, so the mock must already be active then.
    """
    _MOCK.start()


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001
    """Stop the fake environment."""
    _MOCK.stop()


@pytest.fixture
def bc_api() -> responses.RequestsMock:
    """Return the fake Business Central environment, to inspect calls."""
    return _MOCK
