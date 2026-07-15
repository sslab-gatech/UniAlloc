//! Published advisory example for RUSTSEC-2021-0022.
//!
//! Execution requires an installed and initialized YottaDB runtime. The
//! integration status records that prerequisite as an evidence-backed blocker
//! on the current host while preserving this source-level witness.

#![forbid(unsafe_code)]

use yottadb::{Key, YDB_NOTTP};

fn main() {
    let mut key = Key::variable(String::from("a"));
    Key::variable("averylongkeywithlotsofletters")
        .set_st(YDB_NOTTP, Vec::new(), b"some val")
        .unwrap();
    key.sub_next_self_st(YDB_NOTTP, Vec::new()).unwrap();
}
