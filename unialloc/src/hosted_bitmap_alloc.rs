//! Hosted page-run allocation over independent mmap arenas.
//!
//! The fixed-heap bitmap owns one stable address interval. Hosted allocation
//! instead grows through multiple OS mappings, so every mapping gets its own
//! [`SegmentPageAllocator`]. Arena descriptors form an append-only registry;
//! inactive descriptors are reused after their payload and tree mappings are
//! released. The stable descriptor lifetime lets allocation use per-arena
//! locks without a global lifetime hazard.

use crate::bitmap_alloc::{RunBitmapError, RunNode, SegmentPageAllocator};
use crate::pal::sys_alloc as system_alloc;
use crate::sync::PthreadMutex;
use core::alloc::{GlobalAlloc, Layout};
use core::ptr::{self, null_mut};
use core::sync::atomic::{AtomicBool, AtomicPtr, AtomicUsize, Ordering};

const MIN_ARENA_BYTES: usize = 512 * 1024;
const MAX_ARENA_BYTES: usize = 64 * 1024 * 1024;
const WARM_EMPTY_ARENA_LIMIT: usize = 4;
const WARM_EMPTY_ARENA_MAX_BYTES: usize = 2 * 1024 * 1024;
const CONTENTION_ARENA_LIMIT: usize = WARM_EMPTY_ARENA_LIMIT;
const DISCARD_FREE_RUN_MIN_BYTES: usize = 256 * 1024;
const OWNER_RADIX_BITS: usize = 9;
const OWNER_RADIX_SLOTS: usize = 1 << OWNER_RADIX_BITS;
const OWNER_RADIX_MASK: usize = OWNER_RADIX_SLOTS - 1;
// Six 9-bit levels cover every possible page index in a 64-bit address space
// with 4KiB or larger pages.
const OWNER_RADIX_LEVELS: usize = 6;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum HostedBitmapError {
    InvalidLayout,
    MappingFailed,
    Bitmap(RunBitmapError),
}

enum ArenaAllocationAttempt {
    Allocated(*mut u8),
    Busy(*mut HostedArena),
    Miss,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct HostedBitmapStats {
    pub active_arenas: usize,
    pub warm_empty_arenas: usize,
    pub descriptor_count: usize,
    pub mapped_payload_bytes: usize,
}

/// Cold-path memory snapshot for the hosted bitmap page-run backend.
///
/// Mapped byte counters describe allocator-owned mappings.
/// `allocated_payload_bytes` is computed by walking active arena descriptors
/// under their arena locks, so it is intended for diagnostics and benchmark
/// accounting rather than allocation hot paths. Every field is race-safe, but
/// concurrent mutations can make the aggregate span multiple instants; a
/// quiescent checkpoint gives the exact cross-field accounting view.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
#[repr(C)]
pub struct HostedBitmapPageRunSnapshot {
    pub active_arenas: usize,
    pub warm_empty_arenas: usize,
    pub descriptor_count: usize,
    pub live_allocations: usize,
    pub mapped_payload_bytes: usize,
    pub allocated_payload_bytes: usize,
    pub mapped_tree_bytes: usize,
    pub mapped_descriptor_bytes: usize,
    pub mapped_owner_directory_bytes: usize,
}

struct HostedArenaState {
    allocator: SegmentPageAllocator,
    base: usize,
    mapping_bytes: usize,
    node_mapping: *mut u8,
    node_mapping_bytes: usize,
    warm_empty: bool,
}

impl HostedArenaState {
    const fn empty() -> Self {
        Self {
            allocator: SegmentPageAllocator::empty(),
            base: 0,
            mapping_bytes: 0,
            node_mapping: null_mut(),
            node_mapping_bytes: 0,
            warm_empty: false,
        }
    }

    #[inline]
    fn end(&self) -> Option<usize> {
        self.base.checked_add(self.mapping_bytes)
    }

    #[inline]
    fn contains(&self, addr: usize) -> bool {
        self.base != 0 && addr >= self.base && self.end().map(|end| addr < end).unwrap_or(false)
    }
}

struct HostedArena {
    // Written before release-publishing this descriptor and immutable after it
    // joins the registry.
    next: *mut HostedArena,
    // Full active arenas also have a zero max-free summary, so lifetime state is
    // published separately from capacity.
    active: AtomicBool,
    // Caches the tree root's max-free summary and lets readers skip locks for
    // undersized arenas.
    largest_free_run: AtomicUsize,
    base: AtomicUsize,
    end: AtomicUsize,
    state: spin::Mutex<HostedArenaState>,
}

unsafe impl Send for HostedArena {}
unsafe impl Sync for HostedArena {}

impl HostedArena {
    const fn new(next: *mut HostedArena) -> Self {
        Self {
            next,
            active: AtomicBool::new(false),
            largest_free_run: AtomicUsize::new(0),
            base: AtomicUsize::new(0),
            end: AtomicUsize::new(0),
            state: spin::Mutex::new(HostedArenaState::empty()),
        }
    }

    #[inline]
    fn is_candidate(&self, pages: usize) -> bool {
        self.active.load(Ordering::Acquire)
            && self.largest_free_run.load(Ordering::Relaxed) >= pages
    }

    #[inline]
    fn may_contain(&self, addr: usize) -> bool {
        if !self.active.load(Ordering::Acquire) {
            return false;
        }
        let base = self.base.load(Ordering::Relaxed);
        let end = self.end.load(Ordering::Relaxed);
        addr >= base && addr < end
    }
}

#[cfg(not(unialloc_target_arm64e))]
#[derive(Clone, Copy)]
struct HostedBitmapThreadHints {
    // Unit tests instantiate multiple managers. Production has one private
    // static manager, so its hot path keeps only the two arena candidates.
    #[cfg(test)]
    allocator: *const HostedBitmapPageAllocator,
    allocation_arena: *mut HostedArena,
    owner_arena: *mut HostedArena,
}

#[cfg(not(unialloc_target_arm64e))]
impl HostedBitmapThreadHints {
    const fn empty() -> Self {
        Self {
            #[cfg(test)]
            allocator: ptr::null(),
            allocation_arena: null_mut(),
            owner_arena: null_mut(),
        }
    }
}

// Direct static TLS avoids the PAL's destructor-bearing TLS slot. Production
// has one private static manager; tests additionally scope hints by manager
// identity. Every candidate revalidates arena activity, range/capacity, and the
// locked arena state.
#[cfg(not(unialloc_target_arm64e))]
#[thread_local]
static mut HOSTED_BITMAP_THREAD_HINTS: HostedBitmapThreadHints = HostedBitmapThreadHints::empty();

#[repr(C)]
struct OwnerRadixNode {
    slots: [AtomicPtr<()>; OWNER_RADIX_SLOTS],
}

struct OwnerDirectory {
    root: AtomicPtr<OwnerRadixNode>,
    update_lock: PthreadMutex<()>,
    node_count: AtomicUsize,
    mapped_node_bytes: AtomicUsize,
}

unsafe impl Send for OwnerDirectory {}
unsafe impl Sync for OwnerDirectory {}

impl OwnerDirectory {
    const fn new() -> Self {
        Self {
            root: AtomicPtr::new(null_mut()),
            update_lock: PthreadMutex::new(()),
            node_count: AtomicUsize::new(0),
            mapped_node_bytes: AtomicUsize::new(0),
        }
    }

    #[inline]
    fn slot_index(page: usize, level: usize) -> usize {
        page.checked_shr((level * OWNER_RADIX_BITS) as u32)
            .unwrap_or(0)
            & OWNER_RADIX_MASK
    }

    #[inline]
    fn node_mapping_bytes() -> Option<usize> {
        HostedBitmapPageAllocator::rounded_mapping_bytes(core::mem::size_of::<OwnerRadixNode>())
    }

    unsafe fn allocate_node(&self) -> *mut OwnerRadixNode {
        let bytes = match Self::node_mapping_bytes() {
            Some(bytes) => bytes,
            None => return null_mut(),
        };
        let raw = HostedBitmapPageAllocator::map_bytes(bytes);
        if raw.is_null() {
            return null_mut();
        }
        let node = raw.cast::<OwnerRadixNode>();
        for idx in 0..OWNER_RADIX_SLOTS {
            ptr::write(
                core::ptr::addr_of_mut!((*node).slots[idx]),
                AtomicPtr::new(null_mut()),
            );
        }
        self.node_count.fetch_add(1, Ordering::Relaxed);
        self.mapped_node_bytes.fetch_add(bytes, Ordering::Relaxed);
        node
    }

    unsafe fn root_or_create(&self) -> Result<*mut OwnerRadixNode, HostedBitmapError> {
        let current = self.root.load(Ordering::Acquire);
        if !current.is_null() {
            return Ok(current);
        }
        let node = self.allocate_node();
        if node.is_null() {
            return Err(HostedBitmapError::MappingFailed);
        }
        self.root.store(node, Ordering::Release);
        Ok(node)
    }

