# Named-relation materials

Local-file fixtures for the alias-supply / withdrawal layer. Every contract
pair is an ordinary `extcontract` writer/reader contract; nothing here bypasses
the exact adaptation path.

## Catalog (`catalog.json`)

| Relation | Provides (alias class) | Requires |
|----------|------------------------|----------|
| `labels` | `part(tag)`, `inspection(tag)` | — |
| `packages` | `package(pack_tag)` | `labels/part(part_ref)`, `labels/inspection(inspect_ref)`, optional `labels/part(spare_ref)` |
| `shipping` | — | `packages/package(pack_ref)` |
| `ring` | `signal(out_tag)` | optional `ring/signal(in_tag)` so the external entry row X may carry a null input |

Bindings identify by `(relation, class, ordered tuple)` on adapted reader
values: INT32 writers adapt into DECIMAL(10,0) readers, so numeric equality is
representation-independent while a TEXT value stays its own kind.

## Files

* `files/s1-*` — scenario 1: A and B both publish `part=100`; C needs part 100
  and inspection 200 and publishes package 10; D needs package 10.
  `requests/s1-drop-A.json` withdraws A and must remove only A.
* `files/s2-*` — scenario 2: same start, then `s2-labels-revise` moves B from
  tag 100 to 101. `requests/s2-drop-A.json` cascades A -> C -> D;
  `requests/s2-drop-A-retain-D.json` pins D and must be rejected wholesale.
* `files/s3-ring.json`, `requests/s3-drop-X.json` — scenario 3: X is the last
  external alternative supplier of signal 9; after X leaves, E and F still
  supply each other (E needs 9/out 8, F needs 8/out 9), so the cycle survives.

## Run

```bash
PYTHONPATH=. python3.11 examples/run_relations_demo.py
```
