use std::alloc::{GlobalAlloc, Layout};
#[cfg(feature = "fixed_heap")]
use std::mem::{size_of, MaybeUninit};
use std::process;

#[cfg(feature = "fixed_heap")]
use unialloc::alloc_api::{__unialloc_alloc_with_metadata, __unialloc_dealloc_with_metadata};
#[cfg(feature = "fixed_heap")]
use unialloc::{
    __unialloc_constrained_boot_sample_abi_version, __unialloc_constrained_boot_sample_checked,
    __unialloc_constrained_boot_sample_size, fixed_heap_ready, semantic_stats_reset,
    semantic_type_stats_snapshot, unialloc_alloc, unialloc_dealloc, unialloc_fixed_heap_ready,
    unialloc_fixed_heap_try_extend, unialloc_fixed_heap_try_init, unialloc_realloc,
    SemanticTypeStatsSnapshot, UniAlloc, UniallocConstrainedBootSample,
    CONSTRAINED_BOOT_SAMPLE_ABI_VERSION, FLAG_TYPE_ISOLATED,
};
#[cfg(not(feature = "fixed_heap"))]
use unialloc::{fixed_heap_ready, UniAlloc};

// In fixed-heap mode the heap must be explicitly registered before the first
// UniAlloc allocation.  Making UniAlloc the process global allocator would let
// the std runtime allocate before `main`, aborting before this smoke test can
// initialize the mmap-backed range.  The fixed-heap build therefore exercises
// the same allocator instance through `GlobalAlloc` calls after `try_init`.
#[cfg_attr(not(feature = "fixed_heap"), global_allocator)]
static A: UniAlloc = UniAlloc;

const SMALL_HEAP_PAGE_SIZE: usize = unialloc::PAGE_SIZE;
const HEAP_SIZE: usize = 50 * SMALL_HEAP_PAGE_SIZE;
#[cfg(feature = "fixed_heap")]
const SEMANTIC_PROBE_TYPE_ID: u64 = 0x534d_414c_4c48_4541;
#[cfg(feature = "fixed_heap")]
const SEMANTIC_PROBE_MODULE_ID: u64 = 0x534d_414c_4c48_504d;
#[cfg(feature = "fixed_heap")]
const SEMANTIC_PROBE_CALLSITE: u64 = 0x534d_414c_4c48_4353;

#[cfg(feature = "fixed_heap")]
unsafe fn init_unialloc_fixed_heap(heap_start: usize, heap_size: usize, page_size: usize) -> bool {
    unialloc_fixed_heap_try_init(heap_start, heap_size, page_size)
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn init_unialloc_fixed_heap(heap_start: usize, heap_size: usize, page_size: usize) -> bool {
    A.try_init(heap_start, heap_size, page_size)
}

#[cfg(feature = "fixed_heap")]
unsafe fn try_extend_unialloc_fixed_heap(size: usize, page_size: usize) -> bool {
    unialloc_fixed_heap_try_extend(size, page_size)
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn try_extend_unialloc_fixed_heap(size: usize, page_size: usize) -> bool {
    A.try_extend(size, page_size)
}

#[derive(Clone, Copy)]
struct BatchReport {
    allocations: usize,
    bytes: usize,
    checksum: u64,
}

#[derive(Clone, Copy)]
struct CAbiReport {
    invoked: bool,
    ready_after_init: bool,
    invalid_layouts_rejected: usize,
    round_trips: usize,
    over_page_alignment_checked: bool,
    over_page_alignment: usize,
    checksum: u64,
}

#[derive(Clone, Copy)]
struct SemanticCAbiReport {
    allocation_ok: bool,
    deallocation_ok: bool,
    type_stats_rows: usize,
    probe_row_matched: bool,
    probe_allocations: usize,
    probe_deallocations: usize,
    probe_allocated_bytes: usize,
}

#[cfg(not(feature = "fixed_heap"))]
fn run_semantic_c_abi_workload() -> SemanticCAbiReport {
    SemanticCAbiReport {
        allocation_ok: false,
        deallocation_ok: false,
        type_stats_rows: 0,
        probe_row_matched: false,
        probe_allocations: 0,
        probe_deallocations: 0,
        probe_allocated_bytes: 0,
    }
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 8);
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if ch <= '\u{1f}' => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => out.push(ch),
        }
    }
    out
}

