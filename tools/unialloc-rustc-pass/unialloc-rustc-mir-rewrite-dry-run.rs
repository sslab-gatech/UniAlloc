#![feature(rustc_private)]

//! rustc_driver optimized-MIR provider override for UniAlloc C002 lowering work.
//!
//! This tool deliberately does **not** claim production MIR lowering yet. It
//! installs a real `optimized_mir` query override, asks rustc for the original
//! body, clones the body, and can either audit or actually rewrite supported MIR
//! calls before returning the cloned body to rustc. Two opt-in actual modes exist
//! today:
//!
//! - direct allocator calls can be retargeted to
//!   `__unialloc_alloc_with_metadata(size, align, type_id, module_id, flags,
//!   callsite)` or the metadata-complete `_hints` companion;
//! - semantic allocation calls such as `Vec::push`/`BTreeMap::insert` can be
//!   wrapped by inserted MIR calls to the fast `__unialloc_semantic_scope_push/pop`
//!   ABI, with optional lifetime/placement hints.
//!
//! The JSON remains a conservative compiler-owned evidence artifact: actual
//! modes are explicit flags, and every artifact keeps `claim_grade=false` until
//! runtime/full-surface validation exists.

#[cfg(unialloc_rustc_current)]
extern crate rustc_abi;
extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;
extern crate rustc_span;

#[cfg(unialloc_rustc_current)]
use rustc_abi::Size;
#[cfg(unialloc_rustc_current)]
use rustc_driver::{run_compiler, Callbacks, Compilation};
#[cfg(not(unialloc_rustc_current))]
use rustc_driver::{Callbacks, Compilation, RunCompiler};
use rustc_hir::def::Res;
use rustc_interface::interface;
#[cfg(unialloc_rustc_current)]
use rustc_middle::mir::interpret::Scalar;
use rustc_middle::mir::visit::{MutatingUseContext, PlaceContext, Visitor};
use rustc_middle::mir::{
    BasicBlock, BasicBlockData, Body, LocalDecl, Location, Operand, Place, Rvalue, SourceInfo,
    StatementKind, Terminator, TerminatorKind,
};
#[cfg(unialloc_rustc_current)]
use rustc_middle::mir::{CallSource, UnwindAction};
#[cfg(not(unialloc_rustc_current))]
use rustc_middle::mir::{Constant, ConstantKind};
#[cfg(not(unialloc_rustc_current))]
use rustc_middle::ty::query::Providers;
#[cfg(not(unialloc_rustc_current))]
use rustc_middle::ty::TypeFoldable;
#[cfg(unialloc_rustc_current)]
use rustc_middle::ty::TypeVisitableExt;
use rustc_middle::ty::{self, Ty, TyCtxt};
#[cfg(unialloc_rustc_current)]
use rustc_middle::util::Providers;
#[cfg(not(unialloc_rustc_current))]
use rustc_span::def_id::DefId;
#[cfg(unialloc_rustc_current)]
use rustc_span::def_id::{DefId, LocalDefId};
use rustc_span::Span;
#[cfg(unialloc_rustc_current)]
use rustc_span::Spanned;

use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fmt::Write as FmtWrite;
use std::fs;
use std::path::PathBuf;
use std::process::{self, Command};
use std::sync::Mutex;
#[cfg(unix)]
use std::{os::unix::fs::PermissionsExt, os::unix::process::CommandExt};

const PASS_NAME: &str = "unialloc-rustc-driver-mir-rewrite-dry-run";
const LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;
const DEFAULT_LOWERING_POLICY_FLAGS: u32 = 0x1;
const FNV1A64_OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const TYPE_ID_ALGORITHM: &str =
    "direct: nonzero(fnv1a64(mir-rewrite-dry-run-v1 NUL callsite-key)) for unsolved alloc/alloc_zeroed calls; unsolved realloc/dealloc calls use an exact neutral type_id=0 recovery-delegated tuple and the conservative recovery-backed ABI; solved calls use nonzero(fnv1a64(mir-heap-object-type-v1 NUL solved heap object type)) when MIR destination/argument, ShallowInitBox, Layout constructor/raw-pointer constructor provenance, size_of/align_of typed Layout reconstruction, Layout transformer provenance, projection-aware/packed composite Layout provenance, Result<Layout>::ok plus Option<Layout>::expect/unwrap passthrough provenance, same-source Layout size/align reconstruction, or canonicalized MIR place/ref/tuple projection provenance solves a heap object; semantic-scope/drop: nonzero(fnv1a64(mir-heap-object-type-v1 NUL solved rustc_middle heap object type)), so compiler-emitted allocation and Drop/deallocation metadata agree on allocator-visible type identity; receiver-mutating allocation/deallocation and explicit-drop semantic scopes solve receiver/argument heap-owner types before return types, including generic owned-buffer push::<...> calls such as PathBuf::push and OsString::push; constructor/factory scopes solve destination types first; unsolved non-generic heap-object candidates are audited but not lowered as semantic scopes; generic Drop<T> cleanup in generic MIR is classified separately and skipped until monomorphized type evidence exists";
const UNKNOWN_HEAP_OBJECT_TYPE: &str = "<unknown-heap-object-type>";
const PLACEMENT_HINT_CROSS_THREAD_RECOVERY: u16 = 1 << 15;

#[cfg(unialloc_rustc_current)]
type OptimizedMirDefId = LocalDefId;
#[cfg(not(unialloc_rustc_current))]
type OptimizedMirDefId = DefId;

static mut ORIGINAL_OPTIMIZED_MIR: Option<
    for<'tcx> fn(TyCtxt<'tcx>, OptimizedMirDefId) -> &'tcx Body<'tcx>,
> = None;
static mut RECORDS: Option<Mutex<Vec<RewriteRecord>>> = None;
static mut ACTUAL_MIR_REWRITE: bool = false;
static mut ACTUAL_SEMANTIC_SCOPE_REWRITE: bool = false;
static mut LOWERING_POLICY_FLAGS: u32 = DEFAULT_LOWERING_POLICY_FLAGS;
static mut LOWERING_LIFETIME_HINT: u16 = 0;
static mut LOWERING_PLACEMENT_HINT: u16 = 0;
static mut AUTO_CROSS_THREAD_RECOVERY_HINT: bool = false;
static mut DIRECT_LOCAL_METADATA_ABI: bool = false;
static mut DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP: bool = false;
static mut CONTINUE_COMPILATION: bool = false;

// rustc's private APIs drift quickly.  Keep version-specific syntax at this
// boundary so the MIR solver/rewrite logic below stays readable and auditable.
macro_rules! body_basic_blocks {
    ($body:expr) => {{
        #[cfg(unialloc_rustc_current)]
        {
            &$body.basic_blocks
        }
        #[cfg(not(unialloc_rustc_current))]
        {
            $body.basic_blocks()
        }
    }};
}

#[cfg(unialloc_rustc_current)]
type MirCallSource = CallSource;
#[cfg(not(unialloc_rustc_current))]
type MirCallSource = bool;

#[cfg(unialloc_rustc_current)]
type MirUnwind = UnwindAction;
#[cfg(not(unialloc_rustc_current))]
type MirUnwind = Option<BasicBlock>;

#[derive(Debug)]
struct Cli {
    audit_out: PathBuf,
    pass_log_out: Option<PathBuf>,
    actual_rewrite: bool,
    semantic_scope_rewrite: bool,
    policy_flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    auto_cross_thread_recovery_hint: bool,
    direct_local_metadata_abi: bool,
    direct_local_size_align_with_semantic_drop: bool,
    continue_compilation: bool,
    rustc_args: Vec<String>,
}

enum ParsedInvocation {
    Bypass(Vec<String>),
    RunPass(Cli),
}

#[derive(Clone, Debug)]
struct RewriteRecord {
    allocation_site_id: String,
    type_id: u64,
    module_id: u64,
    flags: u32,
    lifetime_hint: u16,
    placement_hint: u16,
    cross_thread_recovery_hint: bool,
    placement_hint_basis: &'static str,
    callsite: u64,
    mir_function: String,
    basic_block: String,
    source_span: String,
    callee: String,
    destination_place: String,
    destination_type: String,
    call_arguments: Vec<String>,
    argument_types: Vec<String>,
    semantic_object_type: String,
    type_id_basis: &'static str,
    size_operand: Option<String>,
    align_operand: Option<String>,
    rewrite_status: &'static str,
    replacement_symbol: &'static str,
    replacement_resolution_status: &'static str,
    replacement_preview: String,
    semantic_scope_unwind_pop_inserted: bool,
    metadata_pairing_contract: &'static str,
    lowering_kind: &'static str,
}

#[derive(Clone, Copy, Debug)]
struct AllocMetadataAbi {
    def_id: DefId,
    symbol: &'static str,
    supports_hints: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DirectAllocatorCallKind {
    SizeAlignAlloc,
    LayoutAlloc,
    LayoutAllocZeroed,
    LayoutRealloc,
    LayoutDealloc,
    GlobalAllocLayoutAlloc,
    GlobalAllocLayoutAllocZeroed,
    GlobalAllocLayoutRealloc,
    GlobalAllocLayoutDealloc,
    Unsupported,
}

#[derive(Clone, Copy, Debug)]
struct DirectAllocatorReplacementAbis {
    size_align_alloc: Option<AllocMetadataAbi>,
    conservative_size_align_alloc: Option<AllocMetadataAbi>,
    layout_alloc: Option<AllocMetadataAbi>,
    layout_alloc_zeroed: Option<AllocMetadataAbi>,
    layout_realloc: Option<AllocMetadataAbi>,
    layout_dealloc: Option<AllocMetadataAbi>,
    recovery_backed_layout_realloc: Option<AllocMetadataAbi>,
    recovery_backed_layout_dealloc: Option<AllocMetadataAbi>,
}

#[derive(Clone, Debug)]
struct DirectAllocatorCandidate {
    bb: BasicBlock,
    call_kind: DirectAllocatorCallKind,
    callee: String,
    call_arguments: Vec<String>,
    argument_types: Vec<String>,
    destination_place: String,
    destination_type: String,
    semantic_object_type: String,
    type_id_basis: &'static str,
    source_span: String,
    fn_span: Span,
}

#[derive(Clone, Debug)]
struct HeapObjectSolution {
    object_type: String,
    type_id_basis: &'static str,
}

#[derive(Default)]
struct DirectLocalSizeAlignPairingCounts {
    local_size_align_alloc_count: usize,
    semantic_drop_scope_count: usize,
    allocation_mir_functions: BTreeSet<String>,
    semantic_drop_mir_functions: BTreeSet<String>,
}

#[derive(Clone, Copy, Debug)]
struct SemanticScopeAbi {
    push_def_id: DefId,
    pop_def_id: DefId,
    push_symbol: &'static str,
    supports_hints: bool,
    local_no_recovery: bool,
}

fn semantic_scope_resolution_status_for(
    supports_hints: bool,
    local_no_recovery: bool,
) -> &'static str {
    match (supports_hints, local_no_recovery) {
        (true, true) => "resolved_unialloc_semantic_scope_push_hints_local_pop",
        (false, true) => "resolved_unialloc_semantic_scope_push_local_pop",
        (true, false) => "resolved_unialloc_semantic_scope_push_hints_pop",
        (false, false) => "resolved_unialloc_semantic_scope_push_pop",
    }
}

fn semantic_scope_resolution_status(scope_abi: SemanticScopeAbi) -> &'static str {
    semantic_scope_resolution_status_for(scope_abi.supports_hints, scope_abi.local_no_recovery)
}

fn semantic_scope_push_symbol(local_no_recovery: bool) -> &'static str {
    match (lowering_metadata_hints_requested(), local_no_recovery) {
        (true, true) => "__unialloc_semantic_scope_push_hints_local",
        (false, true) => "__unialloc_semantic_scope_push_local",
        (true, false) => "__unialloc_semantic_scope_push_hints",
        (false, false) => "__unialloc_semantic_scope_push",
    }
}

fn semantic_scope_unresolved_status(local_no_recovery: bool) -> &'static str {
    match (lowering_metadata_hints_requested(), local_no_recovery) {
        (true, true) => "unialloc_semantic_scope_push_hints_local_pop_not_resolved",
        (false, true) => "unialloc_semantic_scope_push_local_pop_not_resolved",
        (true, false) => "unialloc_semantic_scope_push_hints_pop_not_resolved",
        (false, false) => "unialloc_semantic_scope_push_pop_not_resolved",
    }
}

fn direct_local_size_align_pairing_key(record: &RewriteRecord) -> Option<(u64, String)> {
    if record.semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        return None;
    }
    Some((record.type_id, record.semantic_object_type.clone()))
}

fn is_applied_direct_local_size_align_alloc(record: &RewriteRecord) -> bool {
    record.lowering_kind == "direct_allocator_call_rewrite"
        && record.rewrite_status == "actual_allocator_call_replacement_applied"
        && direct_allocator_call_kind(&record.callee) == DirectAllocatorCallKind::SizeAlignAlloc
        && record.replacement_symbol.ends_with("_local")
        && record.metadata_pairing_contract == "local_metadata_abi_semantic_drop_scope"
}

fn is_applied_semantic_drop_scope(record: &RewriteRecord) -> bool {
    record.lowering_kind == "semantic_scope_drop_rewrite"
        && record.rewrite_status == "actual_semantic_scope_drop_rewrite_applied"
        && record.metadata_pairing_contract == "semantic_scope_drop_active_metadata"
}

fn compute_direct_local_size_align_pairing_counts(
    records: &[RewriteRecord],
) -> BTreeMap<(u64, String), DirectLocalSizeAlignPairingCounts> {
    let mut counts: BTreeMap<(u64, String), DirectLocalSizeAlignPairingCounts> = BTreeMap::new();
    for record in records {
        if is_applied_direct_local_size_align_alloc(record) {
            if let Some(key) = direct_local_size_align_pairing_key(record) {
                let entry = counts.entry(key).or_default();
                entry.local_size_align_alloc_count += 1;
                entry
                    .allocation_mir_functions
                    .insert(record.mir_function.clone());
            }
        } else if is_applied_semantic_drop_scope(record) {
            if let Some(key) = direct_local_size_align_pairing_key(record) {
                let entry = counts.entry(key).or_default();
                entry.semantic_drop_scope_count += 1;
                entry
                    .semantic_drop_mir_functions
                    .insert(record.mir_function.clone());
            }
        }
    }
    counts
}

fn compute_direct_local_size_align_pairing_gap_count(records: &[RewriteRecord]) -> usize {
    let counts = compute_direct_local_size_align_pairing_counts(records);
    counts
        .values()
        .filter(|count| {
            count.local_size_align_alloc_count > 0
                && count.semantic_drop_scope_count < count.local_size_align_alloc_count
        })
        .count()
}

#[derive(Clone, Debug)]
enum PlainCloneHeapClass {
    NotPlainClone,
    Single(String),
    DefiniteNoSupportedOwner,
    Ambiguous(Vec<String>),
    Unresolved,
}

#[derive(Clone, Debug)]
struct SemanticScopeCandidate<'tcx> {
    bb: BasicBlock,
    original_is_cleanup: bool,
    callee: String,
    call_arguments: Vec<String>,
    argument_types: Vec<String>,
    destination: Place<'tcx>,
    destination_place: String,
    destination_type: String,
    semantic_object_type: String,
    plain_clone_heap_class: PlainCloneHeapClass,
    original_target: Option<BasicBlock>,
    original_unwind: MirUnwind,
    original_from_hir_call: MirCallSource,
    fn_span: Span,
    original_terminator: Terminator<'tcx>,
}

#[derive(Clone, Debug)]
struct SemanticDropCandidate<'tcx> {
    bb: BasicBlock,
    original_is_cleanup: bool,
    drop_place: String,
    drop_type: String,
    semantic_object_type: String,
    drop_type_has_generic_param: bool,
    drop_type_has_multiple_heap_owners: bool,
    original_target: BasicBlock,
    original_unwind: MirUnwind,
    fn_span: Span,
    original_terminator: Terminator<'tcx>,
}

#[derive(Default)]
struct RewriteDryRunCallbacks;

fn usage() -> &'static str {
    "Usage:\n  RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs -o /tmp/unialloc-rustc-mir-rewrite-dry-run\n  DYLD_LIBRARY_PATH=$(rustc +$(cat rust-toolchain) --print sysroot)/lib /tmp/unialloc-rustc-mir-rewrite-dry-run \\\n    --unialloc-rewrite-audit-out /tmp/rewrite-audit.json -- --sysroot $(rustc +$(cat rust-toolchain) --print sysroot) --edition=2021 input.rs\n\nOptions:\n  --unialloc-rewrite-audit-out <path>  JSON rewrite audit output. Defaults to env UNIALLOC_REWRITE_AUDIT_OUT or ./unialloc-rustc-mir-rewrite-dry-run.json\n  --unialloc-rewrite-audit-dir <dir>   Wrapper-friendly output directory. Defaults to env UNIALLOC_REWRITE_AUDIT_DIR if set\n  --unialloc-pass-log-out <path>       Optional pass log output. Defaults to env UNIALLOC_PASS_LOG_OUT if set\n  --unialloc-pass-log-dir <dir>        Wrapper-friendly log directory. Defaults to env UNIALLOC_PASS_LOG_DIR if set\n  --unialloc-target-crates <names>     Optional comma-separated Cargo crate allowlist. Defaults to env UNIALLOC_RUSTC_TARGET_CRATES. Hyphens and underscores compare equivalently\n  --unialloc-actual-mir-rewrite        Opt in to replacing supported direct allocator-call terminators. Defaults to env UNIALLOC_ACTUAL_MIR_REWRITE truthiness\n  --unialloc-actual-semantic-scope-rewrite  Opt in to inserting __unialloc_semantic_scope_push/pop around supported semantic allocation calls. Defaults to env UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE truthiness\n  --unialloc-auto-cross-thread-recovery-hint  OR the cross-thread recovery placement bit into solved heap-object scopes when the MIR body contains thread-spawn/escape calls. Defaults to env UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT truthiness\n  --unialloc-direct-local-metadata-abi  Use no-recovery UniAlloc Layout alloc/alloc_zeroed/realloc/dealloc ABI variants only when paired metadata deallocation/reallocation is explicit; unpaired size/align exchange_malloc keeps recovery ABI. Defaults to env UNIALLOC_DIRECT_LOCAL_METADATA_ABI truthiness\n  --unialloc-direct-local-size-align-with-semantic-drop  Enable no-recovery semantic scopes only for destination-proven linear Drop ownership; raw size/align calls without an exact owner link remain recovery-backed. Defaults to env UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP truthiness\n  --unialloc-policy-flags <u32>        Policy flags inserted into UniAlloc metadata. Defaults to env UNIALLOC_LOWERING_POLICY_FLAGS or 0x1 (TYPE_ISOLATED only)\n  --unialloc-lifetime-hint <u16>       Optional metadata lifetime hint. Defaults to env UNIALLOC_LOWERING_LIFETIME_HINT or 0\n  --unialloc-placement-hint <u16>      Optional metadata placement hint. Defaults to env UNIALLOC_LOWERING_PLACEMENT_HINT or 0\n  --unialloc-continue-compilation      Continue after analysis and produce normal rustc outputs. Actual rewrite modes continue by default. Defaults to env UNIALLOC_CONTINUE_COMPILATION truthiness\n  --unialloc-stop-after-analysis       Force analysis-only output even if an actual rewrite flag is set\n  --unialloc-dry-run-only              Force audit-only mode even if actual rewrite env flags are set\n  --unialloc-help                      Print this help\n\nEverything after `--` is passed to rustc_driver as a direct invocation. Without `--`, an executable path or a bare command resolved through PATH/PATHEXT is treated as Cargo's compiler argv0. Executable source files therefore require explicit `--` for direct compilation.\n"
}

fn env_truthy(name: &str) -> bool {
    env::var(name)
        .ok()
        .map(|value| {
            matches!(
                value.as_str(),
                "1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON"
            )
        })
        .unwrap_or(false)
}

fn parse_u32(value: &str) -> Result<u32, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err("empty u32 value".to_string());
    }
    if let Some(hex) = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
    {
        u32::from_str_radix(hex, 16).map_err(|err| format!("invalid hex u32 `{}`: {}", value, err))
    } else {
        trimmed
            .parse::<u32>()
            .map_err(|err| format!("invalid u32 `{}`: {}", value, err))
    }
}

fn parse_u16(value: &str) -> Result<u16, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err("empty u16 value".to_string());
    }
    if let Some(hex) = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
    {
        u16::from_str_radix(hex, 16).map_err(|err| format!("invalid hex u16 `{}`: {}", value, err))
    } else {
        trimmed
            .parse::<u16>()
            .map_err(|err| format!("invalid u16 `{}`: {}", value, err))
    }
}

#[inline]
fn lowering_policy_flags() -> u32 {
    unsafe { LOWERING_POLICY_FLAGS }
}

#[inline]
fn lowering_lifetime_hint() -> u16 {
    unsafe { LOWERING_LIFETIME_HINT }
}

#[inline]
fn lowering_placement_hint() -> u16 {
    unsafe { LOWERING_PLACEMENT_HINT }
}

#[inline]
fn auto_cross_thread_recovery_hint_enabled() -> bool {
    unsafe { AUTO_CROSS_THREAD_RECOVERY_HINT }
}

#[inline]
fn direct_local_metadata_abi_requested() -> bool {
    unsafe { DIRECT_LOCAL_METADATA_ABI }
}

#[inline]
fn direct_local_size_align_with_semantic_drop_requested() -> bool {
    unsafe { DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP }
}

#[inline]
fn lowering_metadata_hints_requested() -> bool {
    lowering_lifetime_hint() != 0
        || lowering_placement_hint() != 0
        || auto_cross_thread_recovery_hint_enabled()
}

#[inline]
fn lowering_placement_hint_for_body(cross_thread_escape: bool) -> (u16, bool, &'static str) {
    let base = lowering_placement_hint();
    let auto_applies = auto_cross_thread_recovery_hint_enabled() && cross_thread_escape;
    let placement_hint = if auto_applies {
        base | PLACEMENT_HINT_CROSS_THREAD_RECOVERY
    } else {
        base
    };
    let manual_cross_thread = (base & PLACEMENT_HINT_CROSS_THREAD_RECOVERY) != 0;
    let has_cross_thread_recovery = (placement_hint & PLACEMENT_HINT_CROSS_THREAD_RECOVERY) != 0;
    let basis = match (manual_cross_thread, auto_applies, base != 0) {
        (true, true, _) => "manual_and_auto_cross_thread_escape",
        (false, true, _) => "auto_cross_thread_escape",
        (true, false, _) => "manual_cross_thread_recovery_hint",
        (false, false, true) => "manual_placement_hint",
        (false, false, false) => "default",
    };
    (placement_hint, has_cross_thread_recovery, basis)
}

fn looks_like_rustc_argv0(value: &str) -> bool {
    let path = PathBuf::from(value);
    path.file_name()
        .and_then(|name| name.to_str())
        .map(|name| {
            let name = name
                .strip_suffix(".exe")
                .or_else(|| name.strip_suffix(".EXE"))
                .unwrap_or(name);
            name == "rustc" || name.starts_with("rustc-")
        })
        .unwrap_or(false)
}

#[cfg(unix)]
fn command_path_resolves(path: &PathBuf) -> bool {
    let metadata = match path.metadata() {
        Ok(metadata) if metadata.is_file() => metadata,
        _ => return false,
    };
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(windows)]
fn command_path_resolves(path: &PathBuf) -> bool {
    // Match `std::process::Command` rather than shell/PATHEXT lookup.  Command
    // accepts an explicitly named executable and permits only the `.exe`
    // suffix to be omitted.  Treating `.cmd`/`.bat` as implicit candidates
    // would classify an argv0 that the later `Command::new(argv0)` bypass
    // cannot execute under the same spelling.
    if path.is_file() {
        return true;
    }
    if path.extension().is_some() {
        return false;
    }
    let mut executable = path.as_os_str().to_os_string();
    executable.push(".exe");
    PathBuf::from(executable).is_file()
}

#[cfg(not(any(unix, windows)))]
fn command_path_resolves(path: &PathBuf) -> bool {
    path.is_file()
}

fn command_has_path_component(value: &str) -> bool {
    let path = PathBuf::from(value);
    path.is_absolute()
        || path
            .parent()
            .map(|parent| !parent.as_os_str().is_empty())
            .unwrap_or(false)
}

fn bare_command_resolves_in_path(value: &str) -> bool {
    let path = match env::var_os("PATH") {
        Some(path) => path,
        None => return false,
    };
    env::split_paths(&path).any(|directory| command_path_resolves(&directory.join(value)))
}

fn looks_like_compiler_argv0(value: &str) -> bool {
    if looks_like_rustc_argv0(value) {
        return true;
    }
    if command_has_path_component(value) {
        command_path_resolves(&PathBuf::from(value))
    } else {
        bare_command_resolves_in_path(value)
    }
}

fn derive_sysroot_from_rustc_argv0(argv0: &str) -> Option<String> {
    let path = PathBuf::from(argv0);
    let file_name = path.file_name()?.to_str()?;
    let executable_name = file_name
        .strip_suffix(".exe")
        .or_else(|| file_name.strip_suffix(".EXE"))
        .unwrap_or(file_name);
    if executable_name != "rustc" {
        return None;
    }
    let bin = path.parent()?;
    if bin.file_name().and_then(|name| name.to_str()) != Some("bin") {
        return None;
    }
    Some(bin.parent()?.to_string_lossy().into_owned())
}

fn inject_sysroot_if_missing(args: &mut Vec<String>) {
    if args
        .iter()
        .any(|arg| arg == "--sysroot" || arg.starts_with("--sysroot="))
    {
        return;
    }
    let sysroot = env::var("UNIALLOC_RUSTC_SYSROOT")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| derive_sysroot_from_rustc_argv0(args.get(0).map(String::as_str).unwrap_or("")));
    if let Some(sysroot) = sysroot {
        args.insert(1, sysroot);
        args.insert(1, "--sysroot".to_string());
    }
}

fn sanitize_filename(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' || ch == '.' {
            out.push(ch);
        } else {
            out.push('_');
        }
    }
    if out.is_empty() {
        "rustc-input".to_string()
    } else {
        out
    }
}

fn rustc_crate_name(args: &[String]) -> Option<&str> {
    let mut idx = 0;
    while idx < args.len() {
        if args[idx] == "--crate-name" {
            if let Some(name) = args.get(idx + 1) {
                if !name.starts_with('-') {
                    return Some(name);
                }
            }
        } else if let Some(name) = args[idx].strip_prefix("--crate-name=") {
            if !name.is_empty() {
                return Some(name);
            }
        }
        idx += 1;
    }
    None
}

fn crate_name_from_rustc_args(args: &[String]) -> String {
    rustc_crate_name(args)
        .map(sanitize_filename)
        .unwrap_or_else(|| "rustc-input".to_string())
}

fn normalized_target_crate_name(name: &str) -> String {
    name.trim().replace('-', "_")
}

fn parse_target_crates(value: &str) -> BTreeSet<String> {
    value
        .split(',')
        .map(normalized_target_crate_name)
        .filter(|name| !name.is_empty())
        .collect()
}

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = FNV1A64_OFFSET_BASIS;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3u64);
    }
    hash
}

fn fnv1a64_text(text: &str) -> u64 {
    fnv1a64(text.as_bytes())
}

#[inline]
fn nonzero_fnv1a64(hash: u64) -> u64 {
    if hash == 0 {
        FNV1A64_OFFSET_BASIS
    } else {
        hash
    }
}

fn nonzero_fnv1a64_text(text: &str) -> u64 {
    nonzero_fnv1a64(fnv1a64_text(text))
}

fn derived_output_path(dir: &PathBuf, args: &[String], extension: &str) -> PathBuf {
    let joined = args.join("\0");
    let crate_name = crate_name_from_rustc_args(args);
    dir.join(format!(
        "{}-{:016x}.{}",
        crate_name,
        fnv1a64_text(&joined),
        extension
    ))
}

