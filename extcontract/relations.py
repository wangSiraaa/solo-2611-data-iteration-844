"""Named relations, public aliases, and cross-relation dependencies.

A *catalog* names several relations.  Each relation is served by exactly the
existing writer/reader contract pair (see :mod:`extcontract.adapter`); this
layer never re-validates values itself, it works strictly on rows that have
already been adapted into the reader domain.

Two kinds of named bindings are declared per relation:

``provides``  — a public alias class.  Every adapted row may publish one key
                per class.  A key is the ordered tuple of canonical reader
                values of the declared fields; several rows may publish the
                same key (a tag can have multiple suppliers).

``requires``  — a dependency on an alias class of another (or the same)
                relation.  The row's own adapted field tuple is looked up
                against the providers of the target class.

Binding identity is ``(relation name, alias class, ordered field tuple)`` and
the tuple holds *canonical reader-domain* values, so numeric ``1`` and
``"2/2"`` are the same key after adaptation while a TEXT ``"1"`` stays its
own kind.  A tuple containing any null publishes/looks up nothing: a null
provision simply does not exist, a null required dependency makes the row
unqualified, and a null *optional* dependency is skipped.

Nothing here can manufacture a reference: only declared source columns,
in-contract types and adapted in-domain values ever reach a binding, so an
undeclared column, a type mismatch or an out-of-contract value is rejected
upstream by the ordinary adaptation path and never becomes a key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .contract import Contract
from .errors import ContractError
from .types import TextType, rational_string

# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingDecl:
    """One alias-class declaration on a relation.

    ``kind`` is "provides" or "requires"; ``target`` is the relation that
    owns the consumed alias class (the relation itself for provisions).
    """

    name: str
    kind: str
    alias_class: str
    fields: tuple[str, ...]
    target: str
    optional: bool = False

    def to_json(self) -> dict:
        out = {
            "name": self.name,
            "class": self.alias_class,
            "fields": list(self.fields),
        }
        if self.kind == "requires":
            out["relation"] = self.target
            if self.optional:
                out["optional"] = True
        return out


@dataclass(frozen=True)
class RelationSpec:
    name: str
    writer: Contract
    reader: Contract
    provides: tuple[BindingDecl, ...]
    requires: tuple[BindingDecl, ...]

    def decls(self) -> tuple[BindingDecl, ...]:
        return self.provides + self.requires

    def to_json(self) -> dict:
        return {
            "relation": self.name,
            "writer_version": self.writer.version,
            "reader_version": self.reader.version,
            "provides": [d.to_json() for d in self.provides],
            "requires": [d.to_json() for d in self.requires],
        }


@dataclass(frozen=True)
class Catalog:
    version: str
    relations: tuple[RelationSpec, ...]

    def relation(self, name: str) -> RelationSpec:
        for spec in self.relations:
            if spec.name == name:
                return spec
        raise ContractError(f"catalog {self.version!r} has no relation {name!r}")

    def has_relation(self, name: str) -> bool:
        return any(spec.name == name for spec in self.relations)

    def to_json(self) -> dict:
        return {
            "catalog_version": self.version,
            "relations": [spec.to_json() for spec in self.relations],
        }


# ---------------------------------------------------------------------------
# Canonical keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingKey:
    """Hashable canonical tuple key: (alias class, ordered reader values)."""

    alias_class: str
    values: tuple[Any, ...]

    def to_json(self) -> dict:
        return {
            "class": self.alias_class,
            "tuple": [encode_domain_value(v) for v in self.values],
        }


def encode_domain_value(value: Any) -> list:
    """Tag a canonical value so its kind survives JSON transport."""
    if value is None:
        return ["NULL"]
    if isinstance(value, str):
        return ["TEXT", value]
    if isinstance(value, (int, Fraction)) and not isinstance(value, bool):
        return ["NUM", rational_string(value)]
    raise ContractError(f"cannot encode binding value {value!r}")


def decode_domain_value(tagged: list) -> Any:
    kind = tagged[0]
    if kind == "NULL":
        return None
    if kind == "TEXT":
        return tagged[1]
    if kind == "NUM":
        return Fraction(tagged[1])
    raise ContractError(f"unknown tagged value {tagged!r}")


def key_to_json(key: BindingKey) -> dict:
    return key.to_json()


def key_from_json(raw: dict) -> BindingKey:
    return BindingKey(
        alias_class=raw["class"],
        values=tuple(decode_domain_value(v) for v in raw["tuple"]),
    )


# ---------------------------------------------------------------------------
# Extracted bindings (per adapted row)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingEntry:
    """One provision or one (satisfied or not) dependency of a row."""

    name: str
    target_relation: str
    alias_class: str
    key: BindingKey
    fields: tuple[str, ...]
    optional: bool
    sources: tuple[dict, ...]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "relation": self.target_relation,
            "class": self.alias_class,
            "key": self.key.to_json(),
            "fields": list(self.fields),
            "optional": self.optional,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class SkippedRequirement:
    """An optional dependency whose tuple held a null and was skipped."""

    name: str
    target_relation: str
    alias_class: str
    fields: tuple[str, ...]
    sources: tuple[dict, ...]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "relation": self.target_relation,
            "class": self.alias_class,
            "fields": list(self.fields),
            "sources": list(self.sources),
            "reason": "optional dependency tuple contains null; skipped",
        }


@dataclass
class RowBindings:
    relation: str
    row_id: str
    revision: int
    provisions: list[BindingEntry] = field(default_factory=list)
    requirements: list[BindingEntry] = field(default_factory=list)
    skipped: list[SkippedRequirement] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "relation": self.relation,
            "row_id": self.row_id,
            "revision": self.revision,
            "provides": [b.to_json() for b in self.provisions],
            "requires": [b.to_json() for b in self.requirements],
            "skipped_requires": [s.to_json() for s in self.skipped],
        }


@dataclass
class ExtractionResult:
    status: str  # "ADAPTED" | "INVALID"
    relation: str
    rows: list[RowBindings]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "ADAPTED"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "relation": self.relation,
            "errors": list(self.errors),
            "rows": [r.to_json() for r in self.rows],
        }


# ---------------------------------------------------------------------------
# Catalog parsing
# ---------------------------------------------------------------------------

_TOP_KEYS = {"catalog_version", "relations"}
_REL_KEYS = {"relation", "writer", "reader", "provides", "requires"}
_PROV_KEYS = {"name", "class", "fields"}
_REQ_KEYS = {"name", "relation", "class", "fields", "optional"}


def parse_catalog(
    raw: Any, contracts: dict[tuple[str, str], Contract] | None = None
) -> Catalog:
    """Parse a catalog document.

    ``contracts`` maps ``(relation, "writer"|"reader")`` to already-parsed
    contracts; each relation's ``writer``/``reader`` entry must then be the
    matching version string.  This keeps contract *parsing* (and its whole
    domain validation) in the existing contract module.
    """
    contracts = contracts or {}
    if not isinstance(raw, dict):
        raise ContractError("catalog must be a JSON object")
    extra = set(raw) - _TOP_KEYS
    missing = _TOP_KEYS - set(raw)
    if extra:
        raise ContractError(f"unexpected catalog keys: {sorted(extra)}")
    if missing:
        raise ContractError(f"missing catalog keys: {sorted(missing)}")
    version = raw["catalog_version"]
    if not isinstance(version, str) or not version:
        raise ContractError("catalog_version must be a non-empty string")
    rels_raw = raw["relations"]
    if not isinstance(rels_raw, list) or not rels_raw:
        raise ContractError("catalog relations must be a non-empty list")

    specs: list[RelationSpec] = []
    seen_names: set[str] = set()
    for index, item in enumerate(rels_raw):
        specs.append(_parse_relation(item, index, contracts, seen_names))

    catalog = Catalog(version=version, relations=tuple(specs))
    _validate_cross_references(catalog)
    return catalog


def _parse_relation(item, index, contracts, seen_names) -> RelationSpec:
    if not isinstance(item, dict):
        raise ContractError(f"relation #{index} must be an object")
    extra = set(item) - _REL_KEYS
    if extra:
        raise ContractError(f"relation #{index}: unexpected keys {sorted(extra)}")
    required = {"relation", "writer", "reader"}
    if required - set(item):
        raise ContractError(
            f"relation #{index}: missing keys {sorted(required - set(item))}"
        )
    name = item["relation"]
    if not isinstance(name, str) or not name:
        raise ContractError(f"relation #{index}: relation name must be a non-empty string")
    if name in seen_names:
        raise ContractError(f"duplicate relation name {name!r}")
    seen_names.add(name)

    writer = _resolve_contract(contracts, name, "writer", item["writer"])
    reader = _resolve_contract(contracts, name, "reader", item["reader"])
    if writer.role != "writer" or reader.role != "reader":
        raise ContractError(f"relation {name!r}: contract roles are wired incorrectly")

    provides = _parse_decls(
        item.get("provides", []), name, "provides", reader, index
    )
    requires = _parse_decls(
        item.get("requires", []), name, "requires", reader, index
    )
    return RelationSpec(
        name=name, writer=writer, reader=reader,
        provides=tuple(provides), requires=tuple(requires),
    )


def _resolve_contract(
    contracts: dict[tuple[str, str], Contract],
    relation: str,
    role: str,
    ref: Any,
) -> Contract:
    if not isinstance(ref, str) or not ref:
        raise ContractError(
            f"relation {relation!r}: {role} must be a contract version string"
        )
    contract = contracts.get((relation, role))
    if contract is None:
        raise ContractError(
            f"relation {relation!r}: no {role} contract supplied for version {ref!r}"
        )
    if contract.version != ref:
        raise ContractError(
            f"relation {relation!r}: catalog declares {role} {ref!r} but the "
            f"supplied contract is {contract.version!r}"
        )
    return contract


def _parse_decls(raw, relation, kind, reader: Contract, rel_index):
    label = "provides" if kind == "provides" else "requires"
    if not isinstance(raw, list):
        raise ContractError(f"relation {relation!r}: {label} must be a list")
    allowed = _PROV_KEYS if kind == "provides" else _REQ_KEYS
    reader_fields = reader.field_by_id()
    decls: list[BindingDecl] = []
    names: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ContractError(f"relation {relation!r}.{label}[{i}] must be an object")
        extra = set(entry) - allowed
        if extra:
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: unexpected keys {sorted(extra)}"
            )
        if {"name", "class", "fields"} - set(entry):
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: needs name/class/fields"
            )
        decl_name = entry["name"]
        if not isinstance(decl_name, str) or not decl_name:
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: name must be a non-empty string"
            )
        if decl_name in names:
            raise ContractError(
                f"relation {relation!r}: duplicate binding name {decl_name!r}"
            )
        names.add(decl_name)
        alias_class = entry["class"]
        if not isinstance(alias_class, str) or not alias_class:
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: class must be a non-empty string"
            )
        fields = entry["fields"]
        if not isinstance(fields, list) or not fields:
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: fields must be a non-empty list"
            )
        if any(not isinstance(f, str) or not f for f in fields):
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: field ids must be non-empty strings"
            )
        if len(set(fields)) != len(fields):
            raise ContractError(
                f"relation {relation!r}.{label}[{i}]: duplicate field in ordered tuple"
            )
        for f in fields:
            if f not in reader_fields:
                raise ContractError(
                    f"relation {relation!r}.{label}[{i}]: field {f!r} is not a "
                    f"reader column of the relation"
                )
        optional = False
        target = relation
        if kind == "requires":
            target = entry.get("relation", relation)
            if not isinstance(target, str) or not target:
                raise ContractError(
                    f"relation {relation!r}.{label}[{i}]: target relation must be a string"
                )
            optional = entry.get("optional", False)
            if not isinstance(optional, bool):
                raise ContractError(
                    f"relation {relation!r}.{label}[{i}]: optional must be a bool"
                )
        # A class may legitimately recur (once among provisions and again among
        # requirements of the same relation); the binding *name* is the unique
        # local handle, so only duplicate names reject.
        decls.append(
            BindingDecl(
                name=decl_name,
                kind=kind,
                alias_class=alias_class,
                fields=tuple(fields),
                target=target,
                optional=optional,
            )
        )
    return decls


def _validate_cross_references(catalog: Catalog) -> None:
    """Every requirement must target an existing alias class with matching arity.

    Position-wise *kind* compatibility is also enforced: TEXT only matches
    TEXT (a TEXT ``"1"`` is never a numeric key), while INT/DECIMAL positions
    may pair because canonical numeric values compare across the two kinds.
    """
    providers: dict[str, RelationSpec] = {}
    for spec in catalog.relations:
        for decl in spec.provides:
            providers[(spec.name, decl.alias_class)] = spec

    def field_types(spec: RelationSpec, field_ids: tuple[str, ...]):
        by_id = spec.reader.field_by_id()
        return [by_id[f].type for f in field_ids]

    for spec in catalog.relations:
        for req in spec.requires:
            owner = providers.get((req.target, req.alias_class))
            if owner is None:
                raise ContractError(
                    f"relation {spec.name!r} requires {req.target!r} class "
                    f"{req.alias_class!r}, which no relation provides"
                )
            prov_decl = next(
                d for d in owner.provides if d.alias_class == req.alias_class
            )
            if len(prov_decl.fields) != len(req.fields):
                raise ContractError(
                    f"relation {spec.name!r}.{req.name}: tuple arity "
                    f"{len(req.fields)} does not match provider class "
                    f"{req.alias_class!r} arity {len(prov_decl.fields)}"
                )
            req_types = field_types(spec, req.fields)
            prov_types = field_types(owner, prov_decl.fields)
            for pos, (rt, pt) in enumerate(zip(req_types, prov_types)):
                rt_text = isinstance(rt, TextType)
                pt_text = isinstance(pt, TextType)
                if rt_text != pt_text:
                    raise ContractError(
                        f"relation {spec.name!r}.{req.name} position {pos}: "
                        f"TEXT and numeric kinds can never be the same key "
                        f"({rt.describe()} vs {pt.describe()})"
                    )


def load_catalog(
    path: str,
    contract_paths: dict[tuple[str, str], str],
) -> Catalog:
    """Load a catalog document and each referenced contract from local files."""
    import json
    from pathlib import Path

    from .contract import load_contract

    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    contracts: dict[tuple[str, str], Contract] = {}
    for (relation, role), contract_path in contract_paths.items():
        contracts[(relation, role)] = load_contract(
            contract_path, role=role
        )
    return parse_catalog(raw, contracts)


# ---------------------------------------------------------------------------
# Binding extraction from adapted rows
# ---------------------------------------------------------------------------


def _binding_source(sources: dict, field_id: str, decl: BindingDecl) -> dict:
    """Preserve the per-column provenance the adapter already produced."""
    base = sources.get(field_id)
    if isinstance(base, dict):
        out = dict(base)
    else:
        out = {"field_id": field_id}
    out["binding"] = decl.name
    out["binding_kind"] = decl.kind
    out["tuple_position"] = decl.fields.index(field_id)
    return out


class _RowView:
    """Common accessor over adapter CanonicalRow and ingest RowState."""

    def __init__(self, row: Any):
        self.row_id = row.row_id
        self.revision = row.revision
        self.domain = row.domain_values if hasattr(row, "domain_values") else row.canonical
        self.sources = row.sources


def extract_relation_bindings(
    catalog: Catalog, relation: str, rows: list[Any]
) -> ExtractionResult:
    """Derive provisions/dependencies from rows already in the reader domain.

    Only adapted rows are accepted, so every value here has already passed
    writer-domain validation, the exact conversion plan and the reader-domain
    guards.  This function therefore cannot create a reference from an
    undeclared column, a bad type or an out-of-contract value.
    """
    spec = catalog.relation(relation)
    errors: list[str] = []
    result_rows: list[RowBindings] = []

    for raw_row in rows:
        view = _RowView(raw_row)
        rb = RowBindings(
            relation=relation, row_id=view.row_id, revision=view.revision
        )
        for decl in spec.provides:
            _build_entry(spec, decl, view, rb, errors, optional_propagates=False)
        for decl in spec.requires:
            _build_entry(spec, decl, view, rb, errors, optional_propagates=True)
        result_rows.append(rb)

    return ExtractionResult(
        status="ADAPTED" if not errors else "INVALID",
        relation=relation,
        rows=result_rows,
        errors=errors,
    )


def _build_entry(
    spec: RelationSpec,
    decl: BindingDecl,
    view: _RowView,
    rb: RowBindings,
    errors: list[str],
    *,
    optional_propagates: bool,
) -> None:
    for f in decl.fields:
        if f not in view.domain:
            errors.append(
                f"row {view.row_id!r}.{decl.name}: binding field {f!r} is not a "
                f"reader column of relation {spec.name!r}"
            )
            return
    values = tuple(view.domain[f] for f in decl.fields)
    sources = tuple(_binding_source(view.sources, f, decl) for f in decl.fields)

    if any(v is None for v in values):
        if decl.kind == "requires" and decl.optional:
            rb.skipped.append(
                SkippedRequirement(
                    name=decl.name,
                    target_relation=decl.target,
                    alias_class=decl.alias_class,
                    fields=decl.fields,
                    sources=sources,
                )
            )
            return
        where = "provision" if decl.kind == "provides" else "required dependency"
        errors.append(
            f"row {view.row_id!r}.{decl.name}: null column in {where} tuple "
            f"{list(decl.fields)}; no key is published"
            + (" and the dependency is not optional" if decl.kind == "requires" else "")
        )
        return

    entry = BindingEntry(
        name=decl.name,
        target_relation=decl.target,
        alias_class=decl.alias_class,
        key=BindingKey(alias_class=decl.alias_class, values=values),
        fields=decl.fields,
        optional=decl.optional,
        sources=sources,
    )
    if decl.kind == "provides":
        rb.provisions.append(entry)
    else:
        rb.requirements.append(entry)


# ---------------------------------------------------------------------------
# Provider index and qualification
# ---------------------------------------------------------------------------


def build_provider_index(
    catalog: Catalog, bindings: dict[str, list[RowBindings]]
) -> dict[tuple[str, str], dict[BindingKey, list[str]]]:
    """Map (relation, alias class) -> key -> sorted provider row ids."""
    index: dict[tuple[str, str], dict[BindingKey, list[str]]] = {}
    for relation, rows in bindings.items():
        spec = catalog.relation(relation)
        for decl in spec.provides:
            index.setdefault((relation, decl.alias_class), {})
    for relation, rows in bindings.items():
        for rb in rows:
            for entry in rb.provisions:
                bucket = index[(relation, entry.alias_class)]
                bucket.setdefault(entry.key, [])
                if rb.row_id not in bucket[entry.key]:
                    bucket[entry.key].append(rb.row_id)
    for bucket in index.values():
        for rows_for_key in bucket.values():
            rows_for_key.sort()
    return index


def qualification_errors(
    catalog: Catalog,
    bindings: dict[str, list[RowBindings]],
    *,
    only_relation: str | None = None,
) -> list[str]:
    """Rows whose non-skipped required keys have no remaining provider.

    A null optional tuple was already skipped during extraction; an optional
    dependency that carries a real tuple still needs a provider, exactly like
    a required one.
    """
    index = build_provider_index(catalog, bindings)
    errors: list[str] = []
    relations = [only_relation] if only_relation else list(bindings)
    for relation in relations:
        if relation not in bindings:
            continue
        for rb in bindings[relation]:
            for req in rb.requirements:
                providers = index.get((req.target_relation, req.alias_class), {})
                if req.key not in providers:
                    errors.append(
                        f"row {rb.row_id!r} of relation {relation!r}: required "
                        f"binding {req.name!r} ({req.target_relation}/"
                        f"{req.alias_class} {req.key.to_json()['tuple']}) has no "
                        f"remaining provider"
                    )
    return errors
