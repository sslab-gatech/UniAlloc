use scc::TreeIndex;
use std::cmp::Ordering;
use std::panic::{catch_unwind, AssertUnwindSafe};

#[derive(Clone, Debug)]
struct PanicKey {
    rank: u64,
    owner: Box<u64>,
    panic_in_compare: bool,
}

impl PanicKey {
    fn new(rank: u64, panic_in_compare: bool) -> Self {
        Self {
            rank,
            owner: Box::new(rank),
            panic_in_compare,
        }
    }
}

impl PartialEq for PanicKey {
    fn eq(&self, other: &Self) -> bool {
        self.rank == other.rank
    }
}

impl Eq for PanicKey {}

impl PartialOrd for PanicKey {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for PanicKey {
    fn cmp(&self, other: &Self) -> Ordering {
        assert_eq!(*self.owner, self.rank);
        if self.panic_in_compare {
            panic!("intentional comparator panic from the published exception-safety edge");
        }
        self.rank.cmp(&other.rank)
    }
}

fn main() {
    let tree = TreeIndex::<PanicKey, Box<u64>>::new();
    tree.insert_sync(PanicKey::new(1, false), Box::new(10))
        .unwrap();

    let unwound = catch_unwind(AssertUnwindSafe(|| {
        tree.upsert_sync(PanicKey::new(2, true), Box::new(20));
    }));
    assert!(unwound.is_err());

    // scc 3.8.3 has already bit-copied the new key/value into the leaf before
    // comparing. Unwinding drops the originals; dropping the tree reclaims the
    // copied owners again. 3.8.4 establishes ownership with ManuallyDrop first.
    drop(tree);
}