unsafe fn mmap_heap(heap_size: usize) -> Result<*mut u8, String> {
    let heap = libc::mmap(
        core::ptr::null_mut(),
        heap_size,
        libc::PROT_READ | libc::PROT_WRITE,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
        -1,
        0,
    );
    if heap.is_null() || heap == libc::MAP_FAILED {
        return Err("mmap failed for small fixed heap".to_string());
    }
    Ok(heap as *mut u8)
}

unsafe fn run_allocation_batch(
    label: &str,
    allocation_count: usize,
    size_modulus: usize,
) -> Result<BatchReport, String> {
    let mut bytes = 0usize;
    let mut checksum = 0x5A11_C0DE_u64;
    for i in 1..=allocation_count {
        let size = i % size_modulus + 1;
        let layout = Layout::from_size_align(size, 8)
            .map_err(|err| format!("{} invalid layout for size {}: {}", label, size, err))?;
        let ptr = A.alloc(layout);
        if ptr.is_null() {
            return Err(format!(
                "{} allocation {} returned null for layout size={} align={}",
                label,
                i,
                layout.size(),
                layout.align()
            ));
        }

        let pattern = (i as u8).wrapping_mul(17).wrapping_add(layout.size() as u8);
        core::ptr::write_bytes(ptr, pattern, layout.size());
        let first = core::ptr::read_volatile(ptr);
        let last = core::ptr::read_volatile(ptr.add(layout.size() - 1));
        if first != pattern || last != pattern {
            A.dealloc(ptr, layout);
            return Err(format!(
                "{} allocation {} failed write/read validation",
                label, i
            ));
        }

        checksum = checksum.rotate_left(5).wrapping_add(layout.size() as u64)
            ^ u64::from(first)
            ^ (u64::from(last) << 8);
        bytes = bytes
            .checked_add(layout.size())
            .ok_or_else(|| format!("{} byte counter overflow", label))?;
        A.dealloc(ptr, layout);
    }

    Ok(BatchReport {
        allocations: allocation_count,
        bytes,
        checksum,
    })
}

#[cfg(feature = "fixed_heap")]
fn empty_semantic_type_stats_row() -> SemanticTypeStatsSnapshot {
    SemanticTypeStatsSnapshot::empty()
}

#[cfg(feature = "fixed_heap")]
unsafe fn run_semantic_c_abi_workload() -> Result<SemanticCAbiReport, String> {
    let size = 96usize;
    let align = 16usize;
    let ptr = __unialloc_alloc_with_metadata(
        size,
        align,
        SEMANTIC_PROBE_TYPE_ID,
        SEMANTIC_PROBE_MODULE_ID,
        FLAG_TYPE_ISOLATED,
        SEMANTIC_PROBE_CALLSITE,
    );
    if ptr.is_null() {
        return Err("semantic C ABI allocation returned null".to_string());
    }

    core::ptr::write_bytes(ptr, 0xD5, size);
    if core::ptr::read_volatile(ptr) != 0xD5 || core::ptr::read_volatile(ptr.add(size - 1)) != 0xD5
    {
        __unialloc_dealloc_with_metadata(
            ptr,
            size,
            align,
            SEMANTIC_PROBE_TYPE_ID,
            SEMANTIC_PROBE_MODULE_ID,
            FLAG_TYPE_ISOLATED,
            SEMANTIC_PROBE_CALLSITE,
        );
        return Err("semantic C ABI allocation failed write/read validation".to_string());
    }

    let deallocation_ok = __unialloc_dealloc_with_metadata(
        ptr,
        size,
        align,
        SEMANTIC_PROBE_TYPE_ID,
        SEMANTIC_PROBE_MODULE_ID,
        FLAG_TYPE_ISOLATED,
        SEMANTIC_PROBE_CALLSITE,
    );
    if !deallocation_ok {
        return Err("semantic C ABI deallocation returned false".to_string());
    }

    let mut rows = [empty_semantic_type_stats_row(); 8];
    let type_stats_rows = semantic_type_stats_snapshot(&mut rows);
    let copied = core::cmp::min(type_stats_rows, rows.len());
    let mut probe_row_matched = false;
    let mut probe_allocations = 0usize;
    let mut probe_deallocations = 0usize;
    let mut probe_allocated_bytes = 0usize;
    for row in rows[..copied].iter() {
        if row.type_id == SEMANTIC_PROBE_TYPE_ID
            && row.module_id == SEMANTIC_PROBE_MODULE_ID
            && row.callsite == SEMANTIC_PROBE_CALLSITE
        {
            probe_allocations = row.allocations;
            probe_deallocations = row.deallocations;
            probe_allocated_bytes = row.allocated_bytes;
            probe_row_matched = row.allocations > 0
                && row.deallocations > 0
                && row.allocated_bytes >= size
                && (row.policy_flags_seen & FLAG_TYPE_ISOLATED) != 0;
        }
    }
    if !probe_row_matched {
        return Err(format!(
            "semantic C ABI type-stats probe row was not observed: rows={} copied={}",
            type_stats_rows, copied
        ));
    }

    Ok(SemanticCAbiReport {
        allocation_ok: true,
        deallocation_ok,
        type_stats_rows,
        probe_row_matched,
        probe_allocations,
        probe_deallocations,
        probe_allocated_bytes,
    })
}

