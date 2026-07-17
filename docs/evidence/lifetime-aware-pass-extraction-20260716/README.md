# Standalone lifetime-pass extraction regression evidence

## Result

The standalone lifetime-aware pass preserves the measured pre-extraction
behavior and passes the runtime regression screen on Bedrock and Convex.
Both rebuilt variants match the checked-in pre-extraction binary and canonical
lifetime-audit pins exactly, then match each other byte-for-byte as complete
executables.

| Target | Immutable binary/audit pin | Lifetime candidates | Median operation slowdown | Median peak-RSS increase | Gate |
|---|---:|---:|---:|---:|---:|
| Bedrock | exact | 60 | +0.0777% | +60 KiB | pass |
| Convex | exact | 71 | +0.3675% | +24 KiB | pass |

Positive operation values mean the standalone-labeled process was slower. The
operation limit is +2%. The peak-RSS limit is
`max(2% of compatibility median, 1024 KiB)`. The two variants produce identical
target binaries, so these small measured differences bound process-level noise;
the exact binary and compiler-audit parity provide the decisive semantic proof.

## Immutable pre-extraction anchors

The runner pins these constants in checked-in code and requires the raw build
record, raw binary, compatibility rebuild, and standalone rebuild to agree:

| Target | Full binary SHA-256 | Canonical lifetime-audit SHA-256 |
|---|---|---|
| Bedrock | `ed26bc99472fd3030394ae877f8a0ea7fdfddc75145f254b2c9d5e405ff12869` | `b1471e9e423946ed15f77df26a7965b240cda261e2f5f3256bb4e8d63308a126` |
| Convex | `55fa4a7eb764c87021c43c1b68e8ff070c84faedeba14569c64163ce7553e1f7` | `b04c9cc050b9dfe42104b61642d58e3969261659b44fcbab24d7bde05c7898af` |

Compatibility and standalone `.text` sections also match exactly. The harness
checks the full executable hash before and after extracting `.text`, preventing
measurement instrumentation from changing the binary.

## Pass provenance

The pass source closure includes each thin entry point plus the shared engine
and lifetime-policy module.

| Variant | Entry point | Source-closure SHA-256 |
|---|---|---|
| Compatibility | `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs` | `5ff7c1044ae3aff1c0af98954786270a588b6b24770609767a092f0cb686290f` |
| Standalone | `tools/unialloc-rustc-pass/unialloc-rustc-lifetime-aware.rs` | `1446349215478e15ec2cc638714ec9cbebf5d3ac13bb22a3939f69e375ce346f` |

Both closures bind the shared engine at
`5d9f3e031386b0747998da5ad4f4b1a98d7633fc3ce61fe055ed4633b607f51b`
and `lifetime_aware.rs` at
`d67c0e12e0f141996423362a2cb8e930b152ebc3d21cf0844ec9553055021ef2`.

## Measurement contract

- Run ID: `lifetime-pass-pinned-final-20260716-2248`
- CPU: 126, matching the original four-arm screen
- Three `AB` plus `BA` pairs per target; 12 fresh processes per target
- Zero warm-up iterations
- Bedrock fixed work: 1,024 chunks, 3,000,000 iterations,
  3,072,000,000 lookups, digest `8ff01d6c0978f587`
- Convex fixed work: 2,731 calendars, 150,000,000 queries, digest
  `5b49db0f0b986115`
- Operation seconds and operation-window peak RSS are separate metrics

Reproduction command:

```sh
python3 evaluation/lifetime-pass-regression-20260716/run_regression.py \
  --run-id lifetime-pass-pinned-final-20260716-2248 \
  --standalone-source \
    tools/unialloc-rustc-pass/unialloc-rustc-lifetime-aware.rs \
  --targets bedrock convex \
  --pairs 3 \
  --cpu 126 \
  --jobs 8
```

`summary.json` contains the pass closure, immutable reference gates, binary and
audit parity, fixed-work identities, separate performance/RSS gates, and hashes
of the retained raw result files. Large raw build and per-process artifacts stay
under the ignored `evaluation/lifetime-pass-regression-20260716/raw/` tree and
are intentionally excluded from this compact evidence package.

This is a two-target fixed-work regression screen with six samples per variant
per target. It establishes extraction equivalence and guards against a material
runtime regression; it does not provide a confidence interval for a new
performance improvement claim.
