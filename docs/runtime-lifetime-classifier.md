# Marker-Free Runtime Lifetime Classification

## Result and claim boundary

UniAlloc can now learn allocation-site lifetime from allocator-observed survival
without application epoch calls. The allocator uses subsequent allocation
pressure as its clock, treats compiler lifetime metadata as a weak prior, and
uses runtime outcomes as the authority for future placement.

The 2026-07-14 real-application campaign establishes a mechanism result:

- 68.49% of eligible allocations received a learned decision;
- 23.66% of exact allocation sites reached a stable class;
- 193 sampled learned predictions received a later runtime outcome, with 100%
  conditional accuracy;
- every sampled learned prediction was Short, so this result supplies no
  real-application Long precision, Long recall, or THP benefit claim;
- the feature-parity performance comparison measured a 0.15% aggregate
  median-time-weighted overhead and a 4.07% geometric-mean overhead;
- a separate host-gated synthetic probe learned one Long site with zero
  semantic epoch calls and realized one 2 MiB `AnonHugePages` mapping.

The strongest presentation claim is **marker-free runtime validation with
conservative selective placement and a realized-THP mechanism proof**. Stable
Long discovery and net THP benefit in real applications remain evaluation
targets.

## What an epoch means

The runtime classifier defines time in bytes of later allocation pressure:

```text
pressure = cumulative allocator-rounded eligible exact-site payload bytes
age      = pressure_at_free - pressure_at_allocation

Short    when age < 2 MiB
Censored when 2 MiB <= age < 8 MiB
Long     when age >= 8 MiB
```

One pressure epoch equals 2 MiB, the target THP extent size. Tracked allocations
and model-directed base-allocator bypasses both advance pressure; an adaptive
arena mapping failure is excluded and claim-grade evaluation requires zero such
failures. The definition measures whether an object occupies allocator space
while the program creates enough later demand to refill one or more extents.
Idle wall-clock time has no effect. The existing explicit semantic epoch API
remains available for applications with synchronized request, transaction, or
iteration barriers.

The 2 MiB and 8 MiB thresholds are preregistered experimental parameters. They
are easy to sweep and should be treated as workload-policy choices rather than
universal lifetime constants.

## Exact site identity

Learning state is keyed by:

```text
(callsite, type_id, module_id, flags, placement_hint,
 payload_capacity, alignment)
```

This separates types, modules, semantic flags, size classes, alignments, and
allocation callsites. `lifetime_hint` stays outside the key because it is
evidence about the same site, rather than part of the site's identity. A later
compiler decision can therefore update or conflict with the original prior
without splitting runtime history into artificial sites.

The table contains 4,096 fixed entries with 16 bounded probes and no eviction.
Missing exact identity, delayed-free metadata, unsupported layouts, and table
saturation use the ordinary allocator path.

## Learning rule

Each new site starts Cold with a small Bayesian-style vote prior:

| Compiler/runtime metadata | Short votes | Long votes |
|---|---:|---:|
| `Unknown` | 1 | 1 |
| `Ephemeral` | 3 | 1 |
| `LongLived` | 1 | 3 |

Runtime evidence controls state transitions:

- at least eight decisive runtime samples are required before a Cold site can
  become stable;
- `Cold -> Long` requires at least 80% Long votes;
- `Cold -> Short` requires at most 20% Long votes;
- a Long site returns to Cold below 60% Long votes;
- a Short site returns to Cold above 40% Long votes;
- demotion requires at least four decisive samples after the preceding
  transition;
- vote counters decay every 256 decisive observations so workload drift can
  change a site;
- outcomes in the 2--8 MiB interval are recorded as censored and add no vote.

Conflicting concrete static hints permanently neutralize the site's static
prior. Runtime observations continue from a neutral 1/1 state. This rule keeps
stale or inconsistent compiler metadata from repeatedly overriding measured
behavior.

## Allocation and placement policy