fn parse_cli() -> Result<ParsedInvocation, String> {
    let raw: Vec<String> = env::args().collect();
    let mut audit_out = env::var_os("UNIALLOC_REWRITE_AUDIT_OUT").map(PathBuf::from);
    let mut pass_log_out = env::var_os("UNIALLOC_PASS_LOG_OUT").map(PathBuf::from);
    let mut audit_dir = env::var_os("UNIALLOC_REWRITE_AUDIT_DIR").map(PathBuf::from);
    let mut pass_log_dir = env::var_os("UNIALLOC_PASS_LOG_DIR").map(PathBuf::from);
    let mut actual_rewrite = env_truthy("UNIALLOC_ACTUAL_MIR_REWRITE");
    let mut semantic_scope_rewrite = env_truthy("UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE");
    let mut policy_flags = None;
    let mut lifetime_hint = None;
    let mut placement_hint = None;
    let mut auto_cross_thread_recovery_hint =
        env_truthy("UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT");
    let mut direct_local_metadata_abi = env_truthy("UNIALLOC_DIRECT_LOCAL_METADATA_ABI");
    let mut direct_local_size_align_with_semantic_drop =
        env_truthy("UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP");
    let mut target_crates = env::var("UNIALLOC_RUSTC_TARGET_CRATES")
        .ok()
        .map(|value| parse_target_crates(&value))
        .unwrap_or_default();
    let mut continue_compilation = env_truthy("UNIALLOC_CONTINUE_COMPILATION");
    let mut force_stop_after_analysis = false;
    let mut passthrough: Vec<String> = Vec::new();
    let mut saw_dashdash = false;

    let mut i = 1;
    while i < raw.len() {
        match raw[i].as_str() {
            "--unialloc-help" | "-h" | "--help" if passthrough.is_empty() => {
                println!("{}", usage());
                process::exit(0);
            }
            "--unialloc-rewrite-audit-out" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-rewrite-audit-out requires a path".to_string());
                }
                audit_out = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-rewrite-audit-dir" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-rewrite-audit-dir requires a directory".to_string());
                }
                audit_dir = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-pass-log-out" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-pass-log-out requires a path".to_string());
                }
                pass_log_out = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-pass-log-dir" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-pass-log-dir requires a directory".to_string());
                }
                pass_log_dir = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-target-crates" => {
                i += 1;
                if i >= raw.len() {
                    return Err(
                        "--unialloc-target-crates requires a comma-separated value".to_string()
                    );
                }
                target_crates = parse_target_crates(&raw[i]);
            }
            value if value.starts_with("--unialloc-target-crates=") => {
                target_crates = parse_target_crates(
                    value
                        .strip_prefix("--unialloc-target-crates=")
                        .unwrap_or_default(),
                );
            }
            "--unialloc-actual-mir-rewrite" => {
                actual_rewrite = true;
            }
            "--unialloc-actual-semantic-scope-rewrite" => {
                semantic_scope_rewrite = true;
            }
            "--unialloc-policy-flags" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-policy-flags requires a u32 value".to_string());
                }
                policy_flags = Some(parse_u32(&raw[i])?);
            }
            "--unialloc-lifetime-hint" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-lifetime-hint requires a u16 value".to_string());
                }
                lifetime_hint = Some(parse_u16(&raw[i])?);
            }
            "--unialloc-placement-hint" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-placement-hint requires a u16 value".to_string());
                }
                placement_hint = Some(parse_u16(&raw[i])?);
            }
            "--unialloc-auto-cross-thread-recovery-hint" => {
                auto_cross_thread_recovery_hint = true;
            }
            "--unialloc-direct-local-metadata-abi" => {
                direct_local_metadata_abi = true;
            }
            "--unialloc-direct-local-size-align-with-semantic-drop" => {
                direct_local_size_align_with_semantic_drop = true;
                direct_local_metadata_abi = true;
                semantic_scope_rewrite = true;
            }
            "--unialloc-continue-compilation" => {
                continue_compilation = true;
                force_stop_after_analysis = false;
            }
            "--unialloc-stop-after-analysis" => {
                continue_compilation = false;
                force_stop_after_analysis = true;
            }
            "--unialloc-dry-run-only" => {
                actual_rewrite = false;
                semantic_scope_rewrite = false;
                direct_local_size_align_with_semantic_drop = false;
            }
            "--" => {
                saw_dashdash = true;
                passthrough.extend_from_slice(&raw[i + 1..]);
                break;
            }
            _ => {
                passthrough.extend_from_slice(&raw[i..]);
                break;
            }
        }
        i += 1;
    }

    if passthrough.is_empty() {
        return Err(format!("no rustc arguments supplied\n\n{}", usage()));
    }

    let mut original_rustc_args = Vec::new();
    if saw_dashdash || !looks_like_compiler_argv0(&passthrough[0]) {
        original_rustc_args.push("rustc".to_string());
    }
    original_rustc_args.extend(passthrough);
    if !target_crates.is_empty() {
        let selected = rustc_crate_name(&original_rustc_args)
            .map(normalized_target_crate_name)
            .map(|crate_name| target_crates.contains(&crate_name))
            .unwrap_or(false);
        if !selected {
            return Ok(ParsedInvocation::Bypass(original_rustc_args));
        }
    }
    let policy_flags = match policy_flags {
        Some(value) => value,
        None => match env::var("UNIALLOC_LOWERING_POLICY_FLAGS") {
            Ok(value) => parse_u32(&value)?,
            Err(_) => DEFAULT_LOWERING_POLICY_FLAGS,
        },
    };
    let lifetime_hint = match lifetime_hint {
        Some(value) => value,
        None => match env::var("UNIALLOC_LOWERING_LIFETIME_HINT") {
            Ok(value) => parse_u16(&value)?,
            Err(_) => 0,
        },
    };
    let placement_hint = match placement_hint {
        Some(value) => value,
        None => match env::var("UNIALLOC_LOWERING_PLACEMENT_HINT") {
            Ok(value) => parse_u16(&value)?,
            Err(_) => 0,
        },
    };
    let mut rustc_args = original_rustc_args.clone();
    inject_sysroot_if_missing(&mut rustc_args);
    if direct_local_size_align_with_semantic_drop {
        direct_local_metadata_abi = true;
        semantic_scope_rewrite = true;
    }
    if !force_stop_after_analysis && (actual_rewrite || semantic_scope_rewrite) {
        continue_compilation = true;
    }

    Ok(ParsedInvocation::RunPass(Cli {
        audit_out: audit_out
            .or_else(|| {
                audit_dir
                    .as_ref()
                    .map(|dir| derived_output_path(dir, &rustc_args, "json"))
            })
            .unwrap_or_else(|| PathBuf::from("unialloc-rustc-mir-rewrite-dry-run.json")),
        pass_log_out: pass_log_out.or_else(|| {
            pass_log_dir
                .as_ref()
                .map(|dir| derived_output_path(dir, &rustc_args, "log"))
        }),
        actual_rewrite,
        semantic_scope_rewrite,
        policy_flags,
        lifetime_hint,
        placement_hint,
        auto_cross_thread_recovery_hint,
        direct_local_metadata_abi,
        direct_local_size_align_with_semantic_drop,
        continue_compilation,
        rustc_args,
    }))
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
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ch if ch <= '\u{1f}' => {
                let _ = write!(out, "\\u{:04x}", ch as u32);
            }
            ch => out.push(ch),
        }
    }
    out
}

fn push_json_string_array(json: &mut String, values: &BTreeSet<String>) {
    json.push('[');
    for (idx, value) in values.iter().enumerate() {
        if idx > 0 {
            json.push_str(", ");
        }
        let _ = write!(json, "\"{}\"", json_escape(value));
    }
    json.push(']');
}

fn push_direct_local_size_align_pairing_details_json(
    json: &mut String,
    field_name: &str,
    counts: &BTreeMap<(u64, String), DirectLocalSizeAlignPairingCounts>,
    gaps_only: bool,
    trailing_comma: bool,
) {
    let _ = writeln!(json, "    \"{}\": [", field_name);
    let mut first = true;
    for ((type_id, object_type), count) in counts.iter() {
        if count.local_size_align_alloc_count == 0 {
            continue;
        }
        let is_gap = count.semantic_drop_scope_count < count.local_size_align_alloc_count;
        if gaps_only && !is_gap {
            continue;
        }
        if !first {
            json.push_str(",\n");
        }
        first = false;
        json.push_str("      {\n");
        let _ = writeln!(json, "        \"type_id\": {},", type_id);
        let _ = writeln!(
            json,
            "        \"semantic_object_type\": \"{}\",",
            json_escape(object_type)
        );
        let _ = writeln!(
            json,
            "        \"local_size_align_alloc_count\": {},",
            count.local_size_align_alloc_count
        );
        let _ = writeln!(
            json,
            "        \"semantic_drop_scope_count\": {},",
            count.semantic_drop_scope_count
        );
        json.push_str("        \"allocation_mir_functions\": ");
        push_json_string_array(json, &count.allocation_mir_functions);
        json.push_str(",\n");
        json.push_str("        \"semantic_drop_mir_functions\": ");
        push_json_string_array(json, &count.semantic_drop_mir_functions);
        json.push_str(",\n");
        let _ = writeln!(json, "        \"paired\": {}", !is_gap);
        json.push_str("      }");
    }
    json.push('\n');
    json.push_str("    ]");
    if trailing_comma {
        json.push(',');
    }
    json.push('\n');
}

fn callee_text(func: &Operand<'_>) -> String {
    match func {
        Operand::Constant(constant) => const_operand_text(constant),
        other => format!("{:?}", other),
    }
}

#[cfg(unialloc_rustc_current)]
fn const_operand_text(constant: &rustc_middle::mir::ConstOperand<'_>) -> String {
    format!("{:?}", constant.const_)
}

#[cfg(not(unialloc_rustc_current))]
fn const_operand_text(constant: &Constant<'_>) -> String {
    format!("{:?}", constant.literal)
}

fn direct_allocator_call(callee: &str) -> bool {
    !matches!(
        direct_allocator_call_kind(callee),
        DirectAllocatorCallKind::Unsupported
    )
}

fn strip_rustc_crate_disambiguators(text: &str) -> String {
    // Current rustc debug strings print external crate paths as
    // `alloc[d734]::...` / `core[2f33]::...`, while older rustc printed plain
    // `alloc::...` / `core::...`.  Normalize only that crate-path suffix.  Do
    // not delete arbitrary bracketed text: type arguments such as `[u64; 16]`
    // are semantic data and must remain visible to the Layout provenance parser.
    let mut out = String::with_capacity(text.len());
    let mut index = 0usize;
    while index < text.len() {
        let rest = &text[index..];
        let ch = match rest.chars().next() {
            Some(ch) => ch,
            None => break,
        };
        if ch == '['
            && out
                .chars()
                .last()
                .map_or(false, |prev| prev == '_' || prev.is_ascii_alphanumeric())
        {
            if let Some(close_rel) = rest.find(']') {
                let after_close = index + close_rel + 1;
                if text[after_close..].starts_with("::") {
                    index = after_close;
                    continue;
                }
            }
        }
        out.push(ch);
        index += ch.len_utf8();
    }
    out
}

fn callee_contains_normalized(callee: &str, marker: &str) -> bool {
    callee.contains(marker) || strip_rustc_crate_disambiguators(callee).contains(marker)
}

fn direct_allocator_callee_contains_alloc_path(callee: &str, leaf: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(callee);
    let std_marker = format!("std::alloc::{}", leaf);
    let alloc_marker = format!("alloc::alloc::{}", leaf);
    normalized.contains(&std_marker) || normalized.contains(&alloc_marker)
}

fn direct_allocator_call_kind(callee: &str) -> DirectAllocatorCallKind {
    if direct_allocator_callee_contains_alloc_path(callee, "exchange_malloc") {
        return DirectAllocatorCallKind::SizeAlignAlloc;
    }
    if global_alloc_callee_contains_method(callee, "alloc_zeroed") {
        return DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed;
    }
    if global_alloc_callee_contains_method(callee, "realloc") {
        return DirectAllocatorCallKind::GlobalAllocLayoutRealloc;
    }
    if global_alloc_callee_contains_method(callee, "dealloc") {
        return DirectAllocatorCallKind::GlobalAllocLayoutDealloc;
    }
    if global_alloc_callee_contains_method(callee, "alloc") {
        return DirectAllocatorCallKind::GlobalAllocLayoutAlloc;
    }
    if direct_allocator_callee_contains_alloc_path(callee, "alloc_zeroed") {
        return DirectAllocatorCallKind::LayoutAllocZeroed;
    }
    if direct_allocator_callee_contains_alloc_path(callee, "realloc") {
        return DirectAllocatorCallKind::LayoutRealloc;
    }
    if direct_allocator_callee_contains_alloc_path(callee, "dealloc") {
        return DirectAllocatorCallKind::LayoutDealloc;
    }
    if direct_allocator_callee_contains_alloc_path(callee, "alloc") {
        return DirectAllocatorCallKind::LayoutAlloc;
    }
    DirectAllocatorCallKind::Unsupported
}

fn global_alloc_callee_contains_method(callee: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let trait_path = format!("GlobalAlloc::{}", method);
    let impl_path = format!("GlobalAlloc>::{}", method);
    callee.contains(&trait_path) || callee.contains(&impl_path)
}

fn direct_allocator_call_kind_label(kind: DirectAllocatorCallKind) -> &'static str {
    match kind {
        DirectAllocatorCallKind::SizeAlignAlloc => "size_align_alloc",
        DirectAllocatorCallKind::LayoutAlloc => "layout_alloc",
        DirectAllocatorCallKind::LayoutAllocZeroed => "layout_alloc_zeroed",
        DirectAllocatorCallKind::LayoutRealloc => "layout_realloc",
        DirectAllocatorCallKind::LayoutDealloc => "layout_dealloc",
        DirectAllocatorCallKind::GlobalAllocLayoutAlloc => "global_alloc_layout_alloc",
        DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => "global_alloc_layout_alloc_zeroed",
        DirectAllocatorCallKind::GlobalAllocLayoutRealloc => "global_alloc_layout_realloc",
        DirectAllocatorCallKind::GlobalAllocLayoutDealloc => "global_alloc_layout_dealloc",
        DirectAllocatorCallKind::Unsupported => "unsupported",
    }
}

#[inline]
fn direct_allocator_layout_abi_kind(
    kind: DirectAllocatorCallKind,
) -> Option<DirectAllocatorCallKind> {
    match kind {
        DirectAllocatorCallKind::LayoutAlloc | DirectAllocatorCallKind::GlobalAllocLayoutAlloc => {
            Some(DirectAllocatorCallKind::LayoutAlloc)
        }
        DirectAllocatorCallKind::LayoutAllocZeroed
        | DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => {
            Some(DirectAllocatorCallKind::LayoutAllocZeroed)
        }
        DirectAllocatorCallKind::LayoutRealloc
        | DirectAllocatorCallKind::GlobalAllocLayoutRealloc => {
            Some(DirectAllocatorCallKind::LayoutRealloc)
        }
        DirectAllocatorCallKind::LayoutDealloc
        | DirectAllocatorCallKind::GlobalAllocLayoutDealloc => {
            Some(DirectAllocatorCallKind::LayoutDealloc)
        }
        _ => None,
    }
}

#[inline]
fn direct_allocator_is_layout_based(kind: DirectAllocatorCallKind) -> bool {
    direct_allocator_layout_abi_kind(kind).is_some()
}

#[inline]
fn direct_allocator_layout_operand_index(kind: DirectAllocatorCallKind) -> Option<usize> {
    match kind {
        DirectAllocatorCallKind::LayoutAlloc | DirectAllocatorCallKind::LayoutAllocZeroed => {
            Some(0)
        }
        DirectAllocatorCallKind::LayoutRealloc | DirectAllocatorCallKind::LayoutDealloc => Some(1),
        DirectAllocatorCallKind::GlobalAllocLayoutAlloc
        | DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => Some(1),
        DirectAllocatorCallKind::GlobalAllocLayoutRealloc
        | DirectAllocatorCallKind::GlobalAllocLayoutDealloc => Some(2),
        _ => None,
    }
}

#[inline]
fn direct_allocator_required_arg_count(kind: DirectAllocatorCallKind) -> usize {
    match kind {
        DirectAllocatorCallKind::SizeAlignAlloc => 2,
        DirectAllocatorCallKind::LayoutAlloc | DirectAllocatorCallKind::LayoutAllocZeroed => 1,
        DirectAllocatorCallKind::LayoutRealloc => 3,
        DirectAllocatorCallKind::LayoutDealloc => 2,
        DirectAllocatorCallKind::GlobalAllocLayoutAlloc
        | DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => 2,
        DirectAllocatorCallKind::GlobalAllocLayoutRealloc => 4,
        DirectAllocatorCallKind::GlobalAllocLayoutDealloc => 3,
        DirectAllocatorCallKind::Unsupported => usize::MAX,
    }
}

fn direct_allocator_global_receiver_supported<'tcx>(
    call_kind: DirectAllocatorCallKind,
    argument_tys: &[Ty<'tcx>],
) -> bool {
    if !direct_allocator_is_global_receiver_call(call_kind) {
        return true;
    }

    argument_tys
        .get(0)
        .map(|receiver_ty| {
            let receiver_ty = format!("{:?}", receiver_ty);
            receiver_ty.contains("RustAllocator") || receiver_ty.contains("UniAlloc")
        })
        .unwrap_or(false)
}

#[inline]
fn direct_allocator_is_global_receiver_call(call_kind: DirectAllocatorCallKind) -> bool {
    matches!(
        call_kind,
        DirectAllocatorCallKind::GlobalAllocLayoutAlloc
            | DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed
            | DirectAllocatorCallKind::GlobalAllocLayoutRealloc
            | DirectAllocatorCallKind::GlobalAllocLayoutDealloc
    )
}

fn direct_allocator_default_replacement_symbol(kind: DirectAllocatorCallKind) -> &'static str {
    direct_allocator_replacement_symbol_for_site(kind, true)
}

fn direct_allocator_recovery_backed_replacement_symbol(
    kind: DirectAllocatorCallKind,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    match (kind, lowering_metadata_hints_requested()) {
        (DirectAllocatorCallKind::LayoutRealloc, false) => {
            "__unialloc_realloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true) => {
            "__unialloc_realloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false) => {
            "__unialloc_dealloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true) => {
            "__unialloc_dealloc_layout_with_metadata_hints"
        }
        _ => direct_allocator_default_replacement_symbol(kind),
    }
}

fn direct_allocator_replacement_symbol_for_site(
    kind: DirectAllocatorCallKind,
    allow_size_align_local: bool,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    let local = direct_local_metadata_abi_requested()
        && (kind != DirectAllocatorCallKind::SizeAlignAlloc
            || !direct_local_size_align_with_semantic_drop_requested()
            || allow_size_align_local);
    match (kind, lowering_metadata_hints_requested(), local) {
        (DirectAllocatorCallKind::SizeAlignAlloc, false, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "__unialloc_alloc_with_metadata_local"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, true, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "__unialloc_alloc_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, true) => {
            "__unialloc_alloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, true) => {
            "__unialloc_alloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, true) => {
            "__unialloc_alloc_zeroed_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, true) => {
            "__unialloc_alloc_zeroed_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, true) => {
            "__unialloc_realloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, true) => {
            "__unialloc_realloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, true) => {
            "__unialloc_dealloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, true) => {
            "__unialloc_dealloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, false, _) => "__unialloc_alloc_with_metadata",
        (DirectAllocatorCallKind::SizeAlignAlloc, true, _) => {
            "__unialloc_alloc_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, false) => {
            "__unialloc_alloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, false) => {
            "__unialloc_alloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, false) => {
            "__unialloc_alloc_zeroed_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, false) => {
            "__unialloc_alloc_zeroed_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, _) => {
            "__unialloc_realloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, _) => {
            "__unialloc_realloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, _) => {
            "__unialloc_dealloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, _) => {
            "__unialloc_dealloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::Unsupported, _, _) => "<unsupported-direct-allocator-call>",
        _ => unreachable!("GlobalAlloc call kinds are mapped to Layout ABI call kinds"),
    }
}

fn direct_allocator_resolved_status_for_site(
    kind: DirectAllocatorCallKind,
    supports_hints: bool,
    allow_size_align_local: bool,
    force_recovery_backed: bool,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    let local = !force_recovery_backed
        && direct_local_metadata_abi_requested()
        && (kind != DirectAllocatorCallKind::SizeAlignAlloc
            || !direct_local_size_align_with_semantic_drop_requested()
            || allow_size_align_local);
    match (kind, supports_hints, local) {
        (DirectAllocatorCallKind::SizeAlignAlloc, false, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "resolved_unialloc_alloc_with_metadata_local"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, true, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "resolved_unialloc_alloc_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, true) => {
            "resolved_unialloc_alloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, true) => {
            "resolved_unialloc_alloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, true) => {
            "resolved_unialloc_alloc_zeroed_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, true) => {
            "resolved_unialloc_alloc_zeroed_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, true) => {
            "resolved_unialloc_realloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, true) => {
            "resolved_unialloc_realloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, true) => {
            "resolved_unialloc_dealloc_layout_with_metadata_local"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, true) => {
            "resolved_unialloc_dealloc_layout_with_metadata_hints_local"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, false, _) => {
            "resolved_unialloc_alloc_with_metadata"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, true, _) => {
            "resolved_unialloc_alloc_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, false) => {
            "resolved_unialloc_alloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, false) => {
            "resolved_unialloc_alloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, false) => {
            "resolved_unialloc_alloc_zeroed_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, false) => {
            "resolved_unialloc_alloc_zeroed_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, _) => {
            "resolved_unialloc_realloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, _) => {
            "resolved_unialloc_realloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, _) => {
            "resolved_unialloc_dealloc_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, _) => {
            "resolved_unialloc_dealloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::Unsupported, _, _) => "unsupported_original_allocator_call_abi",
        _ => unreachable!("GlobalAlloc call kinds are mapped to Layout ABI call kinds"),
    }
}

fn direct_allocator_unresolved_status_for_site(
    kind: DirectAllocatorCallKind,
    allow_size_align_local: bool,
    force_recovery_backed: bool,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    let local = !force_recovery_backed
        && direct_local_metadata_abi_requested()
        && (kind != DirectAllocatorCallKind::SizeAlignAlloc
            || !direct_local_size_align_with_semantic_drop_requested()
            || allow_size_align_local);
    match (kind, lowering_metadata_hints_requested(), local) {
        (DirectAllocatorCallKind::SizeAlignAlloc, false, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "unialloc_alloc_with_metadata_local_not_resolved"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, true, true)
            if direct_local_size_align_with_semantic_drop_requested() =>
        {
            "unialloc_alloc_with_metadata_hints_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, true) => {
            "unialloc_alloc_layout_with_metadata_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, true) => {
            "unialloc_alloc_layout_with_metadata_hints_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, true) => {
            "unialloc_alloc_zeroed_layout_with_metadata_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, true) => {
            "unialloc_alloc_zeroed_layout_with_metadata_hints_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, true) => {
            "unialloc_realloc_layout_with_metadata_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, true) => {
            "unialloc_realloc_layout_with_metadata_hints_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, true) => {
            "unialloc_dealloc_layout_with_metadata_local_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, true) => {
            "unialloc_dealloc_layout_with_metadata_hints_local_not_resolved"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, false, _) => {
            "unialloc_alloc_with_metadata_not_resolved"
        }
        (DirectAllocatorCallKind::SizeAlignAlloc, true, _) => {
            "unialloc_alloc_with_metadata_hints_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAlloc, false, false) => {
            "unialloc_alloc_layout_with_metadata_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAlloc, true, false) => {
            "unialloc_alloc_layout_with_metadata_hints_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false, false) => {
            "unialloc_alloc_zeroed_layout_with_metadata_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true, false) => {
            "unialloc_alloc_zeroed_layout_with_metadata_hints_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutRealloc, false, _) => {
            "unialloc_realloc_layout_with_metadata_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutRealloc, true, _) => {
            "unialloc_realloc_layout_with_metadata_hints_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutDealloc, false, _) => {
            "unialloc_dealloc_layout_with_metadata_not_resolved"
        }
        (DirectAllocatorCallKind::LayoutDealloc, true, _) => {
            "unialloc_dealloc_layout_with_metadata_hints_not_resolved"
        }
        (DirectAllocatorCallKind::Unsupported, _, _) => "unsupported_original_allocator_call_abi",
        _ => unreachable!("GlobalAlloc call kinds are mapped to Layout ABI call kinds"),
    }
}

fn direct_allocator_metadata_pairing_contract(
    kind: DirectAllocatorCallKind,
    replacement_symbol: &str,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    if kind == DirectAllocatorCallKind::SizeAlignAlloc
        && replacement_symbol.ends_with("_local")
        && direct_local_size_align_with_semantic_drop_requested()
    {
        return "local_metadata_abi_semantic_drop_scope";
    }
    if kind == DirectAllocatorCallKind::SizeAlignAlloc && direct_local_metadata_abi_requested() {
        // `exchange_malloc(size, align)` is only the Box allocation half.  The
        // matching deallocation is hidden behind Drop glue, so the direct
        // allocator pass must stay recovery-backed unless/until a paired
        // deallocation metadata path is proven for that allocation.
        return "recovery_backed_size_align_alloc_unpaired_dealloc";
    }
    if replacement_symbol.ends_with("_local") {
        return "local_metadata_abi_explicit_paired_layout";
    }
    if kind == DirectAllocatorCallKind::SizeAlignAlloc {
        return "recovery_backed_size_align_alloc";
    }
    "recovery_backed_layout_metadata_abi"
}

fn strip_reference_type_prefix(mut value: &str) -> &str {
    loop {
        let trimmed = value.trim();
        if let Some(rest) = trimmed.strip_prefix("&mut ") {
            value = rest;
        } else if let Some(rest) = trimmed.strip_prefix('&') {
            value = rest;
        } else {
            return trimmed;
        }
    }
}

struct HeapObjectSupport {
    type_markers: &'static [&'static str],
    adt_def_paths: &'static [&'static str],
    owned_return_markers: &'static [&'static str],
}

const HEAP_OBJECT_SUPPORT: &[HeapObjectSupport] = &[
    HeapObjectSupport {
        type_markers: &["std::vec::Vec<", "alloc::vec::Vec<"],
        adt_def_paths: &["alloc::vec::Vec"],
        owned_return_markers: &[") -> std::vec::Vec", ") -> alloc::vec::Vec"],
    },
    HeapObjectSupport {
        type_markers: &["std::vec::IntoIter<", "alloc::vec::into_iter::IntoIter<"],
        adt_def_paths: &[],
        owned_return_markers: &[
            ") -> std::vec::IntoIter",
            ") -> alloc::vec::into_iter::IntoIter",
        ],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::collections::VecDeque<",
            "alloc::collections::VecDeque<",
        ],
        adt_def_paths: &["alloc::collections::vec_deque::VecDeque"],
        owned_return_markers: &[
            ") -> std::collections::VecDeque",
            ") -> alloc::collections::VecDeque",
        ],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::collections::BinaryHeap<",
            "alloc::collections::BinaryHeap<",
        ],
        adt_def_paths: &["alloc::collections::binary_heap::BinaryHeap"],
        owned_return_markers: &[
            ") -> std::collections::BinaryHeap",
            ") -> alloc::collections::BinaryHeap",
        ],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::collections::BTreeMap<",
            "alloc::collections::BTreeMap<",
        ],
        adt_def_paths: &["alloc::collections::btree::map::BTreeMap"],
        owned_return_markers: &[
            ") -> std::collections::BTreeMap",
            ") -> alloc::collections::BTreeMap",
        ],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::collections::BTreeSet<",
            "alloc::collections::BTreeSet<",
        ],
        adt_def_paths: &["alloc::collections::btree::set::BTreeSet"],
        owned_return_markers: &[
            ") -> std::collections::BTreeSet",
            ") -> alloc::collections::BTreeSet",
        ],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::collections::LinkedList<",
            "alloc::collections::LinkedList<",
        ],
        adt_def_paths: &["alloc::collections::linked_list::LinkedList"],
        owned_return_markers: &[
            ") -> std::collections::LinkedList",
            ") -> alloc::collections::LinkedList",
        ],
    },
    HeapObjectSupport {
        type_markers: &["std::collections::HashMap<", "hashbrown::map::HashMap<"],
        adt_def_paths: &[
            "std::collections::hash::map::HashMap",
            "hashbrown::map::HashMap",
        ],
        owned_return_markers: &[
            ") -> std::collections::HashMap",
            ") -> hashbrown::map::HashMap",
        ],
    },
    HeapObjectSupport {
        type_markers: &["std::collections::HashSet<", "hashbrown::set::HashSet<"],
        adt_def_paths: &[
            "std::collections::hash::set::HashSet",
            "hashbrown::set::HashSet",
        ],
        owned_return_markers: &[
            ") -> std::collections::HashSet",
            ") -> hashbrown::set::HashSet",
        ],
    },
    HeapObjectSupport {
        type_markers: &["indexmap::IndexMap<", "indexmap::map::IndexMap<"],
        adt_def_paths: &["indexmap::map::IndexMap"],
        owned_return_markers: &[") -> indexmap::IndexMap", ") -> indexmap::map::IndexMap"],
    },
    HeapObjectSupport {
        type_markers: &["indexmap::IndexSet<", "indexmap::set::IndexSet<"],
        adt_def_paths: &["indexmap::set::IndexSet"],
        owned_return_markers: &[") -> indexmap::IndexSet", ") -> indexmap::set::IndexSet"],
    },
    HeapObjectSupport {
        type_markers: &["std::string::String", "alloc::string::String"],
        adt_def_paths: &["alloc::string::String"],
        owned_return_markers: &[") -> std::string::String", ") -> alloc::string::String"],
    },
    HeapObjectSupport {
        type_markers: &["std::borrow::Cow<str>", "alloc::borrow::Cow<str>"],
        adt_def_paths: &[],
        owned_return_markers: &[") -> std::borrow::Cow<str>", ") -> alloc::borrow::Cow<str>"],
    },
    HeapObjectSupport {
        type_markers: &["std::boxed::Box<", "alloc::boxed::Box<"],
        adt_def_paths: &["alloc::boxed::Box"],
        owned_return_markers: &[") -> std::boxed::Box", ") -> alloc::boxed::Box"],
    },
    HeapObjectSupport {
        type_markers: &["std::rc::Rc<", "alloc::rc::Rc<"],
        adt_def_paths: &["alloc::rc::Rc"],
        owned_return_markers: &[") -> std::rc::Rc", ") -> alloc::rc::Rc"],
    },
    HeapObjectSupport {
        type_markers: &["std::sync::Arc<", "alloc::sync::Arc<"],
        adt_def_paths: &["alloc::sync::Arc"],
        owned_return_markers: &[") -> std::sync::Arc", ") -> alloc::sync::Arc"],
    },
    HeapObjectSupport {
        type_markers: &["std::path::PathBuf"],
        adt_def_paths: &["std::path::PathBuf"],
        owned_return_markers: &[") -> std::path::PathBuf"],
    },
    HeapObjectSupport {
        type_markers: &["std::ffi::OsString", "std::ffi::os_str::OsString"],
        adt_def_paths: &["std::ffi::os_str::OsString"],
        owned_return_markers: &[") -> std::ffi::OsString", ") -> std::ffi::os_str::OsString"],
    },
    HeapObjectSupport {
        type_markers: &[
            "std::ffi::CString",
            "std::ffi::c_str::CString",
            "alloc::ffi::CString",
            "alloc::ffi::c_str::CString",
        ],
        adt_def_paths: &["alloc::ffi::c_str::CString"],
        owned_return_markers: &[
            ") -> std::ffi::CString",
            ") -> std::ffi::c_str::CString",
            ") -> alloc::ffi::CString",
            ") -> alloc::ffi::c_str::CString",
        ],
    },
];

fn heap_type_marker_matches(value: &str) -> bool {
    HEAP_OBJECT_SUPPORT.iter().any(|support| {
        support
            .type_markers
            .iter()
            .any(|marker| value.contains(marker))
    })
}

