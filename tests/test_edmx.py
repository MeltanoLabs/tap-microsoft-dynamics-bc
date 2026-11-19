"""Tests for reading company-scoped entity sets out of ``$metadata``."""

from __future__ import annotations

from tap_microsoft_dynamics_bc.edmx import SYSTEM_ENTITY_SETS, parse_metadata

_SYSTEM_SETS = "".join(
    f'<EntitySet Name="{name}" EntityType="Microsoft.NAV.{name}"/>'
    for name in sorted(SYSTEM_ENTITY_SETS)
)


def _metadata(company_navigation: str | None, extra_sets: str = "") -> str:
    company = (
        ""
        if company_navigation is None
        else (
            '<EntityType Name="company"><Key><PropertyRef Name="id"/></Key>'
            '<Property Name="id" Type="Edm.Guid" Nullable="false"/>'
            f"{company_navigation}</EntityType>"
        )
    )
    return (
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">'
        "<edmx:DataServices>"
        '<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="Microsoft.NAV">'
        f"{company}"
        '<EntityType Name="ledgerEntry"><Key><PropertyRef Name="id"/></Key>'
        '<Property Name="id" Type="Edm.Guid" Nullable="false"/>'
        '<Property Name="companyId" Type="Edm.Guid"/></EntityType>'
        '<EntityContainer Name="default">'
        '<EntitySet Name="companies" EntityType="Microsoft.NAV.company"/>'
        f"{_SYSTEM_SETS}{extra_sets}"
        "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    )


def test_company_collections_are_the_scoped_sets() -> None:
    metadata = parse_metadata(
        _metadata(
            '<NavigationProperty Name="ledgerEntries" '
            'Type="Collection(Microsoft.NAV.ledgerEntry)" ContainsTarget="true"/>'
            '<NavigationProperty Name="parent" Type="Microsoft.NAV.company"/>',
        ),
    )

    assert metadata.company_scoped_sets() == {"ledgerEntries": "Microsoft.NAV.ledgerEntry"}


def test_company_type_without_collections_has_no_scoped_sets() -> None:
    # The shape of api/microsoft/analytics/beta seen live (CRONUS trial, 2026-11):
    # a company type with no collections, and only the system sets at the top
    # level. Falling back to those produced streams that answer 404.
    metadata = parse_metadata(_metadata(""))

    assert metadata.company_scoped_sets() == {}


def test_fallback_without_company_type_skips_system_sets() -> None:
    metadata = parse_metadata(
        _metadata(
            None,
            extra_sets='<EntitySet Name="ledgerEntries" EntityType="Microsoft.NAV.ledgerEntry"/>',
        ),
    )

    assert metadata.company_scoped_sets() == {"ledgerEntries": "Microsoft.NAV.ledgerEntry"}
