# RSH-073 integration blocker

`RUSTSEC-2026-0139` publishes a safe-API double-free witness for `metacall`
0.5.10 in upstream issue [metacall/core#618]. The exact `MetaCallPointer`
witness is retained in `origin.rs` and `main.rs`, and both harness locks pin the
published 0.5.10 crate archive.

A faithful standalone build requires the native MetaCall core library and its C
headers. On this evaluation host the crate's build script exited 101 before
compiling the witness and reported:

```text
MetaCall library not found. Searched in: /usr/local/lib/, /gnu/lib/.
If you have it installed elsewhere, set METACALL_INSTALL_PATH environment variable.
```

Neither `/usr/local/lib` nor `/gnu/lib` contains `libmetacall`, and the host has
no MetaCall installation prefix to supply through `METACALL_INSTALL_PATH`.
Installing or mocking that native runtime would add an external system under
test and could change the ownership behavior exercised by the published PoC.
This case is therefore excluded from executable counts on this host and remains
ready for a worker image containing a pinned native MetaCall build.

[metacall/core#618]: https://github.com/metacall/core/issues/618