#[cfg(feature = "fixed_heap")]
unsafe fn run_c_abi_workload() -> Result<CAbiReport, String> {
    let mut invalid_layouts_rejected = 0usize;
    if unialloc_alloc(0, 8).is_null() {
        invalid_layouts_rejected += 1;
    } else {
        return Err("C ABI accepted zero-sized allocation".to_string());
    }
    if unialloc_alloc(16, 3).is_null() {
        invalid_layouts_rejected += 1;
    } else {
        return Err("C ABI accepted non-power-of-two alignment".to_string());
    }
    if unialloc_realloc(core::ptr::null_mut(), 16, 3, 32).is_null() {
        invalid_layouts_rejected += 1;
    } else {
        return Err("C ABI accepted realloc with invalid null layout".to_string());
    }
    unialloc_dealloc(core::ptr::null_mut(), 16, 8);

    let mut checksum = 0xC_AB1_5AFE_u64;
    let ptr = unialloc_alloc(64, 8);
    if ptr.is_null() {
        return Err("C ABI alloc returned null for 64-byte layout".to_string());
    }
    core::ptr::write_bytes(ptr, 0xA5, 64);
    let grown = unialloc_realloc(ptr, 64, 8, 192);
    if grown.is_null() {
        return Err("C ABI realloc returned null while growing 64 -> 192".to_string());
    }
    for index in 0..64 {
        let value = *grown.add(index);
        if value != 0xA5 {
            unialloc_dealloc(grown, 192, 8);
            return Err(format!(
                "C ABI realloc did not preserve byte {}: expected 0xA5 got {:#04x}",
                index, value
            ));
        }
        checksum = checksum.rotate_left(3) ^ u64::from(value) ^ index as u64;
    }
    core::ptr::write_bytes(grown.add(64), 0x5A, 128);
    checksum ^= u64::from(core::ptr::read_volatile(grown.add(191))) << 16;
    unialloc_dealloc(grown, 192, 8);

    let over_page_alignment = SMALL_HEAP_PAGE_SIZE
        .checked_mul(2)
        .ok_or_else(|| "C ABI over-page alignment overflow".to_string())?;
    let over_page_size = SMALL_HEAP_PAGE_SIZE + 128;
    let over_page = unialloc_alloc(over_page_size, over_page_alignment);
    if over_page.is_null() {
        return Err(format!(
            "C ABI over-page allocation returned null for size={} align={}",
            over_page_size, over_page_alignment
        ));
    }
    if (over_page as usize) % over_page_alignment != 0 {
        unialloc_dealloc(over_page, over_page_size, over_page_alignment);
        return Err(format!(
            "C ABI over-page allocation was misaligned: ptr={:#x} align={}",
            over_page as usize, over_page_alignment
        ));
    }
    core::ptr::write_bytes(over_page, 0xC3, over_page_size);
    checksum = checksum.rotate_left(11)
        ^ u64::from(core::ptr::read_volatile(over_page))
        ^ (u64::from(core::ptr::read_volatile(over_page.add(over_page_size - 1))) << 8);
    unialloc_dealloc(over_page, over_page_size, over_page_alignment);

    Ok(CAbiReport {
        invoked: true,
        ready_after_init: unialloc_fixed_heap_ready(),
        invalid_layouts_rejected,
        round_trips: 2,
        over_page_alignment_checked: true,
        over_page_alignment,
        checksum,
    })
}

#[cfg(not(feature = "fixed_heap"))]
unsafe fn run_c_abi_workload() -> Result<CAbiReport, String> {
    Ok(CAbiReport {
        invoked: false,
        ready_after_init: fixed_heap_ready(),
        invalid_layouts_rejected: 0,
        round_trips: 0,
        over_page_alignment_checked: false,
        over_page_alignment: 0,
        checksum: 0,
    })
}

