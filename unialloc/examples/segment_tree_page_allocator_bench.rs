use std::alloc::{alloc, dealloc, Layout};
use std::hint::black_box;
use std::time::{Duration, Instant};

use unialloc::bitmap_alloc::{RunNode, SegmentPageAllocator};

const PAGE_COUNT: usize = 4096;
const LEAF_PAGES: usize = u64::BITS as usize;
const TRIALS: usize = 7;

const LEAF_RUN_PAGES: usize = 8;
const CROSS_RUN_START: usize = LEAF_PAGES - 4;
const CROSS_RUN_PAGES: usize = 12;
const CROSS_ALIGN_PAGES: usize = 4;
const CROSS_PREFIX_PAGES: usize = LEAF_PAGES * 8;
const LONG_RUN_PAGES: usize = LEAF_PAGES * 2 + 2;
const UPDATE_CHILD_PAGES: usize = LEAF_PAGES;
const UPDATE_MERGED_PAGES: usize = LEAF_PAGES * 2;

const LEAF_ITERATIONS: usize = 500_000;
const CROSS_ITERATIONS: usize = 250_000;
const LONG_ITERATIONS: usize = 200_000;
const UPDATE_ITERATIONS: usize = 150_000;

struct HeapRange {
    ptr: *mut u8,
    layout: Layout,
}

impl HeapRange {
    fn new() -> Self {
        let page_size = unialloc::PAGE_SIZE;
        let layout = Layout::from_size_align(PAGE_COUNT * page_size, LEAF_PAGES * page_size)
            .expect("valid direct segment-tree benchmark heap layout");
        let ptr = unsafe { alloc(layout) };
        assert!(!ptr.is_null(), "failed to reserve benchmark heap");
        Self { ptr, layout }
    }
}

impl Drop for HeapRange {
    fn drop(&mut self) {
        unsafe { dealloc(self.ptr, self.layout) };
    }
}

struct DirectTree {
    allocator: SegmentPageAllocator,
    _nodes: Vec<RunNode>,
    heap: HeapRange,
}

impl DirectTree {
    fn new() -> Self {
        let heap = HeapRange::new();
        let nodes_len = SegmentPageAllocator::required_node_count(PAGE_COUNT)
            .expect("benchmark page count fits segment-tree metadata");
        let mut nodes = vec![RunNode::EMPTY; nodes_len];
        let mut allocator = SegmentPageAllocator::empty();
        unsafe {
            allocator
                .initialize(
                    heap.ptr as usize,
                    PAGE_COUNT,
                    unialloc::PAGE_SIZE,
                    nodes.as_mut_ptr(),
                    nodes.len(),
                )
                .expect("initialize direct segment-tree benchmark allocator");
        }
        Self {
            allocator,
            _nodes: nodes,
            heap,
        }
    }

    #[inline]
    fn allocate(&mut self, pages: usize, align_pages: usize) -> *mut u8 {
        let ptr = self
            .allocator
            .allocate_bytes(
                pages * unialloc::PAGE_SIZE,
                align_pages * unialloc::PAGE_SIZE,
            )
            .expect("direct segment-tree benchmark allocation");
        black_box(ptr)
    }

    #[inline]
    fn deallocate(&mut self, ptr: *mut u8, pages: usize) {
        self.allocator
            .deallocate_bytes(ptr, pages * unialloc::PAGE_SIZE)
            .expect("direct segment-tree benchmark deallocation");
    }

    fn page_index(&self, ptr: *mut u8) -> usize {
        (ptr as usize - self.heap.ptr as usize) / unialloc::PAGE_SIZE
    }
}

fn elapsed_per_op(elapsed: Duration, operations: usize) -> f64 {
    elapsed.as_secs_f64() * 1_000_000_000.0 / operations as f64
}

fn print_result(name: &str, trial: usize, elapsed: Duration, operations: usize) {
    println!(
        "{{\"workload\":\"{}\",\"trial\":{},\"operations\":{},\"elapsed_ns\":{},\"ns_per_op\":{:.3}}}",
        name,
        trial,
        operations,
        elapsed.as_nanos(),
        elapsed_per_op(elapsed, operations)
    );
}

fn benchmark<F>(name: &str, mut workload: F)
where
    F: FnMut() -> (Duration, usize),
{
    let mut samples = Vec::with_capacity(TRIALS);
    let mut operations = 0usize;
    for trial in 1..=TRIALS {
        let (elapsed, trial_operations) = workload();
        print_result(name, trial, elapsed, trial_operations);
        operations = trial_operations;
        samples.push(elapsed_per_op(elapsed, trial_operations));
    }
    samples.sort_by(|left, right| left.partial_cmp(right).unwrap());
    println!(
        "{{\"summary\":\"median\",\"workload\":\"{}\",\"trials\":{},\"operations_per_trial\":{},\"ns_per_op\":{:.3}}}",
        name,
        TRIALS,
        operations,
        samples[TRIALS / 2]
    );
}

fn leaf_local_reuse(tree: &mut DirectTree, iterations: usize) -> (Duration, usize) {
    let started = Instant::now();
    for _ in 0..iterations {
        let ptr = tree.allocate(LEAF_RUN_PAGES, 1);
        tree.deallocate(ptr, LEAF_RUN_PAGES);
    }
    (started.elapsed(), iterations * 2)
}

