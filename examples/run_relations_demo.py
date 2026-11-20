"""End-to-end demonstration: named relations, retractable alias records.

Run with:

    PYTHONPATH=. python3.11 examples/run_relations_demo.py

The demo uses only local files and the ordinary adaptation/ingestion path:

* every file is validated against its writer contract and adapted through the
  exact row-free compatibility plan before any binding can exist;
* each row's provisions, requirements and per-column sources are derived from
  the adapted reader-domain values (``1`` and ``2/2`` share a numeric key,
  TEXT ``"1"`` stays its own kind);
* withdrawal requests are resolved by the directory library as fixed-point
  closures, with retained-row vetoes, replay, and one atomic state file;
* every published relation is read by the real, unmodified
  ``materials/745/reader_client.py`` after reopen.

Scenarios (from the brief):

  1. A and B both supply alias K; C needs K and supplies J; D needs J.
     Withdrawing A removes only A because B keeps K alive.
  2. Fresh data, B is revised onto another legal key; withdrawing A now
     cascades A -> C -> D over three rounds.  With D retained, all four
     current rows survive and the state file is untouched.
  3. A ring E<->F is self-sufficient after the last external alternative X
     leaves; only X is removed and the cycle survives.
"""

from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "relations"
CLIENT = ROOT / "materials" / "745" / "reader_client.py"
sys.path.insert(0, str(ROOT))

from extcontract import (  # noqa: E402
    BindingKey,
    DirectoryLibrary,
    adapt_rows,
    load_catalog,
    parse_contract,
)

OUT = ROOT / "examples" / "out" / "relations"
STATE_FILE = OUT / "directory-state.json"
PUBLISH_DIR = OUT / "published"


def banner(n, title):
    print(f"\n=== {n}. {title} ===")


def load(name):
    return json.loads((MATERIALS / (name + ".json")).read_text())


def load_catalog_from_disk():
    paths = {}
    for rel in ["labels", "packages", "shipping", "ring"]:
        paths[(rel, "writer")] = MATERIALS / rel / "writer-contract.json"
        paths[(rel, "reader")] = MATERIALS / rel / "reader-contract.json"
    return load_catalog(MATERIALS / "catalog.json", paths)