fn direct_heap_type_marker_matches(value: &str) -> bool {
    let normalized = strip_reference_type_prefix(value);
    HEAP_OBJECT_SUPPORT.iter().any(|support| {
        support.type_markers.iter().any(|marker| {
            if marker.ends_with('<') {
                normalized.starts_with(marker)
            } else {
                normalized == *marker || normalized.starts_with(&format!("{}<", marker))
            }
        })
    })
}

fn looks_like_heap_object_type(value: &str) -> bool {
    let normalized = strip_reference_type_prefix(value);
    heap_type_marker_matches(normalized)
}

fn normalized_heap_object_type(value: &str) -> Option<String> {
    if looks_like_heap_object_type(value) {
        Some(strip_reference_type_prefix(value).trim().to_string())
    } else {
        None
    }
}

fn supported_heap_adt_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    HEAP_OBJECT_SUPPORT.iter().any(|support| {
        support
            .adt_def_paths
            .iter()
            .any(|candidate| normalized == *candidate)
    })
}

fn strip_type_indirection<'tcx>(mut ty: Ty<'tcx>) -> Ty<'tcx> {
    loop {
        match ty.kind() {
            ty::Ref(_, inner, _) => ty = *inner,
            #[cfg(unialloc_rustc_current)]
            ty::RawPtr(inner, _) => ty = *inner,
            #[cfg(not(unialloc_rustc_current))]
            ty::RawPtr(type_and_mut) => ty = type_and_mut.ty,
            _ => return ty,
        }
    }
}

fn heap_object_type_from_ty<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> Option<String> {
    heap_object_type_from_ty_inner(tcx, ty, 0)
}

fn heap_object_types_from_ty<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    collect_heap_object_types_from_ty_inner(tcx, ty, 0, &mut out);
    out
}

fn type_contains_generic_param<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> bool {
    type_contains_generic_param_inner(tcx, ty, 0)
}

fn type_contains_generic_param_inner<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>, depth: usize) -> bool {
    const MAX_GENERIC_PARAM_SCAN_DEPTH: usize = 6;
    if depth > MAX_GENERIC_PARAM_SCAN_DEPTH {
        return false;
    }
    let stripped = strip_type_indirection(ty);
    match stripped.kind() {
        ty::Param(_) => true,
        ty::Adt(adt, substs) => {
            let generic_in_args = substs
                .types()
                .any(|arg_ty| type_contains_generic_param_inner(tcx, arg_ty, depth + 1));
            #[cfg(unialloc_rustc_current)]
            let generic_in_fields = {
                // New rustc exposes variant field types as Unnormalized<Ty>.
                // Do not silently normalize in this audit pass; generic
                // arguments remain the stable evidence source until a typed
                // normalization query is added and validated.
                let _ = adt;
                false
            };
            #[cfg(not(unialloc_rustc_current))]
            let generic_in_fields = adt.variants().iter().any(|variant| {
                variant.fields.iter().any(|field| {
                    type_contains_generic_param_inner(tcx, field.ty(tcx, substs), depth + 1)
                })
            });
            generic_in_args || generic_in_fields
        }
        ty::Tuple(fields) => fields
            .iter()
            .any(|field_ty| type_contains_generic_param_inner(tcx, field_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::Closure(_, substs) => ty::UpvarArgs::Closure(substs)
            .upvar_tys()
            .iter()
            .any(|upvar_ty| type_contains_generic_param_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(not(unialloc_rustc_current))]
        ty::Closure(_, substs) => substs
            .as_closure()
            .upvar_tys()
            .any(|upvar_ty| type_contains_generic_param_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::Coroutine(_, substs) => ty::UpvarArgs::Coroutine(substs)
            .upvar_tys()
            .iter()
            .any(|upvar_ty| type_contains_generic_param_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::CoroutineClosure(_, substs) => ty::UpvarArgs::CoroutineClosure(substs)
            .upvar_tys()
            .iter()
            .any(|upvar_ty| type_contains_generic_param_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(not(unialloc_rustc_current))]
        ty::Generator(_, substs, _) => substs
            .as_generator()
            .upvar_tys()
            .any(|upvar_ty| type_contains_generic_param_inner(tcx, upvar_ty, depth + 1)),
        ty::Array(inner, _) | ty::Slice(inner) => {
            type_contains_generic_param_inner(tcx, *inner, depth + 1)
        }
        _ => false,
    }
}

fn heap_object_type_from_ty_inner<'tcx>(
    tcx: TyCtxt<'tcx>,
    ty: Ty<'tcx>,
    depth: usize,
) -> Option<String> {
    const MAX_HEAP_OBJECT_SOLVER_DEPTH: usize = 6;
    if depth > MAX_HEAP_OBJECT_SOLVER_DEPTH {
        return None;
    }
    let stripped = strip_type_indirection(ty);
    match stripped.kind() {
        ty::Adt(adt, substs) => {
            let def_path = tcx.def_path_str(adt.did());
            if supported_heap_adt_def_path(&def_path)
                || direct_heap_type_marker_matches(&format!("{:?}", stripped))
            {
                Some(format!("{:?}", stripped))
            } else {
                let from_args = substs
                    .types()
                    .find_map(|arg_ty| heap_object_type_from_ty_inner(tcx, arg_ty, depth + 1));
                #[cfg(unialloc_rustc_current)]
                let from_fields = {
                    // See the generic-parameter scanner above: current rustc
                    // requires explicit normalization before field recursion.
                    // Keep the current pass conservative instead of adding an
                    // untested normalization fallback.
                    let _ = adt;
                    None
                };
                #[cfg(not(unialloc_rustc_current))]
                let from_fields = {
                    adt.variants().iter().find_map(|variant| {
                        variant.fields.iter().find_map(|field| {
                            heap_object_type_from_ty_inner(tcx, field.ty(tcx, substs), depth + 1)
                        })
                    })
                };
                from_args
                    .or(from_fields)
                    .or_else(|| normalized_heap_object_type(&format!("{:?}", stripped)))
            }
        }
        ty::Tuple(fields) => fields
            .iter()
            .find_map(|field_ty| heap_object_type_from_ty_inner(tcx, field_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::Closure(_, substs) => ty::UpvarArgs::Closure(substs)
            .upvar_tys()
            .iter()
            .find_map(|upvar_ty| heap_object_type_from_ty_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(not(unialloc_rustc_current))]
        ty::Closure(_, substs) => substs
            .as_closure()
            .upvar_tys()
            .find_map(|upvar_ty| heap_object_type_from_ty_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::Coroutine(_, substs) => ty::UpvarArgs::Coroutine(substs)
            .upvar_tys()
            .iter()
            .find_map(|upvar_ty| heap_object_type_from_ty_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(unialloc_rustc_current)]
        ty::CoroutineClosure(_, substs) => ty::UpvarArgs::CoroutineClosure(substs)
            .upvar_tys()
            .iter()
            .find_map(|upvar_ty| heap_object_type_from_ty_inner(tcx, upvar_ty, depth + 1)),
        #[cfg(not(unialloc_rustc_current))]
        ty::Generator(_, substs, _) => substs
            .as_generator()
            .upvar_tys()
            .find_map(|upvar_ty| heap_object_type_from_ty_inner(tcx, upvar_ty, depth + 1)),
        ty::Array(inner, _) | ty::Slice(inner) => {
            heap_object_type_from_ty_inner(tcx, *inner, depth + 1)
        }
        _ => normalized_heap_object_type(&format!("{:?}", stripped)),
    }
}

fn collect_heap_object_types_from_ty_inner<'tcx>(
    tcx: TyCtxt<'tcx>,
    ty: Ty<'tcx>,
    depth: usize,
    out: &mut BTreeSet<String>,
) {
    const MAX_HEAP_OBJECT_SOLVER_DEPTH: usize = 6;
    if depth > MAX_HEAP_OBJECT_SOLVER_DEPTH {
        return;
    }

    let stripped = strip_type_indirection(ty);
    match stripped.kind() {
        ty::Adt(adt, substs) => {
            let def_path = tcx.def_path_str(adt.did());
            let type_text = format!("{:?}", stripped);
            if supported_heap_adt_def_path(&def_path) || direct_heap_type_marker_matches(&type_text)
            {
                out.insert(type_text);
            }
            // Do not classify an aggregate as a heap owner merely because its
            // rendered generic arguments contain `Vec`, `String`, or another
            // supported marker. The argument/field walk below records the
            // actual owners and lets multi-owner Drop fail closed precisely.
            for arg_ty in substs.types() {
                collect_heap_object_types_from_ty_inner(tcx, arg_ty, depth + 1, out);
            }
            #[cfg(not(unialloc_rustc_current))]
            for variant in adt.variants().iter() {
                for field in variant.fields.iter() {
                    collect_heap_object_types_from_ty_inner(
                        tcx,
                        field.ty(tcx, substs),
                        depth + 1,
                        out,
                    );
                }
            }
        }
        ty::Tuple(fields) => {
            for field_ty in fields.iter() {
                collect_heap_object_types_from_ty_inner(tcx, field_ty, depth + 1, out);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::Closure(_, substs) => {
            for upvar_ty in ty::UpvarArgs::Closure(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, out);
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::Closure(_, substs) => {
            for upvar_ty in substs.as_closure().upvar_tys() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, out);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::Coroutine(_, substs) => {
            for upvar_ty in ty::UpvarArgs::Coroutine(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, out);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::CoroutineClosure(_, substs) => {
            for upvar_ty in ty::UpvarArgs::CoroutineClosure(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, out);
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::Generator(_, substs, _) => {
            for upvar_ty in substs.as_generator().upvar_tys() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, out);
            }
        }
        ty::Array(inner, _) | ty::Slice(inner) => {
            collect_heap_object_types_from_ty_inner(tcx, *inner, depth + 1, out);
        }
        _ => {
            if let Some(normalized) = normalized_heap_object_type(&format!("{:?}", stripped)) {
                out.insert(normalized);
            }
        }
    }
}

#[derive(Default)]
struct PlainCloneHeapOwnerScan {
    owners: BTreeSet<String>,
    unresolved: bool,
}

fn plain_clone_trait_call(callee: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(callee);
    const DIRECT_MARKERS: &[&str] = &["core::clone::Clone::clone", "std::clone::Clone::clone"];
    const QUALIFIED_MARKERS: &[&str] = &[
        " as core::clone::Clone>::clone",
        " as std::clone::Clone>::clone",
    ];
    DIRECT_MARKERS.iter().any(|marker| {
        normalized.match_indices(marker).any(|(idx, _)| {
            marker_has_prefix_boundary(&normalized, idx)
                && path_marker_has_boundary(&normalized, idx, marker)
        })
    }) || QUALIFIED_MARKERS.iter().any(|marker| {
        normalized
            .match_indices(marker)
            .any(|(idx, _)| path_marker_has_boundary(&normalized, idx, marker))
    })
}

#[cfg(unialloc_rustc_current)]
fn plain_clone_trait_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    matches!(
        tcx.def_path_str(trait_item_def_id).as_str(),
        "core::clone::Clone::clone" | "std::clone::Clone::clone"
    )
}

#[cfg(not(unialloc_rustc_current))]
fn plain_clone_trait_def_id(_tcx: TyCtxt<'_>, _def_id: DefId) -> bool {
    // Older supported rustc debug text retains the fully-qualified trait path;
    // the boundary-checked textual matcher remains the compatibility path.
    false
}

fn plain_clone_call(tcx: TyCtxt<'_>, def_id: Option<DefId>, callee: &str) -> bool {
    def_id.map_or(false, |def_id| plain_clone_trait_def_id(tcx, def_id))
        || plain_clone_trait_call(callee)
}

fn clone_transparent_wrapper_def_path(path: &str) -> bool {
    path == "core::option::Option"
        || path.ends_with("::core::option::Option")
        || path == "core::result::Result"
        || path.ends_with("::core::result::Result")
}

fn clone_known_no_supported_owner_adt(path: &str) -> bool {
    path == "core::marker::PhantomData"
        || path.ends_with("::core::marker::PhantomData")
        || path == "core::time::Duration"
        || path.ends_with("::core::time::Duration")
}

fn clone_reference_counted_field_is_nonallocating(path: &str) -> bool {
    // This rule is deliberately DefId-path exact and applies only to the
    // result-owner scan for `Clone::clone`.  Cloning Arc/Rc increments the
    // existing allocation's reference count; it neither clones `T` nor creates
    // a new heap allocation owner.  Arc/Rc remain supported heap objects in all
    // allocation-constructor and Drop analyses outside this plain-Clone scan.
    let normalized = strip_rustc_crate_disambiguators(path);
    matches!(
        normalized.as_str(),
        // Current rustc reports the std re-export path for std-using crates;
        // no suffix/substring match is accepted.
        "alloc::sync::Arc" | "std::sync::Arc" | "alloc::rc::Rc" | "std::rc::Rc"
    )
}

#[cfg(unialloc_rustc_current)]
fn clone_custom_result_is_owner_complete<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> bool {
    !clone_result_has_unresolved_params(ty)
        && !type_contains_generic_param(tcx, ty)
        && tcx.type_is_copy_modulo_regions(ty::TypingEnv::fully_monomorphized(), ty)
}

#[cfg(not(unialloc_rustc_current))]
fn clone_custom_result_is_owner_complete<'tcx>(_tcx: TyCtxt<'tcx>, _ty: Ty<'tcx>) -> bool {
    // On the older rustc_private surface every substituted field type is
    // directly available.  Reaching this decision therefore means the complete
    // field graph was recursively visible and contained only modeled scalar /
    // reference leaves; raw pointers and unknown leaves already set unresolved.
    true
}

#[cfg(unialloc_rustc_current)]
fn clone_result_has_unresolved_params(ty: Ty<'_>) -> bool {
    ty.has_non_region_param()
        || ty.has_infer()
        || ty.has_placeholders()
        || ty.has_escaping_bound_vars()
}

#[cfg(not(unialloc_rustc_current))]
fn clone_result_has_unresolved_params(ty: Ty<'_>) -> bool {
    ty.has_param_types_or_consts()
        || ty.has_infer_types_or_consts()
        || ty.has_placeholders()
        || ty.has_escaping_bound_vars()
}

fn collect_plain_clone_heap_owners_inner<'tcx>(
    tcx: TyCtxt<'tcx>,
    ty: Ty<'tcx>,
    depth: usize,
    scan: &mut PlainCloneHeapOwnerScan,
) {
    const MAX_PLAIN_CLONE_OWNER_DEPTH: usize = 8;
    if depth > MAX_PLAIN_CLONE_OWNER_DEPTH {
        scan.unresolved = true;
        return;
    }

    match ty.kind() {
        // Cloning a reference copies the borrow; it does not clone or own the
        // referenced allocation.  In particular, `&Vec<T>` must not be
        // attributed to `Vec<T>` merely because the pointee is a heap owner.
        ty::Ref(_, _, _) => {}
        #[cfg(unialloc_rustc_current)]
        ty::RawPtr(_, _) => {
            // A direct raw-pointer result has no supported owner.  A raw pointer
            // inside a custom wrapper can encode an ownership convention that
            // rustc's type alone cannot prove, so keep the wrapper unresolved.
            if depth > 0 {
                scan.unresolved = true;
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::RawPtr(_) => {
            if depth > 0 {
                scan.unresolved = true;
            }
        }
        ty::Adt(adt, substs) => {
            let def_path = tcx.def_path_str(adt.did());
            let type_text = format!("{:?}", ty);
            if clone_reference_counted_field_is_nonallocating(&def_path) {
                // Do not inspect Arc<T>/Rc<T>'s generic argument: Clone copies
                // the reference-counted handle and does not invoke T::clone.
                return;
            }
            if supported_heap_adt_def_path(&def_path) || direct_heap_type_marker_matches(&type_text)
            {
                scan.owners.insert(type_text);
                return;
            }
            if clone_known_no_supported_owner_adt(&def_path) {
                // Do not inspect PhantomData<T>'s generic argument: it is not a
                // stored T and scanning it would make PhantomData<Vec<_>> look
                // like an owned Vec.  Duration is likewise a trusted scalar.
                return;
            }
            if clone_transparent_wrapper_def_path(&def_path) {
                // Option and Result store their type arguments directly.  This
                // is the only generic-argument recursion allowed here; arbitrary
                // ADT arguments may be phantom or otherwise non-stored.
                for arg_ty in substs.types() {
                    collect_plain_clone_heap_owners_inner(tcx, arg_ty, depth + 1, scan);
                }
                return;
            }

            let owner_count_before = scan.owners.len();
            let unresolved_before = scan.unresolved;
            #[cfg(unialloc_rustc_current)]
            for variant in adt.variants().iter() {
                for field in variant.fields.iter() {
                    collect_plain_clone_heap_owners_inner(
                        tcx,
                        field.ty(tcx, substs).skip_norm_wip(),
                        depth + 1,
                        scan,
                    );
                }
            }
            #[cfg(not(unialloc_rustc_current))]
            for variant in adt.variants().iter() {
                for field in variant.fields.iter() {
                    collect_plain_clone_heap_owners_inner(
                        tcx,
                        field.ty(tcx, substs),
                        depth + 1,
                        scan,
                    );
                }
            }

            // A custom ADT with visible heap fields can be classified from those
            // fields.  Declaring a scalar-only custom ADT owner-free is stricter:
            // require the current compiler's Copy proof.  This still leaves a
            // raw-pointer wrapper unresolved because nested raw pointers set the
            // dominating unresolved bit above.
            if scan.owners.len() == owner_count_before
                && scan.unresolved == unresolved_before
                && !clone_custom_result_is_owner_complete(tcx, ty)
            {
                scan.unresolved = true;
            }
        }
        ty::Tuple(fields) => {
            for field_ty in fields.iter() {
                collect_plain_clone_heap_owners_inner(tcx, field_ty, depth + 1, scan);
            }
        }
        ty::Array(inner, _) => {
            collect_plain_clone_heap_owners_inner(tcx, *inner, depth + 1, scan);
        }
        ty::Bool
        | ty::Char
        | ty::Int(_)
        | ty::Uint(_)
        | ty::Float(_)
        | ty::Str
        | ty::Never
        | ty::FnDef(_, _)
        | ty::FnPtr(..) => {}
        // Slices, aliases, dynamic objects, generic parameters, inference
        // variables, closures/coroutines, and compiler error types are not
        // structurally complete enough here.  Unknown dominates any owners
        // found in sibling fields, preventing a partial Single classification.
        _ => scan.unresolved = true,
    }
}

fn plain_clone_heap_class<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    destination_ty: Ty<'tcx>,
) -> PlainCloneHeapClass {
    if !plain_clone_call(tcx, callee_def_id, callee) {
        return PlainCloneHeapClass::NotPlainClone;
    }
    // `TypingEnv::fully_monomorphized()` is only valid for fully resolved
    // destination types.  In particular, querying Copy for a Clone result that
    // still contains a const parameter can ICE current rustc.  Keep all such
    // candidates explicit and fail-closed instead of invoking the Copy query.
    if clone_result_has_unresolved_params(destination_ty) {
        return PlainCloneHeapClass::Unresolved;
    }
    let mut scan = PlainCloneHeapOwnerScan::default();
    collect_plain_clone_heap_owners_inner(tcx, destination_ty, 0, &mut scan);
    if scan.unresolved {
        return PlainCloneHeapClass::Unresolved;
    }
    let owners = scan.owners.into_iter().collect::<Vec<_>>();
    match owners.len() {
        0 => PlainCloneHeapClass::DefiniteNoSupportedOwner,
        1 => PlainCloneHeapClass::Single(owners.into_iter().next().unwrap()),
        _ => PlainCloneHeapClass::Ambiguous(owners),
    }
}

fn callee_return_type_text(callee: &str) -> Option<String> {
    let marker = ") -> ";
    let start = callee.find(marker)? + marker.len();
    let rest = &callee[start..];
    let end = rest
        .find(" {")
        .or_else(|| rest.find("}"))
        .unwrap_or(rest.len());
    let candidate = rest[..end].trim();
    if candidate.is_empty() {
        None
    } else {
        Some(candidate.to_string())
    }
}

fn semantic_heap_object_type_from_mir<'tcx>(
    tcx: TyCtxt<'tcx>,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
    callee: &str,
) -> String {
    let destination_heap_object_type = || heap_object_type_from_ty(tcx, destination_ty);
    let argument_heap_object_type = || {
        argument_tys
            .iter()
            .copied()
            .find_map(|arg_ty| heap_object_type_from_ty(tcx, arg_ty))
    };

    let solved = if semantic_scope_receiver_heap_owner_call(callee) {
        argument_heap_object_type().or_else(destination_heap_object_type)
    } else {
        destination_heap_object_type().or_else(argument_heap_object_type)
    };

    solved
        .or_else(|| {
            // Primary solving above is rustc_middle type based.  Keep the old
            // callee-text return fallback only for compiler debug strings whose
            // destination/argument MIR types do not directly expose the heap ADT.
            callee_return_type_text(callee)
                .as_deref()
                .and_then(normalized_heap_object_type)
        })
        .unwrap_or_else(|| UNKNOWN_HEAP_OBJECT_TYPE.to_string())
}

fn unknown_heap_object_solution() -> HeapObjectSolution {
    HeapObjectSolution {
        object_type: UNKNOWN_HEAP_OBJECT_TYPE.to_string(),
        type_id_basis: "direct_allocator_callsite_key",
    }
}

fn ty_heap_object_solution<'tcx>(
    tcx: TyCtxt<'tcx>,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
    callee: &str,
) -> Option<HeapObjectSolution> {
    let object_type = semantic_heap_object_type_from_mir(tcx, destination_ty, argument_tys, callee);
    if object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        None
    } else {
        Some(HeapObjectSolution {
            object_type,
            type_id_basis: "rustc_middle_ty_destination_or_argument_heap_object_type",
        })
    }
}

fn operand_place_text(operand: &Operand<'_>) -> Option<String> {
    match operand {
        Operand::Move(place) | Operand::Copy(place) => Some(format!("{:?}", place)),
        Operand::Constant(_) => None,
        #[cfg(unialloc_rustc_current)]
        Operand::RuntimeChecks(_) => None,
    }
}

fn place_text(place: &Place<'_>) -> String {
    format!("{:?}", place)
}

fn place_parent_for_tuple_layout_field_zero(place: &str) -> Option<String> {
    let field_index = place.find(".0")?;
    let after = place[field_index + 2..].trim_start();
    if !(after.is_empty() || after.starts_with(':') || after.starts_with(')')) {
        return None;
    }
    let parent = place[..field_index]
        .trim()
        .trim_matches(|ch| ch == '(' || ch == ')')
        .trim();
    if parent.is_empty() {
        None
    } else {
        Some(parent.to_string())
    }
}

fn strip_outer_place_parens(text: &str) -> &str {
    let trimmed = text.trim();
    if !trimmed.starts_with('(') || !trimmed.ends_with(')') {
        return trimmed;
    }

    let mut depth = 0isize;
    for (idx, ch) in trimmed.char_indices() {
        match ch {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 && idx + ch.len_utf8() != trimmed.len() {
                    return trimmed;
                }
            }
            _ => {}
        }
        if depth < 0 {
            return trimmed;
        }
    }
    if depth == 0 {
        trimmed[1..trimmed.len() - 1].trim()
    } else {
        trimmed
    }
}

fn strip_place_type_suffix(text: &str) -> &str {
    let mut depth = 0isize;
    let mut prev = '\0';
    for (idx, ch) in text.char_indices() {
        match ch {
            '(' | '[' | '<' => depth += 1,
            ')' | ']' | '>' => depth -= 1,
            ':' if depth == 0 && prev != ':' => return text[..idx].trim(),
            _ => {}
        }
        prev = ch;
    }
    text.trim()
}

fn canonical_mir_place_key(place: &str) -> String {
    // MIR debug text can surface the same Layout local as `_3`, `copy _3`,
    // `(*_3)`, or `(_7.0: std::alloc::Layout)` depending on whether rustc has
    // inserted reference temps, tuple projection temps, or type ascriptions in
    // the formatted Place.  The provenance map should be keyed by compiler
    // identity, not by one exact debug spelling, so normalize only these narrow
    // syntactic wrappers before lookup.
    let mut current = place.trim();
    loop {
        let mut changed = false;
        if let Some(rest) = current.strip_prefix("move ") {
            current = rest.trim();
            changed = true;
        }
        if let Some(rest) = current.strip_prefix("copy ") {
            current = rest.trim();
            changed = true;
        }
        let stripped = strip_outer_place_parens(current);
        if stripped.len() != current.len() {
            current = stripped;
            changed = true;
        }
        if current.starts_with("(*") && current.ends_with(')') {
            let inner = current[2..current.len() - 1].trim();
            if !inner.is_empty() {
                current = inner;
                changed = true;
            }
        } else if let Some(rest) = current.strip_prefix('*') {
            current = rest.trim();
            changed = true;
        }
        let without_type = strip_place_type_suffix(current);
        if without_type.len() != current.len() {
            current = without_type;
            changed = true;
        }
        if !changed {
            break;
        }
    }
    current.trim().to_string()
}

fn place_lookup_keys(place: &str) -> Vec<String> {
    let mut keys = Vec::new();
    let mut seen = BTreeSet::new();
    let mut push_key = |key: String| {
        if !key.is_empty() && seen.insert(key.clone()) {
            keys.push(key);
        }
    };

    let original = place.trim().to_string();
    push_key(original.clone());
    let canonical = canonical_mir_place_key(&original);
    push_key(canonical.clone());
    if let Some(parent) = place_parent_for_tuple_layout_field_zero(&original) {
        push_key(parent.clone());
        push_key(canonical_mir_place_key(&parent));
    }
    if let Some(parent) = place_parent_for_tuple_layout_field_zero(&canonical) {
        push_key(parent.clone());
        push_key(canonical_mir_place_key(&parent));
    }
    keys
}

fn place_heap_object_solution(
    place: &str,
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    place_lookup_keys(place)
        .into_iter()
        .find_map(|key| provenance.get(&key).cloned())
}

fn operand_heap_object_solution(
    operand: &Operand<'_>,
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    operand_place_text(operand).and_then(|place| place_heap_object_solution(&place, provenance))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solution(label: &str) -> HeapObjectSolution {
        HeapObjectSolution {
            object_type: label.to_string(),
            type_id_basis: "test",
        }
    }

    #[test]
    fn compiler_fnv_ids_normalize_only_zero() {
        assert_eq!(nonzero_fnv1a64(0), FNV1A64_OFFSET_BASIS);
        assert_ne!(nonzero_fnv1a64(0), 0);
        assert_eq!(nonzero_fnv1a64(1), 1);
        assert_eq!(
            nonzero_fnv1a64(0xA110_C002_DA11_0001),
            0xA110_C002_DA11_0001
        );
        assert_ne!(nonzero_fnv1a64_text("compiler-generated-id"), 0);
    }

    #[test]
    fn plain_clone_matcher_is_trait_exact_and_excludes_clone_from() {
        assert!(plain_clone_trait_call(
            "Val(ZeroSized, FnDef(DefId(2:1 ~ core[2f33]::clone::Clone::clone), [u64]))"
        ));
        assert!(plain_clone_trait_call(
            "<std::vec::Vec<u8> as std::clone::Clone>::clone"
        ));
        assert!(!plain_clone_trait_call(
            "<std::vec::Vec<u8> as std::clone::Clone>::clone_from"
        ));
        assert!(!plain_clone_trait_call(
            "<std::vec::Vec<u8> as std::clone::Clone>::clone_extra"
        ));
        assert!(!plain_clone_trait_call(
            "my_crate::allocation::CloneFactory::clone"
        ));
    }

    #[test]
    fn supported_heap_adt_matcher_accepts_exact_indexmap_paths_only() {
        assert!(supported_heap_adt_def_path("alloc::vec::Vec"));
        assert!(supported_heap_adt_def_path("alloc[d734]::vec::Vec"));
        assert!(supported_heap_adt_def_path(
            "std::collections::hash::map::HashMap"
        ));
        assert!(supported_heap_adt_def_path("hashbrown::set::HashSet"));
        assert!(supported_heap_adt_def_path("indexmap::map::IndexMap"));
        assert!(supported_heap_adt_def_path("indexmap::set::IndexSet"));
        assert!(!supported_heap_adt_def_path(
            "outer::indexmap::map::IndexMap"
        ));
        assert!(supported_heap_adt_def_path("indexmap[1a2b]::set::IndexSet"));
        assert!(!supported_heap_adt_def_path("notindexmap::map::IndexMap"));
        assert!(!supported_heap_adt_def_path("indexmap::map::IndexMapLike"));
        assert!(!supported_heap_adt_def_path("indexmap::set::IndexSetExtra"));
    }

    #[test]
    fn canonical_mir_place_key_strips_copy_move_ref_and_type_wrappers() {
        assert_eq!(canonical_mir_place_key("copy ((*_12))"), "_12");
        assert_eq!(canonical_mir_place_key("move (*_9)"), "_9");
        assert_eq!(
            canonical_mir_place_key("(_7.0: std::alloc::Layout)"),
            "_7.0"
        );
    }

    #[test]
    fn place_lookup_keys_include_tuple_parent_after_canonicalization() {
        let keys = place_lookup_keys("copy ((_7.0: std::alloc::Layout))");
        assert!(
            keys.iter().any(|key| key == "_7"),
            "tuple field zero provenance should fall back to the parent place: {:?}",
            keys
        );

        let deref_keys = place_lookup_keys("((*_11).0: std::alloc::Layout)");
        assert!(
            deref_keys.iter().any(|key| key == "_11"),
            "tuple field zero through a reference temp should fall back to the dereferenced parent: {:?}",
            deref_keys
        );
    }

    #[test]
    fn place_heap_object_solution_matches_reference_and_tuple_projection_aliases() {
        let mut provenance = BTreeMap::new();
        provenance.insert("_3".to_string(), solution("layout-ref-source"));
        provenance.insert("_7".to_string(), solution("tuple-layout-source"));

        assert_eq!(
            place_heap_object_solution("(*_3)", &provenance)
                .expect("deref place should resolve")
                .object_type,
            "layout-ref-source"
        );
        assert_eq!(
            place_heap_object_solution("(_7.0: std::alloc::Layout)", &provenance)
                .expect("tuple field place should resolve through parent")
                .object_type,
            "tuple-layout-source"
        );
    }

    #[test]
    fn rustc_disambiguator_normalization_preserves_type_brackets() {
        let normalized = strip_rustc_crate_disambiguators(
            "alloc[d734]::alloc::alloc::<[u64; 16_usize], core[2f33]::alloc::Layout>",
        );
        assert!(normalized.contains("alloc::alloc::alloc"));
        assert!(normalized.contains("[u64; 16_usize]"));
        assert!(normalized.contains("core::alloc::Layout"));
    }

    #[test]
    fn direct_allocator_call_kind_matches_current_rustc_crate_paths() {
        assert_eq!(
            direct_allocator_call_kind(
                "Val(ZeroSized, FnDef(DefId(3:42 ~ alloc[d734]::alloc::alloc), []))",
            ),
            DirectAllocatorCallKind::LayoutAlloc
        );
        assert_eq!(
            direct_allocator_call_kind(
                "Val(ZeroSized, FnDef(DefId(3:43 ~ alloc[d734]::alloc::alloc_zeroed), []))",
            ),
            DirectAllocatorCallKind::LayoutAllocZeroed
        );
        assert_eq!(
            direct_allocator_call_kind(
                "Val(ZeroSized, FnDef(DefId(3:44 ~ alloc[d734]::alloc::dealloc), []))",
            ),
            DirectAllocatorCallKind::LayoutDealloc
        );
    }

    #[test]
    fn unresolved_realloc_and_dealloc_delegate_recovery_but_alloc_stays_typed() {
        for kind in [
            DirectAllocatorCallKind::LayoutRealloc,
            DirectAllocatorCallKind::LayoutDealloc,
            DirectAllocatorCallKind::GlobalAllocLayoutRealloc,
            DirectAllocatorCallKind::GlobalAllocLayoutDealloc,
        ] {
            let (type_id, basis) = direct_allocator_type_id(
                kind,
                UNKNOWN_HEAP_OBJECT_TYPE,
                "test_solved_basis",
                "same-callsite",
            );
            assert_eq!(type_id, 0);
            assert_eq!(basis, "direct_allocator_recovery_delegated");
            let symbol = direct_allocator_recovery_backed_replacement_symbol(kind);
            assert!(symbol.contains("_layout_with_metadata"));
            assert!(
                !symbol.ends_with("_local"),
                "delegated recovery must never select the no-recovery local ABI: {}",
                symbol
            );
        }

        for kind in [
            DirectAllocatorCallKind::LayoutAlloc,
            DirectAllocatorCallKind::LayoutAllocZeroed,
            DirectAllocatorCallKind::GlobalAllocLayoutAlloc,
            DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed,
        ] {
            let (type_id, basis) = direct_allocator_type_id(
                kind,
                UNKNOWN_HEAP_OBJECT_TYPE,
                "test_solved_basis",
                "same-callsite",
            );
            assert_ne!(
                type_id, 0,
                "unresolved alloc must create a recoverable identity"
            );
            assert_eq!(basis, "direct_allocator_callsite_key");
        }

        let (solved_type_id, solved_basis) = direct_allocator_type_id(
            DirectAllocatorCallKind::LayoutRealloc,
            "alloc::vec::Vec<u8>",
            "rustc_middle_test_basis",
            "same-callsite",
        );
        assert_ne!(solved_type_id, 0);
        assert_eq!(solved_basis, "rustc_middle_test_basis");
    }

    #[test]
    fn layout_generic_argument_parser_matches_current_rustc_impl_paths() {
        let callee =
            "Val(ZeroSized, FnDef(DefId(2:16920 ~ core[2f33]::alloc::layout::{impl#0}::array), [u64]))";
        assert_eq!(
            extract_layout_angle_argument(callee, "array").as_deref(),
            Some("u64")
        );

        let nested =
            "Val(ZeroSized, FnDef(DefId(2:16921 ~ core[2f33]::alloc::layout::{impl#0}::new), [[u64; 16_usize]]))";
        assert_eq!(
            extract_layout_angle_argument(nested, "new").as_deref(),
            Some("[u64; 16_usize]")
        );
    }

    #[test]
    fn layout_method_matcher_accepts_current_rustc_impl_paths() {
        let callee =
            "Val(ZeroSized, FnDef(DefId(2:16930 ~ core[2f33]::alloc::layout::{impl#0}::extend), []))";
        assert!(callee_mentions_rust_layout_method(callee, "extend"));
        assert!(!callee_mentions_rust_layout_method(callee, "extend_packed"));
    }

    #[test]
    fn result_option_layout_passthrough_matches_current_rustc_impl_paths() {
        let result_expect =
            "Val(ZeroSized, FnDef(DefId(2:11000 ~ core[2f33]::result::{impl#0}::expect), [std::alloc::Layout, std::alloc::LayoutError]))";
        let result_ok =
            "Val(ZeroSized, FnDef(DefId(2:11001 ~ core[2f33]::result::{impl#0}::ok), [std::alloc::Layout, std::alloc::LayoutError]))";
        let option_expect =
            "Val(ZeroSized, FnDef(DefId(2:12000 ~ core[2f33]::option::{impl#0}::expect), [std::alloc::Layout]))";

        assert!(result_layout_unwrap_call(result_expect));
        assert!(fallible_layout_passthrough_call(result_ok));
        assert!(fallible_layout_passthrough_call(option_expect));
    }

    #[test]
    fn receiver_mutating_matcher_accepts_current_rustc_impl_paths() {
        let vec_bare_push = "Vec::<u64>::push(move _9, move _10)";
        let vec_bare_nested_push =
            "Vec::<std::vec::Vec<u64, std::alloc::Global>>::push(move _9, move _10)";
        let vec_bare_shrink = "Vec::<u64>::shrink_to_fit(move _14)";
        let vec_current_push =
            "Val(ZeroSized, FnDef(DefId(3:8586 ~ alloc[d734]::vec::{impl#2}::push), [u64, std::alloc::Global]))";
        let vec_current_shrink =
            "Val(ZeroSized, FnDef(DefId(3:8610 ~ alloc[d734]::vec::{impl#2}::shrink_to_fit), [u64, std::alloc::Global]))";
        let string_push =
            "Val(ZeroSized, FnDef(DefId(3:6738 ~ alloc[d734]::string::{impl#0}::push_str), []))";
        let string_replace =
            "Val(ZeroSized, FnDef(DefId(3:6740 ~ alloc[d734]::string::{impl#0}::replace_range), [std::ops::Range<usize>]))";
        let string_bare_push =
            "String::push_str(move _3, const \"unialloc\") -> [return: bb2, unwind unreachable]";
        let string_bare_insert = "String::insert_str(move _5, const 0_usize, const \"prefix\")";
        let path_push =
            "Val(ZeroSized, FnDef(DefId(1:8080 ~ std[efc3]::path::{impl#7}::push), [&str]))";
        let path_bare_push = "PathBuf::push(move _3, const \"segment\")";
        let os_string_push =
            "Val(ZeroSized, FnDef(DefId(1:9090 ~ std[efc3]::ffi::os_str::{impl#1}::push), [&str]))";
        let os_string_bare_push = "OsString::push(move _3, const \"segment\")";
        let unrelated_push =
            "Val(ZeroSized, FnDef(DefId(9:1 ~ my_crate[0000]::bag::{impl#0}::push), []))";
        let unrelated_bare_push = "MyString::push_str(move _3, const \"x\")";

        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            vec_bare_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            vec_bare_nested_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            vec_bare_shrink
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            vec_current_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            vec_current_shrink
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            string_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            string_replace
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            string_bare_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            string_bare_insert
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            path_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            path_bare_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            os_string_push
        ));
        assert!(semantic_scope_receiver_mutating_allocation_like_call(
            os_string_bare_push
        ));
        assert!(!semantic_scope_current_impl_receiver_mutating_call(
            unrelated_push
        ));
        assert!(!semantic_scope_named_receiver_mutating_call(
            unrelated_bare_push
        ));
    }

    #[test]
    fn current_rustc_into_iterator_is_non_allocating_even_under_collect_module_path() {
        let range_into_iter =
            "Val(ZeroSized, FnDef(DefId(2:10010 ~ core[2f33]::iter::traits::collect::IntoIterator::into_iter), [std::ops::Range<usize>]))";
        let slice_iter_into_iter =
            "Val(ZeroSized, FnDef(DefId(2:10010 ~ core[2f33]::iter::traits::collect::IntoIterator::into_iter), [std::iter::Enumerate<std::slice::IterMut<'{erased}, u64>>]))";

        // The current rustc module path contains `::collect`, which is an
        // allocating marker for real `Iterator::collect` calls.  `IntoIterator`
        // itself only creates/adapts an iterator value and must be filtered
        // first, otherwise for-loops become unresolved heap-object candidates.
        assert!(semantic_scope_allocation_like_call(range_into_iter));
        assert!(semantic_scope_known_non_allocating_call(range_into_iter));
        assert!(semantic_scope_known_non_allocating_call(
            slice_iter_into_iter
        ));
    }

    #[test]
    fn cross_thread_matcher_accepts_current_rustc_spawn_paths() {
        let std_spawn =
            "Val(ZeroSized, FnDef(DefId(1:670 ~ std[efc3]::thread::spawn), [Closure(...), WorkerTrace]))";
        let std_functions_spawn =
            "Val(ZeroSized, FnDef(DefId(1:542 ~ std[efc3]::thread::functions::spawn), [Closure(...), WorkerTrace]))";
        let std_impl_spawn =
            "Val(ZeroSized, FnDef(DefId(1:671 ~ std[efc3]::thread::{impl#0}::spawn), [Closure(...), WorkerTrace]))";
        let scoped_spawn =
            "Val(ZeroSized, FnDef(DefId(1:733 ~ std[efc3]::thread::scoped::{impl#3}::spawn), ['{erased}, WorkerTrace]))";
        let scoped_constructor =
            "Val(ZeroSized, FnDef(DefId(1:712 ~ std[efc3]::thread::scoped::scope), ['{erased}, Closure(...), ()]))";
        let spawn_local = "Val(ZeroSized, FnDef(DefId(1:900 ~ tokio::task::spawn_local), []))";

        assert!(cross_thread_escape_call(std_spawn));
        assert!(cross_thread_escape_call(std_functions_spawn));
        assert!(cross_thread_escape_call(std_impl_spawn));
        assert!(cross_thread_escape_call(scoped_spawn));
        assert!(!cross_thread_escape_call(scoped_constructor));
        assert!(!cross_thread_escape_call(spawn_local));
    }
}