fn prepare_cross_leaf_tree() -> DirectTree {
    let mut tree = DirectTree::new();
    let mut prefix = Vec::with_capacity(CROSS_PREFIX_PAGES);
    for expected_page in 0..CROSS_PREFIX_PAGES {
        let ptr = tree.allocate(1, 1);
        assert_eq!(tree.page_index(ptr), expected_page);
        prefix.push(ptr);
    }
    for ptr in prefix
        .iter()
        .skip(CROSS_RUN_START)
        .take(CROSS_RUN_PAGES)
        .copied()
    {
        tree.deallocate(ptr, 1);
    }
    tree
}

fn aligned_cross_leaf_reuse(tree: &mut DirectTree, iterations: usize) -> (Duration, usize) {
    let started = Instant::now();
    for _ in 0..iterations {
        let ptr = tree.allocate(CROSS_RUN_PAGES, CROSS_ALIGN_PAGES);
        debug_assert_eq!(tree.page_index(ptr), CROSS_RUN_START);
        tree.deallocate(ptr, CROSS_RUN_PAGES);
    }
    (started.elapsed(), iterations * 2)
}

fn long_run_root_reuse(tree: &mut DirectTree, iterations: usize) -> (Duration, usize) {
    let started = Instant::now();
    for _ in 0..iterations {
        let ptr = tree.allocate(LONG_RUN_PAGES, 1);
        tree.deallocate(ptr, LONG_RUN_PAGES);
    }
    (started.elapsed(), iterations * 2)
}

fn cross_leaf_coalesce_updates(tree: &mut DirectTree, iterations: usize) -> (Duration, usize) {
    let started = Instant::now();
    for _ in 0..iterations {
        let left = tree.allocate(UPDATE_CHILD_PAGES, 1);
        let right = tree.allocate(UPDATE_CHILD_PAGES, 1);
        debug_assert_eq!(tree.page_index(left), 0);
        debug_assert_eq!(tree.page_index(right), UPDATE_CHILD_PAGES);
        tree.deallocate(left, UPDATE_CHILD_PAGES);
        tree.deallocate(right, UPDATE_CHILD_PAGES);

        let merged = tree.allocate(UPDATE_MERGED_PAGES, 1);
        debug_assert_eq!(tree.page_index(merged), 0);
        tree.deallocate(merged, UPDATE_MERGED_PAGES);
    }
    (started.elapsed(), iterations * 6)
}

fn run_leaf() {
    let mut tree = DirectTree::new();
    let _ = leaf_local_reuse(&mut tree, 2_000);
    benchmark("leaf_local_reuse", || {
        leaf_local_reuse(&mut tree, LEAF_ITERATIONS)
    });
}

fn run_cross() {
    let mut tree = prepare_cross_leaf_tree();
    let probe = tree.allocate(CROSS_RUN_PAGES, CROSS_ALIGN_PAGES);
    assert_eq!(tree.page_index(probe), CROSS_RUN_START);
    tree.deallocate(probe, CROSS_RUN_PAGES);
    let _ = aligned_cross_leaf_reuse(&mut tree, 2_000);
    benchmark("aligned_cross_leaf_reuse", || {
        aligned_cross_leaf_reuse(&mut tree, CROSS_ITERATIONS)
    });
}

fn run_long() {
    let mut tree = DirectTree::new();
    let _ = long_run_root_reuse(&mut tree, 2_000);
    benchmark("long_run_root_reuse", || {
        long_run_root_reuse(&mut tree, LONG_ITERATIONS)
    });
}

fn run_update() {
    let mut tree = DirectTree::new();
    let left = tree.allocate(UPDATE_CHILD_PAGES, 1);
    let right = tree.allocate(UPDATE_CHILD_PAGES, 1);
    assert_eq!(tree.page_index(left), 0);
    assert_eq!(tree.page_index(right), UPDATE_CHILD_PAGES);
    tree.deallocate(left, UPDATE_CHILD_PAGES);
    tree.deallocate(right, UPDATE_CHILD_PAGES);
    let merged = tree.allocate(UPDATE_MERGED_PAGES, 1);
    assert_eq!(tree.page_index(merged), 0);
    tree.deallocate(merged, UPDATE_MERGED_PAGES);
    let _ = cross_leaf_coalesce_updates(&mut tree, 2_000);
    benchmark("cross_leaf_coalesce_updates", || {
        cross_leaf_coalesce_updates(&mut tree, UPDATE_ITERATIONS)
    });
}

fn main() {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "all".to_owned());
    assert!(
        matches!(mode.as_str(), "all" | "leaf" | "cross" | "long" | "update"),
        "usage: segment_tree_page_allocator_bench [all|leaf|cross|long|update]"
    );
    println!(
        "{{\"backend\":\"segment_tree_direct\",\"mode\":\"{}\",\"page_size\":{},\"page_count\":{},\"trials\":{}}}",
        mode,
        unialloc::PAGE_SIZE,
        PAGE_COUNT,
        TRIALS
    );

    if matches!(mode.as_str(), "all" | "leaf") {
        run_leaf();
    }
    if matches!(mode.as_str(), "all" | "cross") {
        run_cross();
    }
    if matches!(mode.as_str(), "all" | "long") {
        run_long();
    }
    if matches!(mode.as_str(), "all" | "update") {
        run_update();
    }
}
