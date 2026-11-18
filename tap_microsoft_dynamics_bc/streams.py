"""Stream type classes for tap-microsoft-dynamics-bc."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from singer_sdk.exceptions import FatalAPIError

from tap_microsoft_dynamics_bc.client import INCREMENTAL, BusinessCentralStream, odata_datetime

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Iterable

    import requests
    from singer_sdk.helpers.types import Context, Record

COMPANY_ID = {"type": ["string"], "description": "ID of the company the record belongs to."}


class CompaniesStream(BusinessCentralStream):
    """Companies in the environment; the parent of every company-scoped stream."""

    @override
    def get_records(self, context: Context | None) -> Iterable[dict[str, Any]]:
        """Yield companies, restricted to the configured ``company_ids`` if set."""
        wanted = {str(c).lower() for c in self.config.get("company_ids") or []}
        for record in super().get_records(context):
            identifiers = {
                str(record.get(field, "")).lower() for field in ("id", "name", "displayName")
            }
            if not wanted or wanted & identifiers:
                yield record

    @override
    def get_child_context(self, record: Record, context: Context | None) -> Context:
        """Pass the company's ID (API routes) and name (web services) to children."""
        return {"company_id": record["id"], "company_name": record.get("name")}


class CompanyEntityStream(BusinessCentralStream):
    """An entity set under ``companies({company_id})`` on an API route.

    Header streams (orders, invoices, journals...) can own line streams that
    Business Central only returns for a given document. Their lines are fetched
    with ``$expand`` on the header request and handed to the child stream, so
    lines cost no extra requests.

    An incremental header with an ``open_documents_filter`` (draft invoices and
    credit memos) also re-reads the documents matching that filter every run.
    Business Central only updates a header's ``lastModifiedDateTime`` for line
    changes that affect its totals, so an edit to, say, the description of an
    item line on a draft would otherwise be missed.
    """

    def __init__(
        self,
        *args: Any,
        open_documents_filter: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Create the stream and its (empty) expansion buffer."""
        super().__init__(*args, **kwargs)
        self.state_partitioning_keys = ["company_id"]
        self.open_documents_filter = open_documents_filter
        # Set while re-reading open documents: their modification time is before this.
        self._open_documents_before: str | None = None
        # navigation property -> parent record id -> (rows, next link)
        self._expanded: dict[str, dict[str, tuple[list[dict], str | None]]] = {}

    @property
    def expansions(self) -> list[str]:
        """Navigation properties to expand for selected line streams."""
        return [
            child.navigation_property
            for child in self.child_streams
            if isinstance(child, ExpandedLineStream)
            and (child.selected or child.has_selected_descendents)
        ]

    @override
    def get_url_params(
        self,
        context: Context | None,
        next_page_token: Any | None,
    ) -> dict[str, Any]:
        """Add ``$expand`` for selected line streams."""
        params = super().get_url_params(context, next_page_token)
        if expansions := self.expansions:
            params["$expand"] = ",".join(expansions)
        return params

    @override
    def replication_filter(self, context: Context | None) -> str | None:
        """Select unchanged open documents during the re-read pass."""
        if self._open_documents_before is not None and self.replication_key:
            return (
                f"{self.open_documents_filter} and "
                f"{self.replication_key} lt {self._open_documents_before}"
            )
        return super().replication_filter(context)

    def _open_documents_cutoff(self, context: Context | None) -> str | None:
        """Return the start of this run's incremental window if open documents are re-read.

        Open documents are only re-read to refresh their lines, so not when no
        line stream is selected. Without a start (first run, no ``start_date``)
        the incremental pass already reads every document.
        """
        if (
            not self.open_documents_filter
            or not self.replication_key
            or self.replication_method != INCREMENTAL
            or not self.expansions
        ):
            return None
        start = self.get_starting_timestamp(context)
        return None if start is None else odata_datetime(start)

    @override
    def get_records(self, context: Context | None) -> Iterable[dict[str, Any]]:
        """Yield changed documents, then open documents that did not change."""
        # Read the window start before the first pass moves the bookmark.
        cutoff = self._open_documents_cutoff(context)
        yield from super().get_records(context)
        if cutoff is None:
            return
        self._open_documents_before = cutoff
        try:
            yield from super().get_records(context)
        except FatalAPIError as exc:
            self.log(
                "Could not re-read open documents of '%s' (%s). Their lines are "
                "refreshed when the document next changes.",
                self.name,
                exc,
                level=logging.WARNING,
            )
        finally:
            self._open_documents_before = None

    @override
    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        """Move expanded line collections out of header records into the buffer."""
        expansions = self.expansions
        for record in super().parse_response(response):
            record_id = str(record.get("id", ""))
            for nav in expansions:
                rows = record.pop(nav, None)
                next_link = record.pop(f"{nav}@odata.nextLink", None)
                if rows is not None:
                    self._expanded.setdefault(nav, {})[record_id] = (rows, next_link)
            yield record

    def take_expanded(self, nav: str, record_id: str) -> tuple[list[dict], str | None] | None:
        """Remove and return buffered lines for one header record."""
        return self._expanded.get(nav, {}).pop(record_id, None)

    @override
    def get_child_context(self, record: Record, context: Context | None) -> Context:
        """Pass the company and the header's ID to line streams."""
        return {**(context or {}), "parent_id": record.get("id")}


class ExpandedLineStream(BusinessCentralStream):
    """Document lines that Business Central only lists for a given header.

    Records normally come from the parent's ``$expand``. If the expansion was
    truncated (``@odata.nextLink``) or missing, the lines are requested directly
    from ``{header}({id})/{navigation_property}``.
    """

    def __init__(self, *args: Any, navigation_property: str, **kwargs: Any) -> None:
        """Create the line stream for one header navigation property."""
        self.navigation_property = navigation_property
        super().__init__(*args, **kwargs)
        self.state_partitioning_keys = ["company_id"]

    @override
    def get_records(self, context: Context | None) -> Iterable[dict[str, Any]]:
        """Yield buffered lines, falling back to a direct request."""
        parent = self.parent_stream
        buffered = (
            parent.take_expanded(self.navigation_property, str((context or {}).get("parent_id")))
            if isinstance(parent, CompanyEntityStream)
            else None
        )
        if buffered is not None and buffered[1] is None:
            # The header request succeeded, so these are all of the document's lines.
            self.send_activate_version()
            # post_process() is applied by the SDK to every yielded record.
            yield from buffered[0]
            return
        yield from super().get_records(context)


class WebServiceStream(BusinessCentralStream):
    """A page or query published as an OData V4 web service.

    Web services are addressed by company *name*:
    ``ODataV4/Company('{name}')/{service}``. Use them for data the standard
    APIs do not expose, such as customised pages or extension tables.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Create the web service stream."""
        super().__init__(*args, **kwargs)
        self.state_partitioning_keys = ["company_id"]

    @override
    def get_url(self, context: Context | None) -> str:
        """Insert the OData-escaped company name into the URL."""
        name = str((context or {}).get("company_name") or "")
        escaped = quote(name.replace("'", "''"), safe="")
        return f"{self.url_base}{self.path.replace('{company_name}', escaped)}"