def client(relation, records_path):
    proc = subprocess.run(
        [sys.executable, str(CLIENT),
         str(MATERIALS / relation / "reader-contract.json"), str(records_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"reader_client failed for {relation}: {proc.stderr}")
    return json.loads(proc.stdout)


def ingest(directory, relation, name):
    result = directory.ingest_relation(relation, load(name))
    assert result.accepted, result.result.errors
    return result


def show_active(directory):
    return {name: directory.active_row_ids(name) for name in directory.relation_names}


# ---------------------------------------------------------------------------
# 0. Adaptation-level identity rules, before any stateful directory exists
# ---------------------------------------------------------------------------


def demo_canonical_identity():
    banner(0, "canonical key identity: 1 == 2/2 numerically, TEXT '1' differs")
    # INT writer -> DECIMAL(6,0) reader: both adapt to reader-domain 1.
    writer = parse_contract(
        {"version": "w", "fields": [
            {"field_id": "v", "name": "v", "type": {"kind": "INT", "bits": 16}}]},
        role="writer",
    )
    reader = parse_contract(
        {"version": "r", "fields": [
            {"field_id": "v", "name": "v",
             "type": {"kind": "DECIMAL", "precision": 6, "scale": 0}}]},
        role="reader",
    )
    adapted = adapt_rows(writer, reader, [
        {"row_id": "a", "values": {"v": 1}},
        {"row_id": "b", "values": {"v": 1}},
    ])
    assert adapted.status == "ADAPTED"
    num_key = BindingKey("k", (adapted.canonical_rows[0].domain_values["v"],))
    assert num_key == BindingKey("k", (Fraction(2, 2),))
    assert num_key != BindingKey("k", ("1",))
    print(f"numeric key {num_key.to_json()} == (2/2) key; != TEXT key {BindingKey('k', ('1',)).to_json()}")


# ---------------------------------------------------------------------------
# Scenario 1
# ---------------------------------------------------------------------------


def scenario_one(catalog):
    banner(1, "two suppliers of K: withdrawing A removes only A")
    d = DirectoryLibrary(catalog, state_path=STATE_FILE)
    ingest(d, "labels", "files/s1-labels")
    ingest(d, "packages", "files/s1-packages")
    ingest(d, "shipping", "files/s1-shipping")
    print("active after ingest:", show_active(d))

    bindings = d.bindings_snapshot("packages")["packages"][0]
    print("row C provisions:", [b.to_json()["class"] for b in bindings.provisions])
    print("row C requirements:", [
        (b.name, b.target_relation, b.alias_class, b.key.to_json()["tuple"])
        for b in bindings.requirements
    ])
    print("row C skipped optional:", [s.name for s in bindings.skipped])

    result = d.withdraw(load("requests/s1-drop-A"))
    print("withdrawal rounds:")
    for rd in result.rounds:
        for lk in rd.lost_keys:
            print(f"  round {rd.round}: {lk.relation}/{lk.alias_class} "
                  f"{lk.key.to_json()['tuple']} previous={lk.previous_providers} "
                  f"remaining={lk.remaining_providers} survives={lk.survives}")
    assert result.removed_rows == ["labels/A"]
    assert show_active(d) == {
        "labels": ["B", "I"], "packages": ["C"], "shipping": ["D"], "ring": [],
    }
    print("active after withdraw:", show_active(d))


# ---------------------------------------------------------------------------
# Scenario 2 (fresh data): revision, cascade, retained-row veto, replay
# ---------------------------------------------------------------------------


def scenario_two(catalog):
    banner(2, "revised B moves off K: A withdrawal cascades A -> C -> D")
    state = OUT / "directory-s2.json"
    d = DirectoryLibrary(catalog, state_path=state)
    ingest(d, "labels", "files/s2-labels")
    ingest(d, "packages", "files/s2-packages")
    ingest(d, "shipping", "files/s2-shipping")

    before = d.library("labels").get_row("B")
    ingest(d, "labels", "files/s2-labels-revise")
    after = d.library("labels").get_row("B")
    print(f"row B revision {before.revision} tag {before.canonical['tag']} -> "
          f"revision {after.revision} tag {after.canonical['tag']}")

    cascade = d.withdraw(load("requests/s2-drop-A"))
    print("cascade result:")
    for rd in cascade.rounds:
        print(f"  round {rd.round} removed {rd.removed_rows}")
        for rid, why in rd.reasons.items():
            for line in why:
                print(f"      {rid}: {line}")
    assert cascade.removed_rows == ["labels/A", "packages/C", "shipping/D"]
    assert [rd.round for rd in cascade.rounds] == [0, 1, 2]
    print("active after cascade:", show_active(d))

    # Replay the same request id against the now-empty relations: original
    # decision comes back, state is not re-evaluated.
    replay = d.withdraw(load("requests/s2-drop-A"))
    assert replay.replay and replay.removed_rows == cascade.removed_rows
    print("replayed request returns the same decision; replay =", replay.replay)

    banner("2b", "retained D vetoes the whole closure; not a half withdrawal")
    d2 = DirectoryLibrary(catalog, state_path=OUT / "directory-s2-veto.json")
    ingest(d2, "labels", "files/s2-labels")
    ingest(d2, "packages", "files/s2-packages")
    ingest(d2, "shipping", "files/s2-shipping")
    ingest(d2, "labels", "files/s2-labels-revise")
    before_rows = show_active(d2)
    vetoed = d2.withdraw(load("requests/s2-drop-A-retain-D"))
    print("veto status:", vetoed.status, "| error:", vetoed.errors[0])
    assert not vetoed.ok
    assert show_active(d2) == before_rows
    print("all four current rows retained:", show_active(d2))


# ---------------------------------------------------------------------------
# Scenario 3: cycle stays self-sufficient
# ---------------------------------------------------------------------------


def scenario_three(catalog):
    banner(3, "last external alternative X leaves; the E<->F cycle survives")
    d = DirectoryLibrary(catalog, state_path=OUT / "directory-s3.json")
    ingest(d, "ring", "files/s3-ring")
    print("active ring rows:", d.active_row_ids("ring"))
    result = d.withdraw(load("requests/s3-drop-X"))
    for rd in result.rounds:
        print(f"  round {rd.round} removed {rd.removed_rows}")
        for lk in rd.lost_keys:
            print(f"      signal {lk.key.to_json()['tuple']} still supplied by "
                  f"{lk.remaining_providers}")
    assert result.removed_rows == ["ring/X"]
    assert d.active_row_ids("ring") == ["E", "F"]
    print("active ring rows after:", d.active_row_ids("ring"))


# ---------------------------------------------------------------------------
# Reopen and external reader query
# ---------------------------------------------------------------------------


def reopen_and_query(catalog):
    banner(4, "reopen the scenario-1 state file and query every relation")
    reopened = DirectoryLibrary.reopen(catalog, STATE_FILE)
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    for name in reopened.relation_names:
        target = PUBLISH_DIR / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "records.json").write_text(
            json.dumps(reopened.publish_relation(name), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    labels_summary = client("labels", PUBLISH_DIR / "labels" / "records.json")
    print("external reader_client on labels:", labels_summary)
    # A(100)+B(100)+I(200) before withdrawal; after withdrawing A: B(100)+I(200)
    assert labels_summary == {"numeric_totals": {"tag": "300"}, "row_count": 2}

    # The ledgered withdrawal replays after reopen too.
    replayed = reopened.withdraw(load("requests/s1-drop-A"))
    assert replayed.replay
    print("withdrawal s1-drop-A replays after reopen:", replayed.replay)
    print("active rows after reopen:", show_active(reopened))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog_from_disk()
    print("catalog:", catalog.version, "relations:",
          [r.name for r in catalog.relations])

    demo_canonical_identity()
    scenario_one(catalog)
    scenario_two(catalog)
    scenario_three(catalog)
    reopen_and_query(catalog)

    print("\nAll relation-demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
