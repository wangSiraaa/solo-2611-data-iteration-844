"""Pin the exact key-equality rules from the brief using DECIMAL writers.

DECIMAL transport accepts both "1" and "2/2"; after adaptation both are the
same reader-domain value and therefore the same binding key, while a TEXT
"1" remains a different kind and can never match a numeric key.
"""

from __future__ import annotations

from fractions import Fraction

from extcontract import (
    BindingKey,
    adapt_rows,
    extract_relation_bindings,
    parse_catalog,
    parse_contract,
)


def _decimal_catalog():
    writer = parse_contract(
        {"version": "w", "fields": [
            {"field_id": "v", "name": "v",
             "type": {"kind": "DECIMAL", "precision": 6, "scale": 0}}]},
        role="writer",
    )
    reader = parse_contract(
        {"version": "r", "fields": [
            {"field_id": "v", "name": "v",
             "type": {"kind": "DECIMAL", "precision": 8, "scale": 0}}]},
        role="reader",
    )
    raw = {
        "catalog_version": "c",
        "relations": [{
            "relation": "t", "writer": "w", "reader": "r",
            "provides": [{"name": "p", "class": "num", "fields": ["v"]}],
            "requires": [],
        }],
    }
    return parse_catalog(
        raw, {("t", "writer"): writer, ("t", "reader"): reader}
    ), writer, reader


def test_decimal_one_and_two_halves_are_the_same_binding_key():
    catalog, writer, reader = _decimal_catalog()
    result = adapt_rows(writer, reader, [
        {"row_id": "a", "values": {"v": "1"}},
        {"row_id": "b", "values": {"v": "2/2"}},
    ])
    assert result.status == "ADAPTED", result.errors
    extracted = extract_relation_bindings(
        catalog, "t", result.canonical_rows
    )
    assert extracted.ok
    keys = {rb.row_id: rb.provisions[0].key for rb in extracted.rows}
    assert keys["a"] == keys["b"]
    assert hash(keys["a"]) == hash(keys["b"])
    assert keys["a"].values == (Fraction(1),)


def test_text_one_never_matches_numeric_one():
    # Direct identity-level guarantee.
    numeric = BindingKey("k", (Fraction(1),))
    textual = BindingKey("k", ("1",))
    assert numeric != textual
    assert hash(numeric) != hash(textual)


def test_ordered_tuple_position_matters():
    assert BindingKey("k", (1, 2)) != BindingKey("k", (2, 1))
    assert BindingKey("k", (1, 2)) == BindingKey("k", (Fraction(1), Fraction(2)))
    assert BindingKey("k1", (1,)) != BindingKey("k2", (1,))