fn extract_angle_argument_after_marker(text: &str, marker: &str) -> Option<String> {
    let mut depth = 1usize;
    let start = text.find(marker)? + marker.len();
    let mut out = String::new();
    for ch in text[start..].chars() {
        match ch {
            '<' => {
                depth += 1;
                out.push(ch);
            }
            '>' => {
                depth -= 1;
                if depth == 0 {
                    return Some(out.trim().to_string());
                }
                out.push(ch);
            }
            _ => out.push(ch),
        }
    }
    None
}

fn first_top_level_list_item(list: &str) -> Option<String> {
    let mut angle_depth = 0isize;
    let mut paren_depth = 0isize;
    let mut bracket_depth = 0isize;
    let mut brace_depth = 0isize;
    let mut out = String::new();
    for ch in list.chars() {
        match ch {
            '<' => angle_depth += 1,
            '>' => angle_depth -= 1,
            '(' => paren_depth += 1,
            ')' => paren_depth -= 1,
            '[' => bracket_depth += 1,
            ']' => bracket_depth -= 1,
            '{' => brace_depth += 1,
            '}' => brace_depth -= 1,
            ',' if angle_depth == 0
                && paren_depth == 0
                && bracket_depth == 0
                && brace_depth == 0 =>
            {
                break;
            }
            _ => {}
        }
        out.push(ch);
    }
    let trimmed = out.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn bracketed_fndef_generic_list_after_marker(text: &str, marker: &str) -> Option<String> {
    let normalized = strip_rustc_crate_disambiguators(text);
    let marker_end = normalized.find(marker)? + marker.len();
    let after_marker = &normalized[marker_end..];
    let list_start = after_marker.find("), [")? + marker_end + "), [".len();
    let mut depth = 1isize;
    let mut out = String::new();
    for ch in normalized[list_start..].chars() {
        match ch {
            '[' => {
                depth += 1;
                out.push(ch);
            }
            ']' => {
                depth -= 1;
                if depth == 0 {
                    return Some(out);
                }
                out.push(ch);
            }
            _ => out.push(ch),
        }
    }
    None
}

fn extract_first_fndef_generic_argument_after_marker(text: &str, marker: &str) -> Option<String> {
    bracketed_fndef_generic_list_after_marker(text, marker)
        .as_deref()
        .and_then(first_top_level_list_item)
}

fn rust_layout_impl_method_marker(method: &str) -> String {
    format!("core::alloc::layout::{{impl#0}}::{}", method)
}

fn callee_mentions_rust_layout_impl_method(callee: &str, method: &str) -> bool {
    let marker = rust_layout_impl_method_marker(method);
    callee_contains_normalized(callee, &marker)
}

fn extract_layout_angle_argument(callee: &str, method: &str) -> Option<String> {
    [
        format!("Layout::{}::<", method),
        format!("std::alloc::Layout::{}::<", method),
        format!("alloc::alloc::Layout::{}::<", method),
        format!("core::alloc::Layout::{}::<", method),
    ]
    .iter()
    .find_map(|marker| extract_angle_argument_after_marker(callee, marker))
    .or_else(|| {
        extract_first_fndef_generic_argument_after_marker(
            callee,
            &rust_layout_impl_method_marker(method),
        )
    })
}

fn extract_mem_angle_argument(callee: &str, method: &str) -> Option<String> {
    [
        format!("std::mem::{}::<", method),
        format!("core::mem::{}::<", method),
        format!("{}::<", method),
    ]
    .iter()
    .find_map(|marker| extract_angle_argument_after_marker(callee, marker))
    .or_else(|| {
        let marker = format!("core::mem::{}", method);
        extract_first_fndef_generic_argument_after_marker(callee, &marker)
    })
}

fn const_usize_operand_label(operand: &Operand<'_>) -> Option<String> {
    // Do not scrape digits from arbitrary operand debug text: a runtime local
    // like `move _12` is not the constant length 12.  Only literal constants
    // may be promoted into the semantic Layout object label; everything else
    // must remain `<runtime-len>`.
    let text = match operand {
        Operand::Constant(constant) => const_operand_text(constant),
        _ => return None,
    };
    let mut digits = String::new();
    for ch in text.chars() {
        if ch.is_ascii_digit() {
            digits.push(ch);
        } else if !digits.is_empty() {
            break;
        }
    }
    if digits.is_empty() {
        None
    } else {
        Some(digits)
    }
}

fn layout_constructor_heap_object_solution(
    callee: &str,
    args: &[Operand<'_>],
) -> Option<HeapObjectSolution> {
    if let Some(element_ty) = extract_layout_angle_argument(callee, "array") {
        let len = args
            .get(0)
            .and_then(const_usize_operand_label)
            .unwrap_or_else(|| "<runtime-len>".to_string());
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::array::<{}>[{}]", element_ty, len),
            type_id_basis: "rustc_middle_mir_layout_constructor_heap_object_type",
        });
    }

    if let Some(value_ty) = extract_layout_angle_argument(callee, "new") {
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::new::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_constructor_heap_object_type",
        });
    }

    // `for_value_raw` contains `for_value`; match the raw-pointer constructor
    // first so pointer/slice-derived Layout objects are not mislabeled as
    // reference-derived `for_value` objects.
    if let Some(value_ty) = extract_layout_angle_argument(callee, "for_value_raw") {
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::for_value_raw::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_constructor_heap_object_type",
        });
    }

    if let Some(value_ty) = extract_layout_angle_argument(callee, "for_value") {
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::for_value::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_constructor_heap_object_type",
        });
    }

    None
}

fn layout_size_align_operand_heap_object_solution(callee: &str) -> Option<HeapObjectSolution> {
    // Many allocators manually rebuild a `Layout` from
    // `size_of::<T>()`/`align_of::<T>()` instead of starting from
    // `Layout::new::<T>()`.  MIR preserves these calls, so keep a typed
    // provenance token on the integer locals and let `Layout::from_size_align`
    // accept it only when both operands refer to the same `T`.
    extract_mem_angle_argument(callee, "size_of")
        .or_else(|| extract_mem_angle_argument(callee, "align_of"))
        .map(|value_ty| HeapObjectSolution {
            object_type: format!("std::mem::layout_of::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_size_align_operand_heap_object_type",
        })
}

fn layout_transformer_heap_object_solution(
    callee: &str,
    args: &[Operand<'_>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    // These Layout methods preserve the semantic heap object while changing
    // alignment/padding.  Without explicitly propagating provenance through
    // them, a real MIR sequence such as
    // `Layout::new::<T>().align_to(64).unwrap().pad_to_align()` reaches
    // `alloc(layout)` as an otherwise anonymous Layout and falls back to a
    // callsite-only type id.  Keep this narrow: methods returning a combined
    // tuple layout (extend/repeat) need projection-aware propagation before
    // they can be soundly enabled here.
    const LAYOUT_PRESERVING_TRANSFORMERS: &[&str] = &["align_to", "pad_to_align"];

    if LAYOUT_PRESERVING_TRANSFORMERS
        .iter()
        .any(|method| callee_mentions_rust_layout_method(callee, method))
    {
        return args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))
            .map(|mut solution| {
                solution.type_id_basis = "rustc_middle_mir_layout_transformer_heap_object_type";
                solution
            });
    }

    None
}

fn layout_reconstructed_heap_object_solution(
    callee: &str,
    args: &[Operand<'_>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    // `Layout::from_size_align(layout.size(), layout.align())` is common in
    // generic allocator code that normalizes/rebuilds a layout before passing
    // it to `alloc`.  Treat it as preserving the source heap object only when
    // both integer operands carry the same compiler-tracked Layout provenance;
    // size-only reconstruction is intentionally rejected because many Rust
    // types can share a size while requiring different alignment/padding.
    let reconstructs_layout = callee_mentions_rust_layout_method(callee, "from_size_align")
        || callee_mentions_rust_layout_method(callee, "from_size_align_unchecked");
    if !reconstructs_layout {
        return None;
    }

    let size_solution = args
        .get(0)
        .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
    let align_solution = args
        .get(1)
        .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
    if size_solution.object_type != align_solution.object_type {
        return None;
    }

    if size_solution.type_id_basis == "rustc_middle_mir_layout_size_align_operand_heap_object_type"
        && align_solution.type_id_basis
            == "rustc_middle_mir_layout_size_align_operand_heap_object_type"
    {
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::from_size_align({})",
                size_solution.object_type
            ),
            type_id_basis: "rustc_middle_mir_layout_size_align_constructor_heap_object_type",
        });
    }

    Some(HeapObjectSolution {
        object_type: format!(
            "std::alloc::Layout::from_size_align({})",
            size_solution.object_type
        ),
        type_id_basis: "rustc_middle_mir_layout_reconstructed_heap_object_type",
    })
}

fn layout_composite_heap_object_solution(
    callee: &str,
    args: &[Operand<'_>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    // `Layout::extend` and `Layout::repeat` return `Result<(Layout, usize),
    // LayoutError>`.  The allocator eventually consumes the tuple field `.0`,
    // so provenance has to survive both the Result unwrap and the tuple-field
    // projection.  The packed variants return a `Layout` directly but still
    // merge/repeat heap object semantics.  Model all of these as
    // compiler-visible composite heap objects instead of silently reusing
    // either input's type id.
    // Match `_packed` before the shorter method names: MIR debug text is
    // matched by substring, so `Layout::extend_packed` also contains
    // `Layout::extend`.  The order is therefore part of the soundness
    // contract for packed provenance accounting.
    if callee_mentions_rust_layout_method(callee, "extend_packed") {
        let head = args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        let tail = args
            .get(1)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::extend_packed({}, {})",
                head.object_type, tail.object_type
            ),
            type_id_basis: "rustc_middle_mir_layout_composite_heap_object_type",
        });
    }

    if callee_mentions_rust_layout_method(callee, "extend") {
        let head = args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        let tail = args
            .get(1)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::extend({}, {})",
                head.object_type, tail.object_type
            ),
            type_id_basis: "rustc_middle_mir_layout_composite_heap_object_type",
        });
    }

    if callee_mentions_rust_layout_method(callee, "repeat_packed") {
        let element = args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        let len = args
            .get(1)
            .and_then(const_usize_operand_label)
            .unwrap_or_else(|| "<runtime-len>".to_string());
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::repeat_packed({}; {})",
                element.object_type, len
            ),
            type_id_basis: "rustc_middle_mir_layout_composite_heap_object_type",
        });
    }

    if callee_mentions_rust_layout_method(callee, "repeat") {
        let element = args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))?;
        let len = args
            .get(1)
            .and_then(const_usize_operand_label)
            .unwrap_or_else(|| "<runtime-len>".to_string());
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::repeat({}; {})",
                element.object_type, len
            ),
            type_id_basis: "rustc_middle_mir_layout_composite_heap_object_type",
        });
    }

    None
}

fn callee_mentions_exact_method(callee: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let bytes = callee.as_bytes();
    for (index, _) in callee.match_indices(method) {
        if index < 2 || bytes.get(index - 2..index) != Some(b"::") {
            continue;
        }
        match callee[index + method.len()..].chars().next() {
            None | Some('}') | Some(')') | Some(',') | Some('(') | Some('<') | Some(' ')
            | Some(':') => return true,
            _ => {}
        }
    }
    false
}

fn callee_mentions_result_layout(callee: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    callee.contains("Layout")
        && (callee.contains("Result::<")
            || callee.contains("Result<")
            || callee.contains("std::result::Result")
            || callee.contains("core::result::Result")
            || callee.contains("core::result::{impl#"))
}

fn callee_mentions_option_layout(callee: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    callee.contains("Layout")
        && (callee.contains("Option::<")
            || callee.contains("Option<")
            || callee.contains("std::option::Option")
            || callee.contains("core::option::Option")
            || callee.contains("core::option::{impl#"))
}

fn result_layout_unwrap_call(callee: &str) -> bool {
    callee_mentions_result_layout(callee)
        && (callee_mentions_exact_method(callee, "expect")
            || callee_mentions_exact_method(callee, "unwrap"))
}

fn fallible_layout_passthrough_call(callee: &str) -> bool {
    let result_ok_passthrough =
        callee_mentions_result_layout(callee) && callee_mentions_exact_method(callee, "ok");
    let option_passthrough = callee_mentions_option_layout(callee)
        && (callee_mentions_exact_method(callee, "expect")
            || callee_mentions_exact_method(callee, "unwrap"));

    result_ok_passthrough || option_passthrough
}

fn result_layout_unwrap_heap_object_solution<'tcx>(
    callee: &str,
    args: &[Operand<'tcx>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    if result_layout_unwrap_call(callee) {
        args.get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))
    } else {
        None
    }
}

fn fallible_layout_passthrough_heap_object_solution<'tcx>(
    callee: &str,
    args: &[Operand<'tcx>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    if !fallible_layout_passthrough_call(callee) {
        return None;
    }
    args.get(0)
        .and_then(|arg| operand_heap_object_solution(arg, provenance))
        .map(|mut solution| {
            // `Result<Layout>::ok` and `Option<Layout>::expect/unwrap` return
            // the same Layout payload on the success path.  Propagating only
            // these exact passthrough calls is deliberately fail-closed: calls
            // with fallback/closure semantics such as `unwrap_or_else` are not
            // accepted because they may synthesize a different Layout.
            solution.type_id_basis =
                "rustc_middle_mir_layout_result_option_passthrough_heap_object_type";
            solution
        })
}

fn propagate_layout_like_call_provenance<'tcx>(
    callee: &str,
    args: &[Operand<'tcx>],
    destination: &Place<'tcx>,
    provenance: &mut BTreeMap<String, HeapObjectSolution>,
) {
    let destination_key = place_text(destination);
    let solved = layout_constructor_heap_object_solution(callee, args)
        .or_else(|| layout_size_align_operand_heap_object_solution(callee))
        .or_else(|| layout_transformer_heap_object_solution(callee, args, provenance))
        .or_else(|| layout_reconstructed_heap_object_solution(callee, args, provenance))
        .or_else(|| layout_composite_heap_object_solution(callee, args, provenance))
        .or_else(|| fallible_layout_passthrough_heap_object_solution(callee, args, provenance))
        .or_else(|| result_layout_unwrap_heap_object_solution(callee, args, provenance))
        .or_else(|| {
            if callee_mentions_rust_layout_method(callee, "size")
                || callee_mentions_rust_layout_method(callee, "align")
            {
                args.get(0)
                    .and_then(|arg| operand_heap_object_solution(arg, provenance))
            } else {
                None
            }
        });

    if let Some(solution) = solved {
        provenance.insert(destination_key, solution);
    } else {
        provenance.remove(&destination_key);
    }
}

fn propagate_layout_like_statement_provenance<'tcx>(
    statement: &rustc_middle::mir::Statement<'tcx>,
    provenance: &mut BTreeMap<String, HeapObjectSolution>,
) {
    let assigned = match &statement.kind {
        StatementKind::Assign(assigned) => assigned,
        _ => return,
    };
    let (destination, rvalue) = &**assigned;
    let destination_key = place_text(destination);
    let solved = match rvalue {
        #[cfg(unialloc_rustc_current)]
        Rvalue::Use(operand, _) => operand_heap_object_solution(operand, provenance),
        #[cfg(not(unialloc_rustc_current))]
        Rvalue::Use(operand) => operand_heap_object_solution(operand, provenance),
        Rvalue::Ref(_, _, place) => place_heap_object_solution(&place_text(place), provenance),
        _ => None,
    };
    if let Some(solution) = solved {
        provenance.insert(destination_key, solution);
    } else {
        provenance.remove(&destination_key);
    }
}

fn direct_allocator_layout_heap_object_solution(
    call_kind: DirectAllocatorCallKind,
    args: &[Operand<'_>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    match call_kind {
        DirectAllocatorCallKind::LayoutAlloc | DirectAllocatorCallKind::LayoutAllocZeroed => args
            .get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance)),
        DirectAllocatorCallKind::LayoutDealloc => args
            .get(1)
            .and_then(|arg| operand_heap_object_solution(arg, provenance)),
        DirectAllocatorCallKind::LayoutRealloc => args
            .get(2)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))
            .or_else(|| {
                args.get(1)
                    .and_then(|arg| operand_heap_object_solution(arg, provenance))
            }),
        DirectAllocatorCallKind::GlobalAllocLayoutAlloc
        | DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => args
            .get(1)
            .and_then(|arg| operand_heap_object_solution(arg, provenance)),
        DirectAllocatorCallKind::GlobalAllocLayoutDealloc => args
            .get(2)
            .and_then(|arg| operand_heap_object_solution(arg, provenance)),
        DirectAllocatorCallKind::GlobalAllocLayoutRealloc => args
            .get(3)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))
            .or_else(|| {
                args.get(2)
                    .and_then(|arg| operand_heap_object_solution(arg, provenance))
            }),
        DirectAllocatorCallKind::SizeAlignAlloc | DirectAllocatorCallKind::Unsupported => None,
    }
}

fn shallow_init_box_payload_after_call<'tcx>(
    body: &Body<'tcx>,
    destination_place: &str,
    target: Option<BasicBlock>,
) -> Option<HeapObjectSolution> {
    #[cfg(unialloc_rustc_current)]
    let _ = destination_place;
    let target = target?;
    for statement in &body[target].statements {
        let assigned = match &statement.kind {
            StatementKind::Assign(assigned) => assigned,
            _ => continue,
        };
        let (_, rvalue) = &**assigned;
        #[cfg(unialloc_rustc_current)]
        {
            let _ = rvalue;
            // ShallowInitBox no longer exists in current MIR.  Leave current
            // Box payload solving audit-only until a replacement pattern is
            // proven by a real rustc_driver fixture.
            continue;
        }
        #[cfg(not(unialloc_rustc_current))]
        if let Rvalue::ShallowInitBox(operand, payload_ty) = rvalue {
            if operand_place_text(operand).as_deref() == Some(destination_place) {
                return Some(HeapObjectSolution {
                    object_type: format!("std::boxed::Box<{:?}>", payload_ty),
                    type_id_basis: "rustc_middle_mir_shallow_init_box_heap_object_type",
                });
            }
        }
    }
    None
}

fn direct_allocator_semantic_object_type<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    call_kind: DirectAllocatorCallKind,
    destination_place: &str,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
    callee: &str,
    target: Option<BasicBlock>,
    layout_solution: Option<HeapObjectSolution>,
) -> HeapObjectSolution {
    if let Some(solved) = ty_heap_object_solution(tcx, destination_ty, argument_tys, callee) {
        return solved;
    }
    if let Some(solved) = layout_solution {
        return solved;
    }
    if call_kind == DirectAllocatorCallKind::SizeAlignAlloc {
        if let Some(solved) = shallow_init_box_payload_after_call(body, destination_place, target) {
            return solved;
        }
    }
    unknown_heap_object_solution()
}

fn semantic_scope_type_id(semantic_object_type: &str, callsite_key: &str) -> (u64, &'static str) {
    if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        (
            nonzero_fnv1a64_text(&format!(
                "mir-semantic-scope-callsite-fallback-v1\0{}",
                callsite_key
            )),
            "rustc_middle_type_solver_failed_callsite_fallback",
        )
    } else {
        (
            nonzero_fnv1a64_text(&format!(
                "mir-heap-object-type-v1\0{}",
                semantic_object_type
            )),
            "rustc_middle_ty_destination_or_argument_heap_object_type",
        )
    }
}

