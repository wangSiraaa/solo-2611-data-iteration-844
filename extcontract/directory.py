"""Directory library: multi-relation state, alias-supply withdrawal closures.

This is the stateful counterpart of :mod:`extcontract.relations`.  One
:class:`DirectoryLibrary` owns one :class:`Catalog` and one ordinary
:class:`~extcontract.ingest.IngestionLibrary` per relation, so every existing
ingestion guarantee (whole-domain contracts, exact adaptation, revision
resolution, per-file idempotency, atomic accept/reject) stays in force while
the cross-relation supply/demand layer is added on top.

Withdrawal semantics (the directory, not the caller, decides the result)
-----------------------------------------------------------------------
A request names one *root* row.  The root is removed first, then rows are
removed in rounds: a row leaves when one of its non-optional required keys has
no remaining provider.  Rounds repeat until a fixed point.  A cycle whose
members keep supplying one another survives because its members never lose a
required key.  The caller may pin active rows as *retained*; if the closure
would touch any of them the whole request is rejected and nothing changes.

Atomicity
---------
Both ingestion and withdrawal are computed entirely on staged clones and the
on-disk state is one JSON document written via a temp file plus an atomic
rename, covering *all* affected relations at once.  A rejected request or a
replayed request never writes, so no half withdrawal can ever be observed and
a later reopen sees either the old or the complete new state.  This is a
local-file, few-relation system, so no distributed transaction is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ContractError
from .ingest import IngestionLibrary, IngestResult
from .relations import (
    BindingEntry,
    BindingKey,
    Catalog,
    RowBindings,
    build_provider_index,
    extract_relation_bindings,
    key_from_json,
    qualification_errors,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RelationIngestResult:
    relation: str
    result: IngestResult

    @property
    def accepted(self) -> bool:
        return self.result.accepted

    def to_json(self) -> dict:
        out = self.result.to_json()
        out["relation"] = self.relation
        return out


@dataclass
class LostKey:
    relation: str
    alias_class: str
    key: BindingKey
    previous_providers: list[str]
    remaining_providers: list[str]
    lost_because: list[str]

    @property
    def survives(self) -> bool:
        """True when another row still publishes the key after this round."""
        return bool(self.remaining_providers)

    def to_json(self) -> dict:
        return {
            "relation": self.relation,
            "class": self.alias_class,
            "key": self.key.to_json(),
            "previous_providers": list(self.previous_providers),
            "remaining_providers": list(self.remaining_providers),
            "survives": self.survives,
            "lost_because": list(self.lost_because),
        }


@dataclass
class WithdrawalRound:
    round: int
    removed_rows: list[str]
    lost_keys: list[LostKey]
    reasons: dict[str, list[str]]

    def to_json(self) -> dict:
        return {
            "round": self.round,
            "removed_rows": list(self.removed_rows),
            "lost_keys": [k.to_json() for k in self.lost_keys],
            "reasons": {rid: list(why) for rid, why in self.reasons.items()},
        }


@dataclass
class WithdrawalResult:
    status: str  # "WITHDRAWN" | "REJECTED"
    request_id: str | None
    root_relation: str
    root_row: str
    rounds: list[WithdrawalRound] = field(default_factory=list)
    retained_rows: list[str] = field(default_factory=list)
    removed_rows: list[str] = field(default_factory=list)
    affected_relations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    replay: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "WITHDRAWN"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "request_id": self.request_id,
            "root_relation": self.root_relation,
            "root_row": self.root_row,
            "retained_rows": list(self.retained_rows),
            "removed_rows": list(self.removed_rows),
            "affected_relations": list(self.affected_relations),
            "rounds": [r.to_json() for r in self.rounds],
            "errors": list(self.errors),
            "replay": self.replay,
        }


# ---------------------------------------------------------------------------
# Directory library
# ---------------------------------------------------------------------------


class DirectoryLibrary:
    """Multi-relation target state with withdrawal closures and persistence."""

    def __init__(self, catalog: Catalog, state_path: str | os.PathLike | None = None):
        self.catalog = catalog
        self.state_path = Path(state_path) if state_path is not None else None
        self._lock = threading.RLock()
        self._libraries: dict[str, IngestionLibrary] = {
            spec.name: IngestionLibrary(spec.reader) for spec in catalog.relations
        }
        # (relation, row_id) -> None; a set kept as a dict for stable ordering
        self._retained: dict[tuple[str, str], None] = {}
        self._withdrawals: dict[str, dict] = {}
        self._withdrawal_seq = 0

    # -- construction / persistence ----------------------------------------

    @classmethod
    def reopen(cls, catalog: Catalog, state_path: str | os.PathLike) -> "DirectoryLibrary":
        """Rehydrate a directory from a state file written by :meth:`persist`."""
        path = Path(state_path)
        state = json.loads(path.read_text(encoding="utf-8"))
        expected_fp = catalog_fingerprint(catalog)
        if state.get("catalog_fingerprint") != expected_fp:
            raise ContractError(
                f"state file {path} was produced by a different catalog "
                f"(fingerprint mismatch); refusing to reopen against this catalog"
            )
        if state.get("catalog_version") != catalog.version:
            raise ContractError(
                f"state catalog_version {state.get('catalog_version')!r} does not "
                f"match {catalog.version!r}"
            )
        library = cls(catalog, state_path=path)
        for spec in catalog.relations:
            rel_state = state["relations"].get(spec.name)
            if rel_state is None:
                raise ContractError(f"state file missing relation {spec.name!r}")
            library._libraries[spec.name] = IngestionLibrary.import_state(
                spec.reader, rel_state
            )
        library._retained = {
            tuple(item): None for item in state.get("retained_rows", [])
        }
        library._withdrawals = dict(state.get("withdrawals", {}))
        library._withdrawal_seq = state.get("withdrawal_sequence", 0)
        return library

    def persist(self) -> None:
        """Atomically write the complete multi-relation state to one file."""
        if self.state_path is None:
            raise ContractError("this directory has no state_path; persistence disabled")
        doc = self.export_state()
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(self)}")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)  # atomic on a local POSIX filesystem

    def export_state(self) -> dict:
        with self._lock:
            return {
                "catalog_version": self.catalog.version,
                "catalog_fingerprint": catalog_fingerprint(self.catalog),
                "withdrawal_sequence": self._withdrawal_seq,
                "relations": {
                    name: lib.export_state() for name, lib in self._libraries.items()
                },
                "retained_rows": [list(k) for k in sorted(self._retained)],
                "withdrawals": dict(self._withdrawals),
            }

    # -- read views ---------------------------------------------------------

    @property
    def relation_names(self) -> list[str]:
        return [spec.name for spec in self.catalog.relations]

    def library(self, relation: str) -> IngestionLibrary:
        return self._libraries[self.catalog.relation(relation).name]

    def active_row_ids(self, relation: str) -> list[str]:
        return sorted(
            state.row_id for state in self.library(relation).snapshot()
        )

    def row_count(self, relation: str) -> int:
        return self.library(relation).row_count()

    def publish_relation(self, relation: str) -> dict:
        """``reader-records/v1`` snapshot for the external reader client."""
        return self.library(relation).publish()

    def publish_all(self) -> dict[str, dict]:
        return {name: self.publish_relation(name) for name in self.relation_names}

    def bindings_snapshot(
        self, relation: str | None = None
    ) -> dict[str, list[RowBindings]]:
        """Current provisions/dependencies, derived from committed rows."""
        names = [relation] if relation else self.relation_names
        out: dict[str, list[RowBindings]] = {}
        for name in names:
            rows = self._libraries[name].snapshot()
            out[name] = extract_relation_bindings(self.catalog, name, rows).rows
        return out

    # -- retained rows ------------------------------------------------------

    def retain(self, relation: str, row_id: str) -> None:
        self.catalog.relation(relation)
        with self._lock:
            self._retained[(relation, row_id)] = None

    def release(self, relation: str, row_id: str) -> None:
        with self._lock:
            self._retained.pop((relation, row_id), None)

    def retained_rows(self) -> list[tuple[str, str]]:
        return sorted(self._retained)

    # -- ingestion ----------------------------------------------------------

    def ingest_relation(
        self, relation: str, document: dict, *, persist: bool = True
    ) -> RelationIngestResult:
        """Ingest one file for one relation, with catalog-level qualification.

        The ordinary library performs contract/row/revision validation on a
        staged clone.  On top of that, every binding is re-derived from the
        adapted rows and the whole catalog is re-checked: a row whose required
        key has no provider (including a revision that drops a key others need)
        rejects the file and leaves every relation untouched.
        """
        spec = self.catalog.relation(relation)
        with self._lock:
            staged_libs = {
                name: lib.clone() for name, lib in self._libraries.items()
            }
            result = staged_libs[relation].ingest(spec.writer, document)
            if not result.accepted:
                return RelationIngestResult(relation, result)

            errors = self._catalog_errors_on(staged_libs)
            if errors:
                rejected = IngestResult(
                    status="REJECTED",
                    file_id=document.get("file_id"),
                    writer_version=spec.writer.version,
                    reader_version=spec.reader.version,
                    errors=errors,
                    adaptation=result.adaptation,
                )
                return RelationIngestResult(relation, rejected)

            # Commit: swap in the staged libraries (only one relation changed).
            self._libraries = staged_libs
            if persist and self.state_path is not None:
                self.persist()
            return RelationIngestResult(relation, result)

    def _catalog_errors_on(
        self, libraries: dict[str, IngestionLibrary]
    ) -> list[str]:
        bindings: dict[str, list[RowBindings]] = {}
        errors: list[str] = []
        for name in self.relation_names:
            rows = libraries[name].snapshot()
            extracted = extract_relation_bindings(self.catalog, name, rows)
            if not extracted.ok:
                errors.extend(extracted.errors)
            bindings[name] = extracted.rows
        errors.extend(qualification_errors(self.catalog, bindings))
        return errors

    # -- withdrawal ---------------------------------------------------------

    def withdraw(
        self,
        request: dict,
        *,
        persist: bool = True,
    ) -> WithdrawalResult:
        """Run a withdrawal request to its fixed-point decision, atomically."""
        with self._lock:
            return self._withdraw_locked(request, persist=persist)

    def _withdraw_locked(self, request: dict, *, persist: bool) -> WithdrawalResult:
        if not isinstance(request, dict):
            return WithdrawalResult(
                status="REJECTED",
                request_id=None,
                root_relation="",
                root_row="",
                errors=["withdrawal request must be an object"],
            )
        extra = set(request) - {"request_id", "relation", "row_id", "retain"}
        if extra:
            return WithdrawalResult(
                status="REJECTED",
                request_id=request.get("request_id"),
                root_relation=str(request.get("relation", "")),
                root_row=str(request.get("row_id", "")),
                errors=[f"unexpected request keys {sorted(extra)}"],
            )
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return WithdrawalResult(
                status="REJECTED",
                request_id=None,
                root_relation=str(request.get("relation", "")),
                root_row=str(request.get("row_id", "")),
                errors=["request_id must be a non-empty string"],
            )

        prior = self._withdrawals.get(request_id)
        if prior is not None:
            # Idempotent replay: the original full decision is returned.
            return _result_from_ledger(prior, replay=True)

        relation = request.get("relation")
        row_id = request.get("row_id")
        if not isinstance(relation, str) or not relation:
            return WithdrawalResult(
                status="REJECTED", request_id=request_id,
                root_relation=str(relation or ""), root_row=str(row_id or ""),
                errors=["relation must be a non-empty string"],
            )
        if not isinstance(row_id, str) or not row_id:
            return WithdrawalResult(
                status="REJECTED", request_id=request_id,
                root_relation=relation, root_row=str(row_id or ""),
                errors=["row_id must be a non-empty string"],
            )
        try:
            self.catalog.relation(relation)
        except ContractError as exc:
            return WithdrawalResult(
                status="REJECTED", request_id=request_id,
                root_relation=relation, root_row=row_id, errors=[str(exc)],
            )

        request_retain = request.get("retain", [])
        if not isinstance(request_retain, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("relation"), str)
            or not isinstance(item.get("row_id"), str)
            for item in request_retain
        ):
            return WithdrawalResult(
                status="REJECTED", request_id=request_id,
                root_relation=relation, root_row=row_id,
                errors=["retain must be a list of {relation, row_id} objects"],
            )
        retain_set = set(self._retained) | {
            (item["relation"], item["row_id"]) for item in request_retain
        }
        for rel, rid in sorted(retain_set):
            if not self.catalog.has_relation(rel):
                return WithdrawalResult(
                    status="REJECTED", request_id=request_id,
                    root_relation=relation, root_row=row_id,
                    errors=[f"retained row references unknown relation {rel!r}"],
                )
            if rid not in self.active_row_ids(rel):
                return WithdrawalResult(
                    status="REJECTED", request_id=request_id,
                    root_relation=relation, root_row=row_id,
                    errors=[
                        f"retained row {rid!r} is not an active row of relation "
                        f"{rel!r}"
                    ],
                )
        if row_id not in self.active_row_ids(relation):
            return WithdrawalResult(
                status="REJECTED", request_id=request_id,
                root_relation=relation, root_row=row_id,
                errors=[
                    f"root row {row_id!r} is not an active row of relation "
                    f"{relation!r}"
                ],
            )

        staged = {name: lib.clone() for name, lib in self._libraries.items()}
        decision = self._compute_closure(
            staged, relation, row_id, sorted(retain_set)
        )
        decision.request_id = request_id

        if decision.status == "REJECTED":
            # Nothing was committed and the request id is not ledgered.
            return decision

        self._libraries = staged
        self._withdrawal_seq += 1
        ledger = decision.to_json()
        ledger["sequence"] = self._withdrawal_seq
        self._withdrawals[request_id] = ledger
        if persist and self.state_path is not None:
            self.persist()
        return decision

    def _compute_closure(
        self,
        libraries: dict[str, IngestionLibrary],
        root_relation: str,
        root_row: str,
        retained: list[tuple[str, str]],
    ) -> WithdrawalResult:
        result = WithdrawalResult(
            status="WITHDRAWN",
            request_id=None,  # filled by caller for the ledger
            root_relation=root_relation,
            root_row=root_row,
            retained_rows=[f"{rel}/{rid}" for rel, rid in retained],
        )

        def snapshot_bindings() -> dict[str, list[RowBindings]]:
            out: dict[str, list[RowBindings]] = {}
            for name in self.relation_names:
                rows = libraries[name].snapshot()
                out[name] = extract_relation_bindings(
                    self.catalog, name, rows
                ).rows
            return out

        def remove_rows(removed: dict[str, set[str]]) -> None:
            for rel, rids in removed.items():
                if not rids:
                    continue
                lib = libraries[rel]
                state = lib.export_state()
                for rid in rids:
                    state["rows"].pop(rid, None)
                libraries[rel] = IngestionLibrary.import_state(
                    self.catalog.relation(rel).reader, state
                )

        all_removed: dict[str, set[str]] = {}

        def note(rel: str, rid: str) -> None:
            all_removed.setdefault(rel, set()).add(rid)

        # Round 0: the explicitly requested root row leaves first.
        round_no = 0
        root_lost = self._provision_lost_keys(
            libraries, {root_relation: {root_row}}
        )
        result.rounds.append(
            WithdrawalRound(
                round=round_no,
                removed_rows=[f"{root_relation}/{root_row}"],
                lost_keys=root_lost,
                reasons={
                    f"{root_relation}/{root_row}": ["withdrawal request root row"]
                },
            )
        )
        note(root_relation, root_row)
        remove_rows({root_relation: {root_row}})

        # Fixed-point rounds: lose rows whose required providers are all gone.
        while True:
            bindings = snapshot_bindings()
            index = build_provider_index(self.catalog, bindings)
            to_remove: dict[str, list[str]] = {}
            reasons: dict[str, list[str]] = {}
            for rel, rows in bindings.items():
                for rb in rows:
                    missing = self._missing_requirements(rb, index)
                    if not missing:
                        continue
                    label = f"{rel}/{rb.row_id}"
                    to_remove.setdefault(rel, []).append(rb.row_id)
                    reasons[label] = [
                        f"required binding {entry.name!r} key "
                        f"{entry.key.to_json()['tuple']} from "
                        f"{entry.target_relation}/{entry.alias_class} has no "
                        f"remaining provider"
                        for entry in missing
                    ]
            if not to_remove:
                break

            round_no += 1
            removed_sets = {rel: set(rids) for rel, rids in to_remove.items()}
            lost_keys = self._provision_lost_keys(libraries, removed_sets)
            result.rounds.append(
                WithdrawalRound(
                    round=round_no,
                    removed_rows=sorted(
                        f"{rel}/{rid}"
                        for rel, rids in to_remove.items()
                        for rid in rids
                    ),
                    lost_keys=lost_keys,
                    reasons=reasons,
                )
            )
            for rel, rids in removed_sets.items():
                for rid in rids:
                    note(rel, rid)
            remove_rows(removed_sets)

        # Retained-row guard: a closure touching a retained row rejects all.
        touched_retained = [
            f"{rel}/{rid}"
            for rel, rid in retained
            if rid in all_removed.get(rel, set())
        ]
        if touched_retained:
            return WithdrawalResult(
                status="REJECTED",
                request_id=None,
                root_relation=root_relation,
                root_row=root_row,
                retained_rows=result.retained_rows,
                rounds=result.rounds,
                errors=[
                    "withdrawal closure reaches retained row(s) "
                    f"{sorted(touched_retained)}; the whole request is rejected"
                ],
            )

        result.removed_rows = sorted(
            f"{rel}/{rid}" for rel, rids in all_removed.items() for rid in rids
        )
        result.affected_relations = sorted(all_removed)
        return result

    def _missing_requirements(
        self, rb: RowBindings, index
    ) -> list[BindingEntry]:
        missing = []
        for entry in rb.requirements:
            providers = index.get((entry.target_relation, entry.alias_class), {})
            if entry.key not in providers:
                missing.append(entry)
        return missing

    def _provision_lost_keys(
        self,
        libraries: dict[str, IngestionLibrary],
        removed: dict[str, set[str]],
    ) -> list[LostKey]:
        """Keys published by the about-to-be-removed rows, computed pre-delete.

        The state still contains every row, so each key records its full
        previous provider set and, after subtracting rows removed in this
        round, the providers that keep supplying it.  A key appears once even
        if several of its providers leave in the same round.
        """
        full_index = build_provider_index(
            self.catalog,
            {
                name: extract_relation_bindings(
                    self.catalog, name, libraries[name].snapshot()
                ).rows
                for name in self.relation_names
            },
        )
        merged: dict[tuple[str, str, BindingKey], LostKey] = {}
        for rel, rids in removed.items():
            if not rids:
                continue
            rows = libraries[rel].snapshot()
            bindings = extract_relation_bindings(self.catalog, rel, rows).rows
            by_id = {rb.row_id: rb for rb in bindings}
            for rid in sorted(rids):
                rb = by_id.get(rid)
                if rb is None:
                    continue
                for entry in rb.provisions:
                    previous = full_index.get(
                        (rel, entry.alias_class), {}
                    ).get(entry.key, [])
                    remaining = [
                        provider
                        for provider in previous
                        if provider not in removed.get(rel, set())
                    ]
                    dedup_key = (rel, entry.alias_class, entry.key)
                    existing = merged.get(dedup_key)
                    if existing is None:
                        merged[dedup_key] = LostKey(
                            relation=rel,
                            alias_class=entry.alias_class,
                            key=entry.key,
                            previous_providers=list(previous),
                            remaining_providers=remaining,
                            lost_because=[f"provider row {rel}/{rid} removed"],
                        )
                    else:
                        existing.lost_because.append(
                            f"provider row {rel}/{rid} removed"
                        )
        return sorted(
            merged.values(),
            key=lambda lk: (
                lk.relation,
                lk.alias_class,
                json.dumps(lk.key.to_json(), sort_keys=True),
            ),
        )

    # -- ledger access ------------------------------------------------------

    def withdrawal_for(self, request_id: str) -> dict | None:
        with self._lock:
            entry = self._withdrawals.get(request_id)
            return dict(entry) if entry else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def catalog_fingerprint(catalog: Catalog) -> str:
    payload = json.dumps(
        catalog.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_from_ledger(ledger: dict, *, replay: bool) -> WithdrawalResult:
    rounds = [
        WithdrawalRound(
            round=raw["round"],
            removed_rows=list(raw["removed_rows"]),
            lost_keys=[
                LostKey(
                    relation=lk["relation"],
                    alias_class=lk["class"],
                    key=key_from_json(lk["key"]),
                    previous_providers=list(lk["previous_providers"]),
                    remaining_providers=list(lk.get("remaining_providers", [])),
                    lost_because=list(lk["lost_because"]),
                )
                for lk in raw["lost_keys"]
            ],
            reasons={k: list(v) for k, v in raw["reasons"].items()},
        )
        for raw in ledger["rounds"]
    ]
    return WithdrawalResult(
        status=ledger["status"],
        request_id=ledger["request_id"],
        root_relation=ledger["root_relation"],
        root_row=ledger["root_row"],
        rounds=rounds,
        retained_rows=list(ledger.get("retained_rows", [])),
        removed_rows=list(ledger.get("removed_rows", [])),
        affected_relations=list(ledger.get("affected_relations", [])),
        errors=list(ledger.get("errors", [])),
        replay=replay,
    )