    unsafe fn leaf_slot_or_create(&self, page: usize) -> Result<&AtomicPtr<()>, HostedBitmapError> {
        let mut node = self.root_or_create()?;
        for level in (1..OWNER_RADIX_LEVELS).rev() {
            let idx = Self::slot_index(page, level);
            let slot = &(*node).slots[idx];
            let mut child = slot.load(Ordering::Acquire).cast::<OwnerRadixNode>();
            if child.is_null() {
                child = self.allocate_node();
                if child.is_null() {
                    return Err(HostedBitmapError::MappingFailed);
                }
                slot.store(child.cast::<()>(), Ordering::Release);
            }
            node = child;
        }
        Ok(&(*node).slots[page & OWNER_RADIX_MASK])
    }

    unsafe fn leaf_slot(&self, page: usize) -> Option<&AtomicPtr<()>> {
        let mut node = self.root.load(Ordering::Acquire);
        if node.is_null() {
            return None;
        }
        for level in (1..OWNER_RADIX_LEVELS).rev() {
            let idx = Self::slot_index(page, level);
            node = (*node).slots[idx]
                .load(Ordering::Acquire)
                .cast::<OwnerRadixNode>();
            if node.is_null() {
                return None;
            }
        }
        Some(&(*node).slots[page & OWNER_RADIX_MASK])
    }

    unsafe fn rollback_inserted(
        &self,
        first_page: usize,
        inserted: usize,
        owner: *mut HostedArena,
    ) {
        for offset in 0..inserted {
            if let Some(slot) = self.leaf_slot(first_page + offset) {
                if slot.load(Ordering::Acquire).cast::<HostedArena>() == owner {
                    slot.store(null_mut(), Ordering::Release);
                }
            }
        }
    }

    unsafe fn insert_range(
        &self,
        base: usize,
        pages: usize,
        owner: *mut HostedArena,
    ) -> Result<(), HostedBitmapError> {
        let _guard = self.update_lock.lock();
        let first_page = base / crate::PAGE_SIZE;
        let mut inserted = 0usize;
        while inserted < pages {
            let page = match first_page.checked_add(inserted) {
                Some(page) => page,
                None => {
                    self.rollback_inserted(first_page, inserted, owner);
                    return Err(HostedBitmapError::InvalidLayout);
                }
            };
            let slot = match self.leaf_slot_or_create(page) {
                Ok(slot) => slot,
                Err(error) => {
                    self.rollback_inserted(first_page, inserted, owner);
                    return Err(error);
                }
            };
            let current = slot.load(Ordering::Acquire).cast::<HostedArena>();
            if !current.is_null() && current != owner {
                self.rollback_inserted(first_page, inserted, owner);
                return Err(HostedBitmapError::MappingFailed);
            }
            slot.store(owner.cast::<()>(), Ordering::Release);
            inserted += 1;
        }
        Ok(())
    }

    unsafe fn remove_range(&self, base: usize, pages: usize, owner: *mut HostedArena) {
        let _guard = self.update_lock.lock();
        let first_page = base / crate::PAGE_SIZE;
        for offset in 0..pages {
            if let Some(slot) = self.leaf_slot(first_page + offset) {
                if slot.load(Ordering::Acquire).cast::<HostedArena>() == owner {
                    slot.store(null_mut(), Ordering::Release);
                }
            }
        }
    }