fn direct_allocator_type_id(
    call_kind: DirectAllocatorCallKind,
    semantic_object_type: &str,
    solved_type_id_basis: &'static str,
    callsite_key: &str,
) -> (u64, &'static str) {
    if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        if matches!(
            call_kind,
            DirectAllocatorCallKind::LayoutRealloc
                | DirectAllocatorCallKind::LayoutDealloc
                | DirectAllocatorCallKind::GlobalAllocLayoutRealloc
                | DirectAllocatorCallKind::GlobalAllocLayoutDealloc
        ) {
            (0, "direct_allocator_recovery_delegated")
        } else {
            (
                nonzero_fnv1a64_text(&format!("mir-rewrite-dry-run-v1\0{}", callsite_key)),
                "direct_allocator_callsite_key",
            )
        }
    } else {
        (
            nonzero_fnv1a64_text(&format!(
                "mir-heap-object-type-v1\0{}",
                semantic_object_type
            )),
            solved_type_id_basis,
        )
    }
}

fn child_def_id_by_name<'tcx>(tcx: TyCtxt<'tcx>, module: DefId, name: &str) -> Option<DefId> {
    for child in tcx.module_children(module).iter() {
        if child.ident.name.as_str() == name {
            if let Res::Def(_, def_id) = child.res {
                return Some(def_id);
            }
        }
    }
    None
}

fn child_def_id_in_alloc_api_or_type_isolation<'tcx>(
    tcx: TyCtxt<'tcx>,
    crate_root: DefId,
    name: &str,
) -> Option<DefId> {
    let alloc_api = child_def_id_by_name(tcx, crate_root, "alloc_api")?;
    child_def_id_by_name(tcx, alloc_api, name).or_else(|| {
        let type_isolation = child_def_id_by_name(tcx, alloc_api, "type_isolation")?;
        child_def_id_by_name(tcx, type_isolation, name)
    })
}

fn resolve_unialloc_alloc_metadata_abi_in_crate<'tcx>(
    tcx: TyCtxt<'tcx>,
    crate_root: DefId,
    allow_size_align_local: bool,
) -> Option<AllocMetadataAbi> {
    let symbol = direct_allocator_replacement_symbol_for_site(
        DirectAllocatorCallKind::SizeAlignAlloc,
        allow_size_align_local,
    );
    child_def_id_in_alloc_api_or_type_isolation(tcx, crate_root, symbol).map(|def_id| {
        AllocMetadataAbi {
            def_id,
            symbol,
            supports_hints: lowering_metadata_hints_requested(),
        }
    })
}

fn resolve_unialloc_alloc_metadata_abi<'tcx>(
    tcx: TyCtxt<'tcx>,
    allow_size_align_local: bool,
) -> Option<AllocMetadataAbi> {
    for krate in tcx.crates(()).iter().copied() {
        if tcx.crate_name(krate).as_str() == "unialloc" {
            if let Some(abi) = resolve_unialloc_alloc_metadata_abi_in_crate(
                tcx,
                krate.as_def_id(),
                allow_size_align_local,
            ) {
                return Some(abi);
            }
        }
    }
    None
}

fn resolve_unialloc_layout_metadata_abi_in_crate<'tcx>(
    tcx: TyCtxt<'tcx>,
    crate_root: DefId,
    kind: DirectAllocatorCallKind,
    force_recovery_backed: bool,
) -> Option<AllocMetadataAbi> {
    let symbol = if force_recovery_backed {
        direct_allocator_recovery_backed_replacement_symbol(kind)
    } else {
        direct_allocator_default_replacement_symbol(kind)
    };
    if symbol.starts_with('<') {
        return None;
    }
    child_def_id_in_alloc_api_or_type_isolation(tcx, crate_root, symbol).map(|def_id| {
        AllocMetadataAbi {
            def_id,
            symbol,
            supports_hints: lowering_metadata_hints_requested(),
        }
    })
}

fn resolve_unialloc_layout_metadata_abi<'tcx>(
    tcx: TyCtxt<'tcx>,
    kind: DirectAllocatorCallKind,
    force_recovery_backed: bool,
) -> Option<AllocMetadataAbi> {
    for krate in tcx.crates(()).iter().copied() {
        if tcx.crate_name(krate).as_str() == "unialloc" {
            if let Some(abi) = resolve_unialloc_layout_metadata_abi_in_crate(
                tcx,
                krate.as_def_id(),
                kind,
                force_recovery_backed,
            ) {
                return Some(abi);
            }
        }
    }
    None
}

fn resolve_unialloc_direct_allocator_abis<'tcx>(
    tcx: TyCtxt<'tcx>,
) -> DirectAllocatorReplacementAbis {
    DirectAllocatorReplacementAbis {
        size_align_alloc: resolve_unialloc_alloc_metadata_abi(tcx, true),
        conservative_size_align_alloc: resolve_unialloc_alloc_metadata_abi(tcx, false),
        layout_alloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutAlloc,
            false,
        ),
        layout_alloc_zeroed: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutAllocZeroed,
            false,
        ),
        layout_realloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutRealloc,
            false,
        ),
        layout_dealloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutDealloc,
            false,
        ),
        recovery_backed_layout_realloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutRealloc,
            true,
        ),
        recovery_backed_layout_dealloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutDealloc,
            true,
        ),
    }
}

fn direct_allocator_replacement_abi(
    abis: DirectAllocatorReplacementAbis,
    kind: DirectAllocatorCallKind,
    allow_size_align_local: bool,
    force_recovery_backed: bool,
) -> Option<AllocMetadataAbi> {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    match kind {
        DirectAllocatorCallKind::SizeAlignAlloc if allow_size_align_local => abis.size_align_alloc,
        DirectAllocatorCallKind::SizeAlignAlloc => abis.conservative_size_align_alloc,
        DirectAllocatorCallKind::LayoutAlloc => abis.layout_alloc,
        DirectAllocatorCallKind::LayoutAllocZeroed => abis.layout_alloc_zeroed,
        DirectAllocatorCallKind::LayoutRealloc if force_recovery_backed => {
            abis.recovery_backed_layout_realloc
        }
        DirectAllocatorCallKind::LayoutDealloc if force_recovery_backed => {
            abis.recovery_backed_layout_dealloc
        }
        DirectAllocatorCallKind::LayoutRealloc => abis.layout_realloc,
        DirectAllocatorCallKind::LayoutDealloc => abis.layout_dealloc,
        DirectAllocatorCallKind::Unsupported => None,
        _ => unreachable!("GlobalAlloc call kinds are mapped to Layout ABI call kinds"),
    }
}

fn resolve_unialloc_semantic_scope_in_crate<'tcx>(
    tcx: TyCtxt<'tcx>,
    crate_root: DefId,
    local_no_recovery: bool,
) -> Option<SemanticScopeAbi> {
    let pop = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        "__unialloc_semantic_scope_pop",
    )?;
    let supports_hints = lowering_metadata_hints_requested();
    let push_symbol = semantic_scope_push_symbol(local_no_recovery);

    child_def_id_in_alloc_api_or_type_isolation(tcx, crate_root, push_symbol).map(|push_def_id| {
        SemanticScopeAbi {
            push_def_id,
            pop_def_id: pop,
            push_symbol,
            supports_hints,
            local_no_recovery,
        }
    })
}

fn resolve_unialloc_semantic_scope<'tcx>(
    tcx: TyCtxt<'tcx>,
    local_no_recovery: bool,
) -> Option<SemanticScopeAbi> {
    for krate in tcx.crates(()).iter().copied() {
        if tcx.crate_name(krate).as_str() == "unialloc" {
            if let Some(def_ids) =
                resolve_unialloc_semantic_scope_in_crate(tcx, krate.as_def_id(), local_no_recovery)
            {
                return Some(def_ids);
            }
        }
    }
    None
}

fn semantic_scope_candidate_for_mir<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee: &str,
    destination_ty: Ty<'tcx>,
) -> bool {
    if direct_allocator_call(callee) {
        return false;
    }
    if semantic_scope_explicit_drop_call(callee) {
        return true;
    }
    if semantic_scope_deallocation_like_call(callee) {
        return true;
    }
    if semantic_scope_known_non_allocating_call(callee)
        || semantic_scope_receiver_consuming_wait_call(callee)
    {
        return false;
    }
    if semantic_scope_allocation_like_call(callee)
        || semantic_scope_receiver_mutating_allocation_like_call(callee)
        || semantic_scope_returns_owned_heap_container(callee)
    {
        return true;
    }

    // Do not rely only on rustc debug text for candidate discovery.  Indirect
    // calls, generic factories, and wrapper functions may not expose an owned
    // heap return marker in the callee text, while the MIR destination still has
    // the exact rustc_middle type.  Actual lowering remains gated by the same
    // heap-object solver used below, so non-heap returns stay out of evidence.
    heap_object_type_from_ty(tcx, destination_ty).is_some()
}

fn semantic_scope_explicit_drop_call(callee: &str) -> bool {
    callee.contains("std::mem::drop") || callee.contains("core::mem::drop")
}

fn semantic_scope_deallocation_like_call(callee: &str) -> bool {
    // These calls can free collection backing storage or internal nodes without
    // looking allocation-like at the API boundary.  They intentionally run
    // before semantic_scope_known_non_allocating_call so BTreeMap/LinkedList
    // remove/pop/clear paths are not silently hidden from deallocation-type
    // evidence.  The rustc_middle heap-object solver below still gates actual
    // lowering to supported heap-owner ADTs, so unrelated helper calls become
    // explicit unsolved audit rows rather than typed evidence.
    const DEALLOCATION_MARKERS: &[&str] = &[
        "::clear}",
        "::truncate}",
        "::shrink_to}",
        "::shrink_to_fit}",
        "::remove",
        "::remove_entry",
        "::pop}",
        "::pop_back}",
        "::pop_front}",
        "::pop_first}",
        "::pop_last}",
        "::split_off}",
        "::drain",
        "::extract_if",
        "::retain}",
    ];
    DEALLOCATION_MARKERS
        .iter()
        .any(|marker| callee.contains(marker))
}

fn semantic_scope_receiver_mutating_allocation_like_call(callee: &str) -> bool {
    // Calls such as Vec::push or BTreeMap::insert may return unit/Option<V>,
    // but the semantic allocation context belongs to the heap-owning receiver.
    // Constructors and factories (`with_capacity`, `collect`, `from_iter`,
    // `Box::new`, `Rc::new`, `Arc::new`, `to_vec`) keep destination-first
    // solving through `semantic_heap_object_type_from_mir`.
    const RECEIVER_MUTATING_ALLOCATION_MARKERS: &[&str] = &[
        "::reserve",
        "::reserve_exact",
        "::try_reserve",
        "::try_reserve_exact",
        "::push}",
        "::push::<",
        "::push_back}",
        "::push_front}",
        "::insert}",
        "::append",
        "::extend",
        "::resize",
        "::resize_with",
        "::clone_from",
        "::push_str}",
        "::insert_str}",
        "::replace_range}",
    ];
    RECEIVER_MUTATING_ALLOCATION_MARKERS
        .iter()
        .any(|marker| callee.contains(marker))
        || semantic_scope_named_receiver_mutating_call(callee)
        || semantic_scope_current_impl_receiver_mutating_call(callee)
}

fn semantic_scope_receiver_heap_owner_call(callee: &str) -> bool {
    semantic_scope_explicit_drop_call(callee)
        || semantic_scope_deallocation_like_call(callee)
        || semantic_scope_receiver_mutating_allocation_like_call(callee)
}

const VEC_RECEIVER_GROW_METHODS: &[&str] = &[
    "push",
    "reserve",
    "reserve_exact",
    "try_reserve",
    "try_reserve_exact",
    "insert",
    "append",
    "extend",
    "extend_from_slice",
    "resize",
    "resize_with",
    "shrink_to",
    "shrink_to_fit",
];

fn semantic_scope_current_impl_receiver_mutating_call(callee: &str) -> bool {
    // Current rustc debug strings identify many inherent impl methods by the
    // implementation module (`alloc::string::{impl#0}::push_str`) rather than
    // by the public type spelling (`String::push_str`).  Keep this list small
    // and receiver-owned: the MIR argument-type solver still decides the exact
    // heap object, but candidate discovery must not miss mutating grow calls
    // whose return type is just `()`.
    const CURRENT_IMPL_RECEIVER_METHODS: &[(&str, &[&str])] = &[
        ("alloc::vec", VEC_RECEIVER_GROW_METHODS),
        (
            "alloc::string",
            &["push_str", "insert_str", "replace_range"],
        ),
        ("std::path", &["push"]),
        ("std::ffi::os_str", &["push"]),
    ];

    CURRENT_IMPL_RECEIVER_METHODS
        .iter()
        .any(|(module, methods)| {
            methods
                .iter()
                .any(|method| callee_contains_current_impl_method(callee, module, method))
        })
}

fn semantic_scope_named_receiver_mutating_call(callee: &str) -> bool {
    // The MIR pretty-printer used by `-Zdump-mir` and some rustc debug paths
    // can surface receiver calls as public type aliases (`String::push_str`)
    // instead of the crate/internal impl path (`alloc::string::{impl#0}`).
    // Keep this allow-list method-specific and receiver-owned; actual lowering
    // is still gated by rustc_middle argument types, so matching a call here is
    // only candidate discovery, not type attribution.
    const NAMED_RECEIVER_METHODS: &[(&str, &[&str])] = &[
        ("Vec", VEC_RECEIVER_GROW_METHODS),
        ("std::vec::Vec", VEC_RECEIVER_GROW_METHODS),
        ("alloc::vec::Vec", VEC_RECEIVER_GROW_METHODS),
        ("String", &["push_str", "insert_str", "replace_range"]),
        (
            "std::string::String",
            &["push_str", "insert_str", "replace_range"],
        ),
        (
            "alloc::string::String",
            &["push_str", "insert_str", "replace_range"],
        ),
        ("PathBuf", &["push"]),
        ("std::path::PathBuf", &["push"]),
        ("OsString", &["push"]),
        ("std::ffi::OsString", &["push"]),
        ("std::ffi::os_str::OsString", &["push"]),
    ];

    NAMED_RECEIVER_METHODS.iter().any(|(receiver, methods)| {
        methods
            .iter()
            .any(|method| callee_contains_named_receiver_method(callee, receiver, method))
    })
}

fn callee_mentions_heap_owner_path(callee: &str, owner_path: &str) -> bool {
    // rustc debug strings are not consistent about the generic separator:
    // constructor callees normally look like `BTreeSet::<T>::new}`, while
    // destination/return fragments look like `BTreeSet<T>`.  Accept both
    // shapes and the non-generic associated-function form (`String::new}`).
    for (idx, _) in callee.match_indices(owner_path) {
        let after_path = &callee[idx + owner_path.len()..];
        if after_path.starts_with('<') || after_path.starts_with("::") {
            return true;
        }
    }
    false
}

fn semantic_scope_zero_allocation_heap_constructor_call(callee: &str) -> bool {
    // These constructors create an empty heap-owning value but do not allocate
    // backing storage.  The broader MIR destination-type fallback below would
    // otherwise wrap calls such as BTreeMap::new or Vec::new, bloating the
    // compiler type map with static pairs that can never produce runtime
    // allocation events.  Keep allocating constructors such as Box/Rc/Arc::new
    // out of this allowlist.
    const ZERO_ALLOC_HEAP_OWNER_PATHS: &[&str] = &[
        "std::vec::Vec",
        "alloc::vec::Vec",
        "std::string::String",
        "alloc::string::String",
        "std::collections::VecDeque",
        "alloc::collections::VecDeque",
        "std::collections::BinaryHeap",
        "alloc::collections::BinaryHeap",
        "std::collections::BTreeMap",
        "alloc::collections::BTreeMap",
        "std::collections::BTreeSet",
        "alloc::collections::BTreeSet",
        "std::collections::LinkedList",
        "alloc::collections::LinkedList",
        "std::collections::HashMap",
        "hashbrown::map::HashMap",
        "std::collections::HashSet",
        "hashbrown::set::HashSet",
        "std::path::PathBuf",
        "std::ffi::OsString",
        "std::ffi::os_str::OsString",
    ];
    const ZERO_ALLOC_CONSTRUCTOR_MARKERS: &[&str] = &["::new}", "::default}", "::with_hasher}"];

    ZERO_ALLOC_HEAP_OWNER_PATHS
        .iter()
        .any(|owner| callee_mentions_heap_owner_path(callee, owner))
        && ZERO_ALLOC_CONSTRUCTOR_MARKERS
            .iter()
            .any(|marker| callee.contains(marker))
}

fn callee_mentions_rust_layout_method(callee: &str, method: &str) -> bool {
    // Keep the fallback `Layout::method` spelling for rustc debug strings, but
    // do not let arbitrary project types whose names merely end in `Layout`
    // satisfy the bare marker.
    if callee_mentions_rust_layout_impl_method(callee, method) {
        return true;
    }
    let callee = strip_rustc_crate_disambiguators(callee);
    let bare_marker = format!("Layout::{}", method);
    for (idx, _) in callee.match_indices(&bare_marker) {
        if idx == 0 {
            return true;
        }
        let before = &callee[..idx];
        if before.ends_with("std::alloc::")
            || before.ends_with("alloc::alloc::")
            || before.ends_with("core::alloc::")
            || before.ends_with('{')
            || before.ends_with(' ')
            || before.ends_with('<')
            || before.ends_with('(')
        {
            return true;
        }
    }
    false
}

fn semantic_scope_layout_value_call(callee: &str) -> bool {
    // `std::alloc::Layout` constructors/transformers/accessors only produce or
    // inspect value-level layout descriptors.  They may carry heap-object
    // provenance for the direct allocator pass, but they are not heap-object
    // lifetime boundaries themselves.  Without this narrow exclusion, broad
    // allocation-like markers such as `::extend` turn Layout::extend-style value
    // operations into unresolved semantic-scope candidates.
    const LAYOUT_VALUE_METHODS: &[&str] = &[
        "new",
        "array",
        "for_value",
        "for_value_raw",
        "from_size_align",
        "from_size_align_unchecked",
        "extend",
        "extend_packed",
        "repeat",
        "repeat_packed",
        "align_to",
        "pad_to_align",
        "size",
        "align",
    ];
    LAYOUT_VALUE_METHODS
        .iter()
        .any(|method| callee_mentions_rust_layout_method(callee, method))
}

fn semantic_scope_receiver_consuming_wait_call(callee: &str) -> bool {
    // `JoinHandle::join` and scoped variants return a `Result<T, Box<dyn Any +
    // Send>>`, so the broad destination-type fallback can misclassify them as
    // fresh Box allocation scopes.  In the successful path they primarily wait
    // for the worker and release the thread runtime's existing Arc-backed inner
    // state.  Wrapping the whole call in `Box<dyn Any>` metadata therefore
    // poisons internal deallocations with the panic-payload type and causes
    // recovery identity mismatches.  Treat these receiver-consuming wait calls
    // as lifetime/deallocation boundaries handled by recovery records, not as
    // allocation scopes inferred from their error return type.
    const JOIN_RECEIVER_MARKERS: &[(&str, &str)] = &[
        ("std::thread::JoinHandle", "join"),
        ("std::thread::ScopedJoinHandle", "join"),
        ("std::thread::scoped::ScopedJoinHandle", "join"),
        ("crossbeam::thread::ScopedJoinHandle", "join"),
    ];
    JOIN_RECEIVER_MARKERS
        .iter()
        .any(|(receiver_path, method)| callee_contains_path_method(callee, receiver_path, method))
}

fn semantic_scope_known_non_allocating_call(callee: &str) -> bool {
    if semantic_scope_zero_allocation_heap_constructor_call(callee) {
        return true;
    }
    if semantic_scope_layout_value_call(callee) {
        return true;
    }

    const NON_ALLOCATING_MARKERS: &[&str] = &[
        "test::black_box",
        "test::Bencher::iter",
        "std::mem::drop",
        "core::mem::drop",
        "std::mem::forget",
        "core::mem::forget",
        "test::Bencher",
        "std::ops::Deref",
        "core::ops::Deref",
        "std::ops::Index",
        "core::ops::Index",
        " as std::iter::Iterator>::next",
        " as core::iter::Iterator>::next",
        " as std::iter::Iterator>::map::<",
        " as core::iter::Iterator>::map::<",
        " as std::iter::Iterator>::copied::<",
        " as core::iter::Iterator>::copied::<",
        " as std::iter::Iterator>::rev}",
        " as core::iter::Iterator>::rev}",
        " as std::iter::Iterator>::try_fold::<",
        " as core::iter::Iterator>::try_fold::<",
        " as std::iter::IntoIterator>::into_iter",
        " as core::iter::IntoIterator>::into_iter",
        // Current rustc debug strings for `for` loops and slice/range
        // iteration often use the trait's defining module instead of the old
        // fully-qualified `<T as IntoIterator>::into_iter` spelling, e.g.
        // `core::iter::traits::collect::IntoIterator::into_iter`.  This call
        // creates or adapts an iterator view; it does not allocate fresh heap
        // storage, so treating it as an unresolved heap-object scope only adds
        // false negative noise to the compiler lowering audit.
        "iter::traits::collect::IntoIterator::into_iter",
        "::len}",
        "::capacity}",
        "::is_empty}",
        "::as_ptr}",
        "::as_mut_ptr}",
        "::as_slice}",
        "::as_mut_slice}",
        "::iter}",
        "::iter_mut}",
        "::get}",
        "::get::<",
        "::get_key_value::<",
        "::get_mut}",
        "::get_mut::<",
        "::first}",
        "::last}",
        "::unwrap}",
        "::contains",
        "::contains::<",
        "::binary_search",
        "::range::<",
        "::first_key_value}",
        "::last_key_value}",
        "::intersection}",
        "::difference}",
        "::symmetric_difference}",
        "::union}",
        "::count}",
        "::peek}",
        "::pop}",
        "::pop_back}",
        "::pop_front}",
        "::peek_mut}",
        "::remove",
    ];
    NON_ALLOCATING_MARKERS
        .iter()
        .any(|marker| callee.contains(marker))
}

fn semantic_scope_allocation_like_call(callee: &str) -> bool {
    const ALLOCATION_MARKERS: &[&str] = &[
        "::with_capacity",
        "::with_capacity_in",
        "::reserve",
        "::reserve_exact",
        "::try_reserve",
        "::try_reserve_exact",
        "::push}",
        "::push::<",
        "::push_back}",
        "::push_front}",
        "::insert}",
        "::append",
        "::extend",
        "::resize",
        "::resize_with",
        "::push_str}",
        "::insert_str}",
        "::replace_range}",
        "::clone}",
        "::clone_from",
        "::to_vec",
        "::into_boxed_slice",
        "::collect",
        "::from_iter",
        "std::vec::from_elem",
        "alloc::vec::from_elem",
        "String as std::convert::From",
        "String as core::convert::From",
        "Box::<",
        "::new_in",
    ];
    ALLOCATION_MARKERS
        .iter()
        .any(|marker| callee.contains(marker))
}

fn semantic_scope_returns_owned_heap_container(callee: &str) -> bool {
    HEAP_OBJECT_SUPPORT.iter().any(|support| {
        support
            .owned_return_markers
            .iter()
            .any(|marker| callee.contains(marker))
    }) || callee_return_type_text(callee)
        .as_deref()
        .map(looks_like_heap_object_type)
        .unwrap_or(false)
}

fn path_marker_has_boundary(text: &str, marker_start: usize, marker: &str) -> bool {
    let suffix = &text[marker_start + marker.len()..];
    if suffix.starts_with("::<") {
        return true;
    }
    match suffix.chars().next() {
        None => true,
        Some('<' | '{' | '(' | ',' | ' ' | '\t' | '\n' | '}') => true,
        Some(ch) => !ch.is_ascii_alphanumeric() && ch != '_' && ch != ':',
    }
}

fn callee_contains_current_impl_method(callee: &str, module_path: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let impl_marker = format!("{}::{{impl#", module_path);
    let method_marker = format!("::{}", method);

    callee.match_indices(&impl_marker).any(|(idx, _)| {
        let rest = &callee[idx + impl_marker.len()..];
        let impl_close = match rest.find('}') {
            Some(impl_close) => impl_close,
            None => return false,
        };
        let after_impl = &rest[impl_close + 1..];
        after_impl
            .match_indices(&method_marker)
            .next()
            .map(|(method_idx, _)| path_marker_has_boundary(after_impl, method_idx, &method_marker))
            .unwrap_or(false)
    })
}

fn marker_has_prefix_boundary(text: &str, marker_start: usize) -> bool {
    if marker_start == 0 {
        return true;
    }
    let prefix = &text[..marker_start];
    match prefix.chars().next_back() {
        None => true,
        Some(ch) => !ch.is_ascii_alphanumeric() && ch != '_' && ch != ':',
    }
}

fn callee_contains_named_receiver_method(callee: &str, receiver_path: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let marker = format!("{}::{}", receiver_path, method);
    if callee.match_indices(&marker).any(|(idx, _)| {
        marker_has_prefix_boundary(&callee, idx) && path_marker_has_boundary(&callee, idx, &marker)
    }) {
        return true;
    }

    let generic_prefix = format!("{}::<", receiver_path);
    let method_marker = format!("::{}", method);
    callee.match_indices(&generic_prefix).any(|(idx, _)| {
        if !marker_has_prefix_boundary(&callee, idx) {
            return false;
        }
        let rest = &callee[idx + generic_prefix.len()..];
        let mut depth = 1usize;
        for (offset, ch) in rest.char_indices() {
            match ch {
                '<' => depth += 1,
                '>' => {
                    depth -= 1;
                    if depth == 0 {
                        let after_generics = &rest[offset + 1..];
                        return after_generics.starts_with(&method_marker)
                            && path_marker_has_boundary(after_generics, 0, &method_marker);
                    }
                }
                _ => {}
            }
        }
        false
    })
}

fn callee_contains_path_method(callee: &str, receiver_path: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let plain = format!("{}::{}", receiver_path, method);
    if callee.match_indices(&plain).any(|(idx, _)| {
        marker_has_prefix_boundary(&callee, idx) && path_marker_has_boundary(&callee, idx, &plain)
    }) {
        return true;
    }

    let generic = format!("{}::<", receiver_path);
    let method_marker = format!("::{}", method);
    callee.match_indices(&generic).any(|(idx, _)| {
        if !marker_has_prefix_boundary(&callee, idx) {
            return false;
        }
        let rest = &callee[idx + generic.len()..];
        rest.match_indices(&method_marker)
            .any(|(method_idx, _)| path_marker_has_boundary(rest, method_idx, &method_marker))
    })
}

fn cross_thread_escape_call(callee: &str) -> bool {
    // Keep this deliberately marker-specific.  The pass uses this predicate to
    // decide when solved heap-object scopes need UniAlloc's cross-thread
    // recovery placement bit, so broad substring matches such as `spawn` would
    // over-tag unrelated APIs and pollute heap-object metadata.  The entries
    // below name MIR-visible standard-library unscoped thread/task spawn forms
    // and similarly explicit task-spawn APIs from common runtimes. Scope
    // constructors such as `std::thread::scope`, `crossbeam::thread::scope`, and
    // `rayon::scope` are intentionally not markers: without an actual
    // `Scope::spawn` call, the body-level fallback could over-tag unrelated heap
    // objects that merely coexist with a local scope constructor. Function
    // markers are boundary-checked too: `tokio::task::spawn` is cross-thread,
    // but `tokio::task::spawn_local` is not.
    const CROSS_THREAD_FUNCTION_MARKERS: &[&str] = &[
        "std::thread::spawn",
        "std::thread::functions::spawn",
        "rayon::spawn",
        "rayon::spawn_fifo",
        "rayon_core::spawn",
        "rayon_core::spawn_fifo",
        "tokio::task::spawn",
        "tokio::task::spawn_blocking",
        "async_std::task::spawn",
        "smol::spawn",
    ];
    const CROSS_THREAD_METHOD_MARKERS: &[(&str, &str)] = &[
        ("std::thread::Builder", "spawn"),
        ("std::thread::Builder", "spawn_unchecked"),
        ("std::thread::Builder", "spawn_scoped"),
        ("std::thread::Scope", "spawn"),
        ("std::thread::Scope", "spawn_unchecked"),
        ("std::thread::scoped::Scope", "spawn"),
        ("std::thread::scoped::Scope", "spawn_unchecked"),
        ("crossbeam::thread::Scope", "spawn"),
        ("rayon::Scope", "spawn"),
        ("rayon::ScopeFifo", "spawn_fifo"),
        ("tokio::runtime::Handle", "spawn"),
        ("tokio::runtime::Handle", "spawn_blocking"),
    ];
    const CURRENT_IMPL_METHOD_MARKERS: &[(&str, &str)] = &[
        // Some current-nightly std thread entry points appear as an impl module
        // path rather than the free-function spelling after rustc lowering.
        ("std::thread", "spawn"),
        ("std::thread", "spawn_unchecked"),
        // Current rustc often prints scoped-thread methods by implementation
        // module, e.g. `std[... ]::thread::scoped::{impl#3}::spawn`, not by the
        // public receiver spelling `std::thread::Scope::spawn`.
        ("std::thread::scoped", "spawn"),
        ("std::thread::scoped", "spawn_unchecked"),
    ];
    let normalized = strip_rustc_crate_disambiguators(callee);
    CROSS_THREAD_FUNCTION_MARKERS.iter().any(|marker| {
        normalized
            .match_indices(marker)
            .any(|(idx, _)| path_marker_has_boundary(&normalized, idx, marker))
    }) || CROSS_THREAD_METHOD_MARKERS
        .iter()
        .any(|(receiver_path, method)| {
            callee_contains_path_method(&normalized, receiver_path, method)
        })
        || CURRENT_IMPL_METHOD_MARKERS.iter().any(|(module, method)| {
            callee_contains_current_impl_method(&normalized, module, method)
        })
}

#[cfg(unialloc_rustc_current)]
fn call_arg_operands<'tcx>(args: &[Spanned<Operand<'tcx>>]) -> Vec<Operand<'tcx>> {
    args.iter().map(|arg| arg.node.clone()).collect()
}

#[cfg(not(unialloc_rustc_current))]
fn call_arg_operands<'tcx>(args: &[Operand<'tcx>]) -> Vec<Operand<'tcx>> {
    args.to_vec()
}