impl CAbiReport {
    fn passed(self) -> bool {
        self.invoked
            && self.ready_after_init
            && self.invalid_layouts_rejected >= 3
            && self.round_trips >= 2
            && self.over_page_alignment_checked
            && self.over_page_alignment >= SMALL_HEAP_PAGE_SIZE
            && self.checksum != 0
    }
}

#[cfg(feature = "fixed_heap")]
fn constrained_boot_platform() -> &'static str {
    #[cfg(target_os = "redox")]
    {
        "redox"
    }
    #[cfg(not(target_os = "redox"))]
    {
        "host-fixed-heap"
    }
}

#[cfg(feature = "fixed_heap")]
fn marker_token(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | ':') {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    if out.is_empty() {
        out.push_str("unknown");
    }
    out
}

#[cfg(feature = "fixed_heap")]
fn constrained_boot_sample_marker(
    platform: &str,
    boot_cycle: usize,
    c_abi: &CAbiReport,
    semantic_c_abi: &SemanticCAbiReport,
) -> Result<String, String> {
    let mut sample = MaybeUninit::<UniallocConstrainedBootSample>::uninit();
    let copied = unsafe {
        __unialloc_constrained_boot_sample_checked(
            sample.as_mut_ptr(),
            size_of::<UniallocConstrainedBootSample>(),
            boot_cycle,
            c_abi.invoked,
            c_abi.ready_after_init,
            c_abi.round_trips,
            c_abi.invalid_layouts_rejected,
            c_abi.over_page_alignment_checked,
            c_abi.over_page_alignment,
            semantic_c_abi.type_stats_rows,
            semantic_c_abi.probe_row_matched,
        )
    };
    if !copied
        || __unialloc_constrained_boot_sample_abi_version() != CONSTRAINED_BOOT_SAMPLE_ABI_VERSION
        || __unialloc_constrained_boot_sample_size() != size_of::<UniallocConstrainedBootSample>()
    {
        return Err("constrained boot sample ABI version/size check failed".to_string());
    }
    let sample = unsafe { sample.assume_init() };
    Ok(format!(
        concat!(
            "UNIALLOC_CONSTRAINED_BOOT_SAMPLE ",
            "platform={} ",
            "boot_cycle={} ",
            "fixed_heap_ready={} ",
            "c_abi_invoked={} ",
            "c_abi_ready_after_init={} ",
            "c_abi_round_trips={} ",
            "c_abi_invalid_layouts_rejected={} ",
            "c_abi_over_page_alignment_checked={} ",
            "c_abi_over_page_alignment={} ",
            "allocator_total_allocations={} ",
            "allocator_total_deallocations={} ",
            "allocator_typed_allocations={} ",
            "allocator_typed_deallocations={} ",
            "allocator_fallback_allocations={} ",
            "allocator_fallback_deallocations={} ",
            "allocator_total_allocated_bytes={} ",
            "allocator_typed_allocated_bytes={} ",
            "allocator_fallback_allocated_bytes={} ",
            "allocator_coverage_basis_points={} ",
            "allocator_type_stats_rows={} ",
            "allocator_type_stats_dropped_events={} ",
            "allocator_type_stats_probe_matched={} ",
            "c_abi_probe_passed={}"
        ),
        marker_token(platform),
        sample.boot_cycle,
        sample.fixed_heap_ready,
        sample.c_abi_invoked,
        sample.c_abi_ready_after_init,
        sample.c_abi_round_trips,
        sample.c_abi_invalid_layouts_rejected,
        sample.c_abi_over_page_alignment_checked,
        sample.c_abi_over_page_alignment,
        sample.allocator_total_allocations,
        sample.allocator_total_deallocations,
        sample.allocator_typed_allocations,
        sample.allocator_typed_deallocations,
        sample.allocator_fallback_allocations,
        sample.allocator_fallback_deallocations,
        sample.allocator_total_allocated_bytes,
        sample.allocator_typed_allocated_bytes,
        sample.allocator_fallback_allocated_bytes,
        sample.allocator_coverage_basis_points,
        sample.allocator_type_stats_rows,
        sample.allocator_type_stats_dropped_events,
        sample.allocator_type_stats_probe_matched,
        c_abi.passed(),
    ))
}

