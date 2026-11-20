"""Command-line entry points for the external contract toolkit.

    python -m extcontract compat WRITER.json READER.json [--json]
    python -m extcontract adapt  WRITER.json READER.json INPUT.json OUT.json
    python -m extcontract ingest WRITER.json READER.json FILE.json \
                                   [--state-dir DIR] [--publish OUT.json]

    python -m extcontract dir-ingest   CATALOG.json CONTRACT_DIR RELATION FILE \
                                       --state STATE.json [--publish DIR]
    python -m extcontract dir-withdraw CATALOG.json CONTRACT_DIR REQUEST.json \
                                       --state STATE.json [--publish DIR]
    python -m extcontract dir-publish  CATALOG.json CONTRACT_DIR \
                                       --state STATE.json --out-dir DIR

The ``dir-*`` commands keep the whole multi-relation directory in one state
file written atomically, so an accepted withdrawal covers every affected
relation and a rejected or replayed request leaves the file untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapter import adapt_file_document, write_records
from .compat import check_compatibility
from .contract import load_contract
from .ingest import IngestionLibrary
from .relations import parse_catalog
from .directory import DirectoryLibrary


def _load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_compat(args: argparse.Namespace) -> int:
    writer = load_contract(args.writer, role="writer")
    reader = load_contract(args.reader, role="reader")
    report = check_compatibility(writer, reader)
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.safe else 1
    print(report.summary())
    for column in report.columns:
        mark = "SAFE" if column.safe else "UNSAFE"
        source = column.source
        detail = f"[{mark}] {column.field_id} ({source})"
        print(detail)
        for reason in column.reasons:
            print(f"    - {reason}")
        if column.witness is not None:
            print(f"    witness: {json.dumps(column.witness, ensure_ascii=False)}")
    for ignored in report.ignored_writer_fields:
        print(f"[IGNORED] writer field {ignored['field_id']}")
    if report.errors:
        for error in report.errors:
            print(f"[ERROR] {error}")
    if report.safe:
        print("PLAN:")
        print(json.dumps(report.adaptation_plan(), ensure_ascii=False, indent=2))
    return 0 if report.safe else 1


def cmd_adapt(args: argparse.Namespace) -> int:
    writer = load_contract(args.writer, role="writer")
    reader = load_contract(args.reader, role="reader")
    doc = _load_json(args.input)
    result = adapt_file_document(writer, reader, doc)
    if result.status != "ADAPTED":
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
        print(f"adaptation rejected: {result.status}", file=sys.stderr)
        return 1
    write_records(result.records, args.output)
    print(f"wrote {len(result.records['rows'])} rows to {args.output}")
    return 0


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"sequence": 0, "rows": {}, "receipts": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_ingest(args: argparse.Namespace) -> int:
    writer = load_contract(args.writer, role="writer")
    reader = load_contract(args.reader, role="reader")
    doc = _load_json(args.file)
    state_path = Path(args.state_dir) / f"state-{reader.version}.json"

    # Rehydrate a library from the on-disk state, ingest, then persist only on
    # acceptance (atomic at the file level: a rejected file writes nothing).
    if state_path.exists():
        library = IngestionLibrary.import_state(reader, _load_state(state_path))
    else:
        library = IngestionLibrary(reader)
    result = library.ingest(writer, doc)
    if result.accepted:
        _dump_state(library.export_state(), state_path)
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
        if args.publish:
            write_records(library.publish(), args.publish)
        return 0
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
    print("file rejected; target state unchanged", file=sys.stderr)
    return 1


def _load_catalog(catalog_path: str, contract_dir: str) -> tuple:
    """Load the catalog and every referenced writer/reader contract."""
    catalog_raw = _load_json(catalog_path)
    base = Path(contract_dir)
    contracts = {}
    for rel in catalog_raw["relations"]:
        name = rel["relation"]
        contracts[(name, "writer")] = load_contract(
            base / name / "writer-contract.json", role="writer"
        )
        contracts[(name, "reader")] = load_contract(
            base / name / "reader-contract.json", role="reader"
        )
    return parse_catalog(catalog_raw, contracts), base


def _open_directory(catalog, state_path: Path) -> DirectoryLibrary:
    if state_path.exists():
        return DirectoryLibrary.reopen(catalog, state_path)
    return DirectoryLibrary(catalog, state_path=state_path)


def _publish_directory(directory: DirectoryLibrary, out_dir: str) -> None:
    out = Path(out_dir)
    for name in directory.relation_names:
        target = out / name
        target.mkdir(parents=True, exist_ok=True)
        write_records(directory.publish_relation(name), target / "records.json")


def cmd_dir_ingest(args: argparse.Namespace) -> int:
    catalog, _ = _load_catalog(args.catalog, args.contract_dir)
    state_path = Path(args.state)
    directory = _open_directory(catalog, state_path)
    result = directory.ingest_relation(
        args.relation, _load_json(args.file), persist=True
    )
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
    if not result.accepted:
        print("file rejected; directory state unchanged", file=sys.stderr)
        return 1
    if args.publish:
        _publish_directory(directory, args.publish)
    return 0


def cmd_dir_withdraw(args: argparse.Namespace) -> int:
    catalog, _ = _load_catalog(args.catalog, args.contract_dir)
    state_path = Path(args.state)
    directory = _open_directory(catalog, state_path)
    result = directory.withdraw(_load_json(args.request), persist=True)
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
    if not result.ok:
        print("withdrawal rejected; directory state unchanged", file=sys.stderr)
        return 1
    if args.publish:
        _publish_directory(directory, args.publish)
    return 0


def cmd_dir_publish(args: argparse.Namespace) -> int:
    catalog, _ = _load_catalog(args.catalog, args.contract_dir)
    state_path = Path(args.state)
    directory = _open_directory(catalog, state_path)
    out = Path(args.out_dir)
    for name in directory.relation_names:
        target = out / name
        target.mkdir(parents=True, exist_ok=True)
        write_records(directory.publish_relation(name), target / "records.json")
    summary = {
        name: {
            "active_rows": directory.row_count(name),
            "retained": [
                rid for rel, rid in directory.retained_rows() if rel == name
            ],
        }
        for name in directory.relation_names
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="extcontract")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compat = sub.add_parser("compat", help="row-free compatibility report")
    p_compat.add_argument("writer")
    p_compat.add_argument("reader")
    p_compat.add_argument("--json", action="store_true")
    p_compat.set_defaults(func=cmd_compat)

    p_adapt = sub.add_parser("adapt", help="adapt a file to reader-records/v1")
    p_adapt.add_argument("writer")
    p_adapt.add_argument("reader")
    p_adapt.add_argument("input")
    p_adapt.add_argument("output")
    p_adapt.set_defaults(func=cmd_adapt)

    p_ingest = sub.add_parser("ingest", help="atomically ingest a file")
    p_ingest.add_argument("writer")
    p_ingest.add_argument("reader")
    p_ingest.add_argument("file")
    p_ingest.add_argument("--state-dir", default=".extcontract-state")
    p_ingest.add_argument("--publish", default=None)
    p_ingest.set_defaults(func=cmd_ingest)

    p_di = sub.add_parser(
        "dir-ingest", help="ingest one relation file into a catalog directory"
    )
    p_di.add_argument("catalog")
    p_di.add_argument("contract_dir")
    p_di.add_argument("relation")
    p_di.add_argument("file")
    p_di.add_argument("--state", required=True)
    p_di.add_argument("--publish", default=None)
    p_di.set_defaults(func=cmd_dir_ingest)

    p_dw = sub.add_parser(
        "dir-withdraw", help="run an alias-supply withdrawal request"
    )
    p_dw.add_argument("catalog")
    p_dw.add_argument("contract_dir")
    p_dw.add_argument("request")
    p_dw.add_argument("--state", required=True)
    p_dw.add_argument("--publish", default=None)
    p_dw.set_defaults(func=cmd_dir_withdraw)

    p_dp = sub.add_parser(
        "dir-publish", help="reopen state and publish every relation"
    )
    p_dp.add_argument("catalog")
    p_dp.add_argument("contract_dir")
    p_dp.add_argument("--state", required=True)
    p_dp.add_argument("--out-dir", required=True)
    p_dp.set_defaults(func=cmd_dir_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