#[cfg(unialloc_rustc_current)]
fn clone_call_arg_operand<'tcx>(args: &[Spanned<Operand<'tcx>>], index: usize) -> Operand<'tcx> {
    args[index].node.clone()
}

#[cfg(not(unialloc_rustc_current))]
fn clone_call_arg_operand<'tcx>(args: &[Operand<'tcx>], index: usize) -> Operand<'tcx> {
    args[index].clone()
}

#[cfg(unialloc_rustc_current)]
fn make_call_args<'tcx>(args: Vec<Operand<'tcx>>, span: Span) -> Box<[Spanned<Operand<'tcx>>]> {
    args.into_iter()
        .map(|node| Spanned { node, span })
        .collect::<Vec<_>>()
        .into_boxed_slice()
}

#[cfg(not(unialloc_rustc_current))]
fn make_call_args<'tcx>(args: Vec<Operand<'tcx>>, _span: Span) -> Vec<Operand<'tcx>> {
    args
}

#[cfg(unialloc_rustc_current)]
fn no_unwind() -> MirUnwind {
    UnwindAction::Continue
}

#[cfg(not(unialloc_rustc_current))]
fn no_unwind() -> MirUnwind {
    None
}

#[cfg(unialloc_rustc_current)]
fn cleanup_unwind(target: Option<BasicBlock>) -> MirUnwind {
    target
        .map(UnwindAction::Cleanup)
        .unwrap_or(UnwindAction::Continue)
}

#[cfg(not(unialloc_rustc_current))]
fn cleanup_unwind(target: Option<BasicBlock>) -> MirUnwind {
    target
}

#[cfg(unialloc_rustc_current)]
fn unwind_cleanup_target(unwind: MirUnwind) -> Option<BasicBlock> {
    match unwind {
        UnwindAction::Cleanup(block) => Some(block),
        _ => None,
    }
}

#[cfg(not(unialloc_rustc_current))]
fn unwind_cleanup_target(unwind: MirUnwind) -> Option<BasicBlock> {
    unwind
}

fn inserted_call_unwind(original_unwind: MirUnwind, is_cleanup: bool) -> MirUnwind {
    if is_cleanup {
        no_unwind()
    } else {
        original_unwind
    }
}

#[cfg(unialloc_rustc_current)]
fn set_unwind_cleanup(unwind: &mut MirUnwind, cleanup: BasicBlock) {
    *unwind = UnwindAction::Cleanup(cleanup);
}

#[cfg(not(unialloc_rustc_current))]
fn set_unwind_cleanup(unwind: &mut MirUnwind, cleanup: BasicBlock) {
    *unwind = Some(cleanup);
}

fn unit_ty<'tcx>(tcx: TyCtxt<'tcx>) -> Ty<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        tcx.types.unit
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        tcx.mk_unit()
    }
}

fn unialloc_function_handle<'tcx>(tcx: TyCtxt<'tcx>, def_id: DefId, span: Span) -> Operand<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        Operand::function_handle(
            tcx,
            def_id,
            std::iter::empty::<ty::GenericArg<'tcx>>(),
            span,
        )
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        Operand::function_handle(tcx, def_id, tcx.intern_substs(&[]), span)
    }
}

#[cfg(unialloc_rustc_current)]
fn synthetic_call_source() -> MirCallSource {
    CallSource::Misc
}

#[cfg(not(unialloc_rustc_current))]
fn synthetic_call_source() -> MirCallSource {
    false
}

#[cfg(unialloc_rustc_current)]
fn optimized_mir_def_id_to_def_id(def_id: OptimizedMirDefId) -> DefId {
    def_id.to_def_id()
}

#[cfg(not(unialloc_rustc_current))]
fn optimized_mir_def_id_to_def_id(def_id: OptimizedMirDefId) -> DefId {
    def_id
}

fn call_terminator_kind<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    args: Vec<Operand<'tcx>>,
    destination: Place<'tcx>,
    target: Option<BasicBlock>,
    unwind: MirUnwind,
    from_hir_call: MirCallSource,
    fn_span: Span,
) -> TerminatorKind<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        let _ = from_hir_call;
        TerminatorKind::Call {
            func: unialloc_function_handle(tcx, def_id, fn_span),
            args: make_call_args(args, fn_span),
            destination,
            target,
            unwind,
            call_source: CallSource::Misc,
            fn_span,
        }
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        TerminatorKind::Call {
            func: unialloc_function_handle(tcx, def_id, fn_span),
            args: make_call_args(args, fn_span),
            destination,
            target,
            cleanup: unwind,
            from_hir_call,
            fn_span,
        }
    }
}

#[cfg(unialloc_rustc_current)]
fn basic_block_data<'tcx>(terminator: Terminator<'tcx>, is_cleanup: bool) -> BasicBlockData<'tcx> {
    BasicBlockData::new_stmts(Vec::new(), Some(terminator), is_cleanup)
}

#[cfg(not(unialloc_rustc_current))]
fn basic_block_data<'tcx>(terminator: Terminator<'tcx>, is_cleanup: bool) -> BasicBlockData<'tcx> {
    BasicBlockData {
        statements: Vec::new(),
        terminator: Some(terminator),
        is_cleanup,
    }
}

fn body_contains_cross_thread_escape_call(body: &Body<'_>) -> bool {
    body_basic_blocks!(body).iter().any(|data| {
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => return false,
        };
        match &terminator.kind {
            TerminatorKind::Call { func, .. } => cross_thread_escape_call(&callee_text(func)),
            _ => false,
        }
    })
}

fn body_cross_thread_escape_heap_object_types<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for data in body_basic_blocks!(body).iter() {
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => continue,
        };
        let (func, args) = match &terminator.kind {
            TerminatorKind::Call { func, args, .. } => (func, args),
            _ => continue,
        };
        if !cross_thread_escape_call(&callee_text(func)) {
            continue;
        }
        let args = call_arg_operands(args);
        for arg in &args {
            out.extend(heap_object_types_from_ty(
                tcx,
                arg.ty(&body.local_decls, tcx),
            ));
        }
    }
    out
}

fn semantic_object_needs_cross_thread_recovery_hint(
    body_has_cross_thread_escape: bool,
    cross_thread_escape_heap_object_types: &BTreeSet<String>,
    semantic_object_type: &str,
) -> bool {
    if !body_has_cross_thread_escape || semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        return false;
    }
    if cross_thread_escape_heap_object_types.is_empty() {
        // Fallback to the previous conservative body-level behavior when rustc
        // does not expose enough typed closure/upvar information for the
        // escape call.  When the set is non-empty, use it to avoid over-tagging
        // unrelated heap scopes in the same MIR body.
        return true;
    }
    cross_thread_escape_heap_object_types.contains(semantic_object_type)
}

fn const_bits_operand<'tcx>(
    tcx: TyCtxt<'tcx>,
    ty: rustc_middle::ty::Ty<'tcx>,
    value: u128,
    span: Span,
) -> Operand<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        let bits = match ty.kind() {
            ty::Uint(ty::UintTy::U16) => 16,
            ty::Uint(ty::UintTy::U32) => 32,
            ty::Uint(ty::UintTy::U64) => 64,
            _ => 128,
        };
        Operand::const_from_scalar(
            tcx,
            ty,
            Scalar::from_uint(value, Size::from_bits(bits)),
            span,
        )
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        Operand::Constant(Box::new(Constant {
            span,
            user_ty: None,
            literal: ConstantKind::from_bits(tcx, value, ty::ParamEnv::empty().and(ty)),
        }))
    }
}

fn const_u64_operand<'tcx>(tcx: TyCtxt<'tcx>, value: u64, span: Span) -> Operand<'tcx> {
    const_bits_operand(tcx, tcx.types.u64, value as u128, span)
}

fn const_u32_operand<'tcx>(tcx: TyCtxt<'tcx>, value: u32, span: Span) -> Operand<'tcx> {
    const_bits_operand(tcx, tcx.types.u32, value as u128, span)
}

fn const_u16_operand<'tcx>(tcx: TyCtxt<'tcx>, value: u16, span: Span) -> Operand<'tcx> {
    const_bits_operand(tcx, tcx.types.u16, value as u128, span)
}

#[derive(Default)]
struct SemanticLocalOwnershipProof {
    allocation_pairs: BTreeSet<(String, String)>,
    drop_pairs: BTreeSet<(String, String)>,
}

struct SemanticOwnerUseVisitor<'tcx> {
    owner: Place<'tcx>,
    allocation_block: BasicBlock,
    allocation_statement_index: usize,
    disqualified: bool,
}

impl<'tcx> Visitor<'tcx> for SemanticOwnerUseVisitor<'tcx> {
    fn visit_place(&mut self, place: &Place<'tcx>, context: PlaceContext, location: Location) {
        if self.disqualified || place.local != self.owner.local {
            return;
        }

        let exact_owner = *place == self.owner;
        let allocation_destination = exact_owner
            && location.block == self.allocation_block
            && location.statement_index == self.allocation_statement_index
            && matches!(context, PlaceContext::MutatingUse(MutatingUseContext::Call));
        let exact_drop =
            exact_owner && matches!(context, PlaceContext::MutatingUse(MutatingUseContext::Drop));
        let bookkeeping_only = matches!(context, PlaceContext::NonUse(_));

        // Local/no-recovery is intentionally a zero-alias proof.  Apart from
        // the constructor destination, compiler bookkeeping, and an exact
        // Drop, *every* typed use of the owner local fails closed.  This
        // includes Ref/Reborrow/RawPtr, moves/copies, call arguments,
        // projections, overwrites, and deref temporaries.  Rejecting at alias
        // creation means a later call cannot hide ownership behind another
        // local, unlike the former MIR debug-string substring heuristic.
        if !allocation_destination && !exact_drop && !bookkeeping_only {
            self.disqualified = true;
        }
    }
}

fn candidate_has_zero_alias_owner_uses<'tcx>(
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> bool {
    if !candidate.destination.projection.is_empty() {
        return false;
    }

    let mut visitor = SemanticOwnerUseVisitor {
        owner: candidate.destination,
        allocation_block: candidate.bb,
        allocation_statement_index: body[candidate.bb].statements.len(),
        disqualified: false,
    };
    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        for (statement_index, statement) in data.statements.iter().enumerate() {
            visitor.visit_statement(
                statement,
                Location {
                    block: bb,
                    statement_index,
                },
            );
            if visitor.disqualified {
                return false;
            }
        }
        if let Some(terminator) = &data.terminator {
            visitor.visit_terminator(
                terminator,
                Location {
                    block: bb,
                    statement_index: data.statements.len(),
                },
            );
            if visitor.disqualified {
                return false;
            }
        }
    }
    true
}

fn candidate_has_linear_exact_drop<'tcx>(
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> bool {
    if !candidate_has_zero_alias_owner_uses(body, candidate) {
        return false;
    }
    let mut bb = match candidate.original_target {
        Some(bb) => bb,
        None => return false,
    };
    let mut visited = BTreeSet::new();

    loop {
        if !visited.insert(bb) {
            return false;
        }
        let data = &body[bb];
        if data.is_cleanup {
            return false;
        }
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => return false,
        };

        if let TerminatorKind::Drop { place, .. } = &terminator.kind {
            if *place == candidate.destination {
                return true;
            }
        }

        let normal_successors = terminator
            .successors()
            .filter(|successor| !body[*successor].is_cleanup)
            .collect::<Vec<_>>();
        if normal_successors.len() != 1 {
            return false;
        }
        bb = normal_successors[0];
    }
}

fn exact_semantic_local_ownership_proof<'tcx>(
    body: &Body<'tcx>,
    candidates: &[SemanticScopeCandidate<'tcx>],
) -> SemanticLocalOwnershipProof {
    if !direct_local_size_align_with_semantic_drop_requested() {
        return SemanticLocalOwnershipProof::default();
    }

    let mut candidate_counts: BTreeMap<(String, String), usize> = BTreeMap::new();
    let mut candidate_pairs: BTreeMap<String, Vec<((String, String), bool)>> = BTreeMap::new();

    for candidate in candidates {
        if candidate.semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
            continue;
        }
        let key = (
            candidate.semantic_object_type.clone(),
            candidate.destination_place.clone(),
        );
        *candidate_counts.entry(key.clone()).or_insert(0) += 1;
        candidate_pairs
            .entry(candidate.semantic_object_type.clone())
            .or_default()
            .push((
                key,
                !candidate.original_is_cleanup && candidate_has_linear_exact_drop(body, candidate),
            ));
    }

    let mut proof = SemanticLocalOwnershipProof::default();
    for pairs in candidate_pairs.into_values() {
        if pairs
            .iter()
            .any(|(pair, proven)| !proven || candidate_counts.get(pair) != Some(&1))
        {
            continue;
        }
        for (pair, _) in pairs {
            proof.allocation_pairs.insert(pair.clone());
            proof.drop_pairs.insert(pair);
        }
    }
    proof
}

fn record_or_rewrite_candidates<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    body: &mut Body<'tcx>,
    actual_rewrite: bool,
    replacement_abis: Option<DirectAllocatorReplacementAbis>,
    semantic_scope_rewrite: bool,
    semantic_scope_abi: Option<SemanticScopeAbi>,
    semantic_scope_local_abi: Option<SemanticScopeAbi>,
    records: &mut Vec<RewriteRecord>,
) {
    let function_name = tcx.def_path_str(def_id);
    let cross_thread_escape = body_contains_cross_thread_escape_call(body);
    let cross_thread_escape_heap_object_types = if cross_thread_escape {
        body_cross_thread_escape_heap_object_types(tcx, body)
    } else {
        BTreeSet::new()
    };
    let mut direct_candidates = Vec::new();
    let mut layout_provenance: BTreeMap<String, HeapObjectSolution> = BTreeMap::new();

    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        for statement in &data.statements {
            propagate_layout_like_statement_provenance(statement, &mut layout_provenance);
        }
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => continue,
        };
        let (func, args, destination, target, fn_span) = match &terminator.kind {
            TerminatorKind::Call {
                func,
                args,
                destination,
                target,
                fn_span,
                ..
            } => (func, args, destination, target, fn_span),
            _ => continue,
        };
        let callee = callee_text(func);
        let call_kind = direct_allocator_call_kind(&callee);
        let arg_operands = call_arg_operands(args);
        let layout_solution = direct_allocator_layout_heap_object_solution(
            call_kind,
            &arg_operands,
            &layout_provenance,
        );

        if call_kind != DirectAllocatorCallKind::Unsupported {
            let destination_ty = destination.ty(&body.local_decls, tcx).ty;
            let argument_tys = arg_operands
                .iter()
                .map(|arg| arg.ty(&body.local_decls, tcx))
                .collect::<Vec<_>>();
            if !direct_allocator_global_receiver_supported(call_kind, &argument_tys)
                || (direct_allocator_is_global_receiver_call(call_kind)
                    && layout_solution.is_none())
            {
                propagate_layout_like_call_provenance(
                    &callee,
                    &arg_operands,
                    destination,
                    &mut layout_provenance,
                );
                continue;
            }
            let destination_place = format!("{:?}", destination);
            let solution = direct_allocator_semantic_object_type(
                tcx,
                body,
                call_kind,
                &destination_place,
                destination_ty,
                &argument_tys,
                &callee,
                *target,
                layout_solution,
            );

            direct_candidates.push(DirectAllocatorCandidate {
                bb,
                call_kind,
                callee: callee.clone(),
                call_arguments: arg_operands
                    .iter()
                    .map(|arg| format!("{:?}", arg))
                    .collect::<Vec<_>>(),
                argument_types: argument_tys
                    .iter()
                    .map(|arg_ty| format!("{:?}", arg_ty))
                    .collect::<Vec<_>>(),
                destination_place,
                destination_type: format!("{:?}", destination_ty),
                semantic_object_type: solution.object_type,
                type_id_basis: solution.type_id_basis,
                source_span: tcx.sess.source_map().span_to_diagnostic_string(*fn_span),
                fn_span: *fn_span,
            });
        }
        propagate_layout_like_call_provenance(
            &callee,
            &arg_operands,
            destination,
            &mut layout_provenance,
        );
    }

    for DirectAllocatorCandidate {
        bb,
        call_kind,
        callee,
        call_arguments,
        argument_types,
        destination_place,
        destination_type,
        semantic_object_type,
        type_id_basis: solved_type_id_basis,
        source_span,
        fn_span,
    } in direct_candidates
    {
        let layout_operand = direct_allocator_layout_operand_index(call_kind)
            .and_then(|idx| call_arguments.get(idx));
        let size_operand = match call_kind {
            DirectAllocatorCallKind::SizeAlignAlloc => call_arguments.get(0).cloned(),
            kind if direct_allocator_is_layout_based(kind) => {
                layout_operand.map(|operand| format!("<size-from-layout-operand:{}>", operand))
            }
            _ => None,
        };
        let align_operand = match call_kind {
            DirectAllocatorCallKind::SizeAlignAlloc => call_arguments.get(1).cloned(),
            kind if direct_allocator_is_layout_based(kind) => {
                layout_operand.map(|operand| format!("<align-from-layout-operand:{}>", operand))
            }
            _ => None,
        };
        let basic_block = format!("{:?}", bb);
        let key = format!(
            "{}\0{}\0{}\0{}\0{}",
            function_name,
            basic_block,
            source_span,
            callee,
            call_arguments.join("\0")
        );
        let callsite = nonzero_fnv1a64_text(&key);
        let (type_id, type_id_basis) =
            direct_allocator_type_id(call_kind, &semantic_object_type, solved_type_id_basis, &key);
        let recovery_delegated = type_id_basis == "direct_allocator_recovery_delegated";
        // Optimized MIR no longer exposes a sound owner link from a raw
        // size/align allocator destination to the eventual heap-owner Drop.
        // A same-type Drop count is not an ownership proof, so this branch
        // remains recovery-backed until an exact destination link exists.
        let allow_size_align_local = !(call_kind == DirectAllocatorCallKind::SizeAlignAlloc
            && direct_local_size_align_with_semantic_drop_requested());
        let configured_policy_flags = lowering_policy_flags();
        let configured_lifetime_hint = lowering_lifetime_hint();
        let candidate_cross_thread_escape = semantic_object_needs_cross_thread_recovery_hint(
            cross_thread_escape,
            &cross_thread_escape_heap_object_types,
            &semantic_object_type,
        );
        let (
            configured_placement_hint,
            configured_cross_thread_recovery_hint,
            configured_placement_hint_basis,
        ) = lowering_placement_hint_for_body(candidate_cross_thread_escape);
        let (
            module_id,
            policy_flags,
            lifetime_hint,
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
        ) = if recovery_delegated {
            (
                0,
                0,
                0,
                0,
                false,
                "direct_allocator_recovery_delegated_neutral",
            )
        } else {
            (
                LOWERING_MODULE_ID,
                configured_policy_flags,
                configured_lifetime_hint,
                configured_placement_hint,
                configured_cross_thread_recovery_hint,
                configured_placement_hint_basis,
            )
        };
        let replacement_preview = format!(
            "Call allocator metadata ABI(kind={}, size={}, align={}, type_id={}, module_id={}, flags={}, lifetime_hint={}, placement_hint={}, callsite={}) -> {}; semantic_object_type={}",
            direct_allocator_call_kind_label(call_kind),
            size_operand.as_deref().unwrap_or("<missing>"),
            align_operand.as_deref().unwrap_or("<missing>"),
            type_id,
            module_id,
            policy_flags,
            lifetime_hint,
            placement_hint,
            callsite,
            destination_place,
            semantic_object_type
        );
        let replacement_symbol = replacement_abis
            .and_then(|abis| {
                direct_allocator_replacement_abi(
                    abis,
                    call_kind,
                    allow_size_align_local,
                    recovery_delegated,
                )
            })
            .map(|abi| abi.symbol)
            .unwrap_or_else(|| {
                if recovery_delegated {
                    direct_allocator_recovery_backed_replacement_symbol(call_kind)
                } else {
                    direct_allocator_replacement_symbol_for_site(call_kind, allow_size_align_local)
                }
            });
        let metadata_pairing_contract =
            direct_allocator_metadata_pairing_contract(call_kind, replacement_symbol);
        let mut rewrite_status = "provider_override_body_clone_returned_rewrite_planned";
        let mut replacement_resolution_status = "not_requested_dry_run";
        if actual_rewrite {
            let terminator = body[bb].terminator_mut();
            let (func, args) = match &mut terminator.kind {
                TerminatorKind::Call { func, args, .. } => (func, args),
                _ => {
                    rewrite_status = "actual_allocator_call_replacement_skipped_candidate_changed";
                    replacement_resolution_status = "candidate_terminator_changed";
                    records.push(RewriteRecord {
                        allocation_site_id: format!(
                            "rustc-driver-mir-rewrite-dry-run:{:016x}",
                            callsite
                        ),
                        type_id,
                        module_id,
                        flags: policy_flags,
                        lifetime_hint,
                        placement_hint,
                        cross_thread_recovery_hint,
                        placement_hint_basis,
                        callsite,
                        mir_function: function_name.clone(),
                        basic_block,
                        source_span,
                        callee,
                        destination_place,
                        destination_type,
                        call_arguments,
                        argument_types,
                        semantic_object_type,
                        type_id_basis,
                        size_operand,
                        align_operand,
                        rewrite_status,
                        replacement_symbol,
                        replacement_resolution_status,
                        replacement_preview,
                        semantic_scope_unwind_pop_inserted: false,
                        metadata_pairing_contract,
                        lowering_kind: "direct_allocator_call_rewrite",
                    });
                    continue;
                }
            };
            let required_arg_count = direct_allocator_required_arg_count(call_kind);
            if args.len() < required_arg_count {
                rewrite_status =
                    "actual_allocator_call_replacement_skipped_missing_original_operands";
                replacement_resolution_status = "missing_original_allocator_operands";
            } else if let Some(replacement_abi) = replacement_abis.and_then(|abis| {
                direct_allocator_replacement_abi(
                    abis,
                    call_kind,
                    allow_size_align_local,
                    recovery_delegated,
                )
            }) {
                let mut rewritten_args = match call_kind {
                    DirectAllocatorCallKind::SizeAlignAlloc => {
                        vec![
                            clone_call_arg_operand(args, 0),
                            clone_call_arg_operand(args, 1),
                        ]
                    }
                    DirectAllocatorCallKind::LayoutAlloc => vec![clone_call_arg_operand(args, 0)],
                    DirectAllocatorCallKind::LayoutAllocZeroed => {
                        vec![clone_call_arg_operand(args, 0)]
                    }
                    DirectAllocatorCallKind::LayoutRealloc => {
                        vec![
                            clone_call_arg_operand(args, 0),
                            clone_call_arg_operand(args, 1),
                            clone_call_arg_operand(args, 2),
                        ]
                    }
                    DirectAllocatorCallKind::LayoutDealloc => {
                        vec![
                            clone_call_arg_operand(args, 0),
                            clone_call_arg_operand(args, 1),
                        ]
                    }
                    DirectAllocatorCallKind::GlobalAllocLayoutAlloc => {
                        vec![clone_call_arg_operand(args, 1)]
                    }
                    DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed => {
                        vec![clone_call_arg_operand(args, 1)]
                    }
                    DirectAllocatorCallKind::GlobalAllocLayoutRealloc => {
                        vec![
                            clone_call_arg_operand(args, 1),
                            clone_call_arg_operand(args, 2),
                            clone_call_arg_operand(args, 3),
                        ]
                    }
                    DirectAllocatorCallKind::GlobalAllocLayoutDealloc => {
                        vec![
                            clone_call_arg_operand(args, 1),
                            clone_call_arg_operand(args, 2),
                        ]
                    }
                    DirectAllocatorCallKind::Unsupported => Vec::new(),
                };
                rewritten_args.push(const_u64_operand(tcx, type_id, fn_span));
                rewritten_args.push(const_u64_operand(tcx, module_id, fn_span));
                rewritten_args.push(const_u32_operand(tcx, policy_flags, fn_span));
                if replacement_abi.supports_hints {
                    rewritten_args.push(const_u16_operand(tcx, lifetime_hint, fn_span));
                    rewritten_args.push(const_u16_operand(tcx, placement_hint, fn_span));
                }
                rewritten_args.push(const_u64_operand(tcx, callsite, fn_span));
                *func = unialloc_function_handle(tcx, replacement_abi.def_id, fn_span);
                *args = make_call_args(rewritten_args, fn_span);
                rewrite_status = "actual_allocator_call_replacement_applied";
                replacement_resolution_status = direct_allocator_resolved_status_for_site(
                    call_kind,
                    replacement_abi.supports_hints,
                    allow_size_align_local,
                    recovery_delegated,
                );
            } else {
                rewrite_status = "actual_allocator_call_replacement_requested_symbol_unresolved";
                replacement_resolution_status = direct_allocator_unresolved_status_for_site(
                    call_kind,
                    allow_size_align_local,
                    recovery_delegated,
                );
            }
        }
        records.push(RewriteRecord {
            allocation_site_id: format!("rustc-driver-mir-rewrite-dry-run:{:016x}", callsite),
            type_id,
            module_id,
            flags: policy_flags,
            lifetime_hint,
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
            callsite,
            mir_function: function_name.clone(),
            basic_block,
            source_span,
            callee,
            destination_place,
            destination_type,
            call_arguments,
            argument_types,
            semantic_object_type,
            type_id_basis,
            size_operand,
            align_operand,
            rewrite_status,
            replacement_symbol,
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted: false,
            metadata_pairing_contract,
            lowering_kind: "direct_allocator_call_rewrite",
        });
    }
    let local_ownership = record_or_rewrite_semantic_scope_candidates(
        tcx,
        def_id,
        body,
        semantic_scope_rewrite,
        semantic_scope_abi,
        semantic_scope_local_abi,
        records,
    );
    record_or_rewrite_semantic_drop_candidates(
        tcx,
        def_id,
        body,
        semantic_scope_rewrite,
        semantic_scope_abi,
        semantic_scope_local_abi,
        &local_ownership.drop_pairs,
        records,
    );
}

fn push_internal_local<'tcx>(
    body: &mut Body<'tcx>,
    ty: rustc_middle::ty::Ty<'tcx>,
    span: Span,
) -> rustc_middle::mir::Local {
    body.local_decls.push(LocalDecl::new(ty, span))
}

fn push_semantic_scope_pop_block<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &mut Body<'tcx>,
    pop_def_id: DefId,
    source_info: SourceInfo,
    fn_span: Span,
    target: BasicBlock,
    cleanup: Option<BasicBlock>,
    from_hir_call: MirCallSource,
    is_cleanup: bool,
) -> BasicBlock {
    let pop_unit_local = push_internal_local(body, unit_ty(tcx), fn_span);
    let pop_unit_place = Place::from(pop_unit_local);
    let unwind = if is_cleanup {
        no_unwind()
    } else {
        cleanup_unwind(cleanup)
    };
    body.basic_blocks_mut().push(basic_block_data(
        Terminator {
            source_info,
            kind: call_terminator_kind(
                tcx,
                pop_def_id,
                Vec::new(),
                pop_unit_place,
                Some(target),
                unwind,
                from_hir_call,
                fn_span,
            ),
        },
        is_cleanup,
    ))
}