The opt-in policy is
`LifetimeHugepagePolicy::AdaptiveRuntimeHugepage`. It requires the
`TransparentHugepage` backend.

| Learned state | Allocation behavior | Linux advice |
|---|---|---|
| Cold | Track the first eight total observations with at most eight concurrent samples, then sample one allocation in 256 | `MADV_NOHUGEPAGE` for sampled allocations |
| Short | Use the default allocator for 255 of every 256 allocations; route one sample to detect drift | `MADV_NOHUGEPAGE` for sampled allocations |
| Long | Route every future allocation to a lifetime-isolated, 2 MiB-aligned extent | eager `MADV_HUGEPAGE` |

Classification changes affect future allocations. Existing live objects retain
their allocation-time placement and provenance.

Cold-site training limits and low-frequency Cold/Short resampling are important
performance controls. They bound the 32-byte observation trailer, mappings,
and lifetime arena cost even when every early outcome is censored, while
preserving held-out outcomes that can detect a workload change.

## Runtime observation record

Each tracked slot has a hidden 32-byte trailer after its user-visible payload:

```text
birth_pressure, site_fingerprint, site_index, requested_size,
allocation_time_prediction, static_hint, flags, checksum
```

The payload capacity excludes the trailer. In-place reallocations must fit
within payload capacity, which prevents user writes from reaching classifier
metadata. Moving reallocations close the old observation and begin a new one.
Pointer-local trailer provenance supports cross-thread deallocation. A checksum
or fingerprint failure records a corruption event and suppresses learning for
that observation.

The site table, pressure clock, trailer updates, and arena operations share the
existing process-wide arena lock. This gives observations one exact cross-thread
order without adding another lock relationship.

## Static prediction validation

The trailer retains both the allocation-time learned prediction and the static
hint. When an object is freed, the runtime records two prequential confusion
matrices:

1. the learned classifier versus the later pressure-defined outcome;
2. the compiler/static hint versus the same later outcome, with `Unknown`
   counted as abstention.

This directly tests the intended compiler-runtime contract. Compiler analysis
can supply a weak cold-start prior; runtime survival checks it and controls
steady-state placement.

The first strict MIR classifier has a hard coverage problem. It requires a
unique exact owner path from allocation through Drop and an explicit semantic
epoch call for Long evidence. Receiver-owned allocations, ownership-preserving
moves, aliases, loops, opaque calls, and cleanup paths often force abstention.
Across the pinned ripgrep, fd, and Oxipng smoke, the compiler classified 0 of
667 semantic candidate sites. In the runtime campaign, all 588 decisive
observations therefore carried `Unknown` static hints, yielding 0% static-hint
coverage. The marker-free pressure clock supplies useful learning despite that
compiler abstention.

## Real-application evaluation

### Method

- applications: pinned ripgrep, fd, and Oxipng checkouts;
- allocator features in both arms: `type_isolation,lifetime_hugepage`;
- control: `AdaptiveRuntimeHugepage` configured off;
- treatment: `AdaptiveRuntimeHugepage` with THP configured on;
- quick workloads, CPU 20 and NUMA node 0;
- three warmups and 15 measured runs per mode;
- identical output hashes between off and adaptive modes for every app;
- off and adaptive campaigns ran sequentially, so bootstrap intervals capture
  within-campaign sampling and exclude temporal drift between campaigns.

### Performance

Positive values are overhead. Lower values are better.

| Application | Off median | Adaptive median | Wall-time delta | Bootstrap 95% interval | Median RSS delta |
|---|---:|---:|---:|---:|---:|
| fd | 0.05242 s | 0.05624 s | +7.29% | +2.61% to +16.80% | 0 KiB |
| Oxipng | 1.77577 s | 1.77326 s | -0.14% | -0.29% to -0.02% | +160 KiB |
| ripgrep | 0.02708 s | 0.02849 s | +5.20% | +2.58% to +7.78% | +1,024 KiB |

