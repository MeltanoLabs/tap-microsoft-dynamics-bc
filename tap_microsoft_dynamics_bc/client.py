"""REST client handling, including the BusinessCentralStream base class."""

from __future__ import annotations

import datetime
import decimal
import logging
import re
import sys
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from singer_sdk.exceptions import FatalAPIError
from singer_sdk.helpers.jsonpath import extract_jsonpath
from singer_sdk.pagination import BaseHATEOASPaginator
from singer_sdk.streams import RESTStream
from singer_sdk.streams.rest import HTTPRequest

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from urllib.parse import ParseResult

    import requests
    from singer_sdk.helpers.types import Auth, Context
    from singer_sdk.streams.rest import PageContext

    from tap_microsoft_dynamics_bc.tap import TapMicrosoftDynamicsBC

DEFAULT_PAGE_SIZE = 1000
DEFAULT_RETRY_AFTER_SECONDS = 30
FULL_TABLE = "FULL_TABLE"
INCREMENTAL = "INCREMENTAL"

# Responses that mean "this endpoint is not available to this app in this
# environment" (extension not installed, missing permission, unpublished web
# service). The stream is skipped with a warning instead of failing the run.
UNAVAILABLE_STATUSES = {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}

# Business Central answers some endpoints with a 400 when the data they read is
# not set up in the company, e.g. `journals` with "There is no Gen. Journal
# Template within the filter". Only these error codes are treated as
# unavailable; any other 400 is a real failure and still stops the run.
UNAVAILABLE_400_ERROR_CODES = {"Internal_DataNotFoundFilter"}


def is_unavailable(response: requests.Response) -> bool:
    """Return whether the response says the endpoint is not available or not set up."""
    if response.status_code in UNAVAILABLE_STATUSES:
        return True
    if response.status_code != HTTPStatus.BAD_REQUEST:
        return False
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return False
    return code in UNAVAILABLE_400_ERROR_CODES


class StreamUnavailableError(FatalAPIError):
    """Raised when Business Central reports an endpoint as unavailable."""

    def __init__(self, message: str, response: requests.Response) -> None:
        """Keep the response so the status can be reported."""
        super().__init__(message)
        self.response = response


_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")