fn record_or_rewrite_semantic_scope_candidates<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    body: &mut Body<'tcx>,
    semantic_scope_rewrite: bool,
    semantic_scope_abi: Option<SemanticScopeAbi>,
    semantic_scope_local_abi: Option<SemanticScopeAbi>,
    records: &mut Vec<RewriteRecord>,
) -> SemanticLocalOwnershipProof {
    let function_name = tcx.def_path_str(def_id);
    let cross_thread_escape = body_contains_cross_thread_escape_call(body);
    let cross_thread_escape_heap_object_types = if cross_thread_escape {
        body_cross_thread_escape_heap_object_types(tcx, body)
    } else {
        BTreeSet::new()
    };
    let mut candidate_blocks = Vec::new();
    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => continue,
        };
        #[cfg(unialloc_rustc_current)]
        let (func, args, destination, target, original_unwind, from_hir_call, fn_span) =
            match &terminator.kind {
                TerminatorKind::Call {
                    func,
                    args,
                    destination,
                    target,
                    unwind,
                    call_source,
                    fn_span,
                } => (
                    func,
                    args,
                    destination,
                    target,
                    *unwind,
                    *call_source,
                    fn_span,
                ),
                _ => continue,
            };
        #[cfg(not(unialloc_rustc_current))]
        let (func, args, destination, target, original_unwind, from_hir_call, fn_span) =
            match &terminator.kind {
                TerminatorKind::Call {
                    func,
                    args,
                    destination,
                    target,
                    cleanup,
                    from_hir_call,
                    fn_span,
                } => (
                    func,
                    args,
                    destination,
                    target,
                    *cleanup,
                    *from_hir_call,
                    fn_span,
                ),
                _ => continue,
            };
        let callee = callee_text(func);
        let callee_def_id = match func.ty(&body.local_decls, tcx).kind() {
            ty::FnDef(def_id, _) => Some(*def_id),
            _ => None,
        };
        let destination_ty = destination.ty(&body.local_decls, tcx).ty;
        let arg_operands = call_arg_operands(args);
        let argument_tys = arg_operands
            .iter()
            .map(|arg| arg.ty(&body.local_decls, tcx))
            .collect::<Vec<_>>();
        let plain_clone_heap_class =
            plain_clone_heap_class(tcx, callee_def_id, &callee, destination_ty);
        if target.is_none()
            || (matches!(&plain_clone_heap_class, PlainCloneHeapClass::NotPlainClone)
                && !semantic_scope_candidate_for_mir(tcx, &callee, destination_ty))
        {
            continue;
        }
        let destination_type = format!("{:?}", destination_ty);
        let argument_types = argument_tys
            .iter()
            .map(|arg_ty| format!("{:?}", arg_ty))
            .collect::<Vec<_>>();
        let semantic_object_type = match &plain_clone_heap_class {
            PlainCloneHeapClass::Single(owner) => owner.clone(),
            PlainCloneHeapClass::DefiniteNoSupportedOwner
            | PlainCloneHeapClass::Ambiguous(_)
            | PlainCloneHeapClass::Unresolved => UNKNOWN_HEAP_OBJECT_TYPE.to_string(),
            PlainCloneHeapClass::NotPlainClone => {
                semantic_heap_object_type_from_mir(tcx, destination_ty, &argument_tys, &callee)
            }
        };
        candidate_blocks.push(SemanticScopeCandidate {
            bb,
            original_is_cleanup: data.is_cleanup,
            callee,
            call_arguments: arg_operands
                .iter()
                .map(|arg| format!("{:?}", arg))
                .collect::<Vec<_>>(),
            argument_types,
            destination: *destination,
            destination_place: format!("{:?}", destination),
            destination_type,
            semantic_object_type,
            plain_clone_heap_class,
            original_target: *target,
            original_unwind,
            original_from_hir_call: from_hir_call,
            fn_span: *fn_span,
            original_terminator: terminator.clone(),
        });
    }

    // The local ABI is a per-owner optimization, not a per-type heuristic.
    // Authorize the type group only when every allocation destination follows
    // one non-cleanup normal path, is not moved or passed to a call, and reaches
    // its exact destination-matched Drop before any normal exit.
    let local_ownership = exact_semantic_local_ownership_proof(body, &candidate_blocks);

    for SemanticScopeCandidate {
        bb,
        original_is_cleanup,
        callee,
        call_arguments,
        argument_types,
        destination: _,
        destination_place,
        destination_type,
        semantic_object_type,
        plain_clone_heap_class,
        original_target,
        original_unwind,
        original_from_hir_call,
        fn_span,
        mut original_terminator,
    } in candidate_blocks
    {
        let source_span = tcx.sess.source_map().span_to_diagnostic_string(fn_span);
        let basic_block = format!("{:?}", bb);
        let key = format!(
            "semantic-scope\0{}\0{}\0{}\0{}\0{}\0{}",
            function_name,
            basic_block,
            source_span,
            callee,
            semantic_object_type,
            call_arguments.join("\0")
        );
        let callsite = nonzero_fnv1a64_text(&key);
        let (type_id, type_id_basis) = semantic_scope_type_id(&semantic_object_type, &key);
        let policy_flags = lowering_policy_flags();
        let lifetime_hint = lowering_lifetime_hint();
        let candidate_cross_thread_escape = semantic_object_needs_cross_thread_recovery_hint(
            cross_thread_escape,
            &cross_thread_escape_heap_object_types,
            &semantic_object_type,
        );
        let (placement_hint, cross_thread_recovery_hint, placement_hint_basis) =
            lowering_placement_hint_for_body(candidate_cross_thread_escape);
        if matches!(
            &plain_clone_heap_class,
            PlainCloneHeapClass::DefiniteNoSupportedOwner
        ) {
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-scope-no-supported-owner:{:016x}",
                    callsite
                ),
                type_id,
                module_id: LOWERING_MODULE_ID,
                flags: policy_flags,
                lifetime_hint,
                placement_hint,
                cross_thread_recovery_hint,
                placement_hint_basis,
                callsite,
                mir_function: function_name.clone(),
                basic_block,
                source_span,
                callee,
                destination_place,
                destination_type,
                call_arguments,
                argument_types,
                semantic_object_type,
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status: "semantic_scope_rewrite_skipped_non_heap_object_type",
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status:
                    "rustc_middle_no_supported_heap_owner_not_lowered",
                replacement_preview:
                    "Skipped semantic-scope lowering because the Clone result type has no supported heap owner; this does not assert that the Clone implementation performs no temporary allocation"
                        .to_string(),
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "audit_only_no_supported_heap_owner",
                lowering_kind: "semantic_scope_non_heap_object_skipped",
            });
            continue;
        }
        if let PlainCloneHeapClass::Ambiguous(owners) = &plain_clone_heap_class {
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-scope-ambiguous:{:016x}",
                    callsite
                ),
                type_id,
                module_id: LOWERING_MODULE_ID,
                flags: policy_flags,
                lifetime_hint,
                placement_hint,
                cross_thread_recovery_hint,
                placement_hint_basis,
                callsite,
                mir_function: function_name.clone(),
                basic_block,
                source_span,
                callee,
                destination_place,
                destination_type,
                call_arguments,
                argument_types,
                semantic_object_type,
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status: "semantic_scope_rewrite_skipped_ambiguous_heap_object_type",
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status:
                    "rustc_middle_multiple_heap_object_types_not_lowered",
                replacement_preview: format!(
                    "Skipped semantic-scope lowering because the Clone result has multiple supported heap owners: {}",
                    owners.join(", ")
                ),
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "audit_only_ambiguous_heap_object_type",
                lowering_kind: "semantic_scope_unsolved_heap_object_candidate",
            });
            continue;
        }
        if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
            // Do not turn non-heap iterator/Bencher/helper calls into typed
            // allocation evidence.  Keep an explicit audit row so the solver
            // gap is visible instead of silently disappearing from reports.
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-scope-unsolved:{:016x}",
                    callsite
                ),
                type_id,
                module_id: LOWERING_MODULE_ID,
                flags: policy_flags,
                lifetime_hint,
                placement_hint,
                cross_thread_recovery_hint,
                placement_hint_basis,
                callsite,
                mir_function: function_name.clone(),
                basic_block,
                source_span,
                callee,
                destination_place,
                destination_type,
                call_arguments,
                argument_types,
                semantic_object_type,
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status: "semantic_scope_rewrite_skipped_unresolved_heap_object_type",
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status: "rustc_middle_heap_object_type_not_solved",
                replacement_preview:
                    "Skipped semantic-scope lowering because rustc_middle did not solve a supported heap object type"
                        .to_string(),
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "audit_only_unresolved_heap_object_type",
                lowering_kind: "semantic_scope_unsolved_heap_object_candidate",
            });
            continue;
        }
        let mut rewrite_status = "semantic_scope_enter_exit_rewrite_planned";
        let mut replacement_resolution_status = "not_requested_dry_run";
        let mut replacement_preview = format!(
            "Wrap {} with semantic scope metadata(type_id={}, module_id={}, flags={}, lifetime_hint={}, placement_hint={}, callsite={}) / __unialloc_semantic_scope_pop(); semantic_object_type={}",
            callee,
            type_id,
            LOWERING_MODULE_ID,
            policy_flags,
            lifetime_hint,
            placement_hint,
            callsite,
            semantic_object_type
        );
        let mut semantic_scope_unwind_pop_inserted = false;
        let exact_local_pair = local_ownership
            .allocation_pairs
            .contains(&(semantic_object_type.clone(), destination_place.clone()));
        let selected_semantic_scope_abi = if exact_local_pair {
            semantic_scope_local_abi.or(semantic_scope_abi)
        } else {
            semantic_scope_abi
        };

        if semantic_scope_rewrite {
            if let (Some(scope_abi), Some(original_target)) =
                (selected_semantic_scope_abi, original_target)
            {
                let push_unit_local = push_internal_local(body, unit_ty(tcx), fn_span);
                let push_unit_place = Place::from(push_unit_local);
                let source_info = body[bb].terminator().source_info;

                let exit_block = push_semantic_scope_pop_block(
                    tcx,
                    body,
                    scope_abi.pop_def_id,
                    source_info,
                    fn_span,
                    original_target,
                    unwind_cleanup_target(original_unwind),
                    original_from_hir_call,
                    original_is_cleanup,
                );

                let cleanup_exit_block =
                    unwind_cleanup_target(original_unwind).map(|cleanup_target| {
                        push_semantic_scope_pop_block(
                            tcx,
                            body,
                            scope_abi.pop_def_id,
                            source_info,
                            fn_span,
                            cleanup_target,
                            None,
                            original_from_hir_call,
                            true,
                        )
                    });

                #[cfg(unialloc_rustc_current)]
                if let TerminatorKind::Call { target, unwind, .. } = &mut original_terminator.kind {
                    *target = Some(exit_block);
                    if let Some(cleanup_exit_block) = cleanup_exit_block {
                        set_unwind_cleanup(unwind, cleanup_exit_block);
                        semantic_scope_unwind_pop_inserted = true;
                    }
                }
                #[cfg(not(unialloc_rustc_current))]
                if let TerminatorKind::Call {
                    target, cleanup, ..
                } = &mut original_terminator.kind
                {
                    *target = Some(exit_block);
                    if let Some(cleanup_exit_block) = cleanup_exit_block {
                        set_unwind_cleanup(cleanup, cleanup_exit_block);
                        semantic_scope_unwind_pop_inserted = true;
                    }
                }
                let call_block = body
                    .basic_blocks_mut()
                    .push(basic_block_data(original_terminator, original_is_cleanup));

                let mut push_args = vec![
                    const_u64_operand(tcx, type_id, fn_span),
                    const_u64_operand(tcx, LOWERING_MODULE_ID, fn_span),
                    const_u32_operand(tcx, policy_flags, fn_span),
                ];
                if scope_abi.supports_hints {
                    push_args.push(const_u16_operand(tcx, lifetime_hint, fn_span));
                    push_args.push(const_u16_operand(tcx, placement_hint, fn_span));
                }
                push_args.push(const_u64_operand(tcx, callsite, fn_span));
                body[bb].terminator_mut().kind = call_terminator_kind(
                    tcx,
                    scope_abi.push_def_id,
                    push_args,
                    push_unit_place,
                    Some(call_block),
                    inserted_call_unwind(original_unwind, original_is_cleanup),
                    original_from_hir_call,
                    fn_span,
                );
                rewrite_status = "actual_semantic_scope_enter_exit_rewrite_applied";
                replacement_resolution_status = semantic_scope_resolution_status(scope_abi);
                replacement_preview = if semantic_scope_unwind_pop_inserted {
                    format!(
                        "Inserted MIR {} block -> original call block -> pop block for {}; original unwind cleanup also passes through pop",
                        scope_abi.push_symbol, callee
                    )
                } else {
                    format!(
                        "Inserted MIR {} block -> original call block -> pop block for {}",
                        scope_abi.push_symbol, callee
                    )
                };
            } else {
                rewrite_status =
                    "actual_semantic_scope_enter_exit_rewrite_requested_symbol_unresolved";
                replacement_resolution_status = semantic_scope_unresolved_status(exact_local_pair);
            }
        }

        records.push(RewriteRecord {
            allocation_site_id: format!("rustc-driver-mir-semantic-scope:{:016x}", callsite),
            type_id,
            module_id: LOWERING_MODULE_ID,
            flags: policy_flags,
            lifetime_hint,
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
            callsite,
            mir_function: function_name.clone(),
            basic_block,
            source_span,
            callee,
            destination_place,
            destination_type,
            call_arguments,
            argument_types,
            semantic_object_type,
            type_id_basis,
            size_operand: None,
            align_operand: None,
            rewrite_status,
            replacement_symbol: selected_semantic_scope_abi
                .map(|abi| abi.push_symbol)
                .unwrap_or_else(|| semantic_scope_push_symbol(exact_local_pair)),
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted,
            metadata_pairing_contract: "semantic_scope_active_metadata",
            lowering_kind: "semantic_scope_enter_exit_rewrite",
        });
    }
    local_ownership
}

fn record_or_rewrite_semantic_drop_candidates<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    body: &mut Body<'tcx>,
    semantic_scope_rewrite: bool,
    semantic_scope_abi: Option<SemanticScopeAbi>,
    semantic_scope_local_abi: Option<SemanticScopeAbi>,
    local_drop_pairs: &BTreeSet<(String, String)>,
    records: &mut Vec<RewriteRecord>,
) {
    let function_name = tcx.def_path_str(def_id);
    let cross_thread_escape = body_contains_cross_thread_escape_call(body);
    let cross_thread_escape_heap_object_types = if cross_thread_escape {
        body_cross_thread_escape_heap_object_types(tcx, body)
    } else {
        BTreeSet::new()
    };
    let mut candidate_blocks = Vec::new();
    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => continue,
        };
        #[cfg(unialloc_rustc_current)]
        let (place, target, original_unwind) = match &terminator.kind {
            TerminatorKind::Drop {
                place,
                target,
                unwind,
                ..
            } => (place, target, *unwind),
            _ => continue,
        };
        #[cfg(not(unialloc_rustc_current))]
        let (place, target, original_unwind) = match &terminator.kind {
            TerminatorKind::Drop {
                place,
                target,
                unwind,
            } => (place, target, *unwind),
            _ => continue,
        };
        let place_ty = place.ty(&body.local_decls, tcx).ty;
        let drop_type = format!("{:?}", place_ty);
        let heap_owner_types = heap_object_types_from_ty(tcx, place_ty);
        let drop_type_has_multiple_heap_owners = heap_owner_types.len() > 1;
        let semantic_object_type = if drop_type_has_multiple_heap_owners {
            format!(
                "multiple_heap_owners({})",
                heap_owner_types
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(",")
            )
        } else {
            heap_owner_types
                .iter()
                .next()
                .cloned()
                .or_else(|| heap_object_type_from_ty(tcx, place_ty))
                .unwrap_or_else(|| UNKNOWN_HEAP_OBJECT_TYPE.to_string())
        };
        let drop_type_has_generic_param = type_contains_generic_param(tcx, place_ty);
        candidate_blocks.push(SemanticDropCandidate {
            bb,
            original_is_cleanup: data.is_cleanup,
            drop_place: format!("{:?}", place),
            drop_type,
            semantic_object_type,
            drop_type_has_generic_param,
            drop_type_has_multiple_heap_owners,
            original_target: *target,
            original_unwind,
            fn_span: terminator.source_info.span,
            original_terminator: terminator.clone(),
        });
    }

    for SemanticDropCandidate {
        bb,
        original_is_cleanup,
        drop_place,
        drop_type,
        semantic_object_type,
        drop_type_has_generic_param,
        drop_type_has_multiple_heap_owners,
        original_target,
        original_unwind,
        fn_span,
        mut original_terminator,
    } in candidate_blocks
    {
        let source_span = tcx.sess.source_map().span_to_diagnostic_string(fn_span);
        let basic_block = format!("{:?}", bb);
        let key = format!(
            "semantic-drop\0{}\0{}\0{}\0{}\0{}",
            function_name, basic_block, source_span, drop_place, semantic_object_type
        );
        let callsite = nonzero_fnv1a64_text(&key);
        let (type_id, type_id_basis) = semantic_scope_type_id(&semantic_object_type, &key);
        let policy_flags = lowering_policy_flags();
        let lifetime_hint = lowering_lifetime_hint();
        if drop_type_has_multiple_heap_owners {
            // An aggregate Drop may release several independent heap owners. A
            // single active metadata scope cannot represent all of them;
            // wrapping the whole Drop with the first solved owner type
            // misattributes later frees. Keep this as an explicit audit row and
            // let per-allocation recovery records carry each object identity.
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-drop-multi-owner:{:016x}",
                    callsite
                ),
                type_id,
                module_id: LOWERING_MODULE_ID,
                flags: policy_flags,
                lifetime_hint,
                placement_hint: lowering_placement_hint(),
                cross_thread_recovery_hint: false,
                placement_hint_basis: if lowering_placement_hint() != 0 {
                    "manual_placement_hint"
                } else {
                    "default"
                },
                callsite,
                mir_function: function_name.clone(),
                basic_block,
                source_span,
                callee: "TerminatorKind::Drop".to_string(),
                destination_place: drop_place,
                destination_type: drop_type,
                call_arguments: Vec::new(),
                argument_types: Vec::new(),
                semantic_object_type,
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status: "semantic_scope_drop_rewrite_skipped_multiple_heap_owners",
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status: "rustc_middle_drop_multiple_heap_owners_not_lowered",
                replacement_preview:
                    "Skipped drop-scope lowering because one aggregate Drop releases multiple heap-owner identities; recovery records remain authoritative"
                        .to_string(),
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "audit_only_multiple_heap_owner_drop_type",
                lowering_kind: "semantic_scope_drop_multiple_heap_owners_skipped",
            });
            continue;
        }
        let candidate_cross_thread_escape = semantic_object_needs_cross_thread_recovery_hint(
            cross_thread_escape,
            &cross_thread_escape_heap_object_types,
            &semantic_object_type,
        );
        let (placement_hint, cross_thread_recovery_hint, placement_hint_basis) =
            lowering_placement_hint_for_body(candidate_cross_thread_escape);
        if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
            let (
                allocation_site_prefix,
                rewrite_status,
                replacement_resolution_status,
                replacement_preview,
                metadata_pairing_contract,
                lowering_kind,
            ) = if drop_type_has_generic_param {
                (
                    "rustc-driver-mir-semantic-drop-generic",
                    "semantic_scope_drop_rewrite_skipped_generic_type_parameter",
                    "rustc_middle_drop_generic_type_parameter_not_lowered",
                    format!(
                        "Skipped drop-scope lowering for {} because optimized MIR still contains generic Drop type `{}`; this is not monomorphized heap-object evidence",
                        drop_place, drop_type
                    ),
                    "audit_only_generic_drop_type",
                    "semantic_scope_drop_generic_type_parameter_skipped",
                )
            } else if !looks_like_heap_object_type(&drop_type) {
                (
                    "rustc-driver-mir-semantic-drop-non-heap",
                    "semantic_scope_drop_rewrite_skipped_non_heap_object_type",
                    "rustc_middle_drop_non_heap_object_type_not_lowered",
                    format!(
                        "Skipped drop-scope lowering for {} because Drop type `{}` is not a supported heap-owner type",
                        drop_place, drop_type
                    ),
                    "audit_only_non_heap_drop_type",
                    "semantic_scope_drop_non_heap_object_skipped",
                )
            } else {
                (
                    "rustc-driver-mir-semantic-drop-unsolved",
                    "semantic_scope_drop_rewrite_skipped_unresolved_heap_object_type",
                    "rustc_middle_drop_heap_object_type_not_solved",
                    "Skipped drop-scope lowering because rustc_middle did not solve a supported heap owner type"
                        .to_string(),
                    "audit_only_unresolved_drop_type",
                    "semantic_scope_drop_unsolved_heap_object_candidate",
                )
            };
            records.push(RewriteRecord {
                allocation_site_id: format!("{}:{:016x}", allocation_site_prefix, callsite),
                type_id,
                module_id: LOWERING_MODULE_ID,
                flags: policy_flags,
                lifetime_hint,
                placement_hint,
                cross_thread_recovery_hint,
                placement_hint_basis,
                callsite,
                mir_function: function_name.clone(),
                basic_block,
                source_span,
                callee: "TerminatorKind::Drop".to_string(),
                destination_place: drop_place,
                destination_type: drop_type,
                call_arguments: Vec::new(),
                argument_types: Vec::new(),
                semantic_object_type,
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status,
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status,
                replacement_preview,
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract,
                lowering_kind,
            });
            continue;
        }

        let mut rewrite_status = "semantic_scope_drop_rewrite_planned";
        let mut replacement_resolution_status = "not_requested_dry_run";
        let mut replacement_preview = format!(
            "Wrap Drop of {} with semantic scope metadata(type_id={}, module_id={}, flags={}, lifetime_hint={}, placement_hint={}, callsite={}) / __unialloc_semantic_scope_pop(); semantic_object_type={}",
            drop_place,
            type_id,
            LOWERING_MODULE_ID,
            policy_flags,
            lifetime_hint,
            placement_hint,
            callsite,
            semantic_object_type
        );
        let mut semantic_scope_unwind_pop_inserted = false;
        let exact_local_pair =
            local_drop_pairs.contains(&(semantic_object_type.clone(), drop_place.clone()));
        let selected_semantic_scope_abi = if exact_local_pair {
            semantic_scope_local_abi.or(semantic_scope_abi)
        } else {
            semantic_scope_abi
        };

        if semantic_scope_rewrite {
            if let Some(scope_abi) = selected_semantic_scope_abi {
                let push_unit_local = push_internal_local(body, unit_ty(tcx), fn_span);
                let push_unit_place = Place::from(push_unit_local);
                let source_info = body[bb].terminator().source_info;

                let exit_block = push_semantic_scope_pop_block(
                    tcx,
                    body,
                    scope_abi.pop_def_id,
                    source_info,
                    fn_span,
                    original_target,
                    unwind_cleanup_target(original_unwind),
                    synthetic_call_source(),
                    original_is_cleanup,
                );

                let cleanup_exit_block =
                    unwind_cleanup_target(original_unwind).map(|cleanup_target| {
                        push_semantic_scope_pop_block(
                            tcx,
                            body,
                            scope_abi.pop_def_id,
                            source_info,
                            fn_span,
                            cleanup_target,
                            None,
                            synthetic_call_source(),
                            true,
                        )
                    });

                if let TerminatorKind::Drop { target, unwind, .. } = &mut original_terminator.kind {
                    *target = exit_block;
                    if let Some(cleanup_exit_block) = cleanup_exit_block {
                        set_unwind_cleanup(unwind, cleanup_exit_block);
                        semantic_scope_unwind_pop_inserted = true;
                    }
                }

                let drop_block = body
                    .basic_blocks_mut()
                    .push(basic_block_data(original_terminator, original_is_cleanup));

                let mut push_args = vec![
                    const_u64_operand(tcx, type_id, fn_span),
                    const_u64_operand(tcx, LOWERING_MODULE_ID, fn_span),
                    const_u32_operand(tcx, policy_flags, fn_span),
                ];
                if scope_abi.supports_hints {
                    push_args.push(const_u16_operand(tcx, lifetime_hint, fn_span));
                    push_args.push(const_u16_operand(tcx, placement_hint, fn_span));
                }
                push_args.push(const_u64_operand(tcx, callsite, fn_span));
                body[bb].terminator_mut().kind = call_terminator_kind(
                    tcx,
                    scope_abi.push_def_id,
                    push_args,
                    push_unit_place,
                    Some(drop_block),
                    inserted_call_unwind(original_unwind, original_is_cleanup),
                    synthetic_call_source(),
                    fn_span,
                );
                rewrite_status = "actual_semantic_scope_drop_rewrite_applied";
                replacement_resolution_status = semantic_scope_resolution_status(scope_abi);
                replacement_preview = if semantic_scope_unwind_pop_inserted {
                    format!(
                        "Inserted MIR {} block -> original Drop block -> pop block for {}; original unwind cleanup also passes through pop",
                        scope_abi.push_symbol, drop_place
                    )
                } else {
                    format!(
                        "Inserted MIR {} block -> original Drop block -> pop block for {}",
                        scope_abi.push_symbol, drop_place
                    )
                };
            } else {
                rewrite_status = "actual_semantic_scope_drop_rewrite_requested_symbol_unresolved";
                replacement_resolution_status = semantic_scope_unresolved_status(exact_local_pair);
            }
        }

        records.push(RewriteRecord {
            allocation_site_id: format!("rustc-driver-mir-semantic-drop:{:016x}", callsite),
            type_id,
            module_id: LOWERING_MODULE_ID,
            flags: policy_flags,
            lifetime_hint,
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
            callsite,
            mir_function: function_name.clone(),
            basic_block,
            source_span,
            callee: "TerminatorKind::Drop".to_string(),
            destination_place: drop_place,
            destination_type: drop_type,
            call_arguments: Vec::new(),
            argument_types: Vec::new(),
            semantic_object_type,
            type_id_basis,
            size_operand: None,
            align_operand: None,
            rewrite_status,
            replacement_symbol: selected_semantic_scope_abi
                .map(|abi| abi.push_symbol)
                .unwrap_or_else(|| semantic_scope_push_symbol(exact_local_pair)),
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted,
            metadata_pairing_contract: "semantic_scope_drop_active_metadata",
            lowering_kind: "semantic_scope_drop_rewrite",
        });
    }
}

#[cfg_attr(unialloc_rustc_current, allow(static_mut_refs))]
fn optimized_mir_with_rewrite_dry_run<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: OptimizedMirDefId,
) -> &'tcx Body<'tcx> {
    let original = unsafe {
        ORIGINAL_OPTIMIZED_MIR.expect("original optimized_mir provider not installed")(tcx, def_id)
    };
    let mut cloned = original.clone();
    let actual_rewrite = unsafe { ACTUAL_MIR_REWRITE };
    let semantic_scope_rewrite = unsafe { ACTUAL_SEMANTIC_SCOPE_REWRITE };
    let replacement_abis = if actual_rewrite {
        Some(resolve_unialloc_direct_allocator_abis(tcx))
    } else {
        None
    };
    let semantic_scope_abi = if semantic_scope_rewrite {
        resolve_unialloc_semantic_scope(tcx, false)
    } else {
        None
    };
    let semantic_scope_local_abi =
        if semantic_scope_rewrite && direct_local_size_align_with_semantic_drop_requested() {
            resolve_unialloc_semantic_scope(tcx, true)
        } else {
            None
        };
    let record_def_id = optimized_mir_def_id_to_def_id(def_id);
    unsafe {
        if let Some(records) = &RECORDS {
            if let Ok(mut guard) = records.lock() {
                record_or_rewrite_candidates(
                    tcx,
                    record_def_id,
                    &mut cloned,
                    actual_rewrite,
                    replacement_abis,
                    semantic_scope_rewrite,
                    semantic_scope_abi,
                    semantic_scope_local_abi,
                    &mut guard,
                );
            }
        }
    }
    tcx.arena.alloc(cloned)
}

impl Callbacks for RewriteDryRunCallbacks {
    #[cfg(unialloc_rustc_current)]
    fn config(&mut self, config: &mut interface::Config) {
        config.override_queries = Some(|_sess, providers: &mut Providers| {
            unsafe {
                ORIGINAL_OPTIMIZED_MIR = Some(providers.queries.optimized_mir);
            }
            providers.queries.optimized_mir = optimized_mir_with_rewrite_dry_run;
        });
    }

    #[cfg(not(unialloc_rustc_current))]
    fn config(&mut self, config: &mut interface::Config) {
        config.override_queries = Some(|_sess, providers: &mut Providers, _extern_providers| {
            unsafe {
                ORIGINAL_OPTIMIZED_MIR = Some(providers.optimized_mir);
            }
            providers.optimized_mir = optimized_mir_with_rewrite_dry_run;
        });
    }

    #[cfg(unialloc_rustc_current)]
    fn after_analysis<'tcx>(
        &mut self,
        _compiler: &interface::Compiler,
        _tcx: TyCtxt<'tcx>,
    ) -> Compilation {
        if unsafe { CONTINUE_COMPILATION } {
            Compilation::Continue
        } else {
            Compilation::Stop
        }
    }

    #[cfg(not(unialloc_rustc_current))]
    fn after_analysis<'tcx>(
        &mut self,
        _compiler: &interface::Compiler,
        _queries: &'tcx rustc_interface::Queries<'tcx>,
    ) -> Compilation {
        if unsafe { CONTINUE_COMPILATION } {
            Compilation::Continue
        } else {
            Compilation::Stop
        }
    }
}

#[cfg_attr(unialloc_rustc_current, allow(static_mut_refs))]
fn records_snapshot() -> Vec<RewriteRecord> {
    unsafe {
        RECORDS
            .as_ref()
            .and_then(|records| records.lock().ok().map(|guard| guard.clone()))
            .unwrap_or_default()
    }
}

