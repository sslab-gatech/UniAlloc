# Blocked-case reattempt report (2026-07-14)

Archived evidence root:
`docs/evidence/rustsec-all-53-live-20260714/blocked-attempts`
Machine-readable record: `summary.json` (37 captured commands, exact exit codes, output hashes, and 19 critical artifact hashes).

## Result

| Case | Advisory / crate | Reattempt result | Current disposition |
|---|---|---|---|
| RSH-054 | RUSTSEC-2020-0105 / `abi_stable` | Vulnerable and patched archives build and reach the injected predicate panic under native execution and Miri. The public adapter produces no root-specific duplicate-reclaim diagnostic. | Blocked by missing public root-cause PoC/oracle. |
| RSH-056 | RUSTSEC-2021-0005 / `glsl-layout` | ASan reaches only the intentional panic in both controls. Vulnerable Miri stops earlier on independent uninitialized `[f32; 2]` UB; patched Miri reaches the panic. | Blocked by shadowed root-cause oracle. |
| RSH-059 | RUSTSEC-2021-0022 / `yottadb` | `pkg-config` cannot find `yottadb.pc`; both pinned build scripts stop before compiling the witness. | Blocked by native library plus initialized-database prerequisites. |
| RSH-064 | RUSTSEC-2022-0028 / `neon` | **Unblocked.** Node v20.20.2 loads a real N-API addon. neon 0.10.0 returns corrupted external ArrayBuffer bytes from the published stale-slice witness; neon 0.10.1 rejects the same source with E0597/E0505 due the new `'static` bound. | Newly executable: vulnerable runtime + patched compile-rejection control. |
| RSH-073 | RUSTSEC-2026-0139 / `metacall` | `METACALL_INSTALL_PATH` is unset; no core library or headers are present; both retained builds stop before witness compilation. | Blocked by native MetaCall core/headers; no published fixed crate control. |

The blocked set shrinks from five to four after this reattempt. RSH-064 raises the executable corpus from 48 to 49 once integrated into the tracked catalog.

## RSH-064 UniAlloc attribution probe

The addon was rebuilt twice with direct repository UniAlloc identity:

- `reclaim_plain`: Cargo metadata features `[stats]`; build and Node execution exit 0.
- `reclaim_checks`: Cargo metadata features `[reclaim_checks, stats]`; build and Node execution exit 0.

Both runs expose stale bytes (`0,0,0,0` instead of `0,1,2,3`) and emit no exact `pointer already released` family signal. This matches the primitive: one valid free is followed by a stale Node/V8 read; duplicate reclaim never occurs.

At this reattempt stage, the standalone RustSec launcher lacked a Node-addon
and cdylib audit path, so this artifact records no direct Type Isolation claim.
The subsequent dedicated runner added that path; its three-repetition result is
`../rsh064/experiment.json` and remains compiler-coverage inconclusive.

## Reproduction anchors

- System vulnerable addon source: `projects/RSH-064/vulnerable/src/lib.rs`
- Feature-appropriate vulnerable lock: `projects/RSH-064/vulnerable/Cargo.lock`
- Frozen legacy lock: `projects/RSH-064/vulnerable/Cargo.lock.frozen-source`
- Patched compile-rejection log: `logs/RSH-064-patched-build-derived-lock.stderr`
- Node runtime transcript: `logs/RSH-064-vulnerable-node-run.pty`
- UniAlloc reclaim transcripts: `logs/RSH-064-reclaim_plain-node-run.pty`, `logs/RSH-064-reclaim_checks-node-run.pty`
- Exact no-signal search: `logs/RSH-064-reclaim-signal-exact.*`
- Repository source digest used for direct UniAlloc builds: `logs/RSH-064-unialloc-source-digest.txt`
