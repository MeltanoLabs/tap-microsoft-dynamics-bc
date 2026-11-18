"""Parse OData CSDL (``$metadata``) documents into JSON schemas.

Business Central publishes an OData v4 ``$metadata`` document for every API
route (``api/v2.0``, ``api/{publisher}/{group}/{version}``) and for the
``ODataV4`` web services endpoint. The document describes the exact entities
and fields available in *that* environment, including fields added by
extensions, so schemas are always built from it rather than hard-coded.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from xml.etree import ElementTree as ET

EDMX_NS = "http://docs.oasis-open.org/odata/ns/edmx"
EDM_NS = "http://docs.oasis-open.org/odata/ns/edm"
NS = {"edmx": EDMX_NS, "edm": EDM_NS}

# Edm primitive type -> JSON schema fragment (nullability is added separately).
_PRIMITIVES: dict[str, dict[str, Any]] = {
    "Edm.String": {"type": "string"},
    "Edm.Guid": {"type": "string"},
    "Edm.Boolean": {"type": "boolean"},
    "Edm.Byte": {"type": "integer"},
    "Edm.SByte": {"type": "integer"},
    "Edm.Int16": {"type": "integer"},
    "Edm.Int32": {"type": "integer"},
    "Edm.Int64": {"type": "integer"},
    "Edm.Decimal": {"type": "number"},
    "Edm.Double": {"type": "number"},
    "Edm.Single": {"type": "number"},
    "Edm.Date": {"type": "string", "format": "date"},
    "Edm.DateTimeOffset": {"type": "string", "format": "date-time"},
    "Edm.TimeOfDay": {"type": "string", "format": "time"},
    "Edm.Duration": {"type": "string"},
    "Edm.Binary": {"type": "string"},
}

# Nested complex types deeper than this are kept as untyped values.
MAX_COMPLEX_DEPTH = 3

# Properties of these types carry binary content and are never extracted.
_SKIPPED_TYPES = {"Edm.Stream"}


@dataclasses.dataclass
class EntityType:
    """An OData entity type."""

    name: str
    keys: list[str]
    properties: dict[str, str]  # property name -> Edm type
    navigation: dict[str, tuple[str, bool]]  # nav name -> (target type, is collection)


@dataclasses.dataclass
class Metadata:
    """The parts of a ``$metadata`` document the tap needs."""

    entity_types: dict[str, EntityType]  # keyed by qualified name
    enum_types: set[str]
    complex_types: dict[str, dict[str, str]]
    entity_sets: dict[str, str]  # entity set name -> qualified entity type

    def entity_type(self, qualified_name: str) -> EntityType | None:
        """Return an entity type by qualified or short name."""
        if qualified_name in self.entity_types:
            return self.entity_types[qualified_name]
        short = qualified_name.rsplit(".", 1)[-1]
        for name, entity in self.entity_types.items():
            if name.rsplit(".", 1)[-1] == short:
                return entity
        return None

    def company_scoped_sets(self) -> dict[str, str]:
        """Return the entity sets that live under ``companies({id})``.

        Business Central models company-scoped entities as contained collection
        navigation properties of the ``company`` entity type. Older or custom
        routes may instead list them as top-level entity sets, so fall back to
        those when the company type exposes no collections.
        """
        company = self.entity_type("company")
        if company:
            contained = {
                name: target
                for name, (target, is_collection) in company.navigation.items()
                if is_collection
            }
            if contained:
                return contained
        return {name: t for name, t in self.entity_sets.items() if name != "companies"}


def _unwrap_collection(type_name: str) -> tuple[str, bool]:
    if type_name.startswith("Collection(") and type_name.endswith(")"):
        return type_name[len("Collection(") : -1], True
    return type_name, False


def parse_metadata(document: bytes | str) -> Metadata:
    """Parse a CSDL ``$metadata`` document."""
    root = ET.fromstring(document)  # noqa: S314 - authenticated Business Central response
    entity_types: dict[str, EntityType] = {}
    enum_types: set[str] = set()
    complex_types: dict[str, dict[str, str]] = {}
    entity_sets: dict[str, str] = {}

    for schema in root.iter(f"{{{EDM_NS}}}Schema"):
        namespace = schema.get("Namespace", "")
        alias = schema.get("Alias")

        def qualify(name: str, namespace: str = namespace) -> str:
            return f"{namespace}.{name}"

        for enum in schema.findall("edm:EnumType", NS):
            enum_types.add(qualify(enum.get("Name", "")))
            if alias:
                enum_types.add(f"{alias}.{enum.get('Name', '')}")

        for complex_type in schema.findall("edm:ComplexType", NS):
            props = {
                p.get("Name", ""): p.get("Type", "Edm.String")
                for p in complex_type.findall("edm:Property", NS)
            }
            complex_types[qualify(complex_type.get("Name", ""))] = props

        for entity in schema.findall("edm:EntityType", NS):
            keys = [ref.get("Name", "") for ref in entity.findall("edm:Key/edm:PropertyRef", NS)]
            props = {
                p.get("Name", ""): p.get("Type", "Edm.String")
                for p in entity.findall("edm:Property", NS)
            }
            navigation = {
                n.get("Name", ""): _unwrap_collection(n.get("Type", ""))
                for n in entity.findall("edm:NavigationProperty", NS)
            }
            entity_types[qualify(entity.get("Name", ""))] = EntityType(
                name=entity.get("Name", ""),
                keys=keys,
                properties=props,
                navigation=navigation,
            )

        for container in schema.findall("edm:EntityContainer", NS):
            for entity_set in container.findall("edm:EntitySet", NS):
                entity_sets[entity_set.get("Name", "")] = entity_set.get("EntityType", "")

    return Metadata(
        entity_types=entity_types,
        enum_types=enum_types,
        complex_types=complex_types,
        entity_sets=entity_sets,
    )


def _nullable(fragment: dict[str, Any]) -> dict[str, Any]:
    fragment = dict(fragment)
    json_type = fragment.get("type")
    if isinstance(json_type, str):
        fragment["type"] = [json_type, "null"]
    return fragment


def property_schema(
    edm_type: str,
    metadata: Metadata,
    _depth: int = 0,
) -> dict[str, Any] | None:
    """Return the JSON schema for an Edm type, or ``None`` to skip the property."""
    inner, is_collection = _unwrap_collection(edm_type)
    if inner in _SKIPPED_TYPES:
        return None
    if inner in _PRIMITIVES:
        fragment: dict[str, Any] = dict(_PRIMITIVES[inner])
    elif inner in metadata.enum_types or inner.rsplit(".", 1)[-1] in {
        e.rsplit(".", 1)[-1] for e in metadata.enum_types
    }:
        fragment = {"type": "string"}
    elif inner in metadata.complex_types and _depth < MAX_COMPLEX_DEPTH:
        fragment = {
            "type": "object",
            "properties": {
                name: schema
                for name, sub_type in metadata.complex_types[inner].items()
                if (schema := property_schema(sub_type, metadata, _depth + 1)) is not None
            },
        }
    else:
        # Unknown or deeply nested type: keep the value without constraining it.
        fragment = {}
    if is_collection:
        return {"type": ["array", "null"], "items": fragment}
    return _nullable(fragment) if fragment else fragment


def entity_schema(
    entity: EntityType,
    metadata: Metadata,
    descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a JSON schema for an entity type's structural properties."""
    descriptions = descriptions or {}
    properties: dict[str, Any] = {}
    for name, edm_type in entity.properties.items():
        schema = property_schema(edm_type, metadata)
        if schema is None:
            continue
        if description := descriptions.get(name):
            schema = {**schema, "description": description}
        properties[name] = schema
    return {"type": "object", "properties": properties}
