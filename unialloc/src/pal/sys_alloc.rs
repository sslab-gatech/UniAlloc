//! underlying system allocator
use core::alloc::{AllocError, Allocator, GlobalAlloc, Layout};
use core::ptr::NonNull;
use core::result::Result;
use core::slice;
#[cfg(test)]
use core::sync::atomic::AtomicBool;
use core::sync::atomic::{AtomicIsize, AtomicUsize, Ordering};

/// A builder to configure the page heap allocator
pub struct PageHeapBuilder {
    read: bool,
    write: bool,
    exec: bool,
}

impl PageHeapBuilder {
    pub fn build(&self) -> PageHeap {
        PageHeap {
            read: self.read,
            write: self.write,
            exec: self.exec,
        }
    }
}

impl Default for PageHeapBuilder {
    fn default() -> PageHeapBuilder {
        PageHeapBuilder {
            read: true,
            write: true,
            exec: false,
        }
    }
}

/// Page Heap allocator
/// An abstraction for underlying page allocator (e.g., mmap, get_free_pages)
pub struct PageHeap {
    read: bool,
    write: bool,
    exec: bool,
}

impl Default for PageHeap {
    fn default() -> PageHeap {
        PageHeapBuilder::default().build()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HugePageMmapStats {
    pub attempts: usize,
    pub successes: usize,
    pub fallbacks: usize,
    pub aligned_fallbacks: usize,
    pub advised_fallbacks: usize,
    pub fallback_failures: usize,
    /// Linux-only count of actual `MAP_HUGETLB` mmap syscalls.
    ///
    /// `attempts` is the number of allocator hugepage requests.  When Linux
    /// proves hugetlb is stably unavailable, later requests skip the doomed
    /// syscall and go straight to the aligned THP-advised fallback; this counter
    /// makes that hot-path optimization visible to probes without changing the
    /// allocator's fallback semantics.
    pub hugetlb_mmap_syscalls: usize,
    /// Number of allocator hugepage requests that skipped the direct hugetlb
    /// syscall because a previous stable platform error proved it unavailable.
    pub hugetlb_mmap_skips: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum HugePageMmapPlatformErrorStage {
    None = 0,
    InvalidRequest = 1,
    LinuxHugetlbMmap = 2,
    MacosMachVmAllocate = 3,
    MacosMprotect = 4,
    UnixUnsupportedPlatform = 5,
    WindowsLargePageMinimumUnavailable = 6,
    WindowsLargePageAlloc = 7,
    MacosSuperpageUnsupported = 8,
}

impl HugePageMmapPlatformErrorStage {
    #[inline]
    pub const fn as_str(self) -> &'static str {
        match self {
            HugePageMmapPlatformErrorStage::None => "none",
            HugePageMmapPlatformErrorStage::InvalidRequest => "invalid-request",
            HugePageMmapPlatformErrorStage::LinuxHugetlbMmap => "linux-hugetlb-mmap",
            HugePageMmapPlatformErrorStage::MacosMachVmAllocate => "macos-mach-vm-allocate",
            HugePageMmapPlatformErrorStage::MacosMprotect => "macos-mprotect",
            HugePageMmapPlatformErrorStage::UnixUnsupportedPlatform => "unix-unsupported-platform",
            HugePageMmapPlatformErrorStage::WindowsLargePageMinimumUnavailable => {
                "windows-large-page-minimum-unavailable"
            }
            HugePageMmapPlatformErrorStage::WindowsLargePageAlloc => "windows-large-page-alloc",
            HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported => {
                "macos-superpage-unsupported"
            }
        }
    }

    #[inline]
    const fn from_usize(value: usize) -> Self {
        match value {
            1 => HugePageMmapPlatformErrorStage::InvalidRequest,
            2 => HugePageMmapPlatformErrorStage::LinuxHugetlbMmap,
            3 => HugePageMmapPlatformErrorStage::MacosMachVmAllocate,
            4 => HugePageMmapPlatformErrorStage::MacosMprotect,
            5 => HugePageMmapPlatformErrorStage::UnixUnsupportedPlatform,
            6 => HugePageMmapPlatformErrorStage::WindowsLargePageMinimumUnavailable,
            7 => HugePageMmapPlatformErrorStage::WindowsLargePageAlloc,
            8 => HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported,
            _ => HugePageMmapPlatformErrorStage::None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HugePageMmapPlatformStatus {
    pub last_error_stage: HugePageMmapPlatformErrorStage,
    pub last_error_code: isize,
}

impl HugePageMmapPlatformStatus {
    #[inline]
    pub fn last_error_code_name(self) -> &'static str {
        hugepage_mmap_platform_error_code_name(self.last_error_stage, self.last_error_code)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum HugePageMmapBacking {
    Unmapped = 0,
    HugePage = 1,
    OrdinaryFallback = 2,
    OrdinaryFallbackWithHugePageAdvice = 3,
    OrdinaryFallbackAligned = 4,
    OrdinaryFallbackAlignedWithHugePageAdvice = 5,
    Failed = 6,
}

impl HugePageMmapBacking {
    #[inline]
    pub const fn as_str(self) -> &'static str {
        match self {
            HugePageMmapBacking::Unmapped => "unmapped",
            HugePageMmapBacking::HugePage => "hugepage",
            HugePageMmapBacking::OrdinaryFallback => "ordinary-fallback",
            HugePageMmapBacking::OrdinaryFallbackWithHugePageAdvice => {
                "ordinary-fallback-with-hugepage-advice"
            }
            HugePageMmapBacking::OrdinaryFallbackAligned => "ordinary-fallback-aligned",
            HugePageMmapBacking::OrdinaryFallbackAlignedWithHugePageAdvice => {
                "ordinary-fallback-aligned-with-hugepage-advice"
            }
            HugePageMmapBacking::Failed => "failed",
        }
    }

    #[inline]
    pub const fn is_hugepage(self) -> bool {
        matches!(self, HugePageMmapBacking::HugePage)
    }

    #[inline]
    pub const fn is_fallback(self) -> bool {
        matches!(
            self,
            HugePageMmapBacking::OrdinaryFallback
                | HugePageMmapBacking::OrdinaryFallbackWithHugePageAdvice
                | HugePageMmapBacking::OrdinaryFallbackAligned
                | HugePageMmapBacking::OrdinaryFallbackAlignedWithHugePageAdvice
        )
    }

    #[inline]
    pub const fn is_aligned_fallback(self) -> bool {
        matches!(
            self,
            HugePageMmapBacking::OrdinaryFallbackAligned
                | HugePageMmapBacking::OrdinaryFallbackAlignedWithHugePageAdvice
        )
    }
}

#[inline]
pub fn hugepage_mmap_platform_error_code_name(
    stage: HugePageMmapPlatformErrorStage,
    code: isize,
) -> &'static str {
    match stage {
        HugePageMmapPlatformErrorStage::None => "none",
        HugePageMmapPlatformErrorStage::InvalidRequest => "invalid-request",
        HugePageMmapPlatformErrorStage::LinuxHugetlbMmap => match code {
            1 => "EPERM",
            12 => "ENOMEM",
            22 => "EINVAL",
            38 => "ENOSYS",
            _ => "linux-errno",
        },
        HugePageMmapPlatformErrorStage::MacosMachVmAllocate
        | HugePageMmapPlatformErrorStage::MacosMprotect
        | HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported => match code {
            0 => "none",
            1 => "KERN_INVALID_ADDRESS",
            2 => "KERN_PROTECTION_FAILURE",
            3 => "KERN_NO_SPACE",
            4 => "KERN_INVALID_ARGUMENT",
            5 => "KERN_FAILURE",
            6 => "KERN_RESOURCE_SHORTAGE",
            46 => "KERN_NOT_SUPPORTED",
            49 => "KERN_OPERATION_TIMED_OUT",
            53 => "KERN_DENIED",
            _ => "kern-return",
        },
        HugePageMmapPlatformErrorStage::UnixUnsupportedPlatform => "unsupported-platform",
        HugePageMmapPlatformErrorStage::WindowsLargePageMinimumUnavailable => {
            "large-page-minimum-unavailable"
        }
        HugePageMmapPlatformErrorStage::WindowsLargePageAlloc => match code {
            5 => "ERROR_ACCESS_DENIED",
            87 => "ERROR_INVALID_PARAMETER",
            1314 => "ERROR_PRIVILEGE_NOT_HELD",
            1455 => "ERROR_COMMITMENT_LIMIT",
            _ => "windows-error",
        },
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HugePageMmapResult {
    pub ptr: *mut u8,
    pub backing: HugePageMmapBacking,
}

/// One 2 MiB-aligned anonymous mapping submitted to Linux THP policy.
///
/// A successful `MADV_HUGEPAGE` call only makes the VMA eligible for THP. It
/// does not prove that the kernel has installed a PMD-sized mapping, so this
/// result deliberately reports advice state separately from actual backing.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransparentHugePageMmapResult {
    pub ptr: *mut u8,
    pub advice_succeeded: bool,
}

impl TransparentHugePageMmapResult {
    #[inline]
    pub const fn failed() -> Self {
        Self {
            ptr: core::ptr::null_mut(),
            advice_succeeded: false,
        }
    }

    #[inline]
    pub fn is_success(self) -> bool {
        !mmap_failed(self.ptr)
    }
}

impl HugePageMmapResult {
    #[inline]
    pub const fn failed() -> Self {
        HugePageMmapResult {
            ptr: core::ptr::null_mut(),
            backing: HugePageMmapBacking::Failed,
        }
    }

    #[inline]
    pub fn from_ptr(ptr: *mut u8, backing: HugePageMmapBacking) -> Self {
        let ptr = normalize_mmap_result(ptr);
        HugePageMmapResult {
            ptr,
            backing: if ptr.is_null() {
                HugePageMmapBacking::Failed
            } else {
                backing
            },
        }
    }

    #[inline]
    pub fn is_success(self) -> bool {
        !mmap_failed(self.ptr)
    }
}

static HUGEPAGE_MMAP_ATTEMPTS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_SUCCESSES: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_FALLBACKS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_ALIGNED_FALLBACKS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_ADVISED_FALLBACKS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_FALLBACK_FAILURES: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_HUGETLB_SYSCALLS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_HUGETLB_SKIPS: AtomicUsize = AtomicUsize::new(0);
static HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_STAGE: AtomicUsize =
    AtomicUsize::new(HugePageMmapPlatformErrorStage::None as usize);
static HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_CODE: AtomicIsize = AtomicIsize::new(0);
#[cfg(test)]
static HUGEPAGE_STATS_TEST_CAPTURE_ACTIVE: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
#[thread_local]
static mut HUGEPAGE_STATS_TEST_CAPTURE_OWNER: bool = false;
#[cfg(test)]
static HUGEPAGE_STATS_TEST_LOCK: spin::Mutex<()> = spin::Mutex::new(());

#[cfg(test)]
pub(crate) struct HugepageStatsTestGuard {
    _lock: spin::MutexGuard<'static, ()>,
}

#[cfg(test)]
impl HugepageStatsTestGuard {
    pub(crate) fn new() -> Self {
        let lock = HUGEPAGE_STATS_TEST_LOCK.lock();
        unsafe {
            HUGEPAGE_STATS_TEST_CAPTURE_OWNER = true;
        }
        HUGEPAGE_STATS_TEST_CAPTURE_ACTIVE.store(true, Ordering::Release);
        hugepage_mmap_stats_reset_for_test();
        Self { _lock: lock }
    }
}

#[cfg(test)]
impl Drop for HugepageStatsTestGuard {
    fn drop(&mut self) {
        unsafe {
            HUGEPAGE_STATS_TEST_CAPTURE_OWNER = false;
        }
        HUGEPAGE_STATS_TEST_CAPTURE_ACTIVE.store(false, Ordering::Release);
    }
}

#[cfg(target_os = "linux")]
const LINUX_HUGETLB_SUPPORT_UNKNOWN: usize = 0;
#[cfg(target_os = "linux")]
const LINUX_HUGETLB_SUPPORT_PRESENT: usize = 1;
#[cfg(target_os = "linux")]
const LINUX_HUGETLB_SUPPORT_UNSUPPORTED: usize = 2;

#[cfg(target_os = "linux")]
static LINUX_HUGETLB_SUPPORT_STATE: AtomicUsize = AtomicUsize::new(LINUX_HUGETLB_SUPPORT_UNKNOWN);
#[cfg(target_os = "linux")]
static LINUX_HUGETLB_UNSUPPORTED_CODE: AtomicIsize = AtomicIsize::new(0);

const HUGEPAGE_FALLBACK_ALIGNMENT: usize = 2 * 1024 * 1024;

#[inline]
pub fn mmap_failed(ptr: *mut u8) -> bool {
    ptr.is_null() || ptr as usize == usize::MAX
}

#[inline]
pub fn normalize_mmap_result(ptr: *mut u8) -> *mut u8 {
    if mmap_failed(ptr) {
        core::ptr::null_mut()
    } else {
        ptr
    }
}

#[inline]
fn dangling_ptr_for_layout(layout: Layout) -> *mut u8 {
    layout.align() as *mut u8
}

#[inline]
fn zero_sized_slice_for_layout(layout: Layout) -> NonNull<[u8]> {
    let ptr = unsafe { NonNull::new_unchecked(dangling_ptr_for_layout(layout)) };
    NonNull::slice_from_raw_parts(ptr, 0)
}

#[inline]
fn page_heap_layout_supported(layout: Layout) -> bool {
    layout.size() == 0
        || (layout.size() <= isize::MAX as usize
            && ((layout.align() <= crate::PAGE_SIZE
                && page_heap_payload_mapping_size(layout).is_some())
                || over_page_aligned_layout_supported(layout)))
}

#[inline]
fn page_rounded_mapping_size(size: usize) -> Option<usize> {
    size.checked_add(crate::PAGE_SIZE - 1)
        .map(|rounded| rounded & !(crate::PAGE_SIZE - 1))
}

#[inline]
fn page_heap_payload_mapping_size(layout: Layout) -> Option<usize> {
    if layout.size() == 0 {
        None
    } else {
        page_rounded_mapping_size(layout.size())
    }
}

#[inline]
pub fn over_page_aligned_layout_supported(layout: Layout) -> bool {
    if layout.size() == 0
        || layout.size() > isize::MAX as usize
        || layout.align() <= crate::PAGE_SIZE
        || layout.align() % crate::PAGE_SIZE != 0
    {
        return false;
    }
    #[cfg(unix)]
    {
        let extra_alignment_span = layout.align() - crate::PAGE_SIZE;
        page_rounded_mapping_size(layout.size())
            .and_then(|payload_size| payload_size.checked_add(extra_alignment_span))
            .is_some()
    }
    #[cfg(windows)]
    {
        page_rounded_mapping_size(layout.size()).is_some()
            && layout.align() <= windows_allocation_granularity()
    }
    #[cfg(not(any(unix, windows)))]
    {
        false
    }
}

#[cfg(windows)]
#[inline]
fn windows_allocation_granularity() -> usize {
    use core::mem::MaybeUninit;
    use winapi::um::sysinfoapi::{GetSystemInfo, SYSTEM_INFO};

    unsafe {
        let mut sysinfo = MaybeUninit::<SYSTEM_INFO>::uninit();
        GetSystemInfo(sysinfo.as_mut_ptr());
        let sysinfo = sysinfo.assume_init();
        let granularity = sysinfo.dwAllocationGranularity as usize;
        if granularity == 0 {
            crate::PAGE_SIZE
        } else {
            granularity
        }
    }
}

#[inline]
fn record_hugepage_attempt() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_ATTEMPTS.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn record_hugetlb_mmap_syscall() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_HUGETLB_SYSCALLS.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn record_hugetlb_mmap_skip() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_HUGETLB_SKIPS.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn record_hugepage_success() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_SUCCESSES.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn record_hugepage_fallback() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_FALLBACKS.fetch_add(1, Ordering::Relaxed);
}

#[inline]
fn record_hugepage_fallback_backing(backing: HugePageMmapBacking) {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    if backing.is_aligned_fallback() {
        HUGEPAGE_MMAP_ALIGNED_FALLBACKS.fetch_add(1, Ordering::Relaxed);
    }
    if matches!(
        backing,
        HugePageMmapBacking::OrdinaryFallbackWithHugePageAdvice
            | HugePageMmapBacking::OrdinaryFallbackAlignedWithHugePageAdvice
    ) {
        HUGEPAGE_MMAP_ADVISED_FALLBACKS.fetch_add(1, Ordering::Relaxed);
    }
    if matches!(backing, HugePageMmapBacking::Failed) {
        HUGEPAGE_MMAP_FALLBACK_FAILURES.fetch_add(1, Ordering::Relaxed);
    }
}

#[inline]
fn clear_hugepage_platform_error() {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_CODE.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_STAGE.store(
        HugePageMmapPlatformErrorStage::None as usize,
        Ordering::Relaxed,
    );
}

#[inline]
fn record_hugepage_platform_error(stage: HugePageMmapPlatformErrorStage, code: isize) {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_CODE.store(code, Ordering::Relaxed);
    HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_STAGE.store(stage as usize, Ordering::Relaxed);
}

#[cfg(target_os = "linux")]
#[inline]
fn linux_hugetlb_error_is_stable_unsupported(code: isize) -> bool {
    matches!(code, 1 | 22 | 38)
}

#[cfg(target_os = "linux")]
#[inline]
fn record_linux_hugetlb_unsupported(code: isize) {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    LINUX_HUGETLB_UNSUPPORTED_CODE.store(code, Ordering::Relaxed);
    LINUX_HUGETLB_SUPPORT_STATE.store(LINUX_HUGETLB_SUPPORT_UNSUPPORTED, Ordering::Release);
}

#[inline]
fn hugepage_observation_recording_allowed() -> bool {
    #[cfg(test)]
    {
        !HUGEPAGE_STATS_TEST_CAPTURE_ACTIVE.load(Ordering::Acquire)
            || unsafe { HUGEPAGE_STATS_TEST_CAPTURE_OWNER }
    }
    #[cfg(not(test))]
    {
        true
    }
}

#[cfg(target_os = "linux")]
/// # Safety
///
/// `ptr..ptr + size` must describe a live anonymous mapping owned by the
/// caller for the duration of the syscall.
pub unsafe fn advise_transparent_hugepage(ptr: *mut u8, size: usize) -> bool {
    if ptr.is_null() || size == 0 {
        return false;
    }
    libc::madvise(ptr as *mut libc::c_void, size, libc::MADV_HUGEPAGE) == 0
}

#[cfg(not(target_os = "linux"))]
/// # Safety
///
/// The same mapping validity contract applies on every target.
pub unsafe fn advise_transparent_hugepage(_ptr: *mut u8, _size: usize) -> bool {
    false
}

#[inline]
fn hugepage_aligned_fallback_layout(req: usize) -> Option<Layout> {
    if req == 0 || req % HUGEPAGE_FALLBACK_ALIGNMENT != 0 {
        return None;
    }
    Layout::from_size_align(req, HUGEPAGE_FALLBACK_ALIGNMENT).ok()
}

/// Map ordinary anonymous memory aligned to the Linux PMD THP size and make
/// the VMA THP-eligible. Advice success remains distinct from actual THP
/// backing; callers that require point-in-time confirmation can use
/// [`collapse_transparent_hugepage`].
///
/// # Safety
///
/// The returned mapping must be released exactly once with its full `req`
/// length. `req` and `prot` must describe a valid anonymous mapping request.
#[cfg(target_os = "linux")]
pub unsafe fn mmap_transparent_hugepage(
    req: usize,
    prot: prots::Prot,
) -> TransparentHugePageMmapResult {
    let Some(layout) = hugepage_aligned_fallback_layout(req) else {
        return TransparentHugePageMmapResult::failed();
    };
    let ptr = normalize_mmap_result(mmap_over_page_aligned(layout, prot));
    if ptr.is_null() || ptr as usize % HUGEPAGE_FALLBACK_ALIGNMENT != 0 {
        if !ptr.is_null() {
            munmap(ptr, req);
        }
        return TransparentHugePageMmapResult::failed();
    }
    TransparentHugePageMmapResult {
        ptr,
        advice_succeeded: advise_transparent_hugepage(ptr, req),
    }
}

#[cfg(not(target_os = "linux"))]
/// # Safety
///
/// The caller must uphold the same mapping request contract on every target.
pub unsafe fn mmap_transparent_hugepage(
    _req: usize,
    _prot: prots::Prot,
) -> TransparentHugePageMmapResult {
    TransparentHugePageMmapResult::failed()
}

/// Ask Linux to synchronously collapse an aligned anonymous range.
///
/// `Ok(())` confirms that this range was collapsed at this point in time.
/// Linux may split a THP later, so callers must not treat this as a permanent
/// backing guarantee.
///
/// # Safety
///
/// `ptr..ptr + size` must remain a live, writable anonymous mapping while the
/// synchronous collapse executes. Both address and size must be 2 MiB-aligned.
#[cfg(target_os = "linux")]
pub unsafe fn collapse_transparent_hugepage(ptr: *mut u8, size: usize) -> Result<(), isize> {
    // `MADV_COLLAPSE` is Linux UAPI value 25. Defining it locally keeps this
    // PAL buildable for libc targets which have not exported the newer name.
    const MADV_COLLAPSE: libc::c_int = 25;
    if ptr.is_null()
        || size == 0
        || ptr as usize % HUGEPAGE_FALLBACK_ALIGNMENT != 0
        || size % HUGEPAGE_FALLBACK_ALIGNMENT != 0
    {
        return Err(libc::EINVAL as isize);
    }
    if libc::madvise(ptr as *mut libc::c_void, size, MADV_COLLAPSE) == 0 {
        Ok(())
    } else {
        Err(*libc::__errno_location() as isize)
    }
}

#[cfg(not(target_os = "linux"))]
/// # Safety
///
/// The caller must uphold the same live-mapping contract on every target.
pub unsafe fn collapse_transparent_hugepage(_ptr: *mut u8, _size: usize) -> Result<(), isize> {
    Err(0)
}

#[cfg(unix)]
#[inline]
unsafe fn mmap_huge_aligned_fallback(req: usize, prot: prots::Prot) -> (*mut u8, bool) {
    if let Some(layout) = hugepage_aligned_fallback_layout(req) {
        let ptr = normalize_mmap_result(mmap_over_page_aligned(layout, prot));
        if !ptr.is_null() {
            return (ptr, true);
        }
    }
    (normalize_mmap_result(mmap(req, prot)), false)
}

#[cfg(not(unix))]
#[inline]
unsafe fn mmap_huge_aligned_fallback(req: usize, prot: prots::Prot) -> (*mut u8, bool) {
    (normalize_mmap_result(mmap(req, prot)), false)
}

#[inline]
unsafe fn mmap_huge_fallback(req: usize, prot: prots::Prot) -> HugePageMmapResult {
    record_hugepage_fallback();
    let (ptr, aligned) = mmap_huge_aligned_fallback(req, prot);
    let backing = if ptr.is_null() {
        HugePageMmapBacking::Failed
    } else {
        match (aligned, advise_transparent_hugepage(ptr, req)) {
            (true, true) => HugePageMmapBacking::OrdinaryFallbackAlignedWithHugePageAdvice,
            (true, false) => HugePageMmapBacking::OrdinaryFallbackAligned,
            (false, true) => HugePageMmapBacking::OrdinaryFallbackWithHugePageAdvice,
            (false, false) => HugePageMmapBacking::OrdinaryFallback,
        }
    };
    record_hugepage_fallback_backing(backing);
    HugePageMmapResult { ptr, backing }
}

#[cfg(unix)]
#[inline]
pub fn hugepage_fallback_alignment() -> usize {
    HUGEPAGE_FALLBACK_ALIGNMENT
}

#[cfg(not(unix))]
#[inline]
pub fn hugepage_fallback_alignment() -> usize {
    crate::PAGE_SIZE
}

#[cfg(unix)]
#[inline]
pub fn hugepage_aligned_fallback_supported(req: usize) -> bool {
    if let Some(layout) = hugepage_aligned_fallback_layout(req) {
        over_page_aligned_layout_supported(layout)
    } else {
        false
    }
}

#[cfg(not(unix))]
#[inline]
pub fn hugepage_aligned_fallback_supported(_req: usize) -> bool {
    false
}

pub fn hugepage_mmap_stats_snapshot() -> HugePageMmapStats {
    HugePageMmapStats {
        attempts: HUGEPAGE_MMAP_ATTEMPTS.load(Ordering::Relaxed),
        successes: HUGEPAGE_MMAP_SUCCESSES.load(Ordering::Relaxed),
        fallbacks: HUGEPAGE_MMAP_FALLBACKS.load(Ordering::Relaxed),
        aligned_fallbacks: HUGEPAGE_MMAP_ALIGNED_FALLBACKS.load(Ordering::Relaxed),
        advised_fallbacks: HUGEPAGE_MMAP_ADVISED_FALLBACKS.load(Ordering::Relaxed),
        fallback_failures: HUGEPAGE_MMAP_FALLBACK_FAILURES.load(Ordering::Relaxed),
        hugetlb_mmap_syscalls: HUGEPAGE_MMAP_HUGETLB_SYSCALLS.load(Ordering::Relaxed),
        hugetlb_mmap_skips: HUGEPAGE_MMAP_HUGETLB_SKIPS.load(Ordering::Relaxed),
    }
}

pub fn hugepage_mmap_platform_status_snapshot() -> HugePageMmapPlatformStatus {
    HugePageMmapPlatformStatus {
        last_error_stage: HugePageMmapPlatformErrorStage::from_usize(
            HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_STAGE.load(Ordering::Relaxed),
        ),
        last_error_code: HUGEPAGE_MMAP_LAST_PLATFORM_ERROR_CODE.load(Ordering::Relaxed),
    }
}

#[cfg(test)]
fn hugepage_mmap_stats_reset_for_test() {
    HUGEPAGE_MMAP_ATTEMPTS.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_SUCCESSES.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_FALLBACKS.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_ALIGNED_FALLBACKS.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_ADVISED_FALLBACKS.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_FALLBACK_FAILURES.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_HUGETLB_SYSCALLS.store(0, Ordering::Relaxed);
    HUGEPAGE_MMAP_HUGETLB_SKIPS.store(0, Ordering::Relaxed);
    #[cfg(target_os = "linux")]
    {
        LINUX_HUGETLB_SUPPORT_STATE.store(LINUX_HUGETLB_SUPPORT_UNKNOWN, Ordering::Relaxed);
        LINUX_HUGETLB_UNSUPPORTED_CODE.store(0, Ordering::Relaxed);
    }
    #[cfg(target_os = "macos")]
    {
        MACOS_SUPERPAGE_SUPPORT_STATE.store(MACOS_SUPERPAGE_SUPPORT_UNKNOWN, Ordering::Relaxed);
        MACOS_SUPERPAGE_UNSUPPORTED_CODE.store(0, Ordering::Relaxed);
    }
    clear_hugepage_platform_error();
}

/// # Safety
///
/// safe if the size is valid
#[cfg(unix)]
pub unsafe fn mmap(req: usize, prot: i32) -> *mut u8 {
    libc::mmap(
        core::ptr::null_mut(),
        req,
        prot,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
        -1,
        0,
    ) as *mut u8
}

#[cfg(target_os = "linux")]
pub unsafe fn mmap_huge_with_backing(req: usize, prot: i32) -> HugePageMmapResult {
    record_hugepage_attempt();
    clear_hugepage_platform_error();
    if req == 0 {
        record_hugepage_platform_error(HugePageMmapPlatformErrorStage::InvalidRequest, 0);
        return mmap_huge_fallback(req, prot);
    }
    if LINUX_HUGETLB_SUPPORT_STATE.load(Ordering::Acquire) == LINUX_HUGETLB_SUPPORT_UNSUPPORTED {
        let code = LINUX_HUGETLB_UNSUPPORTED_CODE.load(Ordering::Relaxed);
        record_hugetlb_mmap_skip();
        record_hugepage_platform_error(HugePageMmapPlatformErrorStage::LinuxHugetlbMmap, code);
        return mmap_huge_fallback(req, prot);
    }
    record_hugetlb_mmap_syscall();
    let huge_ptr = libc::mmap(
        core::ptr::null_mut(),
        req,
        prot,
        libc::MAP_PRIVATE | libc::MAP_ANONYMOUS | libc::MAP_HUGETLB | libc::MAP_HUGE_2MB,
        -1,
        0,
    ) as *mut u8;
    if !mmap_failed(huge_ptr) {
        if hugepage_observation_recording_allowed() {
            LINUX_HUGETLB_SUPPORT_STATE.store(LINUX_HUGETLB_SUPPORT_PRESENT, Ordering::Release);
        }
        record_hugepage_success();
        return HugePageMmapResult::from_ptr(huge_ptr, HugePageMmapBacking::HugePage);
    }
    let errno = *libc::__errno_location() as isize;
    record_hugepage_platform_error(HugePageMmapPlatformErrorStage::LinuxHugetlbMmap, errno);
    if linux_hugetlb_error_is_stable_unsupported(errno) {
        record_linux_hugetlb_unsupported(errno);
    }

    mmap_huge_fallback(req, prot)
}

#[cfg(target_os = "linux")]
pub unsafe fn mmap_huge(req: usize, prot: i32) -> *mut u8 {
    mmap_huge_with_backing(req, prot).ptr
}

#[cfg(target_os = "macos")]
type MachVmAddress = u64;
#[cfg(target_os = "macos")]
type MachVmSize = u64;

#[cfg(target_os = "macos")]
const KERN_SUCCESS: libc::kern_return_t = 0;
#[cfg(target_os = "macos")]
const KERN_INVALID_ARGUMENT: libc::kern_return_t = 4;
#[cfg(target_os = "macos")]
const KERN_NOT_SUPPORTED: libc::kern_return_t = 46;
#[cfg(target_os = "macos")]
const DARWIN_SUPERPAGE_SIZE_2MB: usize = 2 * 1024 * 1024;
#[cfg(target_os = "macos")]
const VM_FLAGS_ANYWHERE: libc::c_int = 0x0000_0001;
#[cfg(target_os = "macos")]
const VM_FLAGS_SUPERPAGE_SIZE_2MB: libc::c_int = 2 << 16;
#[cfg(all(test, target_os = "macos"))]
const VM_FLAGS_FIXED: libc::c_int = 0x0000_0000;
#[cfg(target_os = "macos")]
const MACOS_SUPERPAGE_SUPPORT_UNKNOWN: usize = 0;
#[cfg(target_os = "macos")]
const MACOS_SUPERPAGE_SUPPORT_PRESENT: usize = 1;
#[cfg(target_os = "macos")]
const MACOS_SUPERPAGE_SUPPORT_UNSUPPORTED: usize = 2;

#[cfg(target_os = "macos")]
static MACOS_SUPERPAGE_SUPPORT_STATE: AtomicUsize =
    AtomicUsize::new(MACOS_SUPERPAGE_SUPPORT_UNKNOWN);
#[cfg(target_os = "macos")]
static MACOS_SUPERPAGE_UNSUPPORTED_CODE: AtomicIsize = AtomicIsize::new(0);

#[cfg(target_os = "macos")]
extern "C" {
    static mach_task_self_: libc::mach_port_t;

    fn mach_vm_allocate(
        target: libc::mach_port_t,
        address: *mut MachVmAddress,
        size: MachVmSize,
        flags: libc::c_int,
    ) -> libc::kern_return_t;

    fn mach_vm_deallocate(
        target: libc::mach_port_t,
        address: MachVmAddress,
        size: MachVmSize,
    ) -> libc::kern_return_t;
}

#[cfg(all(test, target_os = "macos"))]
pub(crate) unsafe fn fixed_address_mapping_available_for_test(ptr: *mut u8, size: usize) -> bool {
    if ptr.is_null() || size == 0 {
        return false;
    }

    let mut address = ptr as MachVmAddress;
    let result = mach_vm_allocate(
        mach_task_self_,
        &mut address,
        size as MachVmSize,
        VM_FLAGS_FIXED,
    );
    if result != KERN_SUCCESS {
        return false;
    }

    let allocated_requested_address = address == ptr as MachVmAddress;
    let _ = mach_vm_deallocate(mach_task_self_, address, size as MachVmSize);
    allocated_requested_address
}

#[cfg(target_os = "macos")]
#[inline]
fn macos_superpage_is_unsupported_error(result: libc::kern_return_t) -> bool {
    result == KERN_INVALID_ARGUMENT || result == KERN_NOT_SUPPORTED
}

#[cfg(target_os = "macos")]
#[inline]
fn record_macos_superpage_unsupported(code: libc::kern_return_t) {
    if !hugepage_observation_recording_allowed() {
        return;
    }
    let code = code as isize;
    MACOS_SUPERPAGE_UNSUPPORTED_CODE.store(code, Ordering::Relaxed);
    MACOS_SUPERPAGE_SUPPORT_STATE.store(MACOS_SUPERPAGE_SUPPORT_UNSUPPORTED, Ordering::Relaxed);
    record_hugepage_platform_error(
        HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported,
        code,
    );
}

#[cfg(target_os = "macos")]
unsafe fn mmap_huge_macos(req: usize, prot: i32) -> *mut u8 {
    if req == 0 || req % DARWIN_SUPERPAGE_SIZE_2MB != 0 {
        record_hugepage_platform_error(HugePageMmapPlatformErrorStage::InvalidRequest, 0);
        return core::ptr::null_mut();
    }

    if MACOS_SUPERPAGE_SUPPORT_STATE.load(Ordering::Relaxed) == MACOS_SUPERPAGE_SUPPORT_UNSUPPORTED
    {
        let cached_code = MACOS_SUPERPAGE_UNSUPPORTED_CODE.load(Ordering::Relaxed);
        record_hugepage_platform_error(
            HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported,
            cached_code,
        );
        return core::ptr::null_mut();
    }

    let mut address: MachVmAddress = 0;
    let result = mach_vm_allocate(
        mach_task_self_,
        &mut address,
        req as MachVmSize,
        VM_FLAGS_ANYWHERE | VM_FLAGS_SUPERPAGE_SIZE_2MB,
    );
    if result != KERN_SUCCESS || address == 0 {
        if macos_superpage_is_unsupported_error(result) {
            record_macos_superpage_unsupported(result);
        } else {
            record_hugepage_platform_error(
                HugePageMmapPlatformErrorStage::MacosMachVmAllocate,
                result as isize,
            );
        }
        return core::ptr::null_mut();
    }

    if hugepage_observation_recording_allowed() {
        MACOS_SUPERPAGE_SUPPORT_STATE.store(MACOS_SUPERPAGE_SUPPORT_PRESENT, Ordering::Relaxed);
    }
    let ptr = address as *mut u8;
    if !mprotect(ptr, req, prot) {
        record_hugepage_platform_error(HugePageMmapPlatformErrorStage::MacosMprotect, 0);
        let _ = mach_vm_deallocate(mach_task_self_, address, req as MachVmSize);
        return core::ptr::null_mut();
    }
    ptr
}

#[cfg(target_os = "macos")]
pub unsafe fn mmap_huge_with_backing(req: usize, prot: i32) -> HugePageMmapResult {
    record_hugepage_attempt();
    clear_hugepage_platform_error();
    let huge_ptr = mmap_huge_macos(req, prot);
    if !mmap_failed(huge_ptr) {
        record_hugepage_success();
        return HugePageMmapResult::from_ptr(huge_ptr, HugePageMmapBacking::HugePage);
    }

    mmap_huge_fallback(req, prot)
}

#[cfg(target_os = "macos")]
pub unsafe fn mmap_huge(req: usize, prot: i32) -> *mut u8 {
    mmap_huge_with_backing(req, prot).ptr
}

#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
pub unsafe fn mmap_huge_with_backing(req: usize, prot: i32) -> HugePageMmapResult {
    // Other Unix targets do not expose a wired hugepage API through this PAL.
    // Keep the feature buildable and fall back to ordinary anonymous mmap;
    // evaluation must record that hugepage acceleration was unavailable.
    record_hugepage_attempt();
    clear_hugepage_platform_error();
    record_hugepage_platform_error(HugePageMmapPlatformErrorStage::UnixUnsupportedPlatform, 0);
    mmap_huge_fallback(req, prot)
}

#[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
pub unsafe fn mmap_huge(req: usize, prot: i32) -> *mut u8 {
    mmap_huge_with_backing(req, prot).ptr
}

#[cfg(target_os = "windows")]
pub unsafe fn mmap_huge_with_backing(req: usize, prot: u32) -> HugePageMmapResult {
    record_hugepage_attempt();
    clear_hugepage_platform_error();

    use winapi::um::errhandlingapi::GetLastError;
    use winapi::um::memoryapi::{GetLargePageMinimum, VirtualAlloc};
    use winapi::um::winnt::{MEM_COMMIT, MEM_LARGE_PAGES, MEM_RESERVE};

    let large_page_minimum = GetLargePageMinimum();
    if req == 0 {
        record_hugepage_platform_error(HugePageMmapPlatformErrorStage::InvalidRequest, 0);
    } else if large_page_minimum == 0 {
        record_hugepage_platform_error(
            HugePageMmapPlatformErrorStage::WindowsLargePageMinimumUnavailable,
            0,
        );
    }
    if req != 0 && large_page_minimum != 0 {
        let large_req = req
            .checked_add(large_page_minimum - 1)
            .map(|size| size / large_page_minimum * large_page_minimum);
        if let Some(large_req) = large_req {
            let large_ptr = VirtualAlloc(
                core::ptr::null_mut(),
                large_req,
                MEM_RESERVE | MEM_COMMIT | MEM_LARGE_PAGES,
                prot,
            ) as *mut u8;
            if !mmap_failed(large_ptr) {
                record_hugepage_success();
                return HugePageMmapResult::from_ptr(large_ptr, HugePageMmapBacking::HugePage);
            }
            record_hugepage_platform_error(
                HugePageMmapPlatformErrorStage::WindowsLargePageAlloc,
                GetLastError() as isize,
            );
        }
    }

    mmap_huge_fallback(req, prot)
}

#[cfg(target_os = "windows")]
pub unsafe fn mmap_huge(req: usize, prot: u32) -> *mut u8 {
    mmap_huge_with_backing(req, prot).ptr
}

/// # Safety
///
/// safe if the size is valid
#[cfg(target_os = "windows")]
pub unsafe fn mmap(req: usize, prot: u32) -> *mut u8 {
    use winapi::um::memoryapi::VirtualAlloc;
    use winapi::um::winnt::MEM_COMMIT;
    use winapi::um::winnt::MEM_RESERVE;

    VirtualAlloc(core::ptr::null_mut(), req, MEM_RESERVE | MEM_COMMIT, prot) as *mut u8
}

/// # Safety
///
/// safe if the size is valid
#[cfg(unix)]
pub unsafe fn munmap(ptr: *mut u8, size: usize) {
    libc::munmap(ptr as *mut libc::c_void, size);
}

#[cfg(unix)]
#[inline]
unsafe fn munmap_checked(ptr: *mut u8, size: usize) -> bool {
    size == 0 || libc::munmap(ptr as *mut libc::c_void, size) == 0
}

/// Allocate a non-empty mapping whose returned address satisfies alignments
/// larger than the OS page size.  The implementation deliberately avoids a
/// side table: it reserves enough extra pages, trims the prefix/suffix with
/// `munmap`, and leaves an exactly deallocatable aligned mapping behind.
#[cfg(unix)]
pub unsafe fn mmap_over_page_aligned(layout: Layout, prot: prots::Prot) -> *mut u8 {
    if !over_page_aligned_layout_supported(layout) {
        return core::ptr::null_mut();
    }

    let payload_size = match page_rounded_mapping_size(layout.size()) {
        Some(size) => size,
        None => return core::ptr::null_mut(),
    };
    let reserve_size = match payload_size.checked_add(layout.align() - crate::PAGE_SIZE) {
        Some(size) => size,
        None => return core::ptr::null_mut(),
    };
    let base = normalize_mmap_result(mmap(reserve_size, prot));
    if base.is_null() {
        return core::ptr::null_mut();
    }

    let base_addr = base as usize;
    let aligned_addr = match base_addr.checked_add(layout.align() - 1) {
        Some(value) => value & !(layout.align() - 1),
        None => {
            munmap(base, reserve_size);
            return core::ptr::null_mut();
        }
    };
    let prefix_size = aligned_addr.saturating_sub(base_addr);
    if prefix_size > reserve_size {
        munmap(base, reserve_size);
        return core::ptr::null_mut();
    }
    let suffix_start = match aligned_addr.checked_add(payload_size) {
        Some(addr) => addr,
        None => {
            munmap(base, reserve_size);
            return core::ptr::null_mut();
        }
    };
    let used = match prefix_size.checked_add(payload_size) {
        Some(size) => size,
        None => {
            munmap(base, reserve_size);
            return core::ptr::null_mut();
        }
    };
    let suffix_size = match reserve_size.checked_sub(used) {
        Some(size) => size,
        None => {
            munmap(base, reserve_size);
            return core::ptr::null_mut();
        }
    };

    if prefix_size != 0 && !munmap_checked(base, prefix_size) {
        munmap(base, reserve_size);
        return core::ptr::null_mut();
    }
    if suffix_size != 0 && !munmap_checked(suffix_start as *mut u8, suffix_size) {
        let _ = munmap_checked(aligned_addr as *mut u8, payload_size);
        return core::ptr::null_mut();
    }
    aligned_addr as *mut u8
}

#[cfg(windows)]
pub unsafe fn mmap_over_page_aligned(layout: Layout, prot: prots::Prot) -> *mut u8 {
    if !over_page_aligned_layout_supported(layout) {
        return core::ptr::null_mut();
    }
    let payload_size = match page_rounded_mapping_size(layout.size()) {
        Some(size) => size,
        None => return core::ptr::null_mut(),
    };
    let ptr = normalize_mmap_result(mmap(payload_size, prot));
    if ptr.is_null() {
        return core::ptr::null_mut();
    }
    // VirtualAlloc returns reservation bases aligned to the system allocation
    // granularity.  We only advertise support for alignments up to that
    // granularity, so the returned base is directly deallocatable with
    // MEM_RELEASE and needs no recovery side table.
    if ptr as usize % layout.align() == 0 {
        ptr
    } else {
        munmap(ptr, payload_size);
        core::ptr::null_mut()
    }
}

#[cfg(not(any(unix, windows)))]
pub unsafe fn mmap_over_page_aligned(_layout: Layout, _prot: prots::Prot) -> *mut u8 {
    core::ptr::null_mut()
}

pub unsafe fn munmap_over_page_aligned(ptr: *mut u8, layout: Layout) {
    if ptr.is_null() || !over_page_aligned_layout_supported(layout) {
        return;
    }
    if let Some(payload_size) = page_rounded_mapping_size(layout.size()) {
        munmap(ptr, payload_size);
    }
}

/// # Safety
///
/// `ptr..ptr+size` must describe pages owned by this process.
#[cfg(unix)]
pub unsafe fn mprotect(ptr: *mut u8, size: usize, prot: i32) -> bool {
    libc::mprotect(ptr as *mut libc::c_void, size, prot) == 0
}

/// # Safety
///
/// safe if the size is valid
#[cfg(target_os = "windows")]
pub unsafe fn munmap(ptr: *mut u8, _size: usize) {
    use winapi::um::memoryapi::VirtualFree;
    use winapi::um::winnt::MEM_RELEASE;
    VirtualFree(ptr as *mut _, 0, MEM_RELEASE);
}

/// # Safety
///
/// `ptr..ptr+size` must describe pages owned by this process.
#[cfg(target_os = "windows")]
pub unsafe fn mprotect(ptr: *mut u8, size: usize, prot: u32) -> bool {
    use winapi::um::memoryapi::VirtualProtect;
    let mut old = 0;
    VirtualProtect(ptr as *mut _, size, prot, &mut old) != 0
}

unsafe impl GlobalAlloc for PageHeap {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.size() == 0 {
            return dangling_ptr_for_layout(layout);
        }
        if !page_heap_layout_supported(layout) {
            return core::ptr::null_mut();
        }
        let prot = prots::get_prot(self.read, self.write, self.exec);
        if layout.align() > crate::PAGE_SIZE {
            return mmap_over_page_aligned(layout, prot);
        }
        let mapping_size = match page_heap_payload_mapping_size(layout) {
            Some(size) => size,
            None => return core::ptr::null_mut(),
        };
        let ptr = mmap(mapping_size, prot);
        if mmap_failed(ptr) {
            core::ptr::null_mut()
        } else {
            ptr
        }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if ptr.is_null() || layout.size() == 0 {
            return;
        }
        if layout.align() > crate::PAGE_SIZE {
            debug_assert_eq!(ptr as usize % layout.align(), 0);
            munmap_over_page_aligned(ptr, layout);
        } else {
            debug_assert_eq!(ptr as usize % crate::PAGE_SIZE, 0);
            if let Some(mapping_size) = page_heap_payload_mapping_size(layout) {
                munmap(ptr, mapping_size);
            }
        }
    }
}

unsafe impl Allocator for PageHeap {
    fn allocate(&self, layout: Layout) -> Result<NonNull<[u8]>, AllocError> {
        if layout.size() == 0 {
            return Ok(zero_sized_slice_for_layout(layout));
        }
        if !page_heap_layout_supported(layout) {
            return Err(AllocError);
        }
        unsafe {
            let prot = prots::get_prot(self.read, self.write, self.exec);
            let ptr = if layout.align() > crate::PAGE_SIZE {
                mmap_over_page_aligned(layout, prot)
            } else {
                let mapping_size = page_heap_payload_mapping_size(layout).ok_or(AllocError)?;
                mmap(mapping_size, prot)
            };
            if mmap_failed(ptr) {
                Err(AllocError)
            } else {
                Ok(NonNull::new_unchecked(slice::from_raw_parts_mut(
                    ptr,
                    layout.size(),
                )))
            }
        }
    }

    unsafe fn deallocate(&self, ptr: NonNull<u8>, layout: Layout) {
        if layout.size() == 0 {
            return;
        }
        if layout.align() > crate::PAGE_SIZE {
            munmap_over_page_aligned(ptr.as_ptr(), layout);
        } else {
            if let Some(mapping_size) = page_heap_payload_mapping_size(layout) {
                munmap(ptr.as_ptr(), mapping_size);
            }
        }
    }
}

// pub fn madvise_willneed(ptr: *mut u8, len: usize) {
//     unsafe {
//         libc::madvise(ptr as *mut libc::c_void, len, libc::MADV_WILLNEED);
//     }
// }

// pub fn madvise_random(ptr: *mut u8, len: usize) {
//     unsafe {
//         libc::madvise(ptr as *mut libc::c_void, len, libc::MADV_RANDOM);
//     }
// }

// https://docs.rs/mmap-alloc/0.2.0/src/mmap_alloc/lib.rs.html
pub mod prots {
    #[cfg(unix)]
    pub use self::unix::*;
    #[cfg(unix)]
    pub type Prot = i32;
    #[cfg(windows)]
    pub use self::windows::*;
    #[cfg(windows)]
    pub type Prot = u32;

    pub fn get_prot(read: bool, write: bool, exec: bool) -> Prot {
        match (read, write, exec) {
            (false, false, false) => PROT_NONE,
            (true, false, false) => PROT_READ,
            (false, true, false) => PROT_WRITE,
            (false, false, true) => PROT_EXEC,
            (true, true, false) => PROT_READ_WRITE,
            (true, false, true) => PROT_READ_EXEC,
            (false, true, true) => PROT_WRITE_EXEC,
            (true, true, true) => PROT_READ_WRITE_EXEC,
        }
    }

    #[cfg(unix)]
    mod unix {
        // NOTE: On some platforms, libc::PROT_WRITE may imply libc::PROT_READ, and libc::PROT_READ
        // may imply libc::PROT_EXEC.
        extern crate libc;
        pub const PROT_NONE: i32 = libc::PROT_NONE;
        pub const PROT_READ: i32 = libc::PROT_READ;
        pub const PROT_WRITE: i32 = libc::PROT_WRITE;
        pub const PROT_EXEC: i32 = libc::PROT_EXEC;
        pub const PROT_READ_WRITE: i32 = libc::PROT_READ | libc::PROT_WRITE;
        pub const PROT_READ_EXEC: i32 = libc::PROT_READ | libc::PROT_EXEC;
        pub const PROT_WRITE_EXEC: i32 = libc::PROT_WRITE | libc::PROT_EXEC;
        pub const PROT_READ_WRITE_EXEC: i32 = libc::PROT_READ | libc::PROT_WRITE | libc::PROT_EXEC;
    }

    #[cfg(windows)]
    mod windows {
        extern crate winapi;
        use self::winapi::um::winnt;
        pub const PROT_NONE: u32 = winnt::PAGE_NOACCESS;
        pub const PROT_READ: u32 = winnt::PAGE_READONLY;
        // windows doesn't have a write-only permission, so write implies read
        pub const PROT_WRITE: u32 = winnt::PAGE_READWRITE;
        pub const PROT_EXEC: u32 = winnt::PAGE_EXECUTE;
        pub const PROT_READ_WRITE: u32 = winnt::PAGE_READWRITE;
        pub const PROT_READ_EXEC: u32 = winnt::PAGE_EXECUTE_READ;
        // windows doesn't have a write/exec permission, so write/exec implies read/write/exec
        pub const PROT_WRITE_EXEC: u32 = winnt::PAGE_EXECUTE_READWRITE;
        pub const PROT_READ_WRITE_EXEC: u32 = winnt::PAGE_EXECUTE_READWRITE;
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;

    #[test]
    fn page_heap_zero_sized_layout_is_aligned_non_null_and_dealloc_noop() {
        let layout = Layout::from_size_align(0, 256).expect("zero-size page heap layout");
        let page_allocator = PageHeap::default();
        let block = page_allocator
            .allocate(layout)
            .expect("zero-sized PageHeap allocation");
        let ptr = block.as_ptr() as *mut u8 as usize;
        assert_ne!(ptr, 0);
        assert_eq!(ptr % layout.align(), 0);
        assert_eq!(block.len(), 0);
        unsafe {
            page_allocator.deallocate(NonNull::new(block.as_ptr() as *mut u8).unwrap(), layout);
        }
    }

    #[cfg(unix)]
    #[test]
    fn page_heap_allocates_overaligned_layout_without_side_table() {
        let align = crate::PAGE_SIZE * 2;
        let layout = Layout::from_size_align(crate::PAGE_SIZE, align).expect("overaligned layout");
        let page_allocator = PageHeap::default();
        let block = page_allocator
            .allocate(layout)
            .expect("overaligned page heap allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % align, 0);
        assert_eq!(block.len(), layout.size());
        unsafe {
            ptr.write(0xA5);
            ptr.add(layout.size() - 1).write(0x5A);
            page_allocator.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[cfg(not(any(unix, windows)))]
    #[test]
    fn page_heap_rejects_overaligned_layout_when_platform_cannot_trim_mapping() {
        let align = crate::PAGE_SIZE * 2;
        let layout = Layout::from_size_align(crate::PAGE_SIZE, align).expect("overaligned layout");
        let page_allocator = PageHeap::default();
        assert!(page_allocator.allocate(layout).is_err());
        unsafe {
            assert!(page_allocator.alloc(layout).is_null());
        }
    }

    #[cfg(windows)]
    #[test]
    fn page_heap_allocates_overaligned_layout_when_virtualalloc_granularity_is_sufficient() {
        let align = crate::PAGE_SIZE * 2;
        assert!(
            align <= windows_allocation_granularity(),
            "Windows over-page alignment test assumes VirtualAlloc granularity covers two pages"
        );
        let layout = Layout::from_size_align(crate::PAGE_SIZE, align).expect("overaligned layout");
        let page_allocator = PageHeap::default();
        let block = page_allocator
            .allocate(layout)
            .expect("Windows VirtualAlloc should cover modest over-page alignment");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(ptr as usize % align, 0);
        unsafe {
            ptr.write(0xA5);
            ptr.add(layout.size() - 1).write(0x5A);
            page_allocator.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[test]
    fn page_heap_allocates_and_deallocates_page() {
        let layout = Layout::from_size_align(0x1000, 8).expect("It does not work");
        let page_allocator = PageHeap::default();
        unsafe {
            let ptr = page_allocator.alloc(layout);
            assert!(!mmap_failed(ptr));
            ptr.write(0xA5);
            page_allocator.dealloc(ptr, layout);
        }
    }

    #[test]
    fn page_heap_rounds_non_page_multiple_mapping_without_changing_slice_len() {
        let layout = Layout::from_size_align(crate::PAGE_SIZE + 17, 8).expect("non-page layout");
        assert_eq!(
            page_heap_payload_mapping_size(layout),
            Some(crate::PAGE_SIZE * 2),
            "backing mapping length should be explicit and page-rounded"
        );

        let page_allocator = PageHeap::default();
        let block = page_allocator
            .allocate(layout)
            .expect("non-page-multiple PageHeap allocation");
        let ptr = block.as_ptr() as *mut u8;
        assert_eq!(
            block.len(),
            layout.size(),
            "Allocator API still exposes the requested usable length"
        );
        unsafe {
            ptr.write(0xA5);
            ptr.add(layout.size() - 1).write(0x5A);
            page_allocator.deallocate(NonNull::new(ptr).unwrap(), layout);
        }
    }

    #[test]
    fn normalize_mmap_result_rejects_null_and_map_failed() {
        assert!(normalize_mmap_result(core::ptr::null_mut()).is_null());
        assert!(normalize_mmap_result(usize::MAX as *mut u8).is_null());
    }

    #[test]
    fn mmap_huge_zero_sized_request_records_single_failed_fallback() {
        let _guard = HugepageStatsTestGuard::new();
        let prot = prots::get_prot(true, true, false);
        unsafe {
            let result = mmap_huge_with_backing(0, prot);
            assert!(mmap_failed(result.ptr));
            assert_eq!(result.backing, HugePageMmapBacking::Failed);

            let stats = hugepage_mmap_stats_snapshot();
            assert_eq!(
                stats.attempts, 1,
                "a failed hugepage request should record exactly one attempt"
            );
            assert_eq!(
                stats.successes, 0,
                "zero-sized hugepage mapping cannot be a hugepage success"
            );
            assert_eq!(
                stats.fallbacks, 1,
                "the ordinary mmap fallback should be recorded exactly once"
            );
            assert_eq!(
                stats.fallback_failures, 1,
                "failed fallback classifications should be counted separately"
            );
            assert_eq!(stats.aligned_fallbacks, 0);
            assert_eq!(stats.advised_fallbacks, 0);
            assert_eq!(stats.hugetlb_mmap_syscalls, 0);
            assert_eq!(stats.hugetlb_mmap_skips, 0);
        }
    }

    #[test]
    fn mmap_huge_returns_usable_mapping_and_records_fallbacks() {
        const CHILD_ENV: &str = "UNIALLOC_HUGEPAGE_STATS_CHILD";
        const TEST_NAME: &str =
            "pal::sys_alloc::tests::mmap_huge_returns_usable_mapping_and_records_fallbacks";

        if std::env::var_os(CHILD_ENV).is_none() {
            let output = std::process::Command::new(
                std::env::current_exe().expect("current allocator test executable"),
            )
            .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
            .env(CHILD_ENV, "1")
            .env("RUST_BACKTRACE", "0")
            .output()
            .expect("spawn isolated hugepage statistics test");
            assert!(
                output.status.success(),
                "isolated hugepage statistics test failed: {}",
                std::string::String::from_utf8_lossy(&output.stderr)
            );
            return;
        }

        let _guard = HugepageStatsTestGuard::new();
        let req = 2 * 1024 * 1024;
        let prot = prots::get_prot(true, true, false);
        unsafe {
            let result = mmap_huge_with_backing(req, prot);
            let ptr = result.ptr;
            assert!(!mmap_failed(ptr));
            assert!(result.backing.is_hugepage() || result.backing.is_fallback());
            ptr.write(0xA5);
            ptr.add(req - 1).write(0x5A);

            let stats = hugepage_mmap_stats_snapshot();
            assert_eq!(stats.attempts, 1);
            assert_eq!(stats.successes + stats.fallbacks, 1);
            assert!(stats.aligned_fallbacks <= stats.fallbacks);
            assert!(stats.advised_fallbacks <= stats.fallbacks);
            assert_eq!(stats.fallback_failures, 0);
            assert!(stats.hugetlb_mmap_syscalls <= stats.attempts);
            assert!(stats.hugetlb_mmap_skips <= stats.attempts);
            if stats.successes == 1 {
                assert_eq!(result.backing, HugePageMmapBacking::HugePage);
                assert_eq!(ptr as usize % req, 0);
            } else {
                assert!(result.backing.is_fallback());
                if result.backing.is_aligned_fallback() {
                    assert_eq!(ptr as usize % hugepage_fallback_alignment(), 0);
                    assert_eq!(stats.aligned_fallbacks, 1);
                }
            }
            #[cfg(all(unix, not(any(target_os = "linux", target_os = "macos"))))]
            assert_eq!(stats.fallbacks, 1);

            munmap(ptr, req);
        }
    }

    #[cfg(unix)]
    #[test]
    fn mmap_huge_fallback_prefers_hugepage_aligned_mapping() {
        let _guard = HugepageStatsTestGuard::new();
        let req = hugepage_fallback_alignment();
        assert!(hugepage_aligned_fallback_supported(req));
        let prot = prots::get_prot(true, true, false);
        unsafe {
            let result = mmap_huge_fallback(req, prot);
            assert!(!mmap_failed(result.ptr));
            assert!(
                result.backing.is_aligned_fallback(),
                "fallback backing should preserve hugepage alignment when possible: {:?}",
                result.backing
            );
            assert_eq!(result.ptr as usize % hugepage_fallback_alignment(), 0);

            let stats = hugepage_mmap_stats_snapshot();
            assert_eq!(stats.attempts, 0);
            assert_eq!(stats.successes, 0);
            assert_eq!(stats.fallbacks, 1);
            assert_eq!(stats.aligned_fallbacks, 1);
            assert_eq!(stats.fallback_failures, 0);
            assert_eq!(stats.hugetlb_mmap_syscalls, 0);
            assert_eq!(stats.hugetlb_mmap_skips, 0);

            munmap(result.ptr, req);
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn mmap_transparent_hugepage_is_aligned_without_claiming_actual_backing() {
        let req = hugepage_fallback_alignment();
        let prot = prots::get_prot(true, true, false);
        unsafe {
            let result = mmap_transparent_hugepage(req, prot);
            assert!(result.is_success());
            assert_eq!(result.ptr as usize % req, 0);
            result.ptr.write(0xA5);
            result.ptr.add(req - 1).write(0x5A);
            // `advice_succeeded` intentionally carries no actual-backing
            // interpretation; smaps or successful collapse supplies proof.
            munmap(result.ptr, req);
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn collapse_transparent_hugepage_rejects_invalid_ranges() {
        unsafe {
            assert_eq!(
                collapse_transparent_hugepage(core::ptr::null_mut(), hugepage_fallback_alignment()),
                Err(libc::EINVAL as isize)
            );
            assert_eq!(
                collapse_transparent_hugepage(1usize as *mut u8, hugepage_fallback_alignment()),
                Err(libc::EINVAL as isize)
            );
        }
    }

    #[test]
    fn hugepage_platform_error_code_name_labels_common_failures() {
        assert_eq!(
            hugepage_mmap_platform_error_code_name(
                HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported,
                4,
            ),
            "KERN_INVALID_ARGUMENT"
        );
        assert_eq!(
            hugepage_mmap_platform_error_code_name(
                HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported,
                46,
            ),
            "KERN_NOT_SUPPORTED"
        );
        assert_eq!(
            hugepage_mmap_platform_error_code_name(
                HugePageMmapPlatformErrorStage::LinuxHugetlbMmap,
                12,
            ),
            "ENOMEM"
        );
        assert_eq!(
            hugepage_mmap_platform_error_code_name(
                HugePageMmapPlatformErrorStage::WindowsLargePageAlloc,
                1314,
            ),
            "ERROR_PRIVILEGE_NOT_HELD"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_hugetlb_support_cache_only_skips_stable_unsupported_errors() {
        assert!(linux_hugetlb_error_is_stable_unsupported(1));
        assert!(linux_hugetlb_error_is_stable_unsupported(22));
        assert!(linux_hugetlb_error_is_stable_unsupported(38));
        assert!(
            !linux_hugetlb_error_is_stable_unsupported(12),
            "ENOMEM can be transient when a hugetlb pool is later provisioned"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn mmap_huge_linux_skips_cached_stable_hugetlb_unsupported() {
        let _guard = HugepageStatsTestGuard::new();
        record_linux_hugetlb_unsupported(22);
        let req = hugepage_fallback_alignment();
        let prot = prots::get_prot(true, true, false);

        unsafe {
            let result = mmap_huge_with_backing(req, prot);
            assert!(
                !mmap_failed(result.ptr),
                "cached hugetlb-unavailable path should still produce a usable fallback"
            );
            assert!(
                result.backing.is_aligned_fallback(),
                "the skip path must keep the same aligned THP-advised fallback semantics"
            );
            assert_eq!(result.ptr as usize % hugepage_fallback_alignment(), 0);

            let stats = hugepage_mmap_stats_snapshot();
            assert_eq!(stats.attempts, 1);
            assert_eq!(stats.hugetlb_mmap_syscalls, 0);
            assert_eq!(stats.hugetlb_mmap_skips, 1);
            assert_eq!(stats.successes, 0);
            assert_eq!(stats.fallbacks, 1);
            assert_eq!(stats.aligned_fallbacks, 1);
            assert_eq!(stats.fallback_failures, 0);

            let status = hugepage_mmap_platform_status_snapshot();
            assert_eq!(
                status.last_error_stage,
                HugePageMmapPlatformErrorStage::LinuxHugetlbMmap
            );
            assert_eq!(status.last_error_code, 22);
            assert_eq!(status.last_error_code_name(), "EINVAL");

            munmap(result.ptr, req);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mmap_huge_macos_classifies_invalid_superpage_flags_as_unsupported() {
        let _guard = HugepageStatsTestGuard::new();
        let req = 2 * 1024 * 1024;
        let prot = prots::get_prot(true, true, false);
        unsafe {
            let result = mmap_huge_with_backing(req, prot);
            let status = hugepage_mmap_platform_status_snapshot();
            if result.backing.is_hugepage() {
                assert_eq!(
                    status.last_error_stage,
                    HugePageMmapPlatformErrorStage::None
                );
            } else if status.last_error_code == KERN_INVALID_ARGUMENT as isize
                || status.last_error_code == KERN_NOT_SUPPORTED as isize
            {
                assert_eq!(
                    status.last_error_stage,
                    HugePageMmapPlatformErrorStage::MacosSuperpageUnsupported
                );
                assert!(status.last_error_code_name().starts_with("KERN_"));
            }
            if !mmap_failed(result.ptr) {
                munmap(result.ptr, req);
            }
        }
    }
}
