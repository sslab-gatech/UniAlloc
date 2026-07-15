//! RUSTSEC-2019-0023 / string-interner 0.7.0.
//!
//! The standalone reproducer published in upstream issue 9. The cloned
//! interner retains raw string references into `old`; `get_or_intern` reads
//! them after `old` is dropped.

use string_interner::{DefaultStringInterner, Sym};

fn clone_and_drop() -> (DefaultStringInterner, Sym) {
    let mut old = DefaultStringInterner::new();
    let foo = old.get_or_intern("foo");
    let new = old.clone();
    let _bar = old.get_or_intern("bar");
    (new, foo)
}

fn main() {
    let (mut new, foo) = clone_and_drop();
    assert_eq!(
        new.get_or_intern("foo"),
        foo,
        "`foo` should represent the string \"foo\" so they should be equal"
    );
}
