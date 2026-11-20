"""Composite ordered tuple keys and multiple suppliers of one alias."""

from __future__ import annotations

from extcontract import (
    DirectoryLibrary,
    extract_relation_bindings,
    parse_catalog,
    parse_contract,
)


def _pair_catalog():
    w = parse_contract({"version": "w", "fields": [
        {"field_id": "a", "name": "a", "type": {"kind": "INT", "bits": 16}},
        {"field_id": "b", "name": "b", "type": {"kind": "INT", "bits": 16}},
        {"field_id": "c", "name": "c", "type": {"kind": "INT", "bits": 16}},
    ]}, role="writer")
    r = parse_contract({"version": "r", "fields": [
        {"field_id": "a", "name": "a", "type": {"kind": "DECIMAL", "precision": 8, "scale": 0}},
        {"field_id": "b", "name": "b", "type": {"kind": "DECIMAL", "precision": 8, "scale": 0}},
        {"field_id": "c", "name": "c", "type": {"kind": "DECIMAL", "precision": 8, "scale": 0}},
    ]}, role="reader")
    raw = {
        "catalog_version": "c",
        "relations": [
            {"relation": "src", "writer": "w", "reader": "r",
             "provides": [{"name": "pair", "class": "pair", "fields": ["a", "b"]}],
             "requires": []},
            {"relation": "dst", "writer": "w", "reader": "r",
             "provides": [],
             "requires": [{"name": "need", "relation": "src", "class": "pair",
                           "fields": ["a", "b"]}]},
        ],
    }
    contracts = {(rel, role): c for rel in ("src", "dst")
                 for role, c in (("writer", w), ("reader", r))}
    return parse_catalog(raw, contracts), w, r


def test_composite_tuple_is_ordered_and_position_sensitive():
    catalog, w, r = _pair_catalog()
    from extcontract import adapt_rows
    result = adapt_rows(w, r, [
        {"row_id": "x", "values": {"a": 1, "b": 2, "c": 0}},
        {"row_id": "y", "values": {"a": 2, "b": 1, "c": 0}},
    ])
    extracted = extract_relation_bindings(catalog, "src", result.canonical_rows)
    keys = {rb.row_id: rb.provisions[0].key for rb in extracted.rows}
    assert keys["x"] != keys["y"]
    assert keys["x"].values == (1, 2) and keys["y"].values == (2, 1)


def test_two_rows_supplying_the_same_tuple_key_both_index():
    catalog, w, r = _pair_catalog()
    d = DirectoryLibrary(catalog)
    doc = {
        "file_id": "srcf", "writer_version": "w", "reader_version": "r",
        "rows": [
            {"row_id": "p1", "revision": 1, "values": {"a": 7, "b": 8, "c": 0}},
            {"row_id": "p2", "revision": 1, "values": {"a": 7, "b": 8, "c": 1}},
        ],
    }
    assert d.ingest_relation("src", doc, persist=False).accepted
    dst = {
        "file_id": "dstf", "writer_version": "w", "reader_version": "r",
        "rows": [{"row_id": "q", "revision": 1,
                  "values": {"a": 7, "b": 8, "c": 0}}],
    }
    assert d.ingest_relation("dst", dst, persist=False).accepted

    # Removing one supplier leaves the key alive; q survives.
    res = d.withdraw(
        {"request_id": "drop-p1", "relation": "src", "row_id": "p1"},
        persist=False,
    )
    assert res.ok and res.removed_rows == ["src/p1"]
    assert d.active_row_ids("dst") == ["q"]

    # Removing the last supplier cascades to q.
    res2 = d.withdraw(
        {"request_id": "drop-p2", "relation": "src", "row_id": "p2"},
        persist=False,
    )
    flat = [r for rd in res2.rounds for r in rd.removed_rows]
    assert "src/p2" in flat and "dst/q" in flat
    assert d.active_row_ids("dst") == []


def test_revision_swap_keeps_dependency_satisfied():
    catalog, w, r = _pair_catalog()
    d = DirectoryLibrary(catalog)
    d.ingest_relation("src", {
        "file_id": "s", "writer_version": "w", "reader_version": "r",
        "rows": [{"row_id": "p1", "revision": 1,
                  "values": {"a": 7, "b": 8, "c": 0}}],
    }, persist=False)
    d.ingest_relation("dst", {
        "file_id": "d", "writer_version": "w", "reader_version": "r",
        "rows": [{"row_id": "q", "revision": 1,
                  "values": {"a": 7, "b": 8, "c": 0}}],
    }, persist=False)
    # p1 moves onto another legal key: q must now reject (its provider vanished),
    # proving the catalog re-checks bindings on every revision.
    rejected = d.ingest_relation("dst", {
        "file_id": "d2", "writer_version": "w", "reader_version": "r",
        "rows": [{"row_id": "q", "revision": 2,
                  "values": {"a": 9, "b": 9, "c": 0}}],
    }, persist=False)
    assert not rejected.accepted
    assert "no remaining provider" in rejected.result.errors[0]
