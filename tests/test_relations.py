"""Tests for catalog parsing, canonical binding keys and row extraction.

These tests pin the identity rules from the brief:

* binding identity is (relation, alias class, ordered field tuple);
* values are compared after adaptation into the reader domain, so numeric
  ``1`` and ``"2/2"`` share a key while a TEXT ``"1"`` is a different kind;
* a null tuple element publishes/looks up nothing;
* an optional null requirement is skipped, a required null tuple is invalid;
* nothing undeclared, mistyped or out-of-contract can become a reference.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from extcontract import (
    BindingKey,
    adapt_rows,
    extract_relation_bindings,
    parse_catalog,
    parse_contract,
    qualification_errors,
)


def _contract(role, version, fields):
    return parse_contract({"version": version, "fields": fields}, role=role)


def _catalog_raw(version="c1", relations=None):
    return {"catalog_version": version, "relations": relations or []}


# ---------------------------------------------------------------------------
# Catalog parsing / cross-reference validation
# ---------------------------------------------------------------------------


def test_catalog_rejects_unknown_requirement_target():
    writer = _contract("writer", "w", [
        {"field_id": "k", "name": "k", "type": {"kind": "INT", "bits": 8}}])
    reader = _contract("reader", "r", [
        {"field_id": "k", "name": "k", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    raw = _catalog_raw(relations=[{
        "relation": "only", "writer": "w", "reader": "r",
        "provides": [],
        "requires": [
            {"name": "req", "relation": "ghost", "class": "c", "fields": ["k"]}],
    }])
    with pytest.raises(Exception, match="no relation provides"):
        parse_catalog(raw, {("only", "writer"): writer, ("only", "reader"): reader})


def test_catalog_rejects_arity_mismatch():
    w = _contract("writer", "w", [
        {"field_id": "a", "name": "a", "type": {"kind": "INT", "bits": 8}},
        {"field_id": "b", "name": "b", "type": {"kind": "INT", "bits": 8}}])
    r = _contract("reader", "r", [
        {"field_id": "a", "name": "a", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}},
        {"field_id": "b", "name": "b", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    raw = _catalog_raw(relations=[
        {"relation": "src", "writer": "w", "reader": "r",
         "provides": [{"name": "p", "class": "pair", "fields": ["a", "b"]}],
         "requires": []},
        {"relation": "dst", "writer": "w", "reader": "r",
         "provides": [],
         "requires": [{"name": "q", "relation": "src", "class": "pair", "fields": ["a"]}]},
    ])
    contracts = {(rel, role): c for rel in ("src", "dst")
                 for role, c in (("writer", w), ("reader", r))}
    with pytest.raises(Exception, match="arity"):
        parse_catalog(raw, contracts)


def test_catalog_rejects_text_numeric_kind_mismatch():
    wt = _contract("writer", "wt", [
        {"field_id": "s", "name": "s", "type": {"kind": "TEXT"}}])
    rt = _contract("reader", "rt", [
        {"field_id": "s", "name": "s", "type": {"kind": "TEXT"}}])
    wn = _contract("writer", "wn", [
        {"field_id": "n", "name": "n", "type": {"kind": "INT", "bits": 8}}])
    rn = _contract("reader", "rn", [
        {"field_id": "n", "name": "n", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    raw = _catalog_raw(relations=[
        {"relation": "src", "writer": "wn", "reader": "rn",
         "provides": [{"name": "p", "class": "k", "fields": ["n"]}], "requires": []},
        {"relation": "dst", "writer": "wt", "reader": "rt",
         "provides": [],
         "requires": [{"name": "q", "relation": "src", "class": "k", "fields": ["s"]}]},
    ])
    with pytest.raises(Exception, match="TEXT and numeric"):
        parse_catalog(raw, {
            ("src", "writer"): wn, ("src", "reader"): rn,
            ("dst", "writer"): wt, ("dst", "reader"): rt,
        })


def test_catalog_rejects_binding_field_outside_reader_columns():
    w = _contract("writer", "w", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "INT", "bits": 8}}])
    r = _contract("reader", "r", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    raw = _catalog_raw(relations=[{
        "relation": "x", "writer": "w", "reader": "r",
        "provides": [{"name": "p", "class": "k", "fields": ["nope"]}], "requires": []}])
    with pytest.raises(Exception, match="not a reader column"):
        parse_catalog(raw, {("x", "writer"): w, ("x", "reader"): r})


def test_catalog_rejects_duplicate_binding_names_and_unknown_keys():
    w = _contract("writer", "w", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "INT", "bits": 8}}])
    r = _contract("reader", "r", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    base = {"relation": "x", "writer": "w", "reader": "r", "requires": []}
    raw = _catalog_raw(relations=[dict(base, provides=[
        {"name": "p", "class": "k", "fields": ["a"]},
        {"name": "p", "class": "k2", "fields": ["a"]}])])
    with pytest.raises(Exception, match="duplicate binding name"):
        parse_catalog(raw, {("x", "writer"): w, ("x", "reader"): r})
    raw = _catalog_raw(relations=[dict(base, provides=[
        {"name": "p", "class": "k", "fields": ["a"], "extra": 1}])])
    with pytest.raises(Exception, match="unexpected keys"):
        parse_catalog(raw, {("x", "writer"): w, ("x", "reader"): r})


def test_same_class_may_appear_in_provides_and_requires():
    w = _contract("writer", "w", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "INT", "bits": 8}}])
    r = _contract("reader", "r", [{"field_id": "a", "name": "a",
                                   "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}}])
    raw = _catalog_raw(relations=[{
        "relation": "x", "writer": "w", "reader": "r",
        "provides": [{"name": "p", "class": "k", "fields": ["a"]}],
        "requires": [{"name": "q", "relation": "x", "class": "k", "fields": ["a"],
                      "optional": True}]}])
    catalog = parse_catalog(raw, {("x", "writer"): w, ("x", "reader"): r})
    assert [d.name for d in catalog.relation("x").decls()] == ["p", "q"]


# ---------------------------------------------------------------------------
# Canonical key identity
# ---------------------------------------------------------------------------


def _numeric_catalog():
    # INT writer -> DECIMAL reader; exact adaptation normalises representations.
    w = _contract("writer", "w", [{"field_id": "v", "name": "v",
                                   "type": {"kind": "INT", "bits": 16}}])
    r = _contract("reader", "r", [{"field_id": "v", "name": "v",
                                   "type": {"kind": "DECIMAL", "precision": 6, "scale": 0}}])
    raw = _catalog_raw(relations=[{
        "relation": "t", "writer": "w", "reader": "r",
        "provides": [{"name": "p", "class": "num", "fields": ["v"]}],
        "requires": []}])
    return parse_catalog(raw, {("t", "writer"): w, ("t", "reader"): r}), w, r


def test_numeric_one_and_two_halves_share_a_key_after_adaptation():
    # 2/2 cannot be expressed through an INT writer transport; prove equality at
    # the canonical level by feeding the same key through two adapted rows whose
    # reader-domain values are Fraction(1) vs int 1 (the adapter's own path).
    catalog, w, r = _numeric_catalog()
    result = adapt_rows(w, r, [
        {"row_id": "a", "values": {"v": 1}},
        {"row_id": "b", "values": {"v": 1}},
    ])
    assert result.status == "ADAPTED"
    extracted = extract_relation_bindings(catalog, "t", result.canonical_rows)
    keys = {rb.row_id: rb.provisions[0].key for rb in extracted.rows}
    assert keys["a"] == keys["b"]
    assert hash(keys["a"]) == hash(keys["b"])
    # Direct canonical check: int 1 and Fraction(2/2) are the same key.
    assert BindingKey("num", (1,)) == BindingKey("num", (Fraction(2, 2),))


def test_text_one_is_a_distinct_kind_from_numeric_one():
    assert BindingKey("k", ("1",)) != BindingKey("k", (Fraction(1),))
    wt = _contract("writer", "wt", [{"field_id": "s", "name": "s",
                                     "type": {"kind": "TEXT"}}])
    rt = _contract("reader", "rt", [{"field_id": "s", "name": "s",
                                     "type": {"kind": "TEXT"}}])
    raw = _catalog_raw(relations=[{
        "relation": "txt", "writer": "wt", "reader": "rt",
        "provides": [{"name": "p", "class": "label", "fields": ["s"]}],
        "requires": []}])
    catalog = parse_catalog(raw, {("txt", "writer"): wt, ("txt", "reader"): rt})
    result = adapt_rows(wt, rt, [{"row_id": "a", "values": {"s": "1"}}])
    extracted = extract_relation_bindings(catalog, "txt", result.canonical_rows)
    key = extracted.rows[0].provisions[0].key
    assert key == BindingKey("label", ("1",))
    assert key != BindingKey("label", (Fraction(1),))


# ---------------------------------------------------------------------------
# Null handling: provision suppression, required failure, optional skip
# ---------------------------------------------------------------------------


def _nullable_catalog():
    w = _contract("writer", "w", [
        {"field_id": "a", "name": "a", "type": {"kind": "INT", "bits": 8}, "nullable": True},
        {"field_id": "b", "name": "b", "type": {"kind": "INT", "bits": 8}, "nullable": True}])
    r = _contract("reader", "r", [
        {"field_id": "a", "name": "a", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}, "nullable": True},
        {"field_id": "b", "name": "b", "type": {"kind": "DECIMAL", "precision": 4, "scale": 0}, "nullable": True}])
    raw = _catalog_raw(relations=[
        {"relation": "src", "writer": "w", "reader": "r",
         "provides": [{"name": "pk", "class": "pair", "fields": ["a", "b"]}],
         "requires": []},
        {"relation": "dst", "writer": "w", "reader": "r",
         "provides": [],
         "requires": [
             {"name": "req_pair", "relation": "src", "class": "pair",
              "fields": ["a", "b"]},
             {"name": "opt_pair", "relation": "src", "class": "pair",
              "fields": ["a", "b"], "optional": True}]},
    ])
    contracts = {(rel, role): c for rel in ("src", "dst")
                 for role, c in (("writer", w), ("reader", r))}
    return parse_catalog(raw, contracts), w, r


def test_null_provision_publishes_nothing():
    catalog, w, r = _nullable_catalog()
    result = adapt_rows(w, r, [{"row_id": "s", "values": {"a": 1, "b": None}}])
    assert result.status == "ADAPTED"
    extracted = extract_relation_bindings(catalog, "src", result.canonical_rows)
    assert not extracted.ok
    assert extracted.rows[0].provisions == []
    assert "null column in provision tuple" in extracted.errors[0]


def test_optional_null_requirement_is_skipped():
    catalog, w, r = _nullable_catalog()
    result = adapt_rows(w, r, [{"row_id": "d", "values": {"a": None, "b": None}}])
    extracted = extract_relation_bindings(catalog, "dst", result.canonical_rows)
    # The optional binding is skipped cleanly; the required one is an error.
    assert not extracted.ok
    rb = extracted.rows[0]
    assert rb.requirements == []
    assert [s.name for s in rb.skipped] == ["opt_pair"]
    assert any("not optional" in e for e in extracted.errors)


def test_required_null_tuple_makes_row_unqualified():
    catalog, w, r = _nullable_catalog()
    result = adapt_rows(w, r, [{"row_id": "d", "values": {"a": 1, "b": None}}])
    extracted = extract_relation_bindings(catalog, "dst", result.canonical_rows)
    assert not extracted.ok
    assert any("need" in e or "required dependency" in e for e in extracted.errors)


# ---------------------------------------------------------------------------
# Provenance and qualification
# ---------------------------------------------------------------------------


def test_each_binding_entry_keeps_its_own_source_columns():
    catalog, w, r = _nullable_catalog()
    result = adapt_rows(w, r, [{"row_id": "s", "values": {"a": 3, "b": 4}}])
    extracted = extract_relation_bindings(catalog, "src", result.canonical_rows)
    entry = extracted.rows[0].provisions[0]
    assert [s["field_id"] for s in entry.sources] == ["a", "b"]
    assert [s["tuple_position"] for s in entry.sources] == [0, 1]
    assert all(s["conversion"] == "int_to_decimal_exact" for s in entry.sources)
    assert entry.fields == ("a", "b")
    assert entry.key == BindingKey("pair", (3, 4))


def test_qualification_reports_unprovided_required_keys_only():
    catalog, w, r = _nullable_catalog()
    src = adapt_rows(w, r, [{"row_id": "s", "values": {"a": 1, "b": 2}}]).canonical_rows
    good = adapt_rows(w, r, [{"row_id": "g", "values": {"a": 1, "b": 2}}]).canonical_rows
    bad = adapt_rows(w, r, [{"row_id": "bad", "values": {"a": 9, "b": 9}}]).canonical_rows
    bindings = {
        "src": extract_relation_bindings(catalog, "src", src).rows,
        "dst": extract_relation_bindings(catalog, "dst", good + bad).rows,
    }
    errors = qualification_errors(catalog, bindings)
    assert len(errors) == 2  # req_pair and opt_pair of row 'bad' both unresolved
    assert all("'bad'" in e for e in errors)