def normalise_timestamp(value: str) -> str:
    """Return an ``Edm.DateTimeOffset`` string in UTC with exactly six fraction digits.

    Business Central trims trailing zeros (``…18.4Z``, ``…18.45Z``, ``…18.457Z``).
    The SDK keeps the replication bookmark by comparing values as strings, and
    ``"…18.4Z"`` sorts after ``"…18.457Z"``, so without a fixed width the
    bookmark can lag the latest row and the next run re-reads up to a second of
    rows. Values that are not timestamps are returned unchanged.
    """
    match = _TIMESTAMP.fullmatch(value)
    if not match:
        return value
    base, fraction, offset = match.groups()
    fraction = (fraction or "")[:6].ljust(6, "0")
    if offset == "Z":
        return f"{base}.{fraction}Z"
    parsed = datetime.datetime.strptime(f"{base}.{fraction}{offset}", "%Y-%m-%dT%H:%M:%S.%f%z")
    return parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def odata_datetime(value: datetime.datetime) -> str:
    """Format a timestamp as an OData ``Edm.DateTimeOffset`` literal in UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class ODataNextLinkPaginator(BaseHATEOASPaginator):
    """Follow OData server-driven paging (``@odata.nextLink``)."""

    @override
    def get_next_url(self, response: requests.Response) -> str | None:
        """Return the absolute URL of the next page, if any."""
        return response.json().get("@odata.nextLink")


class BusinessCentralStream(RESTStream):
    """Base class for all Business Central streams."""

    records_jsonpath = "$.value[*]"

    #: The stream that owns this stream's records, if any.
    parent_stream: BusinessCentralStream | None = None

    def __init__(  # noqa: PLR0913
        self,
        tap: TapMicrosoftDynamicsBC,
        *,
        name: str,
        schema: dict,
        path: str,
        primary_keys: list[str] | None = None,
        replication_key: str | None = None,
        description: str | None = None,
    ) -> None:
        """Create a stream for one Business Central endpoint."""
        self.description = description
        super().__init__(tap, schema=schema, name=name, path=path)
        self.primary_keys = primary_keys or []
        self.replication_key = replication_key
        self._bc_tap = tap
        self._pending_activate_version: int | None = None
        self._activate_version_sent = False

    @override
    @property
    def url_base(self) -> str:
        """Return the environment root, e.g. ``.../v2.0/{tenant}/{environment}``."""
        return self._bc_tap.environment_url

    @override
    @property
    def authenticator(self) -> Auth:
        """Return the tap-wide authenticator, so all streams share one token."""
        return self._bc_tap.authenticator

    @override
    @property
    def http_headers(self) -> dict:
        """Return request headers.

        ``Data-Access-Intent: ReadOnly`` routes reads to a read-only replica where
        Business Central online has one, so extraction does not compete with
        users of the production database.
        """
        page_size = self.config.get("page_size") or DEFAULT_PAGE_SIZE
        return {
            "Accept": "application/json",
            "Data-Access-Intent": "ReadOnly",
            "Prefer": f"odata.maxpagesize={page_size}",
        }

    @override
    @property
    def emit_activate_version_messages(self) -> bool:
        """Emit ACTIVATE_VERSION only for streams that are read in full every run.

        A full-table stream below an incremental one, such as the lines of
        invoices, only receives the children of parents changed since the last
        run. Activating a new version there would make the target delete the
        lines of every unchanged invoice.
        """
        if not self.config.get("emit_activate_version_messages", True):
            return False
        parent = self.parent_stream
        while parent is not None:
            if parent.replication_method != FULL_TABLE:
                return False
            parent = parent.parent_stream
        return True

    @override
    def _write_activate_version_message(self, full_table_version: int) -> None:
        """Hold ACTIVATE_VERSION until the first successful response, then send it once.

        The SDK calls this at the start of every sync of the stream: once per
        company, and for line streams once per document, always with the same
        version. Sending it once spares targets a drain and a delete per
        document. Waiting for a successful response means an endpoint that is
        skipped (HTTP 403/404) does not wipe the rows already loaded.
        """
        if not self._activate_version_sent:
            self._pending_activate_version = full_table_version

    def send_activate_version(self) -> None:
        """Send the held ACTIVATE_VERSION message, if any and not already sent."""
        if self._pending_activate_version is None or self._activate_version_sent:
            return
        super()._write_activate_version_message(self._pending_activate_version)
        self._activate_version_sent = True
        self._pending_activate_version = None

    @override
    def get_new_paginator(self) -> ODataNextLinkPaginator:
        """Return a paginator that follows ``@odata.nextLink``."""
        return ODataNextLinkPaginator()

    @override
    def get_http_request(self, *, page: PageContext[Any]) -> HTTPRequest:
        """Build the request, using the server's next link verbatim when paging."""
        next_link: ParseResult | None = page.next_page_token
        if next_link:
            return HTTPRequest(url=next_link.geturl(), headers=self.http_headers)
        request = super().get_http_request(page=page)
        request.safe_query_chars = "$,'()"
        return request

    def replication_filter(self, context: Context | None) -> str | None:
        """Return the ``$filter`` clause for incremental replication, if any."""
        if not self.replication_key or self.replication_method != INCREMENTAL:
            return None
        key_type = self.schema["properties"].get(self.replication_key, {}).get("type")
        if key_type and "integer" in key_type:
            start = self.get_starting_replication_key_value(context)
            return None if start is None else f"{self.replication_key} gt {int(start)}"
        start_ts = self.get_starting_timestamp(context)
        if start_ts is None:
            return None
        # `ge` rather than `gt`: rows sharing the bookmark timestamp are re-read
        # and de-duplicated by key downstream instead of being missed.
        return f"{self.replication_key} ge {odata_datetime(start_ts)}"

    @override
    def get_url_params(
        self,
        context: Context | None,
        next_page_token: Any | None,
    ) -> dict[str, Any]:
        """Return OData query options for the first page."""
        params: dict[str, Any] = {}
        if clause := self.replication_filter(context):
            params["$filter"] = clause
        return params

    @override
    def validate_response(self, response: requests.Response) -> None:
        """Classify unavailable endpoints separately from real failures."""
        if is_unavailable(response):
            msg = self.response_error_message(response)
            raise StreamUnavailableError(msg, response)
        super().validate_response(response)

    @override
    def get_records(self, context: Context | None) -> Iterable[dict[str, Any]]:
        """Yield records, skipping endpoints Business Central reports as unavailable."""
        try:
            yield from super().get_records(context)
        except StreamUnavailableError as exc:
            status = exc.response.status_code
            self.log(
                "Skipping '%s' (HTTP %s): the endpoint is not available to this "
                "application or is not set up in this company. Check that the "
                "Business Central permission set grants read access, that the owning "
                "extension is installed and that the data exists. %s",
                self.name,
                status,
                exc,
                level=logging.WARNING,
            )

    @override
    def backoff_wait_generator(self) -> Generator[float, None, None]:
        """Honour ``Retry-After`` on throttled (429) and unavailable (503) responses."""

        def _wait(exception: Exception) -> float:
            response = getattr(exception, "response", None)
            retry_after = response.headers.get("Retry-After") if response is not None else None
            try:
                return float(retry_after) if retry_after else DEFAULT_RETRY_AFTER_SECONDS
            except ValueError:
                return DEFAULT_RETRY_AFTER_SECONDS

        return self.backoff_runtime(value=_wait)

    @override
    def backoff_max_tries(self) -> int:
        """Allow more retries than the default, since throttling windows are minutes long."""
        return 8

    @override
    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        """Parse records, keeping decimals exact."""
        self.send_activate_version()
        yield from extract_jsonpath(
            self.records_jsonpath,
            input=response.json(parse_float=decimal.Decimal),
        )

    @override
    def post_process(
        self,
        row: dict,
        context: Context | None = None,
    ) -> dict | None:
        """Drop OData annotations, stamp the owning company, fix the bookmark's width."""
        for key in [k for k in row if "@odata." in k]:
            row.pop(key)
        if context and "company_id" in context:
            row["company_id"] = context["company_id"]
        value = row.get(self.replication_key) if self.replication_key else None
        if isinstance(value, str):
            row[cast("str", self.replication_key)] = normalise_timestamp(value)
        return row