fn run() -> Result<String, String> {
    let heap = unsafe { mmap_heap(HEAP_SIZE)? };
    let initial_heap_size = HEAP_SIZE / 2;
    if !unsafe { init_unialloc_fixed_heap(heap as usize, initial_heap_size, SMALL_HEAP_PAGE_SIZE) }
    {
        return Err("UniAlloc rejected the initial small-heap range".to_string());
    }
    #[cfg(feature = "fixed_heap")]
    if !fixed_heap_ready() {
        return Err("UniAlloc fixed heap did not report ready after init".to_string());
    }

    #[cfg(feature = "fixed_heap")]
    semantic_stats_reset();

    let before_extend = unsafe { run_allocation_batch("before_extend", 1023, 256)? };
    if !unsafe { try_extend_unialloc_fixed_heap(initial_heap_size, SMALL_HEAP_PAGE_SIZE) } {
        return Err("UniAlloc rejected the small-heap extension range".to_string());
    }
    let after_extend = unsafe { run_allocation_batch("after_extend", 1023, 1024)? };
    let c_abi = unsafe { run_c_abi_workload()? };
    #[cfg(feature = "fixed_heap")]
    let semantic_c_abi = unsafe { run_semantic_c_abi_workload()? };
    #[cfg(not(feature = "fixed_heap"))]
    let semantic_c_abi = run_semantic_c_abi_workload();
    #[cfg(feature = "fixed_heap")]
    let boot_platform = constrained_boot_platform();
    #[cfg(feature = "fixed_heap")]
    let boot_sample_marker =
        constrained_boot_sample_marker(boot_platform, 1, &c_abi, &semantic_c_abi)?;

    let json_report = format!(
        concat!(
            "{{",
            "\"passed\":true,",
            "\"heap_size\":{},",
            "\"initial_heap_size\":{},",
            "\"page_size\":{},",
            "\"page_size_source\":\"unialloc::PAGE_SIZE\",",
            "\"fixed_heap_ready\":{},",
            "\"init_api\":\"{}\",",
            "\"before_extend\":{{\"allocations\":{},\"bytes\":{},\"checksum\":{}}},",
            "\"after_extend\":{{\"allocations\":{},\"bytes\":{},\"checksum\":{}}},",
            "\"c_abi\":{{",
            "\"invoked\":{},",
            "\"ready_after_init\":{},",
            "\"invalid_layouts_rejected\":{},",
            "\"round_trips\":{},",
            "\"over_page_alignment_checked\":{},",
            "\"over_page_alignment\":{},",
            "\"checksum\":{}",
            "}},",
            "\"semantic_c_abi\":{{",
            "\"allocation_ok\":{},",
            "\"deallocation_ok\":{},",
            "\"type_stats_rows\":{},",
            "\"probe_row_matched\":{},",
            "\"probe_allocations\":{},",
            "\"probe_deallocations\":{},",
            "\"probe_allocated_bytes\":{}",
            "}}",
            "}}"
        ),
        HEAP_SIZE,
        initial_heap_size,
        SMALL_HEAP_PAGE_SIZE,
        fixed_heap_ready(),
        if cfg!(feature = "fixed_heap") {
            "unialloc_fixed_heap_try_init"
        } else {
            "RustAllocator::try_init"
        },
        before_extend.allocations,
        before_extend.bytes,
        before_extend.checksum,
        after_extend.allocations,
        after_extend.bytes,
        after_extend.checksum,
        c_abi.invoked,
        c_abi.ready_after_init,
        c_abi.invalid_layouts_rejected,
        c_abi.round_trips,
        c_abi.over_page_alignment_checked,
        c_abi.over_page_alignment,
        c_abi.checksum,
        semantic_c_abi.allocation_ok,
        semantic_c_abi.deallocation_ok,
        semantic_c_abi.type_stats_rows,
        semantic_c_abi.probe_row_matched,
        semantic_c_abi.probe_allocations,
        semantic_c_abi.probe_deallocations,
        semantic_c_abi.probe_allocated_bytes
    );

    #[cfg(feature = "fixed_heap")]
    {
        Ok(format!("{}\n{}", boot_sample_marker, json_report))
    }

    #[cfg(not(feature = "fixed_heap"))]
    {
        Ok(json_report)
    }
}

fn main() {
    match run() {
        Ok(report) => println!("{}", report),
        Err(err) => {
            println!(
                "{{\"passed\":false,\"error\":\"{}\"}}",
                json_escape(err.as_str())
            );
            process::exit(1);
        }
    }
}