fn write_json(cli: &Cli, records: &[RewriteRecord]) -> Result<(), String> {
    if let Some(parent) = cli.audit_out.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|err| format!("create {}: {}", parent.display(), err))?;
        }
    }

    let rewrite_applied_count = records
        .iter()
        .filter(|record| record.rewrite_status == "actual_allocator_call_replacement_applied")
        .count();
    let direct_allocator_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "direct_allocator_call_rewrite")
        .count();
    let direct_layout_allocator_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && direct_allocator_is_layout_based(direct_allocator_call_kind(&record.callee))
        })
        .count();
    let direct_size_align_allocator_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && direct_allocator_call_kind(&record.callee)
                    == DirectAllocatorCallKind::SizeAlignAlloc
        })
        .count();
    let direct_size_align_recovery_backed_unpaired_dealloc_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && record.metadata_pairing_contract
                    == "recovery_backed_size_align_alloc_unpaired_dealloc"
        })
        .count();
    let direct_local_size_align_semantic_drop_scope_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && record.metadata_pairing_contract == "local_metadata_abi_semantic_drop_scope"
        })
        .count();
    let direct_local_size_align_metadata_contract_violation_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && direct_allocator_call_kind(&record.callee)
                    == DirectAllocatorCallKind::SizeAlignAlloc
                && record.replacement_symbol.ends_with("_local")
                && record.metadata_pairing_contract != "local_metadata_abi_semantic_drop_scope"
        })
        .count();
    let direct_local_size_align_pairing_counts =
        compute_direct_local_size_align_pairing_counts(records);
    let direct_local_size_align_pairing_gap_count = direct_local_size_align_pairing_counts
        .values()
        .filter(|count| {
            count.local_size_align_alloc_count > 0
                && count.semantic_drop_scope_count < count.local_size_align_alloc_count
        })
        .count();
    let direct_layout_allocator_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && record.rewrite_status == "actual_allocator_call_replacement_applied"
                && direct_allocator_is_layout_based(direct_allocator_call_kind(&record.callee))
        })
        .count();
    let semantic_scope_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_enter_exit_rewrite")
        .count();
    let semantic_scope_deallocation_like_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_unsolved_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_unsolved_heap_object_candidate")
        .count();
    let semantic_scope_drop_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_drop_rewrite")
        .count();
    let semantic_scope_drop_unsolved_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_drop_unsolved_heap_object_candidate"
        })
        .count();
    let semantic_scope_drop_non_heap_skipped_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_drop_non_heap_object_skipped")
        .count();
    let semantic_scope_drop_generic_type_parameter_skipped_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_drop_generic_type_parameter_skipped"
        })
        .count();
    let semantic_scope_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_scope_enter_exit_rewrite_applied"
        })
        .count();
    let semantic_scope_deallocation_like_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_scope_enter_exit_rewrite_applied"
                && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_drop_rewrite_applied_count = records
        .iter()
        .filter(|record| record.rewrite_status == "actual_semantic_scope_drop_rewrite_applied")
        .count();
    let semantic_scope_unwind_pop_inserted_count = records
        .iter()
        .filter(|record| record.semantic_scope_unwind_pop_inserted)
        .count();
    let cross_thread_recovery_hint_count = records
        .iter()
        .filter(|record| record.cross_thread_recovery_hint)
        .count();
    let metadata_hints_requested =
        cli.lifetime_hint != 0 || cli.placement_hint != 0 || cli.auto_cross_thread_recovery_hint;
    let local_semantic_scope_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_drop_rewrite_applied"
            ) && record.replacement_symbol.ends_with("_local")
        })
        .count();
    let recovery_semantic_scope_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_drop_rewrite_applied"
            ) && !record.replacement_symbol.ends_with("_local")
        })
        .count();
    let actual_allocator_call_replacement = cli.actual_rewrite && rewrite_applied_count > 0;
    let actual_semantic_scope_rewrite =
        cli.semantic_scope_rewrite && semantic_scope_rewrite_applied_count > 0;
    let direct_replacement_resolution_status = if !cli.actual_rewrite {
        "not_requested_dry_run"
    } else if rewrite_applied_count > 0 {
        records
            .iter()
            .find(|record| {
                record.rewrite_status == "actual_allocator_call_replacement_applied"
                    && matches!(
                        record.replacement_resolution_status,
                        "resolved_unialloc_alloc_with_metadata"
                            | "resolved_unialloc_alloc_with_metadata_hints"
                    )
            })
            .or_else(|| {
                records.iter().find(|record| {
                    record.rewrite_status == "actual_allocator_call_replacement_applied"
                        && record
                            .replacement_resolution_status
                            .starts_with("resolved_unialloc_")
                })
            })
            .map(|record| record.replacement_resolution_status)
            .unwrap_or("resolved_unialloc_allocator_metadata_abi")
    } else if records.iter().any(|record| {
        record.replacement_resolution_status == "unialloc_alloc_with_metadata_hints_not_resolved"
    }) {
        "unialloc_alloc_with_metadata_hints_not_resolved"
    } else if records.iter().any(|record| {
        record.replacement_resolution_status == "unialloc_alloc_with_metadata_not_resolved"
    }) {
        "unialloc_alloc_with_metadata_not_resolved"
    } else if let Some(record) = records.iter().find(|record| {
        record
            .replacement_resolution_status
            .starts_with("unialloc_")
            && record
                .replacement_resolution_status
                .ends_with("_not_resolved")
    }) {
        record.replacement_resolution_status
    } else {
        "no_supported_rewrite_candidates"
    };
    let semantic_scope_replacement_resolution_status = if !cli.semantic_scope_rewrite {
        "not_requested_dry_run"
    } else if semantic_scope_rewrite_applied_count > 0 {
        match (
            metadata_hints_requested,
            local_semantic_scope_applied_count > 0,
            recovery_semantic_scope_applied_count > 0,
        ) {
            (true, true, true) => {
                "resolved_unialloc_semantic_scope_push_hints_mixed_local_recovery_pop"
            }
            (false, true, true) => "resolved_unialloc_semantic_scope_push_mixed_local_recovery_pop",
            (supports_hints, local, _) => {
                semantic_scope_resolution_status_for(supports_hints, local)
            }
        }
    } else if records.iter().any(|record| {
        record.replacement_resolution_status
            == "unialloc_semantic_scope_push_hints_local_pop_not_resolved"
    }) {
        "unialloc_semantic_scope_push_hints_local_pop_not_resolved"
    } else if records.iter().any(|record| {
        record.replacement_resolution_status
            == "unialloc_semantic_scope_push_local_pop_not_resolved"
    }) {
        "unialloc_semantic_scope_push_local_pop_not_resolved"
    } else if records.iter().any(|record| {
        record.replacement_resolution_status
            == "unialloc_semantic_scope_push_hints_pop_not_resolved"
    }) {
        "unialloc_semantic_scope_push_hints_pop_not_resolved"
    } else if records.iter().any(|record| {
        record.replacement_resolution_status == "unialloc_semantic_scope_push_pop_not_resolved"
    }) {
        "unialloc_semantic_scope_push_pop_not_resolved"
    } else {
        "no_supported_semantic_scope_candidates"
    };
    let replacement_resolution_status = if cli.actual_rewrite {
        direct_replacement_resolution_status
    } else if cli.semantic_scope_rewrite {
        semantic_scope_replacement_resolution_status
    } else {
        "not_requested_dry_run"
    };

    let mut json = String::new();
    json.push_str("{\n");
    json.push_str("  \"schema_version\": 1,\n");
    let _ = writeln!(json, "  \"source\": \"{}\",", PASS_NAME);
    json.push_str("  \"compiler_pass\": {\n");
    let _ = writeln!(json, "    \"name\": \"{}\",", PASS_NAME);
    json.push_str("    \"kind\": \"rustc_driver_optimized_mir_provider_override\",\n");
    json.push_str("    \"query_overridden\": \"optimized_mir\",\n");
    json.push_str("    \"body_clone_returned_to_rustc\": true,\n");
    let _ = writeln!(
        json,
        "    \"actual_allocator_call_replacement_requested\": {},",
        cli.actual_rewrite
    );
    let _ = writeln!(
        json,
        "    \"actual_semantic_scope_rewrite_requested\": {},",
        cli.semantic_scope_rewrite
    );
    let _ = writeln!(json, "    \"policy_flags\": {},", cli.policy_flags);
    let _ = writeln!(json, "    \"lifetime_hint\": {},", cli.lifetime_hint);
    let _ = writeln!(json, "    \"placement_hint\": {},", cli.placement_hint);
    let _ = writeln!(
        json,
        "    \"auto_cross_thread_recovery_hint\": {},",
        cli.auto_cross_thread_recovery_hint
    );
    let _ = writeln!(
        json,
        "    \"direct_local_metadata_abi\": {},",
        cli.direct_local_metadata_abi
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_with_semantic_drop\": {},",
        cli.direct_local_size_align_with_semantic_drop
    );
    let _ = writeln!(
        json,
        "    \"continue_compilation\": {},",
        cli.continue_compilation
    );
    let _ = writeln!(
        json,
        "    \"cross_thread_recovery_placement_hint_bit\": {},",
        PLACEMENT_HINT_CROSS_THREAD_RECOVERY
    );
    let _ = writeln!(
        json,
        "    \"default_policy_flags\": {},",
        DEFAULT_LOWERING_POLICY_FLAGS
    );
    let _ = writeln!(
        json,
        "    \"actual_allocator_call_replacement\": {},",
        actual_allocator_call_replacement
    );
    let _ = writeln!(
        json,
        "    \"actual_semantic_scope_rewrite\": {},",
        actual_semantic_scope_rewrite
    );
    let _ = writeln!(
        json,
        "    \"rewrite_applied_count\": {},",
        rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_rewrite_applied_count\": {},",
        semantic_scope_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_deallocation_like_rewrite_applied_count\": {},",
        semantic_scope_deallocation_like_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_rewrite_applied_count\": {},",
        semantic_scope_drop_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_unwind_pop_inserted_count\": {},",
        semantic_scope_unwind_pop_inserted_count
    );
    let _ = writeln!(
        json,
        "    \"cross_thread_recovery_hint_count\": {},",
        cross_thread_recovery_hint_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_unsolved_candidate_count\": {},",
        semantic_scope_unsolved_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_candidate_count\": {},",
        semantic_scope_drop_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_unsolved_candidate_count\": {},",
        semantic_scope_drop_unsolved_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_non_heap_skipped_count\": {},",
        semantic_scope_drop_non_heap_skipped_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_generic_type_parameter_skipped_count\": {},",
        semantic_scope_drop_generic_type_parameter_skipped_count
    );
    let _ = writeln!(
        json,
        "    \"replacement_resolution_status\": \"{}\",",
        replacement_resolution_status
    );
    let _ = writeln!(
        json,
        "    \"direct_replacement_resolution_status\": \"{}\",",
        direct_replacement_resolution_status
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_replacement_resolution_status\": \"{}\",",
        semantic_scope_replacement_resolution_status
    );
    json.push_str("    \"claim_grade\": false,\n");
    json.push_str("    \"notes\": [\n");
    json.push_str("      \"This installs a real rustc query override and returns cloned MIR bodies to rustc.\",\n");
    json.push_str("      \"Dry-run is the default; explicit actual modes can retarget direct allocator calls or insert semantic-scope enter/exit calls.\"\n");
    json.push_str("    ]\n");
    json.push_str("  },\n");
    json.push_str("  \"lowering_contract\": {\n");
    json.push_str("    \"target_allocator_abi\": \"__unialloc_alloc_with_metadata[_hints](size, align, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite), Rust-ABI __unialloc_{alloc,alloc_zeroed,realloc,dealloc}_layout_with_metadata[_hints] plus optional no-recovery alloc/alloc_zeroed/realloc/dealloc _local variants for explicit paired Layout lowerings; size/align __unialloc_alloc_with_metadata[_hints] remains recovery-backed because exchange_malloc has no direct paired dealloc rewrite (original Layout operands, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite); explicit GlobalAlloc receiver calls are lowered only when the receiver type is UniAlloc/RustAllocator\",\n");
    json.push_str("    \"semantic_scope_abi\": \"__unialloc_semantic_scope_push[_hints][_local](type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite) / __unialloc_semantic_scope_pop(); _local is selected only for a single normal path with no owner move/call before the exact destination Drop; otherwise recovery-backed\",\n");
    json.push_str("    \"size_source\": \"original MIR call arg 0\",\n");
    json.push_str("    \"align_source\": \"original MIR call arg 1\",\n");
    json.push_str("    \"semantic_heap_object_solver\": \"rustc_middle TyKind::Adt destination/argument solver plus MIR ShallowInitBox, Layout::array/new/for_value/for_value_raw constructor provenance, size_of/align_of typed Layout::from_size_align reconstruction, Layout::align_to/pad_to_align transformer provenance, same-source Layout::from_size_align reconstruction provenance, projection-aware/packed Layout::extend/repeat composite provenance tracking, Result<Layout>::ok plus Option<Layout>::expect/unwrap passthrough provenance, and canonicalized MIR place/ref/tuple projection provenance; unsolved candidates are audit-only\",\n");
    let _ = writeln!(json, "    \"type_id_algorithm\": \"{}\"", TYPE_ID_ALGORITHM);
    json.push_str("  },\n");
    json.push_str("  \"rustc_args\": [");
    for (idx, arg) in cli.rustc_args.iter().enumerate() {
        if idx > 0 {
            json.push_str(", ");
        }
        let _ = write!(json, "\"{}\"", json_escape(arg));
    }
    json.push_str("],\n");
    json.push_str("  \"summary\": {\n");
    let _ = writeln!(json, "    \"rewrite_candidate_count\": {},", records.len());
    let _ = writeln!(
        json,
        "    \"direct_allocator_call_site_count\": {},",
        direct_allocator_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"direct_layout_allocator_call_site_count\": {},",
        direct_layout_allocator_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"direct_size_align_allocator_call_site_count\": {},",
        direct_size_align_allocator_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"direct_size_align_recovery_backed_unpaired_dealloc_count\": {},",
        direct_size_align_recovery_backed_unpaired_dealloc_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_semantic_drop_scope_count\": {},",
        direct_local_size_align_semantic_drop_scope_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_metadata_contract_violation_count\": {},",
        direct_local_size_align_metadata_contract_violation_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_pairing_gap_count\": {},",
        direct_local_size_align_pairing_gap_count
    );
    push_direct_local_size_align_pairing_details_json(
        &mut json,
        "direct_local_size_align_pairing_details",
        &direct_local_size_align_pairing_counts,
        false,
        true,
    );
    push_direct_local_size_align_pairing_details_json(
        &mut json,
        "direct_local_size_align_pairing_gaps",
        &direct_local_size_align_pairing_counts,
        true,
        true,
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_candidate_count\": {},",
        semantic_scope_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_deallocation_like_candidate_count\": {},",
        semantic_scope_deallocation_like_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_unsolved_candidate_count\": {},",
        semantic_scope_unsolved_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_candidate_count\": {},",
        semantic_scope_drop_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_unsolved_candidate_count\": {},",
        semantic_scope_drop_unsolved_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_non_heap_skipped_count\": {},",
        semantic_scope_drop_non_heap_skipped_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_generic_type_parameter_skipped_count\": {},",
        semantic_scope_drop_generic_type_parameter_skipped_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_unwind_pop_inserted_count\": {},",
        semantic_scope_unwind_pop_inserted_count
    );
    let _ = writeln!(
        json,
        "    \"cross_thread_recovery_hint_count\": {},",
        cross_thread_recovery_hint_count
    );
    json.push_str("    \"provider_override_installed\": true,\n");
    json.push_str("    \"body_clone_returned_to_rustc\": true,\n");
    let _ = writeln!(
        json,
        "    \"continue_compilation\": {},",
        cli.continue_compilation
    );
    let _ = writeln!(
        json,
        "    \"actual_allocator_call_replacement_requested\": {},",
        cli.actual_rewrite
    );
    let _ = writeln!(
        json,
        "    \"direct_local_metadata_abi\": {},",
        cli.direct_local_metadata_abi
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_with_semantic_drop\": {},",
        cli.direct_local_size_align_with_semantic_drop
    );
    let _ = writeln!(
        json,
        "    \"actual_semantic_scope_rewrite_requested\": {},",
        cli.semantic_scope_rewrite
    );
    let _ = writeln!(
        json,
        "    \"actual_allocator_call_replacement\": {},",
        actual_allocator_call_replacement
    );
    let _ = writeln!(
        json,
        "    \"actual_semantic_scope_rewrite\": {},",
        actual_semantic_scope_rewrite
    );
    let _ = writeln!(
        json,
        "    \"rewrite_applied_count\": {},",
        rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"direct_layout_allocator_rewrite_applied_count\": {},",
        direct_layout_allocator_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"direct_size_align_allocator_call_site_count\": {},",
        direct_size_align_allocator_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"direct_size_align_recovery_backed_unpaired_dealloc_count\": {},",
        direct_size_align_recovery_backed_unpaired_dealloc_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_semantic_drop_scope_count\": {},",
        direct_local_size_align_semantic_drop_scope_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_metadata_contract_violation_count\": {},",
        direct_local_size_align_metadata_contract_violation_count
    );
    let _ = writeln!(
        json,
        "    \"direct_local_size_align_pairing_gap_count\": {},",
        direct_local_size_align_pairing_gap_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_rewrite_applied_count\": {},",
        semantic_scope_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_deallocation_like_rewrite_applied_count\": {},",
        semantic_scope_deallocation_like_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_drop_rewrite_applied_count\": {},",
        semantic_scope_drop_rewrite_applied_count
    );
    let _ = writeln!(
        json,
        "    \"replacement_resolution_status\": \"{}\",",
        replacement_resolution_status
    );
    let _ = writeln!(
        json,
        "    \"direct_replacement_resolution_status\": \"{}\",",
        direct_replacement_resolution_status
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_replacement_resolution_status\": \"{}\",",
        semantic_scope_replacement_resolution_status
    );
    json.push_str("    \"claim_grade\": false,\n");
    json.push_str("    \"complete_for_claim\": false\n");
    json.push_str("  },\n");
    json.push_str("  \"rewrite_candidates\": [\n");
    for (idx, record) in records.iter().enumerate() {
        if idx > 0 {
            json.push_str(",\n");
        }
        json.push_str("    {\n");
        let _ = writeln!(
            json,
            "      \"allocation_site_id\": \"{}\",",
            json_escape(&record.allocation_site_id)
        );
        let _ = writeln!(json, "      \"type_id\": {},", record.type_id);
        let _ = writeln!(json, "      \"module_id\": {},", record.module_id);
        let _ = writeln!(json, "      \"flags\": {},", record.flags);
        let _ = writeln!(json, "      \"lifetime_hint\": {},", record.lifetime_hint);
        let _ = writeln!(json, "      \"placement_hint\": {},", record.placement_hint);
        let _ = writeln!(
            json,
            "      \"cross_thread_recovery_hint\": {},",
            record.cross_thread_recovery_hint
        );
        let _ = writeln!(
            json,
            "      \"placement_hint_basis\": \"{}\",",
            record.placement_hint_basis
        );
        let _ = writeln!(json, "      \"callsite\": {},", record.callsite);
        let _ = writeln!(
            json,
            "      \"mir_function\": \"{}\",",
            json_escape(&record.mir_function)
        );
        let _ = writeln!(
            json,
            "      \"basic_block\": \"{}\",",
            json_escape(&record.basic_block)
        );
        let _ = writeln!(
            json,
            "      \"source_span\": \"{}\",",
            json_escape(&record.source_span)
        );
        let _ = writeln!(
            json,
            "      \"callee\": \"{}\",",
            json_escape(&record.callee)
        );
        let _ = writeln!(
            json,
            "      \"destination_place\": \"{}\",",
            json_escape(&record.destination_place)
        );
        let _ = writeln!(
            json,
            "      \"destination_type\": \"{}\",",
            json_escape(&record.destination_type)
        );
        json.push_str("      \"call_arguments\": [");
        for (arg_idx, arg) in record.call_arguments.iter().enumerate() {
            if arg_idx > 0 {
                json.push_str(", ");
            }
            let _ = write!(json, "\"{}\"", json_escape(arg));
        }
        json.push_str("],\n");
        json.push_str("      \"argument_types\": [");
        for (arg_idx, arg_ty) in record.argument_types.iter().enumerate() {
            if arg_idx > 0 {
                json.push_str(", ");
            }
            let _ = write!(json, "\"{}\"", json_escape(arg_ty));
        }
        json.push_str("],\n");
        let _ = writeln!(
            json,
            "      \"semantic_object_type\": \"{}\",",
            json_escape(&record.semantic_object_type)
        );
        let _ = writeln!(
            json,
            "      \"type_id_basis\": \"{}\",",
            record.type_id_basis
        );
        match &record.size_operand {
            Some(value) => {
                let _ = writeln!(json, "      \"size_operand\": \"{}\",", json_escape(value));
            }
            None => json.push_str("      \"size_operand\": null,\n"),
        }
        match &record.align_operand {
            Some(value) => {
                let _ = writeln!(json, "      \"align_operand\": \"{}\",", json_escape(value));
            }
            None => json.push_str("      \"align_operand\": null,\n"),
        }
        let _ = writeln!(
            json,
            "      \"rewrite_status\": \"{}\",",
            record.rewrite_status
        );
        let _ = writeln!(
            json,
            "      \"replacement_symbol\": \"{}\",",
            record.replacement_symbol
        );
        let _ = writeln!(
            json,
            "      \"replacement_resolution_status\": \"{}\",",
            record.replacement_resolution_status
        );
        let _ = writeln!(
            json,
            "      \"replacement_preview\": \"{}\",",
            json_escape(&record.replacement_preview)
        );
        let _ = writeln!(
            json,
            "      \"semantic_scope_unwind_pop_inserted\": {},",
            record.semantic_scope_unwind_pop_inserted
        );
        let _ = writeln!(
            json,
            "      \"metadata_pairing_contract\": \"{}\",",
            record.metadata_pairing_contract
        );
        let _ = writeln!(
            json,
            "      \"lowering_kind\": \"{}\"",
            record.lowering_kind
        );
        json.push_str("    }");
    }
    json.push_str("\n  ]\n");
    json.push_str("}\n");

    fs::write(&cli.audit_out, json)
        .map_err(|err| format!("write {}: {}", cli.audit_out.display(), err))?;
    Ok(())
}

fn write_pass_log(cli: &Cli, records: &[RewriteRecord]) -> Result<(), String> {
    let path = match &cli.pass_log_out {
        Some(path) => path,
        None => return Ok(()),
    };
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|err| format!("create {}: {}", parent.display(), err))?;
        }
    }
    let mut text = String::new();
    let _ = writeln!(text, "label: {}", PASS_NAME);
    text.push_str("query_overridden: optimized_mir\n");
    text.push_str("body_clone_returned_to_rustc: true\n");
    let rewrite_applied_count = records
        .iter()
        .filter(|record| record.rewrite_status == "actual_allocator_call_replacement_applied")
        .count();
    let direct_layout_allocator_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_allocator_call_replacement_applied"
                && direct_allocator_is_layout_based(direct_allocator_call_kind(&record.callee))
        })
        .count();
    let direct_size_align_allocator_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && direct_allocator_call_kind(&record.callee)
                    == DirectAllocatorCallKind::SizeAlignAlloc
        })
        .count();
    let direct_size_align_recovery_backed_unpaired_dealloc_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && record.metadata_pairing_contract
                    == "recovery_backed_size_align_alloc_unpaired_dealloc"
        })
        .count();
    let direct_local_size_align_semantic_drop_scope_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && record.metadata_pairing_contract == "local_metadata_abi_semantic_drop_scope"
        })
        .count();
    let direct_local_size_align_metadata_contract_violation_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "direct_allocator_call_rewrite"
                && direct_allocator_call_kind(&record.callee)
                    == DirectAllocatorCallKind::SizeAlignAlloc
                && record.replacement_symbol.ends_with("_local")
                && record.metadata_pairing_contract != "local_metadata_abi_semantic_drop_scope"
        })
        .count();
    let direct_local_size_align_pairing_gap_count =
        compute_direct_local_size_align_pairing_gap_count(records);
    let semantic_scope_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_enter_exit_rewrite")
        .count();
    let semantic_scope_deallocation_like_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_unsolved_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_unsolved_heap_object_candidate")
        .count();
    let semantic_scope_drop_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_drop_rewrite")
        .count();
    let semantic_scope_drop_unsolved_candidate_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_drop_unsolved_heap_object_candidate"
        })
        .count();
    let semantic_scope_drop_non_heap_skipped_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_drop_non_heap_object_skipped")
        .count();
    let semantic_scope_drop_generic_type_parameter_skipped_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_drop_generic_type_parameter_skipped"
        })
        .count();
    let semantic_scope_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_scope_enter_exit_rewrite_applied"
        })
        .count();
    let semantic_scope_deallocation_like_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_scope_enter_exit_rewrite_applied"
                && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_drop_rewrite_applied_count = records
        .iter()
        .filter(|record| record.rewrite_status == "actual_semantic_scope_drop_rewrite_applied")
        .count();
    let semantic_scope_unwind_pop_inserted_count = records
        .iter()
        .filter(|record| record.semantic_scope_unwind_pop_inserted)
        .count();
    let cross_thread_recovery_hint_count = records
        .iter()
        .filter(|record| record.cross_thread_recovery_hint)
        .count();
    let _ = writeln!(
        text,
        "actual_allocator_call_replacement_requested: {}",
        cli.actual_rewrite
    );
    let _ = writeln!(
        text,
        "actual_allocator_call_replacement: {}",
        cli.actual_rewrite && rewrite_applied_count > 0
    );
    let _ = writeln!(text, "rewrite_applied_count: {}", rewrite_applied_count);
    let _ = writeln!(
        text,
        "direct_layout_allocator_rewrite_applied_count: {}",
        direct_layout_allocator_rewrite_applied_count
    );
    let _ = writeln!(
        text,
        "direct_size_align_allocator_call_site_count: {}",
        direct_size_align_allocator_candidate_count
    );
    let _ = writeln!(
        text,
        "direct_size_align_recovery_backed_unpaired_dealloc_count: {}",
        direct_size_align_recovery_backed_unpaired_dealloc_count
    );
    let _ = writeln!(
        text,
        "direct_local_size_align_semantic_drop_scope_count: {}",
        direct_local_size_align_semantic_drop_scope_count
    );
    let _ = writeln!(
        text,
        "direct_local_size_align_metadata_contract_violation_count: {}",
        direct_local_size_align_metadata_contract_violation_count
    );
    let _ = writeln!(
        text,
        "direct_local_size_align_pairing_gap_count: {}",
        direct_local_size_align_pairing_gap_count
    );
    let _ = writeln!(
        text,
        "actual_semantic_scope_rewrite_requested: {}",
        cli.semantic_scope_rewrite
    );
    let _ = writeln!(text, "policy_flags: {}", cli.policy_flags);
    let _ = writeln!(text, "lifetime_hint: {}", cli.lifetime_hint);
    let _ = writeln!(text, "placement_hint: {}", cli.placement_hint);
    let _ = writeln!(
        text,
        "auto_cross_thread_recovery_hint: {}",
        cli.auto_cross_thread_recovery_hint
    );
    let _ = writeln!(text, "continue_compilation: {}", cli.continue_compilation);
    let _ = writeln!(
        text,
        "default_policy_flags: {}",
        DEFAULT_LOWERING_POLICY_FLAGS
    );
    let _ = writeln!(
        text,
        "actual_semantic_scope_rewrite: {}",
        cli.semantic_scope_rewrite && semantic_scope_rewrite_applied_count > 0
    );
    let _ = writeln!(
        text,
        "semantic_scope_candidate_count: {}",
        semantic_scope_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_deallocation_like_candidate_count: {}",
        semantic_scope_deallocation_like_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_unsolved_candidate_count: {}",
        semantic_scope_unsolved_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_drop_candidate_count: {}",
        semantic_scope_drop_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_drop_unsolved_candidate_count: {}",
        semantic_scope_drop_unsolved_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_drop_non_heap_skipped_count: {}",
        semantic_scope_drop_non_heap_skipped_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_drop_generic_type_parameter_skipped_count: {}",
        semantic_scope_drop_generic_type_parameter_skipped_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_rewrite_applied_count: {}",
        semantic_scope_rewrite_applied_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_deallocation_like_rewrite_applied_count: {}",
        semantic_scope_deallocation_like_rewrite_applied_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_drop_rewrite_applied_count: {}",
        semantic_scope_drop_rewrite_applied_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_unwind_pop_inserted_count: {}",
        semantic_scope_unwind_pop_inserted_count
    );
    let _ = writeln!(
        text,
        "cross_thread_recovery_hint_count: {}",
        cross_thread_recovery_hint_count
    );
    let _ = writeln!(text, "rewrite_candidate_count: {}", records.len());
    text.push_str("target_allocator_abi: __unialloc_alloc_with_metadata[_hints](size, align, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite), Rust-ABI __unialloc_{alloc,alloc_zeroed,realloc,dealloc}_layout_with_metadata[_hints] plus optional no-recovery alloc/alloc_zeroed/realloc/dealloc _local variants for explicit paired Layout lowerings; size/align __unialloc_alloc_with_metadata[_hints] remains recovery-backed because exchange_malloc has no direct paired dealloc rewrite (original Layout operands, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite); explicit GlobalAlloc receiver calls are lowered only when the receiver type is UniAlloc/RustAllocator\n");
    text.push_str("semantic_scope_abi: __unialloc_semantic_scope_push[_hints][_local](type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite) / __unialloc_semantic_scope_pop(); _local is selected only for a single normal path with no owner move/call before the exact destination Drop; otherwise recovery-backed\n");
    text.push_str("semantic_heap_object_solver: rustc_middle TyKind::Adt destination/argument solver plus MIR ShallowInitBox, Layout::array/new/for_value/for_value_raw constructor provenance, size_of/align_of typed Layout::from_size_align reconstruction, Layout::align_to/pad_to_align transformer provenance, same-source Layout::from_size_align reconstruction provenance, projection-aware/packed Layout::extend/repeat composite provenance tracking, Result<Layout>::ok plus Option<Layout>::expect/unwrap passthrough provenance, and canonicalized MIR place/ref/tuple projection provenance; unsolved candidates are audit-only\n");
    text.push_str("note: real rustc query override; dry-run by default, optional actual modes rewrite supported direct allocator calls or insert semantic-scope enter/exit calls\n");
    fs::write(path, text).map_err(|err| format!("write {}: {}", path.display(), err))?;
    Ok(())
}

fn exec_original_rustc(original_rustc_args: &[String]) -> ! {
    let (rustc, args) = match original_rustc_args.split_first() {
        Some(parts) => parts,
        None => {
            eprintln!("missing original rustc command");
            process::exit(2);
        }
    };
    #[cfg(unix)]
    {
        let err = Command::new(rustc).args(args).exec();
        eprintln!("failed to invoke original rustc `{}`: {}", rustc, err);
        process::exit(1);
    }
    #[cfg(not(unix))]
    {
        match Command::new(rustc).args(args).status() {
            Ok(status) => process::exit(status.code().unwrap_or(1)),
            Err(err) => {
                eprintln!("failed to invoke original rustc `{}`: {}", rustc, err);
                process::exit(1);
            }
        }
    }
}

fn main() {
    let cli = match parse_cli() {
        Ok(ParsedInvocation::RunPass(cli)) => cli,
        Ok(ParsedInvocation::Bypass(original_rustc_args)) => {
            exec_original_rustc(&original_rustc_args)
        }
        Err(err) => {
            eprintln!("{}", err);
            process::exit(2);
        }
    };
    unsafe {
        RECORDS = Some(Mutex::new(Vec::new()));
        ACTUAL_MIR_REWRITE = cli.actual_rewrite;
        ACTUAL_SEMANTIC_SCOPE_REWRITE = cli.semantic_scope_rewrite;
        LOWERING_POLICY_FLAGS = cli.policy_flags;
        LOWERING_LIFETIME_HINT = cli.lifetime_hint;
        LOWERING_PLACEMENT_HINT = cli.placement_hint;
        AUTO_CROSS_THREAD_RECOVERY_HINT = cli.auto_cross_thread_recovery_hint;
        DIRECT_LOCAL_METADATA_ABI = cli.direct_local_metadata_abi;
        DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP = cli.direct_local_size_align_with_semantic_drop;
        CONTINUE_COMPILATION = cli.continue_compilation;
    }
    let mut callbacks = RewriteDryRunCallbacks::default();
    #[cfg(unialloc_rustc_current)]
    run_compiler(&cli.rustc_args, &mut callbacks);
    #[cfg(not(unialloc_rustc_current))]
    let result = RunCompiler::new(&cli.rustc_args, &mut callbacks).run();
    let records = records_snapshot();
    if let Err(err) = write_json(&cli, &records) {
        eprintln!("{}", err);
        process::exit(1);
    }
    if let Err(err) = write_pass_log(&cli, &records) {
        eprintln!("{}", err);
        process::exit(1);
    }
    #[cfg(not(unialloc_rustc_current))]
    if let Err(err) = result {
        eprintln!("rustc_driver failed: {:?}", err);
        process::exit(1);
    }
}
