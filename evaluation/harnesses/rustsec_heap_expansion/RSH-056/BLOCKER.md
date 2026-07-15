# RSH-056 execution blocker

- Advisory: RUSTSEC-2021-0005 (`glsl-layout` 0.3.2; fixed in 0.4.0).
- Stable blocker code: `published_reproducer_absent_and_root_cause_oracle_shadowed`.
- The pinned Rudra record explicitly states that no proof of concept was
  created. The exact record is retained in `origin.rs` at Rudra-PoC commit
  `6226dd030fffbed5601099cb0e24f73e4150a7f5`.
- `main.rs` reaches the cited `MapArray` conversion through the public `vec2`
  conversion and injects the published panic condition with a heap-owning
  input value.
- Native AddressSanitizer runs of both 0.3.2 and 0.4.0 reach only the
  intentional conversion panic and report no duplicate reclaim.
- Current Miri stops the 0.3.2 arm earlier at the independent construction of
  an uninitialized `[f32; 2]`; the 0.4.0 arm reaches only the intentional
  panic with leak checking disabled. That version split proves an unsafe
  implementation difference, while the vulnerable diagnostic does not
  validate the advisory's cited panic-time duplicate-drop edge.
- This case remains source evidence and is excluded from executable baseline
  counts to keep root-cause attribution strict.
