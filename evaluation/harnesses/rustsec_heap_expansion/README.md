# RustSec heap-security expansion harnesses

This directory contains the five-case executable expansion beyond the frozen
RSH-001--RSH-040 pilot. The authoritative manifest is
`evaluation/config/rustsec_heap_expansion_harnesses.json`; it pins every source,
lockfile, archive, and local patch by SHA-256.

| Case | Advisory | Published/upstream scenario | Derived reuse scenario |
|---|---|---|---|
| RSH-041 | RUSTSEC-2026-0152 (`oneringbuf`) | `RSH-041-advisory` | `RSH-041-derived-reuse` |
| RSH-042 | RUSTSEC-2026-0128 (`emap`) | `RSH-042-upstream` | `RSH-042-derived-reuse` |
| RSH-043 | RUSTSEC-2026-0131 (`bitchomp`) | `RSH-043-upstream` | -- |
| RSH-044 | RUSTSEC-2021-0018 (`qwutils`) | `RSH-044-rudra` | -- |
| RSH-045 | RUSTSEC-2021-0033 (`stack_dst`) | `RSH-045-rudra` | -- |

Published/upstream scenarios reproduce the advisory oracle and matched patched
control. Derived scenarios isolate one equal-layout `A -> free -> B` decision
and carry `manual_exact_vulnerability_edge_identity` annotations. Their result
covers the attributed reuse edge. Automatic compiler victim coverage and the
source-level vulnerability remain separate gates.

List or preflight without executing a witness:

```bash
python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action list

python3 evaluation/scripts/run_rustsec_heap_experiment.py \
  --catalog evaluation/config/rustsec_heap_expansion_harnesses.json \
  --action preflight --scenario RSH-041-derived-reuse \
  --variants system,typed_plain,typeiso \
  --archive-variants vulnerable,patched \
  --output-dir /tmp/rustsec-rsh041-derived-preflight \
  --allow-download --repetitions 3
```

Execution compiles historical dependencies and runs memory-unsafe programs on
the host. It requires the explicit `--execute-unsafe` gate. Use a disposable VM
for a claim-grade containment boundary.
