# DataFusion: cold runtime profile versus compiler hint

## Verdict

On the same compiler-instrumented DataFusion 54.0.0 binary, policy 9 direct
compiler-hint placement beat policy 11 cold online profiling in both AB/BA
orders. Median query speedup was **7.226%** and median fixed-operation speedup,
including resident construction and online learning, was **7.221%**. The
compiler-hint arm used **4.052% more peak RSS**.

| Order | Runtime profile query | Compiler hint query | Hint speedup | Hint RSS change |
| --- | ---: | ---: | ---: | ---: |
| policy11 -> policy9 | 21.955670 s | 20.037876 s | 8.735% | +4.544% |
| policy9 -> policy11 | 20.412071 s | 19.244999 s | 5.718% | +3.560% |

## Matched contract

- Binary SHA-256: `8e405c386cc228b6ffd44afe31158e360d171b3ace520e5e8912f78ee49da615`
- Fixed work: g1024, target partitions 1, 896 queries, 256 resident batches.
- Fresh process per arm, zero benchmark warm-up iterations, CPUs 120-123.
- All four arms produced the same materialized, query, result, and
  approximate-distinct digests.
- Host THP stayed on `madvise`; the experiment changed no THP sysfs setting.

Policy 11 preserves the exact compiler-emitted allocation-site identity while
normalizing the static lifetime hint to Unknown for prediction and arena
identity. It therefore begins Cold in every process and pays learning,
trailers, pressure accounting, global arena locking, placement, and collapse
inside measured work. Policy 9 uses the compiler Long hint directly and keeps
runtime learning dormant.

## Decisive mechanism evidence

Policy 11 deterministically observed 192 predictor sites and processed
3,742,201 adaptive-eligible allocations. Those allocations reach
classification after acquiring the single arena lock. It routed only 31,807:
4,618 training, 10,638 Short, and 16,551 Long allocations. It also bypassed
988,039 Cold and 2,722,355 learned-Short allocations after entering adaptive
classification. The lower-bound lock traffic is 4.079 times policy 9's 917,504
direct routes.

The runtime classifier labels an object Short when it is released before 2 MiB
of later allocation pressure and Long after it survives 8 MiB; the middle band
is censored. A prior-free site requires at least eight decisive samples. Each
policy11 arm processed 15,976,278,432 pressure bytes, performed 2,875 survivor
scans, promoted 82 sites, demoted 5, and ended with 47 valid live trailers plus
zero trailer corruptions.

Policy 11 reported five point-in-time collapse successes per arm, followed by
zero `AnonHugePages` in every conservative query-window sample and zero current
collapse-confirmed extents at exit. Policy 9 retained eight confirmed extents
and showed exactly 16 MiB `AnonHugePages` in every query-window sample. This is
the decisive placement difference: online profiling learned useful Long sites,
yet its transient cohorts failed to retain dense physical THP backing.

## Claim boundary

This is a whole-path comparison: online observation, classification, trailers,
locking, placement, and density promotion versus direct compiler-hint
admission, packing, collapse, and reuse. It isolates the control strategy on an
identical binary; it does not assign the 7.226% delta to one individual runtime
instruction. Two AB/BA pairs provide directional evidence without confidence
intervals, and concurrent host activity can widen timing variance.

Machine-readable evidence is in `aggregate.json`. Raw per-process evidence is
under `evaluation/raw/datafusion-runtime-profile-vs-hint-20260716/`.