The weighted result is dominated by Oxipng and measures +0.15% overhead. The
geometric mean gives each app equal weight and measures +4.07% overhead. The
fd and ripgrep quick workloads are short enough for fixed startup and
classifier costs to dominate. The ripgrep RSS delta is one 1,024 KiB reporting
quantum and should be treated as coarse-process noise rather than a
fragmentation result.

### Classification success and failure

| Metric | Result | Interpretation |
|---|---:|---|
| Exact sites | 131 | Runtime table entries observed across the three processes |
| Stable Short sites | 31 | Successful site classifications |
| Stable Long sites | 0 | No real-app Long placement evidence |
| Cold sites | 100 | Sites lacking enough decisive evidence |
| Site classification coverage | 23.66% | Stable sites / exact sites |
| Eligible allocations | 74,494 | Allocations considered by the policy |
| Learned allocation decisions | 51,024 | Short or Long decisions applied |
| Allocation decision coverage | 68.49% | Learned decisions / eligible allocations |
| Decisive observations | 588 | 574 Short and 14 Long outcomes |
| Sampled learned predictions | 193 | Predictions later checked at free |
| Conditional sampled accuracy | 100% | 193 true Short predictions; a short-only result |
| Static hints classified | 0 | Strict compiler analysis abstained throughout |
| Missing-identity bypasses | 1 | Conservative fallback worked |
| Site-table bypasses | 0 | Fixed table remained sufficient |
| Trailer corruptions | 0 | Observation metadata checks passed |
| THP routes/advice attempts | 0 | No stable Long site emerged |

### Synthetic realized-THP probe

A standalone host-gated probe trains one exact site with eight Long outcomes,
using 8 MiB of later byte pressure per outcome and zero semantic epoch calls.
It resets telemetry while preserving the learned model, then allocates and
touches 480 objects from the learned-Long site. Those objects fill exactly one
allocator extent.

On this host the probe recorded:

- one stable Long site and eight Long training observations;
- 480 learned-Long routes into one THP extent;
- one successful `MADV_HUGEPAGE` advice operation;
- one exact 2 MiB pointer-containing VMA with `VmFlags: hg`;
- `AnonHugePages: 2048 kB` from `/proc/self/smaps`;
- zero semantic epoch calls and complete mapping/trailer cleanup.

This establishes allocator-pressure Long learning, selective routing, and
realized THP backing on one host and one synthetic workload. The real-app result
measures classifier overhead and Short learning; application speedup and
fragmentation benefit remain unobserved.

## Reset and experiment lifecycle

`lifetime_hugepage_stats_reset()` clears telemetry and pressure only after all
arena mappings are released. While the adaptive policy is active, it preserves
the learned site model so a clean measurement window can follow warmup. To
discard the model, configure `Disabled` before resetting statistics.

## Presentation guidance

Use this sequence:

1. **Problem:** exact static lifetime proof abstains on ordinary Rust ownership
   and control flow.
2. **Mechanism:** exact compiler identity plus a weak hint prior feeds a
   marker-free allocator pressure clock.
3. **Safety:** eight-sample promotion, hysteresis, censoring, fixed tables,
   bounded sampling, and ordinary-allocation fallback.
4. **Evidence:** 68.49% of eligible real-app allocations receive learned Short
   decisions; sampled Short decisions are correct in this corpus.
5. **Boundary:** zero stable real-app Long sites and zero THP routes; the current
   net result is overhead.
6. **Next claim gate:** evaluate applications with persistent caches or indexes,
   require held-out Long precision, byte-weighted false-positive rate,
   real-app per-VMA `AnonHugePages` coverage, fragmentation, RSS, faults, and
   wall time.

The slide-ready figure is
[`figures/runtime-lifetime-classifier-20260714/summary.svg`](figures/runtime-lifetime-classifier-20260714/summary.svg).
Raw and summarized evidence is documented in
[`evidence/runtime-lifetime-classifier-20260714/README.md`](evidence/runtime-lifetime-classifier-20260714/README.md).
