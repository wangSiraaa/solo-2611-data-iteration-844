"""Tests for the directory library: supply/demand ingestion, withdrawal
closures, retained rows, replay and single-file atomic persistence.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from extcontract import DirectoryLibrary, load_catalog

MATERIALS = Path("materials/relations")
CLIENT_745 = Path("materials/745/reader_client.py")


@pytest.fixture(scope="module")
def catalog():
    paths = {}
    for rel in ["labels", "packages", "shipping", "ring"]:
        paths[(rel, "writer")] = MATERIALS / rel / "writer-contract.json"
        paths[(rel, "reader")] = MATERIALS / rel / "reader-contract.json"
    return load_catalog(MATERIALS / "catalog.json", paths)


@pytest.fixture()
def directory(catalog):
    return DirectoryLibrary(catalog)


def load(name):
    return json.loads((MATERIALS / (name + ".json")).read_text())


def ingest(directory, relation, name):
    result = directory.ingest_relation(relation, load(name), persist=False)
    assert result.accepted, result.result.errors
    return result


def run_reader(contract, records):
    return subprocess.run(
        [sys.executable, str(CLIENT_745), str(contract), str(records)],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Ingestion qualification
# ---------------------------------------------------------------------------


def test_ingest_order_independent_qualification(directory):
    # Downstream first: every file is staged against the full catalog, so the
    # package cannot become visible until its label suppliers exist.
    rejected = directory.ingest_relation(
        "packages", load("files/s1-packages"), persist=False
    )
    assert not rejected.accepted
    assert directory.row_count("packages") == 0
    ingest(directory, "labels", "files/s1-labels")
    ingest(directory, "packages", "files/s1-packages")
    # shipping still lacks its package provider: no package row has been
    # committed yet for this directory (s1-shipping arrives before any package).
    fresh = DirectoryLibrary(directory.catalog)
    ingest(fresh, "labels", "files/s1-labels")
    rejected_ship = fresh.ingest_relation(
        "shipping", load("files/s1-shipping"), persist=False
    )
    assert not rejected_ship.accepted
    assert fresh.row_count("shipping") == 0
    # Once the package exists, the same shipping file qualifies.
    ingest(directory, "shipping", "files/s1-shipping")
    assert directory.row_count("shipping") == 1


def test_undeclared_column_cannot_build_a_reference(directory):
    ingest(directory, "labels", "files/s1-labels")
    bad = {
        "file_id": "bad", "writer_version": "pw1", "reader_version": "pr1",
        "rows": [{
            "row_id": "X",
            "values": {"part_ref": 100, "inspect_ref": 200, "pack_tag": 10,
                       "undeclared": 1},
        }],
    }
    result = directory.ingest_relation("packages", bad, persist=False)
    assert not result.accepted
    assert directory.row_count("packages") == 0


def test_out_of_contract_value_cannot_build_a_reference(directory):
    ingest(directory, "labels", "files/s1-labels")
    bad = {
        "file_id": "bad", "writer_version": "pw1", "reader_version": "pr1",
        "rows": [{
            "row_id": "Y",
            "values": {"part_ref": 100, "inspect_ref": 200,
                       "pack_tag": 99_999_999_999},
        }],
    }
    result = directory.ingest_relation("packages", bad, persist=False)
    assert not result.accepted
    assert directory.row_count("packages") == 0


def test_unsatisfied_required_dependency_rejects_file(directory):
    ingest(directory, "labels", "files/s1-labels")
    bad = {
        "file_id": "bad", "writer_version": "pw1", "reader_version": "pr1",
        "rows": [{
            "row_id": "Z",
            "values": {"part_ref": 555, "inspect_ref": 200, "pack_tag": 11},
        }],
    }
    result = directory.ingest_relation("packages", bad, persist=False)
    assert not result.accepted
    assert "no remaining provider" in result.result.errors[0]
    assert directory.row_count("packages") == 0


def test_revision_can_add_and_remove_provisions(directory):
    ingest(directory, "labels", "files/s2-labels")
    ingest(directory, "packages", "files/s2-packages")
    # B revision 2 moves tag 100 -> 101 (drops key 100, adds key 101)
    ingest(directory, "labels", "files/s2-labels-revise")
    b = directory.library("labels").get_row("B")
    assert b.revision == 2 and b.canonical["tag"] == 101
    after = directory.bindings_snapshot("labels")["labels"]
    tags = sorted(
        {int(e.key.values[0]) for rb in after for e in rb.provisions}
    )
    from fractions import Fraction
    assert [Fraction(v) for v in tags] == [
        Fraction(100), Fraction(101), Fraction(200)
    ]
    # Key 100 now has exactly one remaining provider row (A); B moved to 101.
    # A publishes it under two alias classes, so de-duplicate provider rows.
    providers = directory.bindings_snapshot("labels")
    hundred = sorted({
        rb.row_id
        for rb in providers["labels"]
        for e in rb.provisions
        if e.key.values == (Fraction(100),)
    })
    assert hundred == ["A"]


# ---------------------------------------------------------------------------
# Mandated scenario 1: two suppliers of K, withdrawing one removes only it
# ---------------------------------------------------------------------------


def test_scenario_1_withdraw_a_removes_only_a(directory):
    ingest(directory, "labels", "files/s1-labels")
    ingest(directory, "packages", "files/s1-packages")
    ingest(directory, "shipping", "files/s1-shipping")

    result = directory.withdraw(load("requests/s1-drop-A"), persist=False)
    assert result.ok
    assert result.removed_rows == ["labels/A"]
    assert result.affected_relations == ["labels"]
    assert directory.active_row_ids("labels") == ["B", "I"]
    assert directory.active_row_ids("packages") == ["C"]
    assert directory.active_row_ids("shipping") == ["D"]

    round0 = result.rounds[0]
    by_class = {lk.alias_class: lk for lk in round0.lost_keys}
    # Both alias classes A published are reported, each still supplied by B.
    assert set(by_class) == {"inspection", "part"}
    for lk in by_class.values():
        assert lk.previous_providers == ["A", "B"]
        assert lk.remaining_providers == ["B"]
        assert lk.survives is True


# ---------------------------------------------------------------------------
# Mandated scenario 2: corrected B -> cascade A, C, D; retain D rejects all
# ---------------------------------------------------------------------------


def _scenario_two(directory):
    ingest(directory, "labels", "files/s2-labels")
    ingest(directory, "packages", "files/s2-packages")
    ingest(directory, "shipping", "files/s2-shipping")
    ingest(directory, "labels", "files/s2-labels-revise")


def test_scenario_2_cascade_rounds(directory):
    _scenario_two(directory)
    result = directory.withdraw(load("requests/s2-drop-A"), persist=False)
    assert result.ok
    assert result.removed_rows == ["labels/A", "packages/C", "shipping/D"]
    assert [rd.round for rd in result.rounds] == [0, 1, 2]
    assert result.rounds[0].removed_rows == ["labels/A"]
    assert result.rounds[1].removed_rows == ["packages/C"]
    assert result.rounds[2].removed_rows == ["shipping/D"]

    # The genuinely lost keys carry the previous provider list.
    round0_lost = [lk for lk in result.rounds[0].lost_keys if not lk.survives]
    assert sorted(lk.alias_class for lk in round0_lost) == [
        "inspection", "part"
    ]
    assert all(lk.previous_providers == ["A"] for lk in round0_lost)
    pkg_lost = [lk for lk in result.rounds[1].lost_keys if not lk.survives]
    assert pkg_lost[0].previous_providers == ["C"]
    assert pkg_lost[0].remaining_providers == []


def test_scenario_2_retained_d_rejects_and_keeps_all_four_rows(catalog):
    directory = DirectoryLibrary(catalog)
    _scenario_two(directory)
    before = {n: directory.active_row_ids(n) for n in directory.relation_names}
    result = directory.withdraw(
        load("requests/s2-drop-A-retain-D"), persist=False
    )
    assert not result.ok
    assert "retained row" in result.errors[0]
    after = {n: directory.active_row_ids(n) for n in directory.relation_names}
    assert before == after
    # The closure was still computed and reported for the audit trail.
    flat = [r for rd in result.rounds for r in rd.removed_rows]
    assert "shipping/D" in flat


def test_station_level_retained_row_is_also_guarded(directory):
    _scenario_two(directory)
    directory.retain("shipping", "D")
    result = directory.withdraw(load("requests/s2-drop-A"), persist=False)
    assert not result.ok
    assert directory.active_row_ids("shipping") == ["D"]
    directory.release("shipping", "D")
    assert directory.withdraw(load("requests/s2-drop-A"), persist=False).ok


def test_retain_unknown_or_inactive_row_rejects(directory):
    _scenario_two(directory)
    bad = {
        "request_id": "x", "relation": "labels", "row_id": "A",
        "retain": [{"relation": "shipping", "row_id": "ghost"}],
    }
    result = directory.withdraw(bad, persist=False)
    assert not result.ok and "not an active row" in result.errors[0]


# ---------------------------------------------------------------------------
# Mandated scenario 3: the last external alternative leaves, the cycle stays
# ---------------------------------------------------------------------------


def test_scenario_3_self_sufficient_cycle_survives(directory):
    ingest(directory, "ring", "files/s3-ring")
    result = directory.withdraw(load("requests/s3-drop-X"), persist=False)
    assert result.ok
    assert result.removed_rows == ["ring/X"]
    assert directory.active_row_ids("ring") == ["E", "F"]
    # One round only: after X leaves, E and F still satisfy one another.
    assert len(result.rounds) == 1
    signal = result.rounds[0].lost_keys[0]
    assert signal.remaining_providers == ["F"]
    assert signal.survives is True


def test_cycle_collapses_when_it_stops_self_supplying(catalog):
    # X only (no E/F): after removing X nobody supplies signal 9 at all.
    directory = DirectoryLibrary(catalog)
    doc = {
        "file_id": "lone", "writer_version": "gw1", "reader_version": "gr1",
        "rows": [{"row_id": "X", "revision": 1,
                  "values": {"in_tag": None, "out_tag": 9}}],
    }
    assert directory.ingest_relation("ring", doc, persist=False).accepted
    result = directory.withdraw(
        {"request_id": "w", "relation": "ring", "row_id": "X"}, persist=False
    )
    assert result.ok and result.removed_rows == ["ring/X"]
    assert directory.active_row_ids("ring") == []


# ---------------------------------------------------------------------------
# Request validation, replay, idempotency
# ---------------------------------------------------------------------------


def test_unknown_root_and_bad_request_shapes(directory):
    ingest(directory, "labels", "files/s1-labels")
    r = directory.withdraw(
        {"request_id": "w", "relation": "labels", "row_id": "ghost"},
        persist=False,
    )
    assert not r.ok and "not an active row" in r.errors[0]
    r = directory.withdraw({"relation": "labels", "row_id": "A"}, persist=False)
    assert not r.ok and "request_id" in r.errors[0]
    r = directory.withdraw({"request_id": "w", "relation": "nope", "row_id": "A"},
                           persist=False)
    assert not r.ok and "no relation" in r.errors[0]
    r = directory.withdraw(
        {"request_id": "w", "relation": "labels", "row_id": "A", "surprise": 1},
        persist=False,
    )
    assert not r.ok and "unexpected request keys" in r.errors[0]
    r = directory.withdraw(
        {"request_id": "w", "relation": "labels", "row_id": "A",
         "retain": [{"relation": "labels"}]},
        persist=False,
    )
    assert not r.ok and "retain must be" in r.errors[0]


def test_withdrawal_replay_returns_original_decision_and_changes_nothing(directory):
    ingest(directory, "labels", "files/s1-labels")
    first = directory.withdraw(load("requests/s1-drop-A"), persist=False)
    assert first.ok and not first.replay
    # A is already gone; a fresh decision would now be rejected, but the
    # ledgered request id replays its original result without touching state.
    replay = directory.withdraw(load("requests/s1-drop-A"), persist=False)
    assert replay.ok and replay.replay is True
    assert replay.removed_rows == first.removed_rows
    assert directory.active_row_ids("labels") == ["B", "I"]
    assert directory.withdrawal_for("s1-drop-A")["status"] == "WITHDRAWN"


# ---------------------------------------------------------------------------
# Persistence: one state file, atomic commit, reopen
# ---------------------------------------------------------------------------


def test_persist_and_reopen_round_trip(catalog, tmp_path):
    state = tmp_path / "directory.json"
    directory = DirectoryLibrary(catalog, state_path=state)
    ingest(directory, "labels", "files/s2-labels")
    ingest(directory, "packages", "files/s2-packages")
    ingest(directory, "shipping", "files/s2-shipping")
    ingest(directory, "labels", "files/s2-labels-revise")
    directory.retain("labels", "I")
    result = directory.withdraw(load("requests/s2-drop-A"))
    assert result.ok
    assert state.exists()

    reopened = DirectoryLibrary.reopen(catalog, state)
    assert reopened.active_row_ids("labels") == ["B", "I"]
    assert reopened.active_row_ids("packages") == []
    assert reopened.active_row_ids("shipping") == []
    assert reopened.retained_rows() == [("labels", "I")]
    # Withdrawal ledger replays after reopen as well.
    again = reopened.withdraw(load("requests/s2-drop-A"))
    assert again.replay and again.removed_rows == result.removed_rows


def test_rejected_withdrawal_writes_nothing(catalog, tmp_path):
    state = tmp_path / "directory.json"
    directory = DirectoryLibrary(catalog, state_path=state)
    ingest(directory, "labels", "files/s2-labels")
    ingest(directory, "packages", "files/s2-packages")
    ingest(directory, "shipping", "files/s2-shipping")
    ingest(directory, "labels", "files/s2-labels-revise")
    # Flush the ingested state, then attempt a withdrawal the retained D vetoes.
    directory.persist()
    before = state.read_bytes()
    result = directory.withdraw(
        {"request_id": "never-ledgered", "relation": "labels", "row_id": "A",
         "retain": [{"relation": "shipping", "row_id": "D"}]},
    )
    assert not result.ok
    assert state.read_bytes() == before
    assert directory.withdrawal_for("never-ledgered") is None


def test_reopen_rejects_foreign_catalog(tmp_path):
    paths = {}
    for rel in ["labels", "packages", "shipping", "ring"]:
        paths[(rel, "writer")] = MATERIALS / rel / "writer-contract.json"
        paths[(rel, "reader")] = MATERIALS / rel / "reader-contract.json"
    catalog = load_catalog(MATERIALS / "catalog.json", paths)
    state = tmp_path / "directory.json"
    directory = DirectoryLibrary(catalog, state_path=state)
    result = directory.ingest_relation("labels", load("files/s1-labels"))
    assert result.accepted
    from extcontract import Catalog
    tampered = Catalog(version="cat9", relations=catalog.relations)
    with pytest.raises(Exception, match="fingerprint mismatch"):
        DirectoryLibrary.reopen(tampered, state)


def test_published_records_remain_reader_client_compatible(catalog, tmp_path):
    directory = DirectoryLibrary(catalog)
    ingest(directory, "labels", "files/s1-labels")
    records = tmp_path / "labels.json"
    records.write_text(json.dumps(directory.publish_relation("labels")),
                       encoding="utf-8")
    proc = run_reader(MATERIALS / "labels/reader-contract.json", records)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary == {"numeric_totals": {"tag": "400"}, "row_count": 3}


def test_ingest_replay_is_idempotent_at_directory_level(directory):
    ingest(directory, "labels", "files/s1-labels")
    again = directory.ingest_relation("labels", load("files/s1-labels"),
                                      persist=False)
    assert again.accepted and again.result.receipt["replay"] is True
    assert directory.row_count("labels") == 3