    #[inline]
    unsafe fn owner_for(&self, addr: usize) -> *mut HostedArena {
        self.leaf_slot(addr / crate::PAGE_SIZE)
            .map(|slot| slot.load(Ordering::Acquire).cast::<HostedArena>())
            .unwrap_or(null_mut())
    }
}

/// Multi-arena hosted wrapper around [`SegmentPageAllocator`].
///
/// The registry is append-only, while payload and tree mappings are retired
/// and inactive descriptors are reused. Allocation scans atomic max-free
/// summaries and takes only the selected arena lock. Thread-local candidate
/// hints bypass that scan and the owner radix on repeated arena traffic. A
/// short creation lock serializes mmap and descriptor publication on capacity
/// misses.
pub(crate) struct HostedBitmapPageAllocator {
    head: AtomicPtr<HostedArena>,
    owners: OwnerDirectory,
    create_lock: PthreadMutex<()>,
    warm_empty_limit: usize,
    active_arenas: AtomicUsize,
    warm_empty_arenas: AtomicUsize,
    descriptor_count: AtomicUsize,
    mapped_payload_bytes: AtomicUsize,
    mapped_tree_bytes: AtomicUsize,
    mapped_descriptor_bytes: AtomicUsize,
    live_allocations: AtomicUsize,
    #[cfg(test)]
    local_allocation_hint_hits: AtomicUsize,
    #[cfg(test)]
    local_owner_hint_hits: AtomicUsize,
    #[cfg(test)]
    owner_directory_lookups: AtomicUsize,
}

unsafe impl Send for HostedBitmapPageAllocator {}
unsafe impl Sync for HostedBitmapPageAllocator {}

impl HostedBitmapPageAllocator {
    const fn new(warm_empty_limit: usize) -> Self {
        Self {
            head: AtomicPtr::new(null_mut()),
            owners: OwnerDirectory::new(),
            create_lock: PthreadMutex::new(()),
            warm_empty_limit,
            active_arenas: AtomicUsize::new(0),
            warm_empty_arenas: AtomicUsize::new(0),
            descriptor_count: AtomicUsize::new(0),
            mapped_payload_bytes: AtomicUsize::new(0),
            mapped_tree_bytes: AtomicUsize::new(0),
            mapped_descriptor_bytes: AtomicUsize::new(0),
            live_allocations: AtomicUsize::new(0),
            #[cfg(test)]
            local_allocation_hint_hits: AtomicUsize::new(0),
            #[cfg(test)]
            local_owner_hint_hits: AtomicUsize::new(0),
            #[cfg(test)]
            owner_directory_lookups: AtomicUsize::new(0),
        }
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[inline]
    fn thread_allocation_hint(&self) -> *mut HostedArena {
        unsafe {
            let hints = core::ptr::addr_of!(HOSTED_BITMAP_THREAD_HINTS);
            #[cfg(test)]
            if core::ptr::addr_of!((*hints).allocator).read() != self as *const Self {
                return null_mut();
            }
            core::ptr::addr_of!((*hints).allocation_arena).read()
        }
    }

    #[cfg(unialloc_target_arm64e)]
    #[inline]
    fn thread_allocation_hint(&self) -> *mut HostedArena {
        null_mut()
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[inline]
    fn thread_owner_hint(&self, addr: usize) -> *mut HostedArena {
        unsafe {
            let hints = core::ptr::addr_of!(HOSTED_BITMAP_THREAD_HINTS);
            #[cfg(test)]
            if core::ptr::addr_of!((*hints).allocator).read() != self as *const Self {
                return null_mut();
            }
            let owner = core::ptr::addr_of!((*hints).owner_arena).read();
            match owner.as_ref() {
                Some(arena) if arena.may_contain(addr) => owner,
                _ => null_mut(),
            }
        }
    }

    #[cfg(unialloc_target_arm64e)]
    #[inline]
    fn thread_owner_hint(&self, _addr: usize) -> *mut HostedArena {
        null_mut()
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[inline]
    fn remember_thread_arena(&self, arena: *mut HostedArena, has_capacity: bool) {
        unsafe {
            let hints_ptr = core::ptr::addr_of_mut!(HOSTED_BITMAP_THREAD_HINTS);
            #[cfg(test)]
            if core::ptr::addr_of!((*hints_ptr).allocator).read() != self as *const Self {
                core::ptr::addr_of_mut!((*hints_ptr).allocator).write(self as *const Self);
            }
            if core::ptr::addr_of!((*hints_ptr).owner_arena).read() != arena {
                core::ptr::addr_of_mut!((*hints_ptr).owner_arena).write(arena);
            }
            let allocation_arena = if has_capacity { arena } else { null_mut() };
            if core::ptr::addr_of!((*hints_ptr).allocation_arena).read() != allocation_arena {
                core::ptr::addr_of_mut!((*hints_ptr).allocation_arena).write(allocation_arena);
            }
        }
    }

    #[cfg(unialloc_target_arm64e)]
    #[inline]
    fn remember_thread_arena(&self, _arena: *mut HostedArena, _has_capacity: bool) {}

    #[cfg(not(unialloc_target_arm64e))]
    #[inline]
    fn forget_thread_arena(&self, arena: *mut HostedArena) {
        unsafe {
            let hints_ptr = core::ptr::addr_of_mut!(HOSTED_BITMAP_THREAD_HINTS);
            #[cfg(test)]
            if core::ptr::addr_of!((*hints_ptr).allocator).read() != self as *const Self {
                return;
            }
            if core::ptr::addr_of!((*hints_ptr).allocation_arena).read() == arena {
                core::ptr::addr_of_mut!((*hints_ptr).allocation_arena).write(null_mut());
            }
            if core::ptr::addr_of!((*hints_ptr).owner_arena).read() == arena {
                core::ptr::addr_of_mut!((*hints_ptr).owner_arena).write(null_mut());
            }
        }
    }

    #[cfg(unialloc_target_arm64e)]
    #[inline]
    fn forget_thread_arena(&self, _arena: *mut HostedArena) {}

    #[cfg(all(test, not(unialloc_target_arm64e)))]
    #[inline]
    fn clear_thread_hints(&self) {
        unsafe {
            let hints_ptr = core::ptr::addr_of_mut!(HOSTED_BITMAP_THREAD_HINTS);
            if hints_ptr.read().allocator == self as *const Self {
                hints_ptr.write(HostedBitmapThreadHints::empty());
            }
        }
    }

    #[cfg(all(test, unialloc_target_arm64e))]
    #[inline]
    fn clear_thread_hints(&self) {}

    #[inline]
    fn rounded_pages(size: usize) -> Option<usize> {
        size.checked_add(crate::PAGE_SIZE - 1)?
            .checked_div(crate::PAGE_SIZE)
            .filter(|pages| *pages != 0)
    }

    #[inline]
    fn rounded_mapping_bytes(size: usize) -> Option<usize> {
        Self::rounded_pages(size)?.checked_mul(crate::PAGE_SIZE)
    }

    #[inline]
    fn alignment_slack_pages(align: usize) -> Option<usize> {
        if align <= crate::PAGE_SIZE {
            Some(0)
        } else if align % crate::PAGE_SIZE == 0 && align.is_power_of_two() {
            align.checked_div(crate::PAGE_SIZE)?.checked_sub(1)
        } else {
            None
        }
    }

    fn arena_mapping_bytes(layout: Layout) -> Option<usize> {
        let payload_pages = Self::rounded_pages(layout.size())?;
        let required_pages =
            payload_pages.checked_add(Self::alignment_slack_pages(layout.align())?)?;
        let required_bytes = required_pages.checked_mul(crate::PAGE_SIZE)?;
        if required_bytes > MAX_ARENA_BYTES {
            return None;
        }
        let requested = core::cmp::max(required_bytes, MIN_ARENA_BYTES);
        requested
            .checked_next_power_of_two()
            .filter(|bytes| *bytes <= MAX_ARENA_BYTES)
    }

    unsafe fn map_bytes(bytes: usize) -> *mut u8 {
        let prot = system_alloc::prots::get_prot(true, true, false);
        let ptr = system_alloc::mmap(bytes, prot);
        if system_alloc::mmap_failed(ptr) {
            null_mut()
        } else {
            ptr
        }
    }

    unsafe fn allocate_descriptor(&self) -> Result<*mut HostedArena, HostedBitmapError> {
        let descriptor_bytes = Self::rounded_mapping_bytes(core::mem::size_of::<HostedArena>())
            .ok_or(HostedBitmapError::InvalidLayout)?;
        let raw = Self::map_bytes(descriptor_bytes);
        if raw.is_null() {
            return Err(HostedBitmapError::MappingFailed);
        }
        let descriptor = raw.cast::<HostedArena>();
        let head = self.head.load(Ordering::Acquire);
        ptr::write(descriptor, HostedArena::new(head));
        // All descriptor fields, including immutable `next`, become visible
        // before readers can traverse from the new head.
        self.head.store(descriptor, Ordering::Release);
        self.descriptor_count.fetch_add(1, Ordering::Relaxed);
        self.mapped_descriptor_bytes
            .fetch_add(descriptor_bytes, Ordering::Relaxed);
        Ok(descriptor)
    }

    fn allocate_locked(
        &self,
        arena_ptr: *mut HostedArena,
        arena: &HostedArena,
        state: &mut HostedArenaState,
        layout: Layout,
    ) -> Option<*mut u8> {
        if !arena.active.load(Ordering::Acquire) {
            return None;
        }
        let ptr = state
            .allocator
            .allocate_bytes(layout.size(), layout.align())
            .ok()?;
        if state.warm_empty {
            state.warm_empty = false;
            let previous = self.warm_empty_arenas.fetch_sub(1, Ordering::AcqRel);
            debug_assert!(previous > 0, "warm arena accounting underflow");
        }
        let capacity = state.allocator.largest_free_run();
        arena.largest_free_run.store(capacity, Ordering::Release);
        self.remember_thread_arena(arena_ptr, capacity != 0);
        self.live_allocations.fetch_add(1, Ordering::Release);
        Some(ptr)
    }

    fn try_allocate_arena(
        &self,
        arena_ptr: *mut HostedArena,
        layout: Layout,
        pages: usize,
    ) -> ArenaAllocationAttempt {
        let arena = match unsafe { arena_ptr.as_ref() } {
            Some(arena) => arena,
            None => return ArenaAllocationAttempt::Miss,
        };
        if !arena.is_candidate(pages) {
            return ArenaAllocationAttempt::Miss;
        }
        let mut state = match arena.state.try_lock() {
            Some(state) => state,
            None => return ArenaAllocationAttempt::Busy(arena_ptr),
        };
        match self.allocate_locked(arena_ptr, arena, &mut state, layout) {
            Some(ptr) => ArenaAllocationAttempt::Allocated(ptr),
            None => ArenaAllocationAttempt::Miss,
        }
    }

    fn allocate_arena_blocking(
        &self,
        arena_ptr: *mut HostedArena,
        layout: Layout,
    ) -> Option<*mut u8> {
        let arena = unsafe { arena_ptr.as_ref()? };
        let mut state = arena.state.lock();
        self.allocate_locked(arena_ptr, arena, &mut state, layout)
    }

    fn try_allocate_existing(&self, layout: Layout, pages: usize) -> ArenaAllocationAttempt {
        let hint = self.thread_allocation_hint();
        let mut busy = null_mut();
        match self.try_allocate_arena(hint, layout, pages) {
            ArenaAllocationAttempt::Allocated(ptr) => {
                #[cfg(test)]
                self.local_allocation_hint_hits
                    .fetch_add(1, Ordering::Relaxed);
                return ArenaAllocationAttempt::Allocated(ptr);
            }
            ArenaAllocationAttempt::Busy(arena) => busy = arena,
            ArenaAllocationAttempt::Miss => {}
        }

        let mut current = self.head.load(Ordering::Acquire);
        while let Some(arena) = unsafe { current.as_ref() } {
            let arena_ptr = arena as *const HostedArena as *mut HostedArena;
            if arena_ptr != hint {
                match self.try_allocate_arena(arena_ptr, layout, pages) {
                    ArenaAllocationAttempt::Allocated(ptr) => {
                        return ArenaAllocationAttempt::Allocated(ptr)
                    }
                    ArenaAllocationAttempt::Busy(arena) if busy.is_null() => busy = arena,
                    ArenaAllocationAttempt::Busy(_) | ArenaAllocationAttempt::Miss => {}
                }
            }
            current = arena.next;
        }
        if busy.is_null() {
            ArenaAllocationAttempt::Miss
        } else {
            ArenaAllocationAttempt::Busy(busy)
        }
    }

    unsafe fn initialize_and_allocate(
        &self,
        arena: &HostedArena,
        state: &mut HostedArenaState,
        mapping_bytes: usize,
        layout: Layout,
    ) -> Result<*mut u8, HostedBitmapError> {
        debug_assert!(!arena.active.load(Ordering::Acquire));
        let payload = Self::map_bytes(mapping_bytes);
        if payload.is_null() {
            return Err(HostedBitmapError::MappingFailed);
        }
        let end = match (payload as usize).checked_add(mapping_bytes) {
            Some(end) => end,
            None => {
                system_alloc::munmap(payload, mapping_bytes);
                return Err(HostedBitmapError::InvalidLayout);
            }
        };

        let pages = mapping_bytes / crate::PAGE_SIZE;
        let node_layout = match SegmentPageAllocator::required_layout(pages) {
            Some(layout) => layout,
            None => {
                system_alloc::munmap(payload, mapping_bytes);
                return Err(HostedBitmapError::InvalidLayout);
            }
        };
        let node_mapping_bytes = match Self::rounded_mapping_bytes(node_layout.size()) {
            Some(bytes) => bytes,
            None => {
                system_alloc::munmap(payload, mapping_bytes);
                return Err(HostedBitmapError::InvalidLayout);
            }
        };
        let node_mapping = Self::map_bytes(node_mapping_bytes);
        if node_mapping.is_null() {
            system_alloc::munmap(payload, mapping_bytes);
            return Err(HostedBitmapError::MappingFailed);
        }

        let mut allocator = SegmentPageAllocator::empty();
        if let Err(error) = allocator.initialize(
            payload as usize,
            pages,
            crate::PAGE_SIZE,
            node_mapping.cast::<RunNode>(),
            node_layout.size() / core::mem::size_of::<RunNode>(),
        ) {
            system_alloc::munmap(node_mapping, node_mapping_bytes);
            system_alloc::munmap(payload, mapping_bytes);
            return Err(HostedBitmapError::Bitmap(error));
        }
        let allocated = match allocator.allocate_bytes(layout.size(), layout.align()) {
            Ok(ptr) => ptr,
            Err(error) => {
                system_alloc::munmap(node_mapping, node_mapping_bytes);
                system_alloc::munmap(payload, mapping_bytes);
                return Err(HostedBitmapError::Bitmap(error));
            }
        };

        let arena_ptr = arena as *const HostedArena as *mut HostedArena;
        if let Err(error) = self.owners.insert_range(payload as usize, pages, arena_ptr) {
            system_alloc::munmap(node_mapping, node_mapping_bytes);
            system_alloc::munmap(payload, mapping_bytes);
            return Err(error);
        }

        *state = HostedArenaState {
            allocator,
            base: payload as usize,
            mapping_bytes,
            node_mapping,
            node_mapping_bytes,
            warm_empty: false,
        };
        arena.base.store(state.base, Ordering::Relaxed);
        arena.end.store(end, Ordering::Relaxed);
        arena
            .largest_free_run
            .store(state.allocator.largest_free_run(), Ordering::Release);
        // Publish activity last so lock-free registry readers observe the base,
        // end, and root capacity from this initialization.
        arena.active.store(true, Ordering::Release);
        self.remember_thread_arena(arena_ptr, state.allocator.largest_free_run() != 0);
        self.active_arenas.fetch_add(1, Ordering::Relaxed);
        self.mapped_payload_bytes
            .fetch_add(mapping_bytes, Ordering::Relaxed);
        self.mapped_tree_bytes
            .fetch_add(node_mapping_bytes, Ordering::Relaxed);
        self.live_allocations.fetch_add(1, Ordering::Release);
        Ok(allocated)
    }

    unsafe fn create_arena_and_allocate(
        &self,
        mapping_bytes: usize,
        layout: Layout,
        contention_shard: bool,
    ) -> Result<*mut u8, HostedBitmapError> {
        let _create_guard = self.create_lock.lock();

        let pages = Self::rounded_pages(layout.size()).ok_or(HostedBitmapError::InvalidLayout)?;
        let rescan_existing = !contention_shard
            || self.active_arenas.load(Ordering::Acquire) >= CONTENTION_ARENA_LIMIT;
        if rescan_existing {
            match self.try_allocate_existing(layout, pages) {
                ArenaAllocationAttempt::Allocated(ptr) => return Ok(ptr),
                ArenaAllocationAttempt::Busy(arena) => {
                    if let Some(ptr) = self.allocate_arena_blocking(arena, layout) {
                        return Ok(ptr);
                    }
                }
                ArenaAllocationAttempt::Miss => {}
            }
        }

        let mut current = self.head.load(Ordering::Acquire);
        while let Some(arena) = current.as_ref() {
            if !arena.active.load(Ordering::Acquire) {
                let mut state = arena.state.lock();
                if !arena.active.load(Ordering::Acquire) && state.base == 0 {
                    return self.initialize_and_allocate(arena, &mut state, mapping_bytes, layout);
                }
            }
            current = arena.next;
        }

        let descriptor = self.allocate_descriptor()?;
        let arena = descriptor
            .as_ref()
            .ok_or(HostedBitmapError::MappingFailed)?;
        let mut state = arena.state.lock();
        self.initialize_and_allocate(arena, &mut state, mapping_bytes, layout)
    }

    fn validate_layout(layout: Layout) -> Result<(), HostedBitmapError> {
        if layout.size() == 0
            || layout.size() > isize::MAX as usize
            || Self::alignment_slack_pages(layout.align()).is_none()
        {
            Err(HostedBitmapError::InvalidLayout)
        } else {
            Ok(())
        }
    }

    unsafe fn allocate_arena_layout(
        &self,
        layout: Layout,
        mapping_bytes: usize,
    ) -> Result<*mut u8, HostedBitmapError> {
        let pages = Self::rounded_pages(layout.size()).ok_or(HostedBitmapError::InvalidLayout)?;
        let contention_shard = match self.try_allocate_existing(layout, pages) {
            ArenaAllocationAttempt::Allocated(ptr) => return Ok(ptr),
            ArenaAllocationAttempt::Busy(arena) => {
                if self.active_arenas.load(Ordering::Acquire) >= CONTENTION_ARENA_LIMIT {
                    if let Some(ptr) = self.allocate_arena_blocking(arena, layout) {
                        return Ok(ptr);
                    }
                    false
                } else {
                    true
                }
            }
            ArenaAllocationAttempt::Miss => false,
        };
        self.create_arena_and_allocate(mapping_bytes, layout, contention_shard)
    }

    pub(crate) unsafe fn allocate_pooled_layout(
        &self,
        layout: Layout,
    ) -> Result<*mut u8, HostedBitmapError> {
        Self::validate_layout(layout)?;
        let mapping_bytes =
            Self::arena_mapping_bytes(layout).ok_or(HostedBitmapError::InvalidLayout)?;
        self.allocate_arena_layout(layout, mapping_bytes)
    }

    pub(crate) unsafe fn allocate_layout(
        &self,
        layout: Layout,
    ) -> Result<*mut u8, HostedBitmapError> {
        Self::validate_layout(layout)?;
        if let Some(mapping_bytes) = Self::arena_mapping_bytes(layout) {
            if let Ok(ptr) = self.allocate_arena_layout(layout, mapping_bytes) {
                return Ok(ptr);
            }
        }

        // The full hosted backend preserves allocation progress when the
        // request shape, arena metadata, or payload mapping cannot use the
        // pooled path. Adaptive routing calls `allocate_pooled_layout` instead
        // so every successful bitmap allocation has owner-directory provenance.
        let ptr = system_alloc::PageHeap::default().alloc(layout);
        if ptr.is_null() {
            Err(HostedBitmapError::MappingFailed)
        } else {
            Ok(ptr)
        }
    }

    unsafe fn retire_arena(&self, arena: &HostedArena, state: &mut HostedArenaState) {
        debug_assert_eq!(state.allocator.allocated_pages(), 0);
        // Stop new readers before invalidating either mapping. Readers that saw
        // the old active value recheck it after taking the arena lock.
        arena.active.store(false, Ordering::Release);
        let arena_ptr = arena as *const HostedArena as *mut HostedArena;
        self.forget_thread_arena(arena_ptr);
        arena.largest_free_run.store(0, Ordering::Release);
        arena.base.store(0, Ordering::Relaxed);
        arena.end.store(0, Ordering::Relaxed);

        let base = state.base as *mut u8;
        let mapping_bytes = state.mapping_bytes;
        let node_mapping = state.node_mapping;
        let node_mapping_bytes = state.node_mapping_bytes;
        self.owners
            .remove_range(state.base, state.allocator.page_count(), arena_ptr);
        *state = HostedArenaState::empty();

        system_alloc::munmap(node_mapping, node_mapping_bytes);
        system_alloc::munmap(base, mapping_bytes);
        self.active_arenas.fetch_sub(1, Ordering::Relaxed);
        self.mapped_payload_bytes
            .fetch_sub(mapping_bytes, Ordering::Relaxed);
        self.mapped_tree_bytes
            .fetch_sub(node_mapping_bytes, Ordering::Relaxed);
    }

    #[cfg(unix)]
    unsafe fn discard_free_pages(ptr: *mut u8, bytes: usize) {
        let _ = libc::madvise(ptr.cast::<libc::c_void>(), bytes, libc::MADV_DONTNEED);
    }

    #[cfg(not(unix))]
    unsafe fn discard_free_pages(_ptr: *mut u8, _bytes: usize) {}

    pub(crate) unsafe fn try_deallocate_owned(
        &self,
        ptr: *mut u8,
        layout: Layout,
    ) -> Result<bool, HostedBitmapError> {
        if ptr.is_null() || layout.size() == 0 {
            return Ok(true);
        }
        if self.live_allocations.load(Ordering::Acquire) == 0 {
            return Ok(false);
        }
        let addr = ptr as usize;
        let hinted_owner = self.thread_owner_hint(addr);
        let arena_ptr = if hinted_owner.is_null() {
            #[cfg(test)]
            self.owner_directory_lookups.fetch_add(1, Ordering::Relaxed);
            self.owners.owner_for(addr)
        } else {
            #[cfg(test)]
            self.local_owner_hint_hits.fetch_add(1, Ordering::Relaxed);
            hinted_owner
        };
        let arena = match arena_ptr.as_ref() {
            Some(arena) => arena,
            None => return Ok(false),
        };
        let mut state = arena.state.lock();
        if !arena.active.load(Ordering::Acquire) || !state.contains(addr) {
            return Err(HostedBitmapError::Bitmap(RunBitmapError::InvalidPointer));
        }
        state
            .allocator
            .deallocate_bytes(ptr, layout.size())
            .map_err(HostedBitmapError::Bitmap)?;
        let previous_live = self.live_allocations.fetch_sub(1, Ordering::AcqRel);
        debug_assert!(
            previous_live > 0,
            "hosted live-allocation accounting underflow"
        );

        if state.allocator.allocated_pages() == 0 {
            let can_stay_warm = state.mapping_bytes <= WARM_EMPTY_ARENA_MAX_BYTES;
            let previous = if can_stay_warm {
                self.warm_empty_arenas.fetch_add(1, Ordering::AcqRel)
            } else {
                self.warm_empty_limit
            };
            if can_stay_warm && previous < self.warm_empty_limit {
                state.warm_empty = true;
                arena
                    .largest_free_run
                    .store(state.allocator.largest_free_run(), Ordering::Release);
                self.remember_thread_arena(arena_ptr, true);
            } else {
                if can_stay_warm {
                    let counted = self.warm_empty_arenas.fetch_sub(1, Ordering::AcqRel);
                    debug_assert!(counted > 0, "warm arena accounting underflow");
                }
                self.retire_arena(arena, &mut state);
            }
            return Ok(true);
        }

        arena
            .largest_free_run
            .store(state.allocator.largest_free_run(), Ordering::Release);
        self.remember_thread_arena(arena_ptr, true);
        if let Some(bytes) = Self::rounded_mapping_bytes(layout.size()) {
            if bytes >= DISCARD_FREE_RUN_MIN_BYTES {
                Self::discard_free_pages(ptr, bytes);
            }
        }
        Ok(true)
    }

    pub(crate) unsafe fn deallocate_layout(
        &self,
        ptr: *mut u8,
        layout: Layout,
    ) -> Result<(), HostedBitmapError> {
        if !self.try_deallocate_owned(ptr, layout)? {
            system_alloc::PageHeap::default().dealloc(ptr, layout);
        }
        Ok(())
    }

    pub(crate) fn stats(&self) -> HostedBitmapStats {
        HostedBitmapStats {
            active_arenas: self.active_arenas.load(Ordering::Acquire),
            warm_empty_arenas: self.warm_empty_arenas.load(Ordering::Acquire),
            descriptor_count: self.descriptor_count.load(Ordering::Acquire),
            mapped_payload_bytes: self.mapped_payload_bytes.load(Ordering::Acquire),
        }
    }

    pub fn snapshot(&self) -> HostedBitmapPageRunSnapshot {
        let mut allocated_payload_bytes = 0usize;
        let mut current = self.head.load(Ordering::Acquire);
        while let Some(arena) = unsafe { current.as_ref() } {
            let state = arena.state.lock();
            if arena.active.load(Ordering::Acquire) && state.allocator.is_initialized() {
                allocated_payload_bytes = allocated_payload_bytes.saturating_add(
                    state
                        .allocator
                        .allocated_pages()
                        .saturating_mul(crate::PAGE_SIZE),
                );
            }
            current = arena.next;
        }

        HostedBitmapPageRunSnapshot {
            active_arenas: self.active_arenas.load(Ordering::Acquire),
            warm_empty_arenas: self.warm_empty_arenas.load(Ordering::Acquire),
            descriptor_count: self.descriptor_count.load(Ordering::Acquire),
            live_allocations: self.live_allocations.load(Ordering::Acquire),
            mapped_payload_bytes: self.mapped_payload_bytes.load(Ordering::Acquire),
            allocated_payload_bytes,
            mapped_tree_bytes: self.mapped_tree_bytes.load(Ordering::Acquire),
            mapped_descriptor_bytes: self.mapped_descriptor_bytes.load(Ordering::Acquire),
            mapped_owner_directory_bytes: self.owners.mapped_node_bytes.load(Ordering::Acquire),
        }
    }

    #[cfg(test)]
    fn arena_bounds_for_tests(&self, addr: usize) -> Option<(usize, usize)> {
        let arena = unsafe { self.owners.owner_for(addr).as_ref()? };
        let state = arena.state.lock();
        if arena.active.load(Ordering::Acquire) && state.contains(addr) {
            state.end().map(|end| (state.base, end))
        } else {
            None
        }
    }

    #[cfg(test)]
    fn hint_stats_for_tests(&self) -> (usize, usize, usize) {
        (
            self.local_allocation_hint_hits.load(Ordering::Relaxed),
            self.local_owner_hint_hits.load(Ordering::Relaxed),
            self.owner_directory_lookups.load(Ordering::Relaxed),
        )
    }

    #[cfg(test)]
    unsafe fn release_payloads_for_tests(&self) {
        let _create_guard = self.create_lock.lock();
        self.clear_thread_hints();
        let mut current = self.head.load(Ordering::Acquire);
        while let Some(arena) = current.as_ref() {
            let mut state = arena.state.lock();
            if state.base != 0 {
                arena.active.store(false, Ordering::Release);
                arena.largest_free_run.store(0, Ordering::Release);
                arena.base.store(0, Ordering::Relaxed);
                arena.end.store(0, Ordering::Relaxed);
                self.owners.remove_range(
                    state.base,
                    state.allocator.page_count(),
                    arena as *const HostedArena as *mut HostedArena,
                );
                system_alloc::munmap(state.node_mapping, state.node_mapping_bytes);
                system_alloc::munmap(state.base as *mut u8, state.mapping_bytes);
                *state = HostedArenaState::empty();
            }
            current = arena.next;
        }
        self.active_arenas.store(0, Ordering::Release);
        self.warm_empty_arenas.store(0, Ordering::Release);
        self.mapped_payload_bytes.store(0, Ordering::Release);
        self.mapped_tree_bytes.store(0, Ordering::Release);
        self.live_allocations.store(0, Ordering::Release);
    }
}

pub(crate) static HOSTED_PAGE_RUN_BITMAP: HostedBitmapPageAllocator =
    HostedBitmapPageAllocator::new(WARM_EMPTY_ARENA_LIMIT);

pub fn hosted_bitmap_page_run_snapshot() -> HostedBitmapPageRunSnapshot {
    HOSTED_PAGE_RUN_BITMAP.snapshot()
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec::Vec;
    use std::collections::HashSet;
    use std::sync::{mpsc, Arc, Barrier, Mutex};
    use std::thread;

    extern crate std;

    struct TestAllocator {
        allocator: HostedBitmapPageAllocator,
    }

    impl TestAllocator {
        fn new(warm_empty_limit: usize) -> Self {
            Self {
                allocator: HostedBitmapPageAllocator::new(warm_empty_limit),
            }
        }
    }

    impl Drop for TestAllocator {
        fn drop(&mut self) {
            unsafe { self.allocator.release_payloads_for_tests() }
        }
    }

    fn page_layout(pages: usize) -> Layout {
        Layout::from_size_align(pages * crate::PAGE_SIZE, crate::PAGE_SIZE).unwrap()
    }

    #[test]
    fn owner_radix_high_levels_collapse_to_zero_without_shift_overflow() {
        let level_above_pointer_width = usize::BITS as usize / OWNER_RADIX_BITS + 1;
        assert_eq!(
            OwnerDirectory::slot_index(usize::MAX, level_above_pointer_width),
            0
        );
    }

    #[test]
    fn hosted_arena_cross_leaf_adjacent_frees_recombine() {
        let manager = TestAllocator::new(1);
        unsafe {
            let guard = manager.allocator.allocate_layout(page_layout(32)).unwrap();
            let first = manager.allocator.allocate_layout(page_layout(32)).unwrap();
            let second = manager.allocator.allocate_layout(page_layout(32)).unwrap();
            let other_guard = manager.allocator.allocate_layout(page_layout(1)).unwrap();

            manager
                .allocator
                .deallocate_layout(first, page_layout(32))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(second, page_layout(32))
                .unwrap();
            let merged = manager.allocator.allocate_layout(page_layout(64)).unwrap();
            assert_eq!(
                merged, first,
                "cross-leaf neighbors should merge in-place: guard={guard:p} first={first:p} \
                 second={second:p} other_guard={other_guard:p}"
            );

            manager
                .allocator
                .deallocate_layout(merged, page_layout(64))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(other_guard, page_layout(1))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(guard, page_layout(32))
                .unwrap();
        }
    }

    #[test]
    fn hosted_arena_honors_over_page_alignment() {
        let manager = TestAllocator::new(1);
        unsafe {
            let prefix = manager.allocator.allocate_layout(page_layout(1)).unwrap();
            let aligned_layout =
                Layout::from_size_align(3 * crate::PAGE_SIZE, 8 * crate::PAGE_SIZE).unwrap();
            let aligned = manager.allocator.allocate_layout(aligned_layout).unwrap();
            assert_eq!(aligned as usize % aligned_layout.align(), 0);
            assert!(aligned as usize >= prefix as usize + crate::PAGE_SIZE);
            manager
                .allocator
                .deallocate_layout(aligned, aligned_layout)
                .unwrap();
            manager
                .allocator
                .deallocate_layout(prefix, page_layout(1))
                .unwrap();
        }
    }

    #[test]
    fn hosted_directory_uses_an_arena_with_sufficient_max_free() {
        let manager = TestAllocator::new(2);
        unsafe {
            let first = manager.allocator.allocate_layout(page_layout(97)).unwrap();
            let second = manager.allocator.allocate_layout(page_layout(32)).unwrap();
            let (first_base, first_end) = manager
                .allocator
                .arena_bounds_for_tests(first as usize)
                .expect("first allocation arena");
            assert!(
                (second as usize) < first_base || (second as usize) >= first_end,
                "a 31-page tail cannot satisfy a 32-page request"
            );
            assert_eq!(manager.allocator.stats().active_arenas, 2);
            manager
                .allocator
                .deallocate_layout(second, page_layout(32))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(first, page_layout(97))
                .unwrap();
        }
    }

    #[test]
    fn hosted_full_arena_remains_addressable_for_deallocation() {
        let manager = TestAllocator::new(1);
        let full_layout = page_layout(MIN_ARENA_BYTES / crate::PAGE_SIZE);
        unsafe {
            let full = manager.allocator.allocate_layout(full_layout).unwrap();
            assert_eq!(manager.allocator.stats().active_arenas, 1);
            manager
                .allocator
                .deallocate_layout(full, full_layout)
                .expect("a zero max-free root still belongs to an active arena");
            assert_eq!(manager.allocator.stats().warm_empty_arenas, 1);

            let reused = manager.allocator.allocate_layout(page_layout(1)).unwrap();
            assert_eq!(reused, full, "warm full arena should become reusable");
            manager
                .allocator
                .deallocate_layout(reused, page_layout(1))
                .unwrap();
        }
    }

    #[test]
    fn hosted_empty_policy_keeps_one_warm_arena_and_reuses_descriptors() {
        let manager = TestAllocator::new(1);
        unsafe {
            let first = manager.allocator.allocate_layout(page_layout(97)).unwrap();
            let second = manager.allocator.allocate_layout(page_layout(32)).unwrap();
            manager
                .allocator
                .deallocate_layout(first, page_layout(97))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(second, page_layout(32))
                .unwrap();
            let after_free = manager.allocator.stats();
            assert_eq!(after_free.active_arenas, 1);
            assert_eq!(after_free.warm_empty_arenas, 1);
            assert_eq!(after_free.mapped_payload_bytes, MIN_ARENA_BYTES);

            let descriptors = after_free.descriptor_count;
            let reused = manager.allocator.allocate_layout(page_layout(97)).unwrap();
            assert_eq!(manager.allocator.stats().descriptor_count, descriptors);
            manager
                .allocator
                .deallocate_layout(reused, page_layout(97))
                .unwrap();
        }
    }

    #[test]
    fn hosted_large_request_bypasses_arena_registry() {
        let manager = TestAllocator::new(1);
        let layout = page_layout(MAX_ARENA_BYTES / crate::PAGE_SIZE + 1);
        unsafe {
            let guard = manager.allocator.allocate_layout(page_layout(1)).unwrap();
            let reusable = manager.allocator.allocate_layout(page_layout(1)).unwrap();
            manager
                .allocator
                .deallocate_layout(reusable, page_layout(1))
                .unwrap();
            let before = manager.allocator.stats();

            let direct = manager.allocator.allocate_layout(layout).unwrap();
            assert!(!direct.is_null());
            assert!(manager
                .allocator
                .owners
                .owner_for(direct as usize)
                .is_null());
            manager.allocator.deallocate_layout(direct, layout).unwrap();
            assert_eq!(manager.allocator.stats(), before);

            let reused = manager.allocator.allocate_layout(page_layout(1)).unwrap();
            assert_eq!(reused, reusable);
            manager
                .allocator
                .deallocate_layout(reused, page_layout(1))
                .unwrap();
            manager
                .allocator
                .deallocate_layout(guard, page_layout(1))
                .unwrap();
        }
    }

    #[test]
    fn hosted_pooled_allocation_declines_direct_only_layouts() {
        let manager = TestAllocator::new(1);
        let layout = page_layout(MAX_ARENA_BYTES / crate::PAGE_SIZE + 1);
        unsafe {
            assert_eq!(
                manager.allocator.allocate_pooled_layout(layout),
                Err(HostedBitmapError::InvalidLayout)
            );
            assert_eq!(manager.allocator.stats(), HostedBitmapStats::default());
        }
    }

    #[test]
    fn hosted_snapshot_reports_precise_live_and_mapped_bytes() {
        let manager = TestAllocator::new(1);
        let descriptor_bytes =
            HostedBitmapPageAllocator::rounded_mapping_bytes(core::mem::size_of::<HostedArena>())
                .unwrap();
        let owner_node_bytes = OwnerDirectory::node_mapping_bytes().unwrap();
        let tree_bytes = HostedBitmapPageAllocator::rounded_mapping_bytes(
            SegmentPageAllocator::required_layout(MIN_ARENA_BYTES / crate::PAGE_SIZE)
                .unwrap()
                .size(),
        )
        .unwrap();

        assert_eq!(
            manager.allocator.snapshot(),
            HostedBitmapPageRunSnapshot::default()
        );

        unsafe {
            let eight_pages = manager.allocator.allocate_layout(page_layout(8)).unwrap();
            let first_snapshot = manager.allocator.snapshot();
            assert_eq!(first_snapshot.active_arenas, 1);
            assert_eq!(first_snapshot.warm_empty_arenas, 0);
            assert_eq!(first_snapshot.descriptor_count, 1);
            assert_eq!(first_snapshot.live_allocations, 1);
            assert_eq!(first_snapshot.mapped_payload_bytes, MIN_ARENA_BYTES);
            assert_eq!(first_snapshot.allocated_payload_bytes, 8 * crate::PAGE_SIZE);
            assert_eq!(first_snapshot.mapped_tree_bytes, tree_bytes);
            assert_eq!(first_snapshot.mapped_descriptor_bytes, descriptor_bytes);
            assert_eq!(
                first_snapshot.mapped_owner_directory_bytes,
                manager.allocator.owners.node_count.load(Ordering::Acquire) * owner_node_bytes
            );
            assert!(
                first_snapshot.mapped_owner_directory_bytes
                    >= OWNER_RADIX_LEVELS * owner_node_bytes
            );

            let three_pages = manager.allocator.allocate_layout(page_layout(3)).unwrap();
            let two_live = manager.allocator.snapshot();
            assert_eq!(two_live.live_allocations, 2);
            assert_eq!(two_live.allocated_payload_bytes, 11 * crate::PAGE_SIZE);
            assert_eq!(two_live.mapped_payload_bytes, MIN_ARENA_BYTES);
            assert_eq!(two_live.mapped_tree_bytes, tree_bytes);
            assert_eq!(two_live.mapped_descriptor_bytes, descriptor_bytes);

            manager
                .allocator
                .deallocate_layout(eight_pages, page_layout(8))
                .unwrap();
            let one_live = manager.allocator.snapshot();
            assert_eq!(one_live.live_allocations, 1);
            assert_eq!(one_live.allocated_payload_bytes, 3 * crate::PAGE_SIZE);

            manager
                .allocator
                .deallocate_layout(three_pages, page_layout(3))
                .unwrap();
            let warm = manager.allocator.snapshot();
            assert_eq!(warm.active_arenas, 1);
            assert_eq!(warm.warm_empty_arenas, 1);
            assert_eq!(warm.live_allocations, 0);
            assert_eq!(warm.allocated_payload_bytes, 0);
            assert_eq!(warm.mapped_payload_bytes, MIN_ARENA_BYTES);
            assert_eq!(warm.mapped_tree_bytes, tree_bytes);
            assert_eq!(warm.mapped_descriptor_bytes, descriptor_bytes);
            assert_eq!(
                warm.mapped_owner_directory_bytes,
                manager.allocator.owners.node_count.load(Ordering::Acquire) * owner_node_bytes
            );
        }
    }

    #[test]
    fn hosted_snapshot_drops_payload_and_tree_bytes_on_retirement() {
        let manager = TestAllocator::new(0);
        let descriptor_bytes =
            HostedBitmapPageAllocator::rounded_mapping_bytes(core::mem::size_of::<HostedArena>())
                .unwrap();
        let owner_node_bytes = OwnerDirectory::node_mapping_bytes().unwrap();
        unsafe {
            let ptr = manager.allocator.allocate_layout(page_layout(8)).unwrap();
            manager
                .allocator
                .deallocate_layout(ptr, page_layout(8))
                .unwrap();
            let snapshot = manager.allocator.snapshot();
            assert_eq!(snapshot.active_arenas, 0);
            assert_eq!(snapshot.warm_empty_arenas, 0);
            assert_eq!(snapshot.descriptor_count, 1);
            assert_eq!(snapshot.live_allocations, 0);
            assert_eq!(snapshot.mapped_payload_bytes, 0);
            assert_eq!(snapshot.allocated_payload_bytes, 0);
            assert_eq!(snapshot.mapped_tree_bytes, 0);
            assert_eq!(snapshot.mapped_descriptor_bytes, descriptor_bytes);
            assert_eq!(
                snapshot.mapped_owner_directory_bytes,
                manager.allocator.owners.node_count.load(Ordering::Acquire) * owner_node_bytes
            );
        }
    }

    #[test]
    fn hosted_pooled_overaligned_run_preserves_owner_provenance() {
        let manager = TestAllocator::new(1);
        let guard_layout = page_layout(1);
        let aligned_layout =
            Layout::from_size_align(3 * crate::PAGE_SIZE, 8 * crate::PAGE_SIZE).unwrap();
        unsafe {
            let guard = manager
                .allocator
                .allocate_pooled_layout(guard_layout)
                .unwrap();
            let aligned = manager
                .allocator
                .allocate_pooled_layout(aligned_layout)
                .unwrap();

            assert_eq!(aligned as usize % aligned_layout.align(), 0);
            let owner = manager.allocator.owners.owner_for(aligned as usize);
            assert!(!owner.is_null());
            assert_eq!(
                manager
                    .allocator
                    .owners
                    .owner_for(aligned.add(aligned_layout.size() - 1) as usize),
                owner
            );
            assert!(manager
                .allocator
                .try_deallocate_owned(aligned, aligned_layout)
                .unwrap());
            assert!(manager
                .allocator
                .try_deallocate_owned(guard, guard_layout)
                .unwrap());
            assert_eq!(
                manager.allocator.live_allocations.load(Ordering::Acquire),
                0
            );
        }
    }

    #[test]
    fn hosted_owned_deallocation_distinguishes_foreign_page_heap_runs() {
        let manager = TestAllocator::new(1);
        let layout = page_layout(8);
        unsafe {
            let foreign = system_alloc::PageHeap::default().alloc(layout);
            assert!(!foreign.is_null());
            let lookups_before = manager.allocator.hint_stats_for_tests().2;
            assert!(!manager
                .allocator
                .try_deallocate_owned(foreign, layout)
                .unwrap());
            assert_eq!(manager.allocator.hint_stats_for_tests().2, lookups_before);
            system_alloc::PageHeap::default().dealloc(foreign, layout);

            let owned = manager.allocator.allocate_pooled_layout(layout).unwrap();
            assert_eq!(
                manager.allocator.live_allocations.load(Ordering::Acquire),
                1
            );
            assert!(matches!(
                manager.allocator.try_deallocate_owned(owned.add(1), layout),
                Err(HostedBitmapError::Bitmap(RunBitmapError::InvalidPointer))
            ));
            assert_eq!(
                manager.allocator.live_allocations.load(Ordering::Acquire),
                1
            );
            assert!(manager
                .allocator
                .try_deallocate_owned(owned, layout)
                .unwrap());
            assert_eq!(
                manager.allocator.live_allocations.load(Ordering::Acquire),
                0
            );
        }
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_thread_hints_are_allocator_scoped() {
        thread::spawn(|| unsafe {
            let first_manager = TestAllocator::new(1);
            let second_manager = TestAllocator::new(1);
            let layout = page_layout(1);

            let first = first_manager.allocator.allocate_layout(layout).unwrap();
            let second = second_manager.allocator.allocate_layout(layout).unwrap();
            let first_again = first_manager.allocator.allocate_layout(layout).unwrap();

            assert!(!first_manager
                .allocator
                .owners
                .owner_for(first as usize)
                .is_null());
            assert!(!first_manager
                .allocator
                .owners
                .owner_for(first_again as usize)
                .is_null());
            assert!(second_manager
                .allocator
                .owners
                .owner_for(first_again as usize)
                .is_null());

            first_manager
                .allocator
                .deallocate_layout(first_again, layout)
                .unwrap();
            first_manager
                .allocator
                .deallocate_layout(first, layout)
                .unwrap();
            second_manager
                .allocator
                .deallocate_layout(second, layout)
                .unwrap();
        })
        .join()
        .unwrap();
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_same_thread_fast_path_hits_local_arena_and_owner_hints() {
        thread::spawn(|| unsafe {
            let manager = TestAllocator::new(1);
            let layout = page_layout(8);
            let first = manager.allocator.allocate_layout(layout).unwrap();
            let second = manager.allocator.allocate_layout(layout).unwrap();
            manager.allocator.deallocate_layout(first, layout).unwrap();

            let (allocation_hits, owner_hits, directory_lookups) =
                manager.allocator.hint_stats_for_tests();
            assert!(allocation_hits >= 1);
            assert!(owner_hits >= 1);
            assert_eq!(directory_lookups, 0);

            manager.allocator.deallocate_layout(second, layout).unwrap();
        })
        .join()
        .unwrap();
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_owner_hint_falls_back_between_live_arenas_then_recaches() {
        thread::spawn(|| unsafe {
            let manager = TestAllocator::new(2);
            let layout = page_layout(8);
            let mapping_bytes = HostedBitmapPageAllocator::arena_mapping_bytes(layout).unwrap();
            let first = manager.allocator.allocate_layout(layout).unwrap();
            let second = manager.allocator.allocate_layout(layout).unwrap();
            let first_arena = manager.allocator.owners.owner_for(first as usize);
            assert_eq!(
                manager.allocator.owners.owner_for(second as usize),
                first_arena
            );

            let other = manager
                .allocator
                .create_arena_and_allocate(mapping_bytes, layout, true)
                .unwrap();
            let other_arena = manager.allocator.owners.owner_for(other as usize);
            let other_guard = manager
                .allocator
                .allocate_arena_blocking(other_arena, layout)
                .unwrap();
            assert_ne!(other_arena, first_arena);

            manager.allocator.deallocate_layout(first, layout).unwrap();
            manager.allocator.deallocate_layout(second, layout).unwrap();
            let (_, owner_hits, directory_lookups) = manager.allocator.hint_stats_for_tests();
            assert_eq!(directory_lookups, 1);
            assert_eq!(owner_hits, 1);

            manager
                .allocator
                .deallocate_layout(other_guard, layout)
                .unwrap();
            manager.allocator.deallocate_layout(other, layout).unwrap();
        })
        .join()
        .unwrap();
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_cross_thread_free_uses_radix_once_then_owner_hint() {
        let manager = Arc::new(TestAllocator::new(1));
        let layout = page_layout(8);
        unsafe {
            let first = manager.allocator.allocate_layout(layout).unwrap();
            let second = manager.allocator.allocate_layout(layout).unwrap();
            let guard = manager.allocator.allocate_layout(layout).unwrap();
            let first = first as usize;
            let second = second as usize;
            let worker_manager = Arc::clone(&manager);
            thread::spawn(move || {
                worker_manager
                    .allocator
                    .deallocate_layout(first as *mut u8, layout)
                    .unwrap();
                worker_manager
                    .allocator
                    .deallocate_layout(second as *mut u8, layout)
                    .unwrap();
                let replacement_first = worker_manager.allocator.allocate_layout(layout).unwrap();
                let replacement_second = worker_manager.allocator.allocate_layout(layout).unwrap();
                let expected = HashSet::from([first, second]);
                let actual =
                    HashSet::from([replacement_first as usize, replacement_second as usize]);
                assert_eq!(actual, expected);
                worker_manager
                    .allocator
                    .deallocate_layout(replacement_first, layout)
                    .unwrap();
                worker_manager
                    .allocator
                    .deallocate_layout(replacement_second, layout)
                    .unwrap();
            })
            .join()
            .unwrap();

            let (_, owner_hits, directory_lookups) = manager.allocator.hint_stats_for_tests();
            assert!(owner_hits >= 1);
            assert_eq!(directory_lookups, 1);
            manager.allocator.deallocate_layout(guard, layout).unwrap();
        }
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_stale_thread_hint_survives_retire_and_descriptor_reuse() {
        let manager = Arc::new(TestAllocator::new(1));
        let small_layout = page_layout(8);
        let large_layout = page_layout(MIN_ARENA_BYTES / crate::PAGE_SIZE + 1);
        let mapping_bytes = HostedBitmapPageAllocator::arena_mapping_bytes(small_layout).unwrap();
        unsafe {
            let first_arena_live = manager.allocator.allocate_layout(page_layout(97)).unwrap();
            let second_arena_guard = manager
                .allocator
                .create_arena_and_allocate(mapping_bytes, small_layout, true)
                .unwrap();
            let second_arena = manager
                .allocator
                .owners
                .owner_for(second_arena_guard as usize);
            let second_arena_target = manager
                .allocator
                .allocate_arena_blocking(second_arena, small_layout)
                .unwrap();
            let second_arena_target = second_arena_target as usize;
            manager
                .allocator
                .deallocate_layout(first_arena_live, page_layout(97))
                .unwrap();

            let (ready_tx, ready_rx) = mpsc::sync_channel(0);
            let (reuse_tx, reuse_rx) = mpsc::sync_channel(0);
            let worker_manager = Arc::clone(&manager);
            let worker = thread::spawn(move || {
                worker_manager
                    .allocator
                    .deallocate_layout(second_arena_target as *mut u8, small_layout)
                    .unwrap();
                let before = worker_manager.allocator.hint_stats_for_tests().0;
                ready_tx.send(before).unwrap();
                reuse_rx.recv().unwrap();
                let ptr = worker_manager
                    .allocator
                    .allocate_layout(small_layout)
                    .unwrap();
                let owner = worker_manager.allocator.owners.owner_for(ptr as usize);
                worker_manager
                    .allocator
                    .deallocate_layout(ptr, small_layout)
                    .unwrap();
                (owner as usize, ptr as usize)
            });

            let allocation_hits_before = ready_rx.recv().unwrap();
            manager
                .allocator
                .deallocate_layout(second_arena_guard, small_layout)
                .unwrap();
            assert!(!(*second_arena).active.load(Ordering::Acquire));
            assert!(manager
                .allocator
                .owners
                .owner_for(second_arena_guard as usize)
                .is_null());

            let large = manager.allocator.allocate_layout(large_layout).unwrap();
            assert_eq!(
                manager.allocator.owners.owner_for(large as usize),
                second_arena
            );
            assert_eq!(manager.allocator.stats().descriptor_count, 2);
            reuse_tx.send(()).unwrap();
            let (worker_owner, worker_ptr) = worker.join().unwrap();
            assert_eq!(worker_owner, second_arena as usize);
            assert_ne!(worker_ptr, 0);
            assert!(manager.allocator.hint_stats_for_tests().0 > allocation_hits_before);

            manager
                .allocator
                .deallocate_layout(large, large_layout)
                .unwrap();
        }
    }

    #[cfg(not(unialloc_target_arm64e))]
    #[test]
    fn hosted_retirement_clears_current_thread_hints() {
        thread::spawn(|| unsafe {
            let manager = TestAllocator::new(0);
            let layout = page_layout(8);
            let first = manager.allocator.allocate_layout(layout).unwrap();
            let descriptor = manager.allocator.owners.owner_for(first as usize);
            manager.allocator.deallocate_layout(first, layout).unwrap();

            assert!(manager.allocator.thread_allocation_hint().is_null());
            assert!(manager
                .allocator
                .thread_owner_hint(first as usize)
                .is_null());
            let allocation_hits = manager.allocator.hint_stats_for_tests().0;

            let reused = manager.allocator.allocate_layout(layout).unwrap();
            assert_eq!(
                manager.allocator.owners.owner_for(reused as usize),
                descriptor
            );
            assert_eq!(manager.allocator.stats().descriptor_count, 1);
            assert_eq!(manager.allocator.hint_stats_for_tests().0, allocation_hits);
            manager.allocator.deallocate_layout(reused, layout).unwrap();
        })
        .join()
        .unwrap();
    }

    #[test]
    fn hosted_randomized_multi_arena_operations_do_not_overlap() {
        let manager = TestAllocator::new(2);
        let mut live: Vec<(*mut u8, Layout)> = Vec::new();
        let mut random = 0x9e37_79b9_u32;

        unsafe {
            for _ in 0..10_000 {
                random = random.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                if !live.is_empty() && random & 1 == 0 {
                    let idx = random as usize % live.len();
                    let (ptr, layout) = live.swap_remove(idx);
                    manager.allocator.deallocate_layout(ptr, layout).unwrap();
                    continue;
                }

                let pages = 1 + random as usize % 40;
                let align_pages = 1usize << ((random >> 8) as usize % 4);
                let layout = Layout::from_size_align(
                    pages * crate::PAGE_SIZE,
                    align_pages * crate::PAGE_SIZE,
                )
                .unwrap();
                let ptr = manager.allocator.allocate_layout(layout).unwrap();
                let start = ptr as usize;
                let end = start + pages * crate::PAGE_SIZE;
                assert_eq!(start % layout.align(), 0);
                for (other, other_layout) in live.iter().copied() {
                    let other_start = other as usize;
                    let other_end = other_start
                        + HostedBitmapPageAllocator::rounded_mapping_bytes(other_layout.size())
                            .unwrap();
                    assert!(end <= other_start || start >= other_end);
                }
                ptr.write_volatile(0xA5);
                ptr.add(layout.size() - 1).write_volatile(0x5A);
                live.push((ptr, layout));
            }

            for (ptr, layout) in live.drain(..) {
                manager.allocator.deallocate_layout(ptr, layout).unwrap();
            }
        }
    }

    #[test]
    fn hosted_multi_thread_alloc_free_keeps_live_runs_unique() {
        let manager = Arc::new(TestAllocator::new(4));
        let live = Arc::new(Mutex::new(HashSet::new()));
        let barrier = Arc::new(Barrier::new(5));

        thread::scope(|scope| {
            for thread_idx in 0..4 {
                let manager = Arc::clone(&manager);
                let live = Arc::clone(&live);
                let barrier = Arc::clone(&barrier);
                scope.spawn(move || {
                    barrier.wait();
                    for iteration in 0..5_000 {
                        let pages = if (iteration + thread_idx) & 7 == 0 {
                            32
                        } else {
                            8
                        };
                        let layout = page_layout(pages);
                        unsafe {
                            let ptr = manager.allocator.allocate_layout(layout).unwrap();
                            {
                                let mut live = live.lock().unwrap();
                                assert!(live.insert(ptr as usize), "duplicate live page run");
                            }
                            ptr.write_volatile(thread_idx as u8);
                            ptr.add(layout.size() - 1).write_volatile(iteration as u8);
                            {
                                let mut live = live.lock().unwrap();
                                assert!(live.remove(&(ptr as usize)));
                            }
                            manager.allocator.deallocate_layout(ptr, layout).unwrap();
                        }
                    }
                });
            }
            barrier.wait();
        });

        assert!(live.lock().unwrap().is_empty());
    }

    #[test]
    fn hosted_busy_arena_creates_a_contention_shard() {
        let manager = TestAllocator::new(CONTENTION_ARENA_LIMIT);
        let layout = page_layout(8);
        unsafe {
            let original = manager.allocator.allocate_layout(layout).unwrap();
            manager
                .allocator
                .deallocate_layout(original, layout)
                .unwrap();

            let original_arena = manager.allocator.owners.owner_for(original as usize);
            let original_arena = original_arena.as_ref().expect("warm original arena");
            let original_guard = original_arena.state.lock();
            let sharded = manager.allocator.allocate_layout(layout).unwrap();
            assert_ne!(
                manager.allocator.owners.owner_for(sharded as usize),
                original_arena as *const HostedArena as *mut HostedArena
            );
            drop(original_guard);

            manager
                .allocator
                .deallocate_layout(sharded, layout)
                .unwrap();
            let stats = manager.allocator.stats();
            assert_eq!(stats.active_arenas, 2);
            assert_eq!(stats.warm_empty_arenas, 2);
            assert_eq!(stats.descriptor_count, 2);
        }
    }

    #[test]
    fn hosted_contention_shards_stop_at_the_warm_arena_limit() {
        let manager = TestAllocator::new(CONTENTION_ARENA_LIMIT);
        let layout = page_layout(8);
        let mapping_bytes = HostedBitmapPageAllocator::arena_mapping_bytes(layout).unwrap();
        let mut allocations = Vec::new();
        let mut owners = Vec::new();
        unsafe {
            for _ in 0..CONTENTION_ARENA_LIMIT {
                let ptr = manager
                    .allocator
                    .create_arena_and_allocate(mapping_bytes, layout, true)
                    .unwrap();
                allocations.push(ptr);
                owners.push(manager.allocator.owners.owner_for(ptr as usize));
            }
            let capped = manager
                .allocator
                .create_arena_and_allocate(mapping_bytes, layout, true)
                .unwrap();
            let capped_owner = manager.allocator.owners.owner_for(capped as usize);
            assert!(owners.contains(&capped_owner));

            let stats = manager.allocator.stats();
            assert_eq!(stats.active_arenas, CONTENTION_ARENA_LIMIT);
            assert_eq!(stats.descriptor_count, CONTENTION_ARENA_LIMIT);

            allocations.push(capped);
            for ptr in allocations {
                manager.allocator.deallocate_layout(ptr, layout).unwrap();
            }
        }
    }
}
