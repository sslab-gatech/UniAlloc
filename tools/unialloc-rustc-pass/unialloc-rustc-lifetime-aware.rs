#![feature(rustc_private)]

//! Standalone compiler-directed lifetime-aware allocation pass.
//!
//! This entry point selects the measured semantic-scope rewrite plus automatic
//! Rust lifetime-prior path and fails closed on incompatible placement or
//! direct-allocator rewrite configuration.

#[cfg(unialloc_rustc_current)]
extern crate rustc_abi;
extern crate rustc_ast;
#[cfg(unialloc_rustc_current)]
extern crate rustc_data_structures;
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

#[path = "unialloc-rustc-driver-engine.rs"]
mod engine;

fn main() {
    engine::run(engine::PassMode::LifetimeAware);
}
