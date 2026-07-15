# RustSec security mechanism set diagrams

These slide-ready diagrams use the post-source-audit heap scope. The frozen
ledger screened **53** cases. RSH-006 is
removed from the heap allocator denominator because its dangling target is a
moved stack-local object. The retained scope contains
**52** cases: **48**
executable cases and **4** audit-only cases.

## Recommended slide assets

- `rustsec-security-sets-overview.svg`: one-slide overview with the full scope
  and UAF inset.
- `rustsec-security-sets-full.svg`: full-scope explanation slide.
- `rustsec-security-sets-uaf.svg`: UAF explanation slide.
- Matching PNG files are high-resolution fallbacks. SVG is the editable
  PowerPoint source.

## Full retained heap scope

| Set or region | Count | Cases |
| --- | ---: | --- |
| Type Isolation only | 11 | RSH-008, RSH-041, RSH-042, RSH-052, RSH-055, RSH-064, RSH-065, RSH-066, RSH-067, RSH-068, RSH-069 |
| Type Isolation and reclaim checks | 1 | RSH-002 |
| Reclaim checks only | 30 | RSH-001, RSH-005, RSH-009, RSH-010, RSH-011, RSH-012, RSH-013, RSH-014, RSH-018, RSH-020, RSH-021, RSH-043, RSH-044, RSH-045, RSH-046, RSH-047, RSH-048, RSH-051, RSH-053, RSH-057, RSH-058, RSH-060, RSH-061, RSH-062, RSH-063, RSH-070, RSH-071, RSH-072, RSH-074, RSH-076 |
| Recovery-layout validation | 1 | RSH-031 |
| Completed no allocator signal | 3 | RSH-049, RSH-050, RSH-075 |
| Allocator-mechanism boundary | 2 | RSH-003, RSH-019 |
| Audit-only | 4 | RSH-054, RSH-056, RSH-059, RSH-073 |
| Removed after storage-domain audit | 1 | RSH-006 |

The positive mechanism union covers **43/48**
executable cases. Type Isolation contributes 12 measured reuse-edge
mitigations, reclaim checks contribute 31 exact duplicate-reclaim detections,
and recovery-layout validation contributes one exact layout diagnostic.
RSH-002 is the only positive overlap.

## Executable UAF subset

| Set or region | Count | Cases |
| --- | ---: | --- |
| Type Isolation only | 11 | RSH-008, RSH-041, RSH-042, RSH-052, RSH-055, RSH-064, RSH-065, RSH-066, RSH-067, RSH-068, RSH-069 |
| Type Isolation and reclaim checks | 1 | RSH-002 |
| Reclaim checks only | 2 | RSH-053, RSH-057 |
| Foreign-allocator no-signal control | 1 | RSH-075 |
| Same-object/pre-reuse concurrency boundary | 2 | RSH-003, RSH-019 |
| Audit-only UAF | 1 | RSH-059 |

The positive union covers **14/17**
executable heap UAF cases. RSH-003 and RSH-019 require temporal-access or
concurrency mechanisms. RSH-075 uses SQLite's C allocator. RSH-006 is outside
the heap denominator.

## Speaker-note boundaries

- **Type Isolation:** causal mitigation of a compiler-bound, measured
  cross-identity reuse edge. This column does not claim full source-level UAF
  detection.
- **Reclaim checks:** exact duplicate-reclaim diagnostic in the vulnerable
  treatment with matched plain and patched controls.
- **Recovery-layout validation:** exact allocation/deallocation layout-mismatch
  diagnostic with Miri-backed ground truth.
- **No allocator signal:** a completed matched experiment with no qualified
  current UniAlloc signal.
- **Mechanism boundary:** the vulnerability requires synchronization or
  ordinary-access temporal validation before allocator reuse.
- **Audit-only:** no valid executable vulnerable/patched witness; these cases
  remain outside every efficacy numerator and denominator.

## Reproduction

```bash
uv run python evaluation/scripts/plot_rustsec_security_sets.py --rasterize
```

Inputs are hash-bound in `rustsec-security-sets-data.json`. All counts remain
exploratory (`claim_grade=false`).
