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
//! - the exact pointer-preserving `Box<[T], A> -> Vec<T, A>` standard-library
//!   ownership transfer can be retargeted to a UniAlloc metadata-rebind helper
//!   after DefId, structural type, generic-argument, and symbol proof.
//! - the exact pointer-preserving `String -> Vec<u8, Global>` standard-library
//!   ownership transfer can likewise be retargeted after an exact structural
//!   proof, without inserting a generic semantic allocation scope.
//!
//! The JSON remains a conservative compiler-owned evidence artifact: actual
//! modes are explicit flags, and every artifact keeps `claim_grade=false` until
//! runtime/full-surface validation exists.

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

#[cfg(unialloc_rustc_current)]
use rustc_abi::{Endian, Size};
#[cfg(unialloc_rustc_current)]
use rustc_data_structures::steal::Steal;
#[cfg(unialloc_rustc_current)]
use rustc_driver::{run_compiler, Callbacks, Compilation};
#[cfg(not(unialloc_rustc_current))]
use rustc_driver::{Callbacks, Compilation, RunCompiler};
use rustc_hir::def::Res;
use rustc_interface::interface;
#[cfg(unialloc_rustc_current)]
use rustc_middle::mir::interpret::Scalar;
use rustc_middle::mir::visit::{MutatingUseContext, NonMutatingUseContext, PlaceContext, Visitor};
use rustc_middle::mir::{
    BasicBlock, BasicBlockData, Body, CastKind, Local, LocalDecl, Location, Operand, Place, Rvalue,
    SourceInfo, StatementKind, Terminator, TerminatorKind, RETURN_PLACE,
};
#[cfg(unialloc_rustc_current)]
use rustc_middle::mir::{CallSource, UnwindAction, UnwindTerminateReason};
#[cfg(not(unialloc_rustc_current))]
use rustc_middle::mir::{Constant, ConstantKind};
#[cfg(not(unialloc_rustc_current))]
use rustc_middle::ty::adjustment::PointerCast;
#[cfg(unialloc_rustc_current)]
use rustc_middle::ty::adjustment::PointerCoercion;
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
use rustc_span::sym;
use rustc_span::Span;
#[cfg(unialloc_rustc_current)]
use rustc_span::Spanned;

use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fmt::Write as FmtWrite;
use std::fs;
use std::path::PathBuf;
use std::process::{self, Command};
use std::sync::{Mutex, OnceLock};
#[cfg(unix)]
use std::{os::unix::fs::PermissionsExt, os::unix::process::CommandExt};

const PASS_NAME: &str = "unialloc-rustc-driver-mir-rewrite-dry-run";
const LEGACY_UNIALLOC_LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;
const DEFAULT_LOWERING_POLICY_FLAGS: u32 = 0x1;
const FNV1A64_OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const RUNTIME_SEMANTIC_TYPE_ID_DOMAIN: &[u8] = b"rust-type-id-v2";
const MODULE_ID_ALGORITHM: &str =
    "unialloc keeps legacy 0xC002_DA00_0000_0001; other crates use nonzero(fnv1a64(mir-crate-module-v1 NUL normalized crate name NUL rustc -C metadata disambiguator, or canonical primary input path when metadata is absent, or full rustc argv as a last-resort invocation identity))";
const TYPE_ID_ALGORITHM: &str =
    "every allocator-authorizing nonzero type_id is derived from one exact resolved rustc_middle Ty: tcx.type_id_hash(owner_ty) supplies core TypeId's stable Hash128, core hashes the second target-native-endian u64 word, and UniAlloc applies nonzero(FNV-1a-64(rust-type-id-v2 || that u64 in target-native-endian bytes)); semantic_object_type and all callee/type debug strings are audit display only and never authorize reuse; exact canonical semantic allocation scopes carry a compiler-derived numeric id, while exact Global Box and Vec::with_capacity scopes use the monomorphized generic runtime helper that computes the same semantic_type_id<T>; ownership transfers carry exact old/new Ty values and abstain unless both compiler-derived ids resolve; owner scans and ambiguity dedup key each supported owner by compiler-derived id plus display text, preserving distinct same-display nominal types; unresolved types, aliases, inference/placeholder/escaping-variable types, text-only Layout provenance, and composite/reconstructed Layouts use neutral type_id=0 and recovery-backed metadata; every direct allocator call remains neutral and recovery-backed until a CFG-proven exact owner link exists; Layout provenance remains audit-only because the current block-order map is not a CFG reaching-definition proof; built-in owners and methods remain authenticated against compiler lang/diagnostic items and canonical core/alloc/std CrateNums; hashbrown/indexmap and callback-capable receiver calls remain audit-only; exact Clone lowering remains limited to compiler-authenticated alloc String and Vec<u8, Global>, while exact slice::to_vec whole-call scopes require compiler primitive scalar elements; effectful owner, wrapper-owned, and dropful Box Drops stay unwrapped and recover identity from authenticated allocation records; the pinned rustc TypeId algorithm, the 64-bit runtime identity width, and the unique unialloc replacement-ABI CrateNum are explicit compiler/build provenance trust boundaries";
const UNKNOWN_HEAP_OBJECT_TYPE: &str = "<unknown-heap-object-type>";
const PLACEMENT_HINT_CROSS_THREAD_RECOVERY: u16 = 1 << 15;
const LIFETIME_PROFILE_FORMAT_V1: &str = "unialloc-lifetime-profile-v1";
const LIFETIME_PROFILE_FORMAT_V2: &str = "unialloc-lifetime-profile-v2";
const LIFETIME_PROFILE_UNSUPPORTED_FORMAT: &str = "unsupported";
const LIFETIME_PROFILE_V1_CONFIDENCE: u8 = 100;
const LIFETIME_PROFILE_BINDING: &str = "exact(callsite,type_id,module_id)";
const LIFETIME_PROFILE_DIGEST_ALGORITHM: &str = "fnv1a64-raw-bytes";
const LIFETIME_HINT_EPHEMERAL: u16 = 1;
const LIFETIME_HINT_LONG_LIVED: u16 = 2;
/// Bounded process-long oracle used only for exact `mem::forget`/`Box::leak`
/// smoke patterns. This tag is deliberately outside real-program Long claims.
const LIFETIME_HINT_BOUNDED_PROCESS_LONG_ORACLE: u16 = 0xA102;
const RUST_LIFETIME_PRIOR_SHORT_CONFIDENCE: u8 = 85;
const RUST_LIFETIME_PRIOR_LONG_CONFIDENCE: u8 = 70;
const AUTOMATIC_LIFETIME_CLASSIFIER_PRECEDENCE: &str =
    "exact_profile>manual_global>automatic>Unknown";
const AUTOMATIC_HEAP_LIFETIME_INFERENCE_PRECEDENCE: &str =
    "exact_profile>manual_global>automatic_heap>automatic_epoch>Unknown";
const AUTOMATIC_RUST_LIFETIME_PRIOR_PRECEDENCE: &str =
    "exact_profile>manual_global>bounded_process_long_oracle>automatic_rust_lifetime_prior>automatic_heap_fact>automatic_epoch>Unknown";

#[cfg(unialloc_rustc_current)]
type OptimizedMirDefId = LocalDefId;
#[cfg(not(unialloc_rustc_current))]
type OptimizedMirDefId = DefId;

static mut ORIGINAL_OPTIMIZED_MIR: Option<
    for<'tcx> fn(TyCtxt<'tcx>, OptimizedMirDefId) -> &'tcx Body<'tcx>,
> = None;
#[cfg(unialloc_rustc_current)]
static mut ORIGINAL_MIR_DROPS_ELABORATED_AND_CONST_CHECKED: Option<
    for<'tcx> fn(TyCtxt<'tcx>, LocalDefId) -> &'tcx Steal<Body<'tcx>>,
> = None;
static mut RECORDS: Option<Mutex<Vec<RewriteRecord>>> = None;
static mut ACTUAL_MIR_REWRITE: bool = false;
static mut ACTUAL_SEMANTIC_SCOPE_REWRITE: bool = false;
static mut LOWERING_MODULE_ID: u64 = LEGACY_UNIALLOC_LOWERING_MODULE_ID;
static mut LOWERING_POLICY_FLAGS: u32 = DEFAULT_LOWERING_POLICY_FLAGS;
static mut LOWERING_LIFETIME_HINT: u16 = 0;
static mut LOWERING_LIFETIME_CONFIDENCE_THRESHOLD: u8 = 0;
static mut AUTO_LIFETIME_CLASSIFIER: bool = false;
static mut AUTO_HEAP_LIFETIME_INFERENCE: bool = false;
static mut AUTO_RUST_LIFETIME_PRIOR: bool = false;
static mut LOWERING_PLACEMENT_HINT: u16 = 0;
static mut AUTO_CROSS_THREAD_RECOVERY_HINT: bool = false;
static mut DIRECT_LOCAL_METADATA_ABI: bool = false;
static mut DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP: bool = false;
static mut CONTINUE_COMPILATION: bool = false;
static LIFETIME_PROFILE: OnceLock<LifetimeProfile> = OnceLock::new();

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
    lifetime_confidence_threshold: u8,
    lifetime_profile: Option<LifetimeProfile>,
    auto_lifetime_classifier: bool,
    auto_heap_lifetime_inference: bool,
    auto_rust_lifetime_prior: bool,
    placement_hint: u16,
    auto_cross_thread_recovery_hint: bool,
    direct_local_metadata_abi: bool,
    direct_local_size_align_with_semantic_drop: bool,
    continue_compilation: bool,
    rustc_args: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct LifetimeProfileKey {
    callsite: u64,
    type_id: u64,
    module_id: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LifetimeProfileEntry {
    Unique { hint: u16, confidence: u8 },
    Invalid,
    Ambiguous,
}

#[derive(Clone, Debug)]
struct LifetimeProfile {
    path: PathBuf,
    raw_digest: u64,
    format: Option<&'static str>,
    format_valid: bool,
    source_entry_line_count: usize,
    invalid_line_count: usize,
    duplicate_key_count: usize,
    entries: BTreeMap<LifetimeProfileKey, LifetimeProfileEntry>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct LifetimeHintSelection {
    hint: u16,
    confidence: u8,
    basis: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AutomaticLifetimeDecision {
    ExactLocalDropBeforePhaseBoundary,
    ExactLocalDropAfterPhaseBoundary,
    EffectfulDropGlueUnknown,
    AliasOrEscapeUnknown,
    MissingOrCleanupDropUnknown,
    NonlinearControlFlowUnknown,
    InterveningCallMayAdvanceEpochUnknown,
    CleanupBeforeBoundaryUnknown,
    AmbiguousOwnerSiteUnknown,
    UnsupportedSiteUnknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AutomaticHeapLifetimeDecision {
    ExactLocalDrop,
    ExactLocalMoveChainDrop,
    ExactMemForget,
    ExactBoxLeak,
    AliasOrEscapeUnknown,
    CleanupOrUnwindUnknown,
    MissingTerminalUnknown,
    NonlinearControlFlowUnknown,
    UnsupportedOwnerUnknown,
    RefcountedOwnerUnknown,
    AmbiguousOwnerSiteUnknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AutomaticRustLifetimePriorDecision {
    AllPathLocalReleaseShort,
    ReceiverOwnedShort,
    ReturnLong,
    EscapeLong,
    CleanupOrUnwindUnknown,
    OwnerLiveCallUnknown,
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
    lifetime_hint_confidence: u8,
    lifetime_hint_basis: &'static str,
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
    lifetime_analysis_features: Option<SemanticLifetimeFeatureExport>,
}

#[derive(Clone, Debug)]
struct SemanticLifetimeFeatureExport {
    analysis_phase: &'static str,
    owner_place: Option<String>,
    owner_place_basis: &'static str,
    requested_size_bytes: Option<u64>,
    requested_align_bytes: Option<u64>,
    requested_layout_basis: &'static str,
    owner_move_count: usize,
    return_sink: bool,
    escape_sink: bool,
    store_sink: bool,
    owner_live_opaque_call: bool,
    allocation_in_natural_loop: bool,
    reachable_backedge_after_allocation: bool,
    function_has_yield_or_await: bool,
    reachable_yield_or_await: bool,
    receiver_owned_allocation: bool,
    exact_drop_path: bool,
    conditional_drop_path: bool,
    cleanup_drop_path: bool,
    normal_drop_blocks: Vec<String>,
    cleanup_drop_blocks: Vec<String>,
    return_sink_blocks: Vec<String>,
    escape_sink_blocks: Vec<String>,
    store_sink_blocks: Vec<String>,
    owner_live_opaque_call_blocks: Vec<String>,
    reachable_normal_blocks: Vec<String>,
    reachable_cleanup_blocks: Vec<String>,
    normal_successor_count: usize,
    cleanup_successor_count: usize,
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
    recovery_backed_layout_alloc: Option<AllocMetadataAbi>,
    recovery_backed_layout_alloc_zeroed: Option<AllocMetadataAbi>,
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
    generic_push_def_id: Option<DefId>,
    pop_def_id: DefId,
    push_symbol: &'static str,
    supports_hints: bool,
    local_no_recovery: bool,
}

#[derive(Clone, Copy, Debug)]
struct SemanticOwnershipTransferAbi {
    box_slice_into_vec_def_id: DefId,
    vec_into_boxed_slice_def_id: DefId,
    string_into_bytes_def_id: DefId,
    string_into_boxed_str_def_id: DefId,
    boxed_str_into_string_def_id: DefId,
    cstring_into_bytes_with_nul_def_id: DefId,
    vec_into_iter_def_id: DefId,
}

const SEMANTIC_BOX_SLICE_INTO_VEC_SYMBOL: &str = "__unialloc_semantic_box_slice_into_vec";
const SEMANTIC_VEC_INTO_BOXED_SLICE_SYMBOL: &str = "__unialloc_semantic_vec_into_boxed_slice";
const SEMANTIC_STRING_INTO_BYTES_SYMBOL: &str = "__unialloc_semantic_string_into_bytes";
const SEMANTIC_STRING_INTO_BOXED_STR_SYMBOL: &str = "__unialloc_semantic_string_into_boxed_str";
const SEMANTIC_BOXED_STR_INTO_STRING_SYMBOL: &str = "__unialloc_semantic_boxed_str_into_string";
const SEMANTIC_CSTRING_INTO_BYTES_WITH_NUL_SYMBOL: &str =
    "__unialloc_semantic_cstring_into_bytes_with_nul";
const SEMANTIC_VEC_INTO_ITER_SYMBOL: &str = "__unialloc_semantic_vec_into_iter";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SemanticOwnershipTransferKind {
    BoxSliceIntoVec,
    VecIntoBoxedSlice,
    StringIntoBytes,
    StringIntoBoxedStr,
    BoxedStrIntoString,
    CStringIntoBytesWithNul,
    VecIntoIter,
    VecIntoIterViaZip,
}

#[derive(Clone, Debug)]
enum SemanticOwnershipTransferCallShape<'tcx> {
    Direct,
    IteratorZip {
        zip_def_id: DefId,
        iterator_ty: Ty<'tcx>,
        into_iter_ty: Ty<'tcx>,
    },
}

#[derive(Clone, Debug)]
struct SemanticOwnershipTransferProof<'tcx> {
    element_ty: Ty<'tcx>,
    allocator_ty: Ty<'tcx>,
    old_owner_ty: Ty<'tcx>,
    new_owner_ty: Ty<'tcx>,
    old_owner_type: String,
    new_owner_type: String,
}

#[derive(Clone, Debug)]
struct SemanticOwnershipTransferCandidate<'tcx> {
    bb: BasicBlock,
    callee: String,
    call_arguments: Vec<String>,
    argument_types: Vec<String>,
    destination: Place<'tcx>,
    destination_place: String,
    destination_type: String,
    kind: SemanticOwnershipTransferKind,
    call_shape: SemanticOwnershipTransferCallShape<'tcx>,
    proof: SemanticOwnershipTransferProof<'tcx>,
    expected_old_owner_ty: Ty<'tcx>,
    expected_old_owner_type: String,
    expected_old_owner_basis: &'static str,
    source_span: String,
    fn_span: Span,
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

fn generic_semantic_scope_push_symbol(local_no_recovery: bool) -> &'static str {
    match (lowering_metadata_hints_requested(), local_no_recovery) {
        (true, true) => "__unialloc_semantic_scope_push_for_rust_type_hints_local",
        (false, true) => "__unialloc_semantic_scope_push_for_rust_type_local",
        (true, false) => "__unialloc_semantic_scope_push_for_rust_type_hints",
        (false, false) => "__unialloc_semantic_scope_push_for_rust_type",
    }
}

fn generic_semantic_scope_resolution_status(
    supports_hints: bool,
    local_no_recovery: bool,
) -> &'static str {
    match (supports_hints, local_no_recovery) {
        (true, true) => "resolved_unialloc_semantic_scope_push_for_rust_type_hints_local_pop",
        (false, true) => "resolved_unialloc_semantic_scope_push_for_rust_type_local_pop",
        (true, false) => "resolved_unialloc_semantic_scope_push_for_rust_type_hints_pop",
        (false, false) => "resolved_unialloc_semantic_scope_push_for_rust_type_pop",
    }
}

fn generic_semantic_scope_unresolved_status(
    supports_hints: bool,
    local_no_recovery: bool,
) -> &'static str {
    match (supports_hints, local_no_recovery) {
        (true, true) => "unialloc_semantic_scope_push_for_rust_type_hints_local_pop_not_resolved",
        (false, true) => "unialloc_semantic_scope_push_for_rust_type_local_pop_not_resolved",
        (true, false) => "unialloc_semantic_scope_push_for_rust_type_hints_pop_not_resolved",
        (false, false) => "unialloc_semantic_scope_push_for_rust_type_pop_not_resolved",
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
enum SemanticScopeHeapClass {
    Single(String),
    Ambiguous(Vec<String>),
    CallbackCapable,
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
    compiler_type_id: Option<u64>,
    runtime_identity_owner_ty: Option<Ty<'tcx>>,
    plain_clone_heap_class: PlainCloneHeapClass,
    non_plain_heap_class: Option<SemanticScopeHeapClass>,
    original_target: Option<BasicBlock>,
    original_unwind: MirUnwind,
    original_from_hir_call: MirCallSource,
    fn_span: Span,
    original_terminator: Terminator<'tcx>,
    feature_owner: Option<Place<'tcx>>,
    feature_owner_basis: &'static str,
    receiver_owned_allocation: bool,
    lifetime_analysis_features: Option<SemanticLifetimeFeatureExport>,
}

#[derive(Clone, Debug)]
struct SemanticDropCandidate<'tcx> {
    bb: BasicBlock,
    original_is_cleanup: bool,
    drop_place: String,
    drop_type: String,
    semantic_object_type: String,
    compiler_type_id: Option<u64>,
    runtime_identity_owner_ty: Option<Ty<'tcx>>,
    recovery_only_exact_box_drop: bool,
    recovery_only_effectful_owner_drop: bool,
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
    "Usage:\n  RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs -o /tmp/unialloc-rustc-mir-rewrite-dry-run\n  DYLD_LIBRARY_PATH=$(rustc +$(cat rust-toolchain) --print sysroot)/lib /tmp/unialloc-rustc-mir-rewrite-dry-run \\\n    --unialloc-rewrite-audit-out /tmp/rewrite-audit.json -- --sysroot $(rustc +$(cat rust-toolchain) --print sysroot) --edition=2021 input.rs\n\nOptions:\n  --unialloc-rewrite-audit-out <path>  JSON rewrite audit output. Defaults to env UNIALLOC_REWRITE_AUDIT_OUT or ./unialloc-rustc-mir-rewrite-dry-run.json\n  --unialloc-rewrite-audit-dir <dir>   Wrapper-friendly output directory. Defaults to env UNIALLOC_REWRITE_AUDIT_DIR if set\n  --unialloc-pass-log-out <path>       Optional pass log output. Defaults to env UNIALLOC_PASS_LOG_OUT if set\n  --unialloc-pass-log-dir <dir>        Wrapper-friendly log directory. Defaults to env UNIALLOC_PASS_LOG_DIR if set\n  --unialloc-target-crates <names>     Optional comma-separated Cargo crate allowlist. Defaults to env UNIALLOC_RUSTC_TARGET_CRATES. Hyphens and underscores compare equivalently\n  --unialloc-actual-mir-rewrite        Opt in to replacing supported direct allocator-call terminators. Defaults to env UNIALLOC_ACTUAL_MIR_REWRITE truthiness\n  --unialloc-actual-semantic-scope-rewrite  Opt in to inserting __unialloc_semantic_scope_push/pop around supported semantic allocation calls. Defaults to env UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE truthiness\n  --unialloc-auto-lifetime-classifier  Classify stable nonescaping linear owners as Ephemeral before, or LongLived across, an exact UniAlloc epoch boundary; read-only uses are allowed and every unproven site stays Unknown. Defaults to env UNIALLOC_AUTO_LIFETIME_CLASSIFIER truthiness\n  --unialloc-auto-heap-lifetime-inference  Export marker-free ownership features; exact Drop stays Unknown as an eventual-release fact, while exact mem::forget/Box::leak may emit only the bounded process-long smoke oracle. No automatic short or real-program Long claim is made. Defaults to env UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE truthiness\n  --unialloc-auto-rust-lifetime-prior  Emit advisory Short/Long hints from MIR owner live ranges, all-path local release, receiver ownership, and propagated return/escape carriers. Runtime per-site correction remains authoritative. Defaults to env UNIALLOC_AUTO_RUST_LIFETIME_PRIOR truthiness\n  --unialloc-auto-cross-thread-recovery-hint  OR the cross-thread recovery placement bit into solved heap-object scopes when the MIR body contains thread-spawn/escape calls. Defaults to env UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT truthiness\n  --unialloc-direct-local-metadata-abi  Use no-recovery UniAlloc Layout alloc/alloc_zeroed/realloc/dealloc ABI variants only when paired metadata deallocation/reallocation is explicit; unpaired size/align exchange_malloc keeps recovery ABI. Defaults to env UNIALLOC_DIRECT_LOCAL_METADATA_ABI truthiness\n  --unialloc-direct-local-size-align-with-semantic-drop  Enable no-recovery semantic scopes only for destination-proven linear Drop ownership; raw size/align calls without an exact owner link remain recovery-backed. Defaults to env UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP truthiness\n  --unialloc-policy-flags <u32>        Policy flags inserted into UniAlloc metadata. Defaults to env UNIALLOC_LOWERING_POLICY_FLAGS or 0x1 (TYPE_ISOLATED only)\n  --unialloc-lifetime-hint <u16>       Optional invocation-wide metadata lifetime hint. Defaults to env UNIALLOC_LOWERING_LIFETIME_HINT or 0\n  --unialloc-lifetime-profile <path>   Exact per-site lifetime profile. Defaults to env UNIALLOC_LIFETIME_PROFILE. Profile misses override manual and automatic inputs with Unknown (0)\n  --unialloc-lifetime-confidence-threshold <0..100>  Minimum v2 profile or automatic-classifier confidence to emit a lifetime hint. Defaults to env UNIALLOC_LIFETIME_CONFIDENCE_THRESHOLD or 0\n  --unialloc-placement-hint <u16>      Optional metadata placement hint. Defaults to env UNIALLOC_LOWERING_PLACEMENT_HINT or 0\n  --unialloc-continue-compilation      Continue after analysis and produce normal rustc outputs. Actual rewrite modes continue by default. Defaults to env UNIALLOC_CONTINUE_COMPILATION truthiness\n  --unialloc-stop-after-analysis       Force analysis-only output even if an actual rewrite flag is set\n  --unialloc-dry-run-only              Force audit-only mode even if actual rewrite env flags are set\n  --unialloc-help                      Print this help\n\nEverything after `--` is passed to rustc_driver as a direct invocation. Without `--`, an executable path or a bare command resolved through PATH/PATHEXT is treated as Cargo's compiler argv0. Executable source files therefore require explicit `--` for direct compilation.\n"
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

fn parse_u64(value: &str) -> Result<u64, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err("empty u64 value".to_string());
    }
    if let Some(hex) = trimmed
        .strip_prefix("0x")
        .or_else(|| trimmed.strip_prefix("0X"))
    {
        u64::from_str_radix(hex, 16).map_err(|err| format!("invalid hex u64 `{}`: {}", value, err))
    } else {
        trimmed
            .parse::<u64>()
            .map_err(|err| format!("invalid u64 `{}`: {}", value, err))
    }
}

fn parse_profile_lifetime_hint(value: &str) -> Result<u16, String> {
    match value.trim() {
        "1" | "ephemeral" => Ok(LIFETIME_HINT_EPHEMERAL),
        "2" | "long-lived" | "long_lived" => Ok(LIFETIME_HINT_LONG_LIVED),
        _ => Err(format!(
            "invalid lifetime class `{}`; expected ephemeral/1 or long-lived/2",
            value
        )),
    }
}

fn parse_profile_confidence(value: &str) -> Result<u8, String> {
    let confidence = parse_u16(value)?;
    if (1..=100).contains(&confidence) {
        Ok(confidence as u8)
    } else {
        Err(format!(
            "invalid lifetime profile confidence `{}`; expected 1..=100",
            value
        ))
    }
}

fn parse_lifetime_confidence_threshold(value: &str) -> Result<u8, String> {
    let threshold = parse_u16(value)?;
    if threshold <= 100 {
        Ok(threshold as u8)
    } else {
        Err(format!(
            "invalid lifetime confidence threshold `{}`; expected 0..=100",
            value
        ))
    }
}

fn insert_lifetime_profile_entry(
    entries: &mut BTreeMap<LifetimeProfileKey, LifetimeProfileEntry>,
    key: LifetimeProfileKey,
    entry: LifetimeProfileEntry,
) -> bool {
    use std::collections::btree_map::Entry;
    match entries.entry(key) {
        Entry::Vacant(vacant) => {
            vacant.insert(entry);
            false
        }
        Entry::Occupied(mut occupied) => {
            occupied.insert(LifetimeProfileEntry::Ambiguous);
            true
        }
    }
}

fn parse_lifetime_profile(path: PathBuf, raw: &[u8]) -> LifetimeProfile {
    let text = String::from_utf8_lossy(raw);
    let mut profile = LifetimeProfile {
        path,
        raw_digest: fnv1a64(raw),
        format: None,
        format_valid: false,
        source_entry_line_count: 0,
        invalid_line_count: 0,
        duplicate_key_count: 0,
        entries: BTreeMap::new(),
    };
    let mut content_lines = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'));
    match content_lines.next() {
        Some(header) if header == LIFETIME_PROFILE_FORMAT_V1 => {
            profile.format = Some(LIFETIME_PROFILE_FORMAT_V1);
            profile.format_valid = true;
        }
        Some(header) if header == LIFETIME_PROFILE_FORMAT_V2 => {
            profile.format = Some(LIFETIME_PROFILE_FORMAT_V2);
            profile.format_valid = true;
        }
        Some(_) | None => {
            profile.invalid_line_count = 1;
            return profile;
        }
    }

    let format = profile.format.expect("validated lifetime profile format");
    let expected_field_count = if format == LIFETIME_PROFILE_FORMAT_V1 {
        4
    } else {
        5
    };

    for line in content_lines {
        profile.source_entry_line_count += 1;
        let fields = line.split_whitespace().collect::<Vec<_>>();
        if fields.len() != expected_field_count {
            profile.invalid_line_count += 1;
            continue;
        }
        let callsite = parse_u64(fields[0]);
        let type_id = parse_u64(fields[1]);
        let module_id = parse_u64(fields[2]);
        let hint = parse_profile_lifetime_hint(fields[3]);
        let confidence = if format == LIFETIME_PROFILE_FORMAT_V1 {
            Ok(LIFETIME_PROFILE_V1_CONFIDENCE)
        } else {
            parse_profile_confidence(fields[4])
        };
        let key = match (callsite, type_id, module_id) {
            (Ok(callsite), Ok(type_id), Ok(module_id)) => LifetimeProfileKey {
                callsite,
                type_id,
                module_id,
            },
            _ => {
                profile.invalid_line_count += 1;
                continue;
            }
        };
        let entry = match (hint, confidence) {
            (Ok(hint), Ok(confidence))
                if key.callsite != 0 && key.type_id != 0 && key.module_id != 0 =>
            {
                LifetimeProfileEntry::Unique { hint, confidence }
            }
            _ => {
                profile.invalid_line_count += 1;
                LifetimeProfileEntry::Invalid
            }
        };
        if insert_lifetime_profile_entry(&mut profile.entries, key, entry) {
            profile.duplicate_key_count += 1;
        }
    }
    profile
}

fn load_lifetime_profile(path: PathBuf) -> Result<LifetimeProfile, String> {
    let raw = fs::read(&path)
        .map_err(|err| format!("read lifetime profile {}: {}", path.display(), err))?;
    Ok(parse_lifetime_profile(path, &raw))
}

fn select_lifetime_hint(
    profile: Option<&LifetimeProfile>,
    configured_hint: u16,
    confidence_threshold: u8,
    callsite: u64,
    type_id: u64,
    module_id: u64,
) -> LifetimeHintSelection {
    let profile = match profile {
        Some(profile) => profile,
        None => {
            return LifetimeHintSelection {
                hint: configured_hint,
                confidence: if configured_hint == 0 {
                    0
                } else {
                    LIFETIME_PROFILE_V1_CONFIDENCE
                },
                basis: if configured_hint == 0 {
                    "default_unknown"
                } else {
                    "manual_global_lifetime_hint"
                },
            };
        }
    };
    if !profile.format_valid {
        return LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "profile_invalid_format",
        };
    }
    let key = LifetimeProfileKey {
        callsite,
        type_id,
        module_id,
    };
    match profile.entries.get(&key) {
        Some(LifetimeProfileEntry::Unique {
            hint: _,
            confidence,
        }) if *confidence < confidence_threshold => LifetimeHintSelection {
            hint: 0,
            confidence: *confidence,
            basis: "profile_below_confidence_threshold",
        },
        Some(LifetimeProfileEntry::Unique { hint, confidence }) => LifetimeHintSelection {
            hint: *hint,
            confidence: *confidence,
            basis: "profile_exact_match",
        },
        Some(LifetimeProfileEntry::Invalid) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "profile_invalid_entry",
        },
        Some(LifetimeProfileEntry::Ambiguous) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "profile_ambiguous_entry",
        },
        None if profile.entries.keys().any(|key| key.callsite == callsite) => {
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_type_or_module_guard_mismatch",
            }
        }
        None => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "profile_missing_entry",
        },
    }
}

fn select_lifetime_hint_with_automatic_decision(
    profile: Option<&LifetimeProfile>,
    configured_hint: u16,
    confidence_threshold: u8,
    automatic_classifier_enabled: bool,
    automatic_decision: Option<AutomaticLifetimeDecision>,
    callsite: u64,
    type_id: u64,
    module_id: u64,
) -> LifetimeHintSelection {
    // An explicitly supplied profile remains fail-closed and authoritative:
    // a profile miss does not silently fall back to a different classifier.
    // The invocation-wide hint is likewise an explicit operator override.
    if profile.is_some() || configured_hint != 0 || !automatic_classifier_enabled {
        return select_lifetime_hint(
            profile,
            configured_hint,
            confidence_threshold,
            callsite,
            type_id,
            module_id,
        );
    }

    let selection = match automatic_decision {
        Some(AutomaticLifetimeDecision::ExactLocalDropBeforePhaseBoundary) => {
            LifetimeHintSelection {
                hint: LIFETIME_HINT_EPHEMERAL,
                confidence: 100,
                basis: "automatic_exact_local_drop_before_phase_boundary",
            }
        }
        Some(AutomaticLifetimeDecision::ExactLocalDropAfterPhaseBoundary) => {
            LifetimeHintSelection {
                hint: LIFETIME_HINT_LONG_LIVED,
                confidence: 100,
                basis: "automatic_exact_local_drop_after_phase_boundary",
            }
        }
        Some(AutomaticLifetimeDecision::EffectfulDropGlueUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_effectful_drop_glue_unknown",
        },
        Some(AutomaticLifetimeDecision::AliasOrEscapeUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_alias_or_escape_unknown",
        },
        Some(AutomaticLifetimeDecision::MissingOrCleanupDropUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_missing_or_cleanup_drop_unknown",
        },
        Some(AutomaticLifetimeDecision::NonlinearControlFlowUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_nonlinear_control_flow_unknown",
        },
        Some(AutomaticLifetimeDecision::InterveningCallMayAdvanceEpochUnknown) => {
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "automatic_intervening_call_may_advance_epoch_unknown",
            }
        }
        Some(AutomaticLifetimeDecision::CleanupBeforeBoundaryUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_cleanup_before_boundary_unknown",
        },
        Some(AutomaticLifetimeDecision::AmbiguousOwnerSiteUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_ambiguous_owner_site_unknown",
        },
        Some(AutomaticLifetimeDecision::UnsupportedSiteUnknown) | None => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_unsupported_site_unknown",
        },
    };
    if selection.hint != 0 && selection.confidence < confidence_threshold {
        LifetimeHintSelection {
            hint: 0,
            confidence: selection.confidence,
            basis: "automatic_below_confidence_threshold",
        }
    } else {
        selection
    }
}

fn automatic_heap_lifetime_selection(
    decision: Option<AutomaticHeapLifetimeDecision>,
) -> LifetimeHintSelection {
    match decision {
        Some(AutomaticHeapLifetimeDecision::ExactLocalDrop) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_exact_local_drop_fact_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_exact_move_chain_drop_fact_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::ExactMemForget) => LifetimeHintSelection {
            hint: LIFETIME_HINT_BOUNDED_PROCESS_LONG_ORACLE,
            confidence: 100,
            basis: "automatic_heap_exact_mem_forget",
        },
        Some(AutomaticHeapLifetimeDecision::ExactBoxLeak) => LifetimeHintSelection {
            hint: LIFETIME_HINT_BOUNDED_PROCESS_LONG_ORACLE,
            confidence: 100,
            basis: "automatic_heap_exact_box_leak",
        },
        Some(AutomaticHeapLifetimeDecision::AliasOrEscapeUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_alias_or_escape_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::CleanupOrUnwindUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_cleanup_or_unwind_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::MissingTerminalUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_missing_terminal_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::NonlinearControlFlowUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_nonlinear_control_flow_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::UnsupportedOwnerUnknown) | None => {
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "automatic_heap_unsupported_owner_unknown",
            }
        }
        Some(AutomaticHeapLifetimeDecision::RefcountedOwnerUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_refcounted_owner_unknown",
        },
        Some(AutomaticHeapLifetimeDecision::AmbiguousOwnerSiteUnknown) => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_heap_ambiguous_owner_site_unknown",
        },
    }
}

fn automatic_rust_lifetime_prior_decision(
    automatic_decision: Option<AutomaticLifetimeDecision>,
    heap_decision: Option<AutomaticHeapLifetimeDecision>,
    features: Option<&SemanticLifetimeFeatureExport>,
) -> Option<AutomaticRustLifetimePriorDecision> {
    let features = features?;

    // Cleanup reached after the allocation target can release an owner before
    // the normal-path sink. Receiver allocations additionally have an owner
    // live on the allocation call's own cleanup edge. Advisory priors abstain
    // whenever either shape is present.
    let owner_live_cleanup_or_unwind = matches!(
        heap_decision,
        Some(AutomaticHeapLifetimeDecision::CleanupOrUnwindUnknown)
    ) || features.cleanup_drop_path
        || (features.receiver_owned_allocation && features.cleanup_successor_count > 0);
    if owner_live_cleanup_or_unwind {
        return Some(AutomaticRustLifetimePriorDecision::CleanupOrUnwindUnknown);
    }

    // A moved owner that reaches the return place or an opaque consuming call
    // escapes the current function's local release region. Carrier propagation
    // includes aggregate/projected stores, so this is a Rust ownership-flow
    // prior rather than a source-name or allocation-API correlation.
    let stable_escape_region = features.normal_drop_blocks.is_empty()
        && !features.reachable_backedge_after_allocation
        && !features.reachable_yield_or_await;
    if features.return_sink && stable_escape_region {
        return Some(AutomaticRustLifetimePriorDecision::ReturnLong);
    }
    if features.escape_sink && stable_escape_region {
        return Some(AutomaticRustLifetimePriorDecision::EscapeLong);
    }

    if features.owner_live_opaque_call
        || matches!(
            automatic_decision,
            Some(AutomaticLifetimeDecision::InterveningCallMayAdvanceEpochUnknown)
                | Some(AutomaticLifetimeDecision::EffectfulDropGlueUnknown)
        )
    {
        return Some(AutomaticRustLifetimePriorDecision::OwnerLiveCallUnknown);
    }

    // A receiver already owns the backing allocation when reserve-like MIR is
    // entered. One exact all-path local Drop, no nonlocal sink, no cleanup,
    // and no loop/yield ambiguity bound the allocation to the receiver's
    // current local live range.
    if features.receiver_owned_allocation
        && features.normal_successor_count == 1
        && features.cleanup_successor_count == 0
        && features.exact_drop_path
        && !features.conditional_drop_path
        && !features.return_sink
        && !features.escape_sink
        && !features.store_sink
        && !features.owner_live_opaque_call
        && !features.reachable_backedge_after_allocation
        && !features.reachable_yield_or_await
    {
        return Some(AutomaticRustLifetimePriorDecision::ReceiverOwnedShort);
    }

    // Eventual Drop alone is insufficient: the exact 16 MiB-pressure
    // counterexample crosses an opaque call. Require both the ownership trace
    // and the epoch/effect trace to prove one all-path local release region.
    if matches!(
        heap_decision,
        Some(AutomaticHeapLifetimeDecision::ExactLocalDrop)
            | Some(AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop)
    ) && features.exact_drop_path
        && !features.store_sink
        && !features.owner_live_opaque_call
        && !features.reachable_backedge_after_allocation
        && !features.reachable_yield_or_await
    {
        return Some(AutomaticRustLifetimePriorDecision::AllPathLocalReleaseShort);
    }

    None
}

fn automatic_rust_lifetime_prior_selection(
    decision: AutomaticRustLifetimePriorDecision,
) -> LifetimeHintSelection {
    match decision {
        AutomaticRustLifetimePriorDecision::AllPathLocalReleaseShort => LifetimeHintSelection {
            hint: LIFETIME_HINT_EPHEMERAL,
            confidence: RUST_LIFETIME_PRIOR_SHORT_CONFIDENCE,
            basis: "automatic_rust_lifetime_prior_all_path_local_release_short",
        },
        AutomaticRustLifetimePriorDecision::ReceiverOwnedShort => LifetimeHintSelection {
            hint: LIFETIME_HINT_EPHEMERAL,
            confidence: RUST_LIFETIME_PRIOR_SHORT_CONFIDENCE,
            basis: "automatic_rust_lifetime_prior_receiver_owned_short",
        },
        AutomaticRustLifetimePriorDecision::ReturnLong => LifetimeHintSelection {
            hint: LIFETIME_HINT_LONG_LIVED,
            confidence: RUST_LIFETIME_PRIOR_LONG_CONFIDENCE,
            basis: "automatic_rust_lifetime_prior_return_long",
        },
        AutomaticRustLifetimePriorDecision::EscapeLong => LifetimeHintSelection {
            hint: LIFETIME_HINT_LONG_LIVED,
            confidence: RUST_LIFETIME_PRIOR_LONG_CONFIDENCE,
            basis: "automatic_rust_lifetime_prior_escape_long",
        },
        AutomaticRustLifetimePriorDecision::CleanupOrUnwindUnknown => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_rust_lifetime_prior_cleanup_or_unwind_unknown",
        },
        AutomaticRustLifetimePriorDecision::OwnerLiveCallUnknown => LifetimeHintSelection {
            hint: 0,
            confidence: 0,
            basis: "automatic_rust_lifetime_prior_owner_live_call_unknown",
        },
    }
}

fn automatic_rust_lifetime_prior_joinable_layout(features: &SemanticLifetimeFeatureExport) -> bool {
    // Ownership flow can predict a useful class while the scoped call still
    // performs dynamic or nested allocations. Route an advisory prior only
    // when the compiler audit can name the same exact-layout cohort that the
    // runtime observer will validate. The ownership facts remain exported for
    // ground-truth analysis when this deployment gate abstains.
    matches!(features.requested_size_bytes, Some(size) if size > 0)
        && matches!(
            features.requested_align_bytes,
            Some(align) if align.is_power_of_two()
        )
}

fn automatic_rust_lifetime_prior_unjoinable_layout_selection(
    selection: LifetimeHintSelection,
) -> LifetimeHintSelection {
    let basis = match selection.basis {
        "automatic_rust_lifetime_prior_all_path_local_release_short" => {
            "automatic_rust_lifetime_prior_all_path_local_release_short_unjoinable_layout_unknown"
        }
        "automatic_rust_lifetime_prior_receiver_owned_short" => {
            "automatic_rust_lifetime_prior_receiver_owned_short_unjoinable_layout_unknown"
        }
        "automatic_rust_lifetime_prior_return_long" => {
            "automatic_rust_lifetime_prior_return_long_unjoinable_layout_unknown"
        }
        "automatic_rust_lifetime_prior_escape_long" => {
            "automatic_rust_lifetime_prior_escape_long_unjoinable_layout_unknown"
        }
        _ => return selection,
    };
    LifetimeHintSelection {
        hint: 0,
        confidence: 0,
        basis,
    }
}

fn select_lifetime_hint_with_heap_inference(
    profile: Option<&LifetimeProfile>,
    configured_hint: u16,
    confidence_threshold: u8,
    heap_inference_enabled: bool,
    heap_decision: Option<AutomaticHeapLifetimeDecision>,
    rust_lifetime_prior_enabled: bool,
    lifetime_features: Option<&SemanticLifetimeFeatureExport>,
    automatic_classifier_enabled: bool,
    automatic_decision: Option<AutomaticLifetimeDecision>,
    callsite: u64,
    type_id: u64,
    module_id: u64,
) -> LifetimeHintSelection {
    if profile.is_some() || configured_hint != 0 {
        return select_lifetime_hint(
            profile,
            configured_hint,
            confidence_threshold,
            callsite,
            type_id,
            module_id,
        );
    }

    if heap_inference_enabled || rust_lifetime_prior_enabled {
        let heap_selection = automatic_heap_lifetime_selection(heap_decision);
        if automatic_heap_lifetime_bounded_process_long_oracle_basis(heap_selection.basis) {
            return if heap_selection.confidence < confidence_threshold {
                LifetimeHintSelection {
                    hint: 0,
                    confidence: heap_selection.confidence,
                    basis: "automatic_heap_below_confidence_threshold",
                }
            } else {
                heap_selection
            };
        }
    }

    if rust_lifetime_prior_enabled {
        if let Some(decision) = automatic_rust_lifetime_prior_decision(
            automatic_decision,
            heap_decision,
            lifetime_features,
        ) {
            let selection = automatic_rust_lifetime_prior_selection(decision);
            let selection = if selection.hint != 0
                && !automatic_rust_lifetime_prior_joinable_layout(
                    lifetime_features.expect("Rust lifetime prior requires feature export"),
                ) {
                automatic_rust_lifetime_prior_unjoinable_layout_selection(selection)
            } else {
                selection
            };
            return if selection.hint != 0 && selection.confidence < confidence_threshold {
                LifetimeHintSelection {
                    hint: 0,
                    confidence: selection.confidence,
                    basis: "automatic_rust_lifetime_prior_below_confidence_threshold",
                }
            } else {
                selection
            };
        }
    }

    if heap_inference_enabled {
        let selection = automatic_heap_lifetime_selection(heap_decision);
        if heap_decision.is_some() {
            // Heap inference has higher precedence than the epoch classifier.
            // Its Unknown decisions carry ownership, cleanup, and control-flow
            // facts that a narrower epoch-boundary proof cannot discharge.
            return selection;
        }
        if !automatic_classifier_enabled {
            return selection;
        }
    }

    select_lifetime_hint_with_automatic_decision(
        None,
        0,
        confidence_threshold,
        automatic_classifier_enabled,
        automatic_decision,
        callsite,
        type_id,
        module_id,
    )
}

fn automatic_heap_lifetime_bounded_process_long_oracle_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_heap_exact_mem_forget" | "automatic_heap_exact_box_leak"
    )
}

fn automatic_heap_lifetime_unknown_basis(basis: &str) -> bool {
    basis == "automatic_heap_below_confidence_threshold"
        || (basis.starts_with("automatic_heap_") && basis.ends_with("_unknown"))
}

fn automatic_rust_lifetime_prior_short_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_rust_lifetime_prior_all_path_local_release_short"
            | "automatic_rust_lifetime_prior_receiver_owned_short"
    )
}

fn automatic_rust_lifetime_prior_long_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_rust_lifetime_prior_return_long" | "automatic_rust_lifetime_prior_escape_long"
    )
}

fn automatic_rust_lifetime_prior_unknown_basis(basis: &str) -> bool {
    basis == "automatic_rust_lifetime_prior_below_confidence_threshold"
        || (basis.starts_with("automatic_rust_lifetime_prior_") && basis.ends_with("_unknown"))
}

fn automatic_lifetime_ephemeral_basis(basis: &str) -> bool {
    basis == "automatic_exact_local_drop_before_phase_boundary"
}

fn automatic_lifetime_long_lived_basis(basis: &str) -> bool {
    basis == "automatic_exact_local_drop_after_phase_boundary"
}

fn automatic_lifetime_unknown_basis(basis: &str) -> bool {
    basis == "automatic_below_confidence_threshold"
        || (basis.starts_with("automatic_")
            && !basis.starts_with("automatic_heap_")
            && !basis.starts_with("automatic_rust_lifetime_prior_")
            && basis.ends_with("_unknown"))
}

fn automatic_lifetime_unsupported_basis(basis: &str) -> bool {
    basis == "automatic_unsupported_site_unknown"
}

fn automatic_lifetime_eligible_basis(basis: &str) -> bool {
    automatic_lifetime_ephemeral_basis(basis)
        || automatic_lifetime_long_lived_basis(basis)
        || (automatic_lifetime_unknown_basis(basis) && !automatic_lifetime_unsupported_basis(basis))
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
fn lowering_lifetime_confidence_threshold() -> u8 {
    unsafe { LOWERING_LIFETIME_CONFIDENCE_THRESHOLD }
}

#[inline]
fn lowering_lifetime_hint_for_site(
    callsite: u64,
    type_id: u64,
    module_id: u64,
    automatic_decision: Option<AutomaticLifetimeDecision>,
    heap_decision: Option<AutomaticHeapLifetimeDecision>,
    lifetime_features: Option<&SemanticLifetimeFeatureExport>,
) -> LifetimeHintSelection {
    select_lifetime_hint_with_heap_inference(
        LIFETIME_PROFILE.get(),
        lowering_lifetime_hint(),
        lowering_lifetime_confidence_threshold(),
        auto_heap_lifetime_inference_enabled(),
        heap_decision,
        auto_rust_lifetime_prior_enabled(),
        lifetime_features,
        auto_lifetime_classifier_enabled(),
        automatic_decision,
        callsite,
        type_id,
        module_id,
    )
}

#[inline]
fn auto_lifetime_classifier_enabled() -> bool {
    unsafe { AUTO_LIFETIME_CLASSIFIER }
}

#[inline]
fn auto_heap_lifetime_inference_enabled() -> bool {
    unsafe { AUTO_HEAP_LIFETIME_INFERENCE }
}

#[inline]
fn auto_rust_lifetime_prior_enabled() -> bool {
    unsafe { AUTO_RUST_LIFETIME_PRIOR }
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
fn local_no_recovery_lifetime_source_safe() -> bool {
    // A site profile can be intentionally partial. Allocation and Drop rows
    // have distinct callsite keys, so profile presence cannot prove that both
    // sides selected the same lifetime identity. Keep direct allocator calls
    // and semantic scopes on recovery-backed ABIs until profile pairing is
    // validated explicitly.
    LIFETIME_PROFILE.get().is_none()
}

#[inline]
fn lowering_metadata_hints_requested() -> bool {
    LIFETIME_PROFILE.get().is_some()
        || lowering_lifetime_hint() != 0
        || auto_lifetime_classifier_enabled()
        || auto_heap_lifetime_inference_enabled()
        || auto_rust_lifetime_prior_enabled()
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

fn semantic_scope_placement_hint_from_configured(
    placement_hint: u16,
    cross_thread_recovery_hint: bool,
    placement_hint_basis: &'static str,
    local_no_recovery: bool,
) -> (u16, bool, &'static str) {
    if local_no_recovery {
        let basis = if placement_hint == 0
            && !cross_thread_recovery_hint
            && placement_hint_basis == "default"
        {
            "exact_local_no_recovery"
        } else {
            placement_hint_basis
        };
        return (placement_hint, cross_thread_recovery_hint, basis);
    }
    if cross_thread_recovery_hint {
        return (
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
        );
    }

    let basis = match placement_hint_basis {
        "default" => "default_recovery_backed_semantic_scope",
        "manual_placement_hint" => {
            "manual_placement_hint_and_default_recovery_backed_semantic_scope"
        }
        _ => "configured_hint_and_default_recovery_backed_semantic_scope",
    };
    (
        placement_hint | PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
        true,
        basis,
    )
}

fn lowering_semantic_scope_placement_hint_for_body(
    cross_thread_escape: bool,
    local_no_recovery: bool,
) -> (u16, bool, &'static str) {
    let (placement_hint, cross_thread_recovery_hint, placement_hint_basis) =
        lowering_placement_hint_for_body(cross_thread_escape);
    semantic_scope_placement_hint_from_configured(
        placement_hint,
        cross_thread_recovery_hint,
        placement_hint_basis,
        local_no_recovery,
    )
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

fn rustc_crate_metadata(args: &[String]) -> Option<&str> {
    let mut idx = 0;
    while idx < args.len() {
        if args[idx] == "-C" {
            if let Some(value) = args
                .get(idx + 1)
                .and_then(|arg| arg.strip_prefix("metadata="))
            {
                if !value.is_empty() {
                    return Some(value);
                }
            }
        } else if let Some(value) = args[idx].strip_prefix("-Cmetadata=") {
            if !value.is_empty() {
                return Some(value);
            }
        }
        idx += 1;
    }
    None
}

fn rustc_primary_input(args: &[String]) -> Option<&str> {
    args.iter().skip(1).find_map(|arg| {
        if arg == "-" || (!arg.starts_with('-') && arg.ends_with(".rs")) {
            Some(arg.as_str())
        } else {
            None
        }
    })
}

fn crate_module_disambiguator(args: &[String]) -> String {
    if let Some(metadata) = rustc_crate_metadata(args) {
        return format!("metadata\0{}", metadata);
    }
    if let Some(input) = rustc_primary_input(args) {
        if input != "-" {
            let path = PathBuf::from(input);
            let stable_path = path.canonicalize().unwrap_or(path);
            return format!("primary-input\0{}", stable_path.display());
        }
    }
    format!("rustc-argv\0{}", args.join("\0"))
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

#[cfg(unialloc_rustc_current)]
fn runtime_semantic_type_id_from_rustc_hash(type_id_hash: u128, endian: Endian) -> u64 {
    // `core::any::TypeId::Hash` hashes the second native-endian u64 word of
    // its internal Hash128. Select and hash that word in the compilation
    // target's endianness; rustc_driver itself may run on a host with a
    // different byte order.
    let bytes = match endian {
        Endian::Little => type_id_hash.to_le_bytes(),
        Endian::Big => type_id_hash.to_be_bytes(),
    };
    let mut bytes_to_hash =
        Vec::with_capacity(RUNTIME_SEMANTIC_TYPE_ID_DOMAIN.len() + std::mem::size_of::<u64>());
    bytes_to_hash.extend_from_slice(RUNTIME_SEMANTIC_TYPE_ID_DOMAIN);
    bytes_to_hash.extend_from_slice(&bytes[8..16]);
    nonzero_fnv1a64(fnv1a64(&bytes_to_hash))
}

#[cfg(unialloc_rustc_current)]
fn compiler_semantic_type_id<'tcx>(tcx: TyCtxt<'tcx>, owner_ty: Ty<'tcx>) -> Option<u64> {
    if clone_result_has_unresolved_params(owner_ty) || owner_ty.has_aliases() {
        return None;
    }
    Some(runtime_semantic_type_id_from_rustc_hash(
        tcx.type_id_hash(owner_ty).as_u128(),
        tcx.data_layout.endian,
    ))
}

#[cfg(not(unialloc_rustc_current))]
fn compiler_semantic_type_id<'tcx>(_tcx: TyCtxt<'tcx>, _owner_ty: Ty<'tcx>) -> Option<u64> {
    // Compatibility rustc surfaces do not expose the pinned stable TypeId
    // query. They retain audit/recovery behavior and never synthesize an
    // allocator-authorizing identity from type debug text.
    None
}

fn lowering_module_id_from_rustc_args(args: &[String]) -> u64 {
    let crate_name = rustc_crate_name(args)
        .map(normalized_target_crate_name)
        .unwrap_or_else(|| "rustc_input".to_string());
    if crate_name == "unialloc" {
        // Preserve the public in-tree probe ABI while giving every external
        // crate compilation unit its own compiler-derived isolation context.
        return LEGACY_UNIALLOC_LOWERING_MODULE_ID;
    }

    let mut identity = String::from("mir-crate-module-v1\0");
    identity.push_str(&crate_name);
    identity.push('\0');
    identity.push_str(&crate_module_disambiguator(args));
    nonzero_fnv1a64_text(&identity)
}

#[inline]
fn lowering_module_id() -> u64 {
    unsafe { LOWERING_MODULE_ID }
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
    let mut lifetime_confidence_threshold = None;
    let mut lifetime_profile_path = env::var_os("UNIALLOC_LIFETIME_PROFILE").map(PathBuf::from);
    let mut auto_lifetime_classifier = env_truthy("UNIALLOC_AUTO_LIFETIME_CLASSIFIER");
    let mut auto_heap_lifetime_inference = env_truthy("UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE");
    let mut auto_rust_lifetime_prior = env_truthy("UNIALLOC_AUTO_RUST_LIFETIME_PRIOR");
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
            "--unialloc-lifetime-profile" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-lifetime-profile requires a path".to_string());
                }
                lifetime_profile_path = Some(PathBuf::from(&raw[i]));
            }
            value if value.starts_with("--unialloc-lifetime-profile=") => {
                let path = value
                    .strip_prefix("--unialloc-lifetime-profile=")
                    .unwrap_or_default();
                if path.is_empty() {
                    return Err("--unialloc-lifetime-profile requires a path".to_string());
                }
                lifetime_profile_path = Some(PathBuf::from(path));
            }
            "--unialloc-lifetime-confidence-threshold" => {
                i += 1;
                if i >= raw.len() {
                    return Err(
                        "--unialloc-lifetime-confidence-threshold requires a 0..=100 value"
                            .to_string(),
                    );
                }
                lifetime_confidence_threshold = Some(parse_lifetime_confidence_threshold(&raw[i])?);
            }
            value if value.starts_with("--unialloc-lifetime-confidence-threshold=") => {
                let threshold = value
                    .strip_prefix("--unialloc-lifetime-confidence-threshold=")
                    .unwrap_or_default();
                lifetime_confidence_threshold =
                    Some(parse_lifetime_confidence_threshold(threshold)?);
            }
            "--unialloc-auto-lifetime-classifier" => {
                auto_lifetime_classifier = true;
            }
            "--unialloc-auto-heap-lifetime-inference" => {
                auto_heap_lifetime_inference = true;
            }
            "--unialloc-auto-rust-lifetime-prior" => {
                auto_rust_lifetime_prior = true;
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
    let lifetime_confidence_threshold = match lifetime_confidence_threshold {
        Some(value) => value,
        None => match env::var("UNIALLOC_LIFETIME_CONFIDENCE_THRESHOLD") {
            Ok(value) => parse_lifetime_confidence_threshold(&value)?,
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
    let lifetime_profile = lifetime_profile_path
        .map(load_lifetime_profile)
        .transpose()?;
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
        lifetime_confidence_threshold,
        lifetime_profile,
        auto_lifetime_classifier,
        auto_heap_lifetime_inference,
        auto_rust_lifetime_prior,
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

fn push_json_string_vec(json: &mut String, values: &[String]) {
    json.push('[');
    for (index, value) in values.iter().enumerate() {
        if index > 0 {
            json.push_str(", ");
        }
        let _ = write!(json, "\"{}\"", json_escape(value));
    }
    json.push(']');
}

fn push_semantic_lifetime_features_json(json: &mut String, record: &RewriteRecord) {
    let Some(features) = &record.lifetime_analysis_features else {
        json.push_str("      \"lifetime_analysis_features\": null,\n");
        return;
    };
    json.push_str("      \"lifetime_analysis_features\": {\n");
    json.push_str("        \"schema_version\": 1,\n");
    json.push_str("        \"analysis_only\": true,\n");
    let _ = writeln!(
        json,
        "        \"analysis_phase\": \"{}\",",
        features.analysis_phase
    );
    let rust_lifetime_prior_rule_applied = record.lifetime_hint != 0
        && record
            .lifetime_hint_basis
            .starts_with("automatic_rust_lifetime_prior_");
    let _ = writeln!(
        json,
        "        \"classification_rule_applied\": {},",
        rust_lifetime_prior_rule_applied
    );
    json.push_str("        \"runtime_join_key_contract\": \"exact(callsite,type_id,module_id,requested_size_bytes,requested_align_bytes)\",\n");
    let runtime_join_key_complete =
        features.requested_size_bytes.is_some() && features.requested_align_bytes.is_some();
    let _ = writeln!(
        json,
        "        \"runtime_join_key_complete\": {},",
        runtime_join_key_complete
    );
    json.push_str("        \"runtime_join_key\": {\n");
    let _ = writeln!(json, "          \"callsite\": {},", record.callsite);
    let _ = writeln!(json, "          \"type_id\": {},", record.type_id);
    let _ = writeln!(json, "          \"module_id\": {},", record.module_id);
    match features.requested_size_bytes {
        Some(size) => {
            let _ = writeln!(json, "          \"requested_size_bytes\": {},", size);
        }
        None => json.push_str("          \"requested_size_bytes\": null,\n"),
    }
    match features.requested_align_bytes {
        Some(align) => {
            let _ = writeln!(json, "          \"requested_align_bytes\": {}", align);
        }
        None => json.push_str("          \"requested_align_bytes\": null\n"),
    }
    json.push_str("        },\n");
    let _ = writeln!(
        json,
        "        \"requested_layout_basis\": \"{}\",",
        features.requested_layout_basis
    );
    match &features.owner_place {
        Some(owner) => {
            let _ = writeln!(json, "        \"owner_place\": \"{}\",", json_escape(owner));
        }
        None => json.push_str("        \"owner_place\": null,\n"),
    }
    let _ = writeln!(
        json,
        "        \"owner_place_basis\": \"{}\",",
        features.owner_place_basis
    );
    let _ = writeln!(
        json,
        "        \"owner_move_count\": {},",
        features.owner_move_count
    );
    for (name, value) in [
        ("return_sink", features.return_sink),
        ("escape_sink", features.escape_sink),
        ("store_sink", features.store_sink),
        ("owner_live_opaque_call", features.owner_live_opaque_call),
        (
            "allocation_in_natural_loop",
            features.allocation_in_natural_loop,
        ),
        (
            "reachable_backedge_after_allocation",
            features.reachable_backedge_after_allocation,
        ),
        (
            "function_has_yield_or_await",
            features.function_has_yield_or_await,
        ),
        (
            "reachable_yield_or_await",
            features.reachable_yield_or_await,
        ),
        (
            "receiver_owned_allocation",
            features.receiver_owned_allocation,
        ),
        ("exact_drop_path", features.exact_drop_path),
        ("conditional_drop_path", features.conditional_drop_path),
        ("cleanup_drop_path", features.cleanup_drop_path),
    ] {
        let _ = writeln!(json, "        \"{}\": {},", name, value);
    }
    let _ = writeln!(
        json,
        "        \"normal_successor_count\": {},",
        features.normal_successor_count
    );
    let _ = writeln!(
        json,
        "        \"cleanup_successor_count\": {},",
        features.cleanup_successor_count
    );
    let _ = writeln!(
        json,
        "        \"allocation_block\": \"{}\",",
        json_escape(&record.basic_block)
    );
    let _ = writeln!(
        json,
        "        \"allocation_source_span\": \"{}\",",
        json_escape(&record.source_span)
    );
    for (name, values) in [
        ("normal_drop_blocks", &features.normal_drop_blocks),
        ("cleanup_drop_blocks", &features.cleanup_drop_blocks),
        ("return_sink_blocks", &features.return_sink_blocks),
        ("escape_sink_blocks", &features.escape_sink_blocks),
        ("store_sink_blocks", &features.store_sink_blocks),
        (
            "owner_live_opaque_call_blocks",
            &features.owner_live_opaque_call_blocks,
        ),
        ("reachable_normal_blocks", &features.reachable_normal_blocks),
    ] {
        let _ = write!(json, "        \"{}\": ", name);
        push_json_string_vec(json, values);
        json.push_str(",\n");
    }
    json.push_str("        \"reachable_cleanup_blocks\": ");
    push_json_string_vec(json, &features.reachable_cleanup_blocks);
    json.push('\n');
    json.push_str("      },\n");
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

fn authenticated_direct_allocator_call_kind(
    tcx: TyCtxt<'_>,
    def_id: DefId,
) -> DirectAllocatorCallKind {
    let normalized = strip_rustc_crate_disambiguators(&tcx.def_path_str(def_id));
    if exact_alloc_crate_def_id(tcx, def_id) {
        return match normalized.as_str() {
            "alloc::alloc::exchange_malloc" | "std::alloc::exchange_malloc" => {
                DirectAllocatorCallKind::SizeAlignAlloc
            }
            "alloc::alloc::alloc" | "std::alloc::alloc" => DirectAllocatorCallKind::LayoutAlloc,
            "alloc::alloc::alloc_zeroed" | "std::alloc::alloc_zeroed" => {
                DirectAllocatorCallKind::LayoutAllocZeroed
            }
            "alloc::alloc::realloc" | "std::alloc::realloc" => {
                DirectAllocatorCallKind::LayoutRealloc
            }
            "alloc::alloc::dealloc" | "std::alloc::dealloc" => {
                DirectAllocatorCallKind::LayoutDealloc
            }
            _ => DirectAllocatorCallKind::Unsupported,
        };
    }

    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    let Some(trait_def_id) = tcx.trait_of_assoc(trait_item_def_id) else {
        return DirectAllocatorCallKind::Unsupported;
    };
    if !exact_core_crate_def_id(tcx, trait_item_def_id)
        || !exact_core_crate_def_id(tcx, trait_def_id)
        || !matches!(
            strip_rustc_crate_disambiguators(&tcx.def_path_str(trait_def_id)).as_str(),
            "core::alloc::global::GlobalAlloc" | "std::alloc::GlobalAlloc"
        )
    {
        return DirectAllocatorCallKind::Unsupported;
    }
    match strip_rustc_crate_disambiguators(&tcx.def_path_str(trait_item_def_id)).as_str() {
        "core::alloc::global::GlobalAlloc::alloc" | "std::alloc::GlobalAlloc::alloc" => {
            DirectAllocatorCallKind::GlobalAllocLayoutAlloc
        }
        "core::alloc::global::GlobalAlloc::alloc_zeroed"
        | "std::alloc::GlobalAlloc::alloc_zeroed" => {
            DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed
        }
        "core::alloc::global::GlobalAlloc::realloc" | "std::alloc::GlobalAlloc::realloc" => {
            DirectAllocatorCallKind::GlobalAllocLayoutRealloc
        }
        "core::alloc::global::GlobalAlloc::dealloc" | "std::alloc::GlobalAlloc::dealloc" => {
            DirectAllocatorCallKind::GlobalAllocLayoutDealloc
        }
        _ => DirectAllocatorCallKind::Unsupported,
    }
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
    tcx: TyCtxt<'tcx>,
    call_kind: DirectAllocatorCallKind,
    callee_def_id: DefId,
    trusted_unialloc_crate: Option<DefId>,
    argument_tys: &[Ty<'tcx>],
) -> bool {
    if !direct_allocator_is_global_receiver_call(call_kind) {
        return true;
    }

    let trusted_unialloc_crate = match trusted_unialloc_crate {
        Some(crate_root) if crate_root.krate == callee_def_id.krate => crate_root,
        _ => return false,
    };
    let mut receiver_ty = match argument_tys.first() {
        Some(receiver_ty) => *receiver_ty,
        None => return false,
    };
    while let ty::Ref(_, inner, _) = receiver_ty.kind() {
        receiver_ty = *inner;
    }
    match receiver_ty.kind() {
        ty::Adt(def, args) => {
            args.is_empty()
                && def.did().krate == trusted_unialloc_crate.krate
                && strip_rustc_crate_disambiguators(&tcx.def_path_str(def.did()))
                    == "unialloc::cache::RustAllocator"
        }
        _ => false,
    }
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
        (DirectAllocatorCallKind::SizeAlignAlloc, false) => "__unialloc_alloc_with_metadata",
        (DirectAllocatorCallKind::SizeAlignAlloc, true) => "__unialloc_alloc_with_metadata_hints",
        (DirectAllocatorCallKind::LayoutAlloc, false) => "__unialloc_alloc_layout_with_metadata",
        (DirectAllocatorCallKind::LayoutAlloc, true) => {
            "__unialloc_alloc_layout_with_metadata_hints"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, false) => {
            "__unialloc_alloc_zeroed_layout_with_metadata"
        }
        (DirectAllocatorCallKind::LayoutAllocZeroed, true) => {
            "__unialloc_alloc_zeroed_layout_with_metadata_hints"
        }
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
        (DirectAllocatorCallKind::Unsupported, _) => "<unsupported-direct-allocator-call>",
        _ => unreachable!("GlobalAlloc call kinds are mapped to Layout ABI call kinds"),
    }
}

fn direct_allocator_replacement_symbol_for_site(
    kind: DirectAllocatorCallKind,
    allow_size_align_local: bool,
) -> &'static str {
    let kind = direct_allocator_layout_abi_kind(kind).unwrap_or(kind);
    let local = direct_local_metadata_abi_requested()
        && local_no_recovery_lifetime_source_safe()
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
        && local_no_recovery_lifetime_source_safe()
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
        && local_no_recovery_lifetime_source_safe()
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

fn direct_supported_heap_object_destination<'tcx>(tcx: TyCtxt<'tcx>, mut ty: Ty<'tcx>) -> bool {
    while let ty::Ref(_, inner, _) = ty.kind() {
        ty = *inner;
    }
    match ty.kind() {
        // Use the rustc DefId path, not textual type markers: a custom ADT
        // name can contain `Vec`/`Box` without itself being the allocation
        // owner that an exact constructor creates.
        ty::Adt(adt, _) => supported_heap_adt_def_id(tcx, adt.did()),
        _ => false,
    }
}

fn direct_supported_heap_owner_ty<'tcx>(tcx: TyCtxt<'tcx>, mut ty: Ty<'tcx>) -> Option<Ty<'tcx>> {
    while let ty::Ref(_, inner, _) = ty.kind() {
        ty = *inner;
    }
    match ty.kind() {
        ty::Adt(adt, _) if supported_heap_adt_def_id(tcx, adt.did()) => Some(ty),
        _ => None,
    }
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

fn third_party_supported_heap_adt_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "hashbrown::map::HashMap"
            | "hashbrown::set::HashSet"
            | "indexmap::map::IndexMap"
            | "indexmap::set::IndexSet"
    )
}

fn supported_builtin_heap_adt_reexport_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "std::vec::Vec"
            | "std::collections::VecDeque"
            | "std::collections::BinaryHeap"
            | "std::collections::BTreeMap"
            | "std::collections::BTreeSet"
            | "std::collections::LinkedList"
            | "std::collections::HashMap"
            | "std::collections::HashSet"
            | "std::string::String"
            | "std::boxed::Box"
            | "std::rc::Rc"
            | "std::sync::Arc"
            | "std::ffi::CString"
            | "std::ffi::c_str::CString"
    )
}

fn supported_heap_adt_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let def_path = tcx.def_path_str(def_id);
    if !supported_heap_adt_def_path(&def_path)
        && !supported_builtin_heap_adt_reexport_def_path(&def_path)
    {
        return false;
    }
    // rustc can spell an alloc-owned DefId through a stable std re-export.
    // Authenticate the actual CrateNum anchor before interpreting that path.
    if exact_alloc_crate_def_id(tcx, def_id) || exact_std_crate_def_id(tcx, def_id) {
        return true;
    }

    if third_party_supported_heap_adt_def_path(&def_path) {
        // External owner identity requires a pinned DefPathHash/allowlist. A
        // crate name plus textual path is only a dependency-provenance trust
        // boundary, so hashbrown/indexmap remain explicit audit-only inputs.
        return false;
    }
    false
}

fn exact_global_allocator_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    _owner_def_id: DefId,
    allocator_ty: Ty<'tcx>,
) -> bool {
    match allocator_ty.kind() {
        ty::Adt(def, args) => {
            args.is_empty() && exact_alloc_adt_def_id(tcx, def.did(), exact_alloc_global_def_path)
        }
        _ => false,
    }
}

fn supported_owner_uses_canonical_global_allocator<'tcx>(
    tcx: TyCtxt<'tcx>,
    owner_ty: Ty<'tcx>,
) -> bool {
    let (owner_def, owner_args) = match owner_ty.kind() {
        ty::Adt(def, args) if supported_heap_adt_def_id(tcx, def.did()) => (def, args),
        _ => return false,
    };
    let normalized = strip_rustc_crate_disambiguators(&tcx.def_path_str(owner_def.did()));
    let implicit_global_arity = match normalized.as_str() {
        "alloc::vec::Vec"
        | "std::vec::Vec"
        | "alloc::collections::vec_deque::VecDeque"
        | "std::collections::VecDeque"
        | "alloc::collections::binary_heap::BinaryHeap"
        | "std::collections::BinaryHeap"
        | "alloc::collections::btree::set::BTreeSet"
        | "std::collections::BTreeSet"
        | "alloc::collections::linked_list::LinkedList"
        | "std::collections::LinkedList"
        | "alloc::boxed::Box"
        | "std::boxed::Box"
        | "alloc::rc::Rc"
        | "std::rc::Rc"
        | "alloc::sync::Arc"
        | "std::sync::Arc" => 1,
        "alloc::collections::btree::map::BTreeMap" | "std::collections::BTreeMap" => 2,
        "std::collections::hash::map::HashMap" => 3,
        "std::collections::hash::set::HashSet" => 2,
        // These canonical owners expose no allocator type parameter. Their
        // backing storage is fixed to the platform Global allocator.
        "alloc::string::String"
        | "std::string::String"
        | "alloc::ffi::c_str::CString"
        | "std::ffi::CString"
        | "std::ffi::c_str::CString"
        | "std::path::PathBuf"
        | "std::ffi::os_str::OsString" => return owner_args.is_empty(),
        _ => return false,
    };
    if owner_args.len() == implicit_global_arity {
        // Legacy rustc surfaces omit an explicit default Global argument.
        return true;
    }
    if owner_args.len() != implicit_global_arity + 1 {
        return false;
    }
    owner_args
        .get(implicit_global_arity)
        .and_then(generic_arg_type)
        .map_or(false, |allocator_ty| {
            exact_global_allocator_ty(tcx, owner_def.did(), allocator_ty)
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

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct CompilerHeapOwner {
    type_id: u64,
    display: String,
}

fn compiler_heap_owner<'tcx>(tcx: TyCtxt<'tcx>, owner_ty: Ty<'tcx>) -> Option<CompilerHeapOwner> {
    Some(CompilerHeapOwner {
        type_id: compiler_semantic_type_id(tcx, owner_ty)?,
        display: format!("{:?}", owner_ty),
    })
}

fn compiler_heap_owner_displays(owners: &BTreeSet<CompilerHeapOwner>) -> Vec<String> {
    owners.iter().map(|owner| owner.display.clone()).collect()
}

#[derive(Default)]
struct HeapObjectTypeScan {
    owners: BTreeSet<CompilerHeapOwner>,
    unresolved: bool,
    borrowed_slice_iterators_are_nonowners: bool,
    borrowed_str_iterators_are_nonowners: bool,
}

fn heap_object_type_scan_from_ty<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> HeapObjectTypeScan {
    let mut scan = HeapObjectTypeScan::default();
    collect_heap_object_types_from_ty_inner(tcx, ty, 0, &mut scan);
    scan
}

fn heap_object_type_hazard_scan_from_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    ty: Ty<'tcx>,
) -> HeapObjectTypeScan {
    let mut scan = HeapObjectTypeScan {
        borrowed_slice_iterators_are_nonowners: true,
        borrowed_str_iterators_are_nonowners: true,
        ..HeapObjectTypeScan::default()
    };
    collect_heap_object_types_from_ty_inner(tcx, ty, 0, &mut scan);
    scan
}

fn heap_object_types_from_ty<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> BTreeSet<String> {
    heap_object_type_scan_from_ty(tcx, ty)
        .owners
        .into_iter()
        .map(|owner| owner.display)
        .collect()
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
            if supported_heap_adt_def_id(tcx, adt.did()) {
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
                from_args.or(from_fields)
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
    scan: &mut HeapObjectTypeScan,
) {
    const MAX_HEAP_OBJECT_SOLVER_DEPTH: usize = 6;
    if depth > MAX_HEAP_OBJECT_SOLVER_DEPTH {
        scan.unresolved = true;
        return;
    }

    match ty.kind() {
        // A top-level receiver is normally `&mut Vec<T>` in MIR and must be
        // inspected to identify the receiver. A reference stored inside an ADT
        // is only a borrow and must never make that aggregate own the pointee.
        ty::Ref(_, inner, _) => {
            if depth == 0 {
                collect_heap_object_types_from_ty_inner(tcx, *inner, depth + 1, scan);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::RawPtr(inner, _) => {
            if depth == 0 {
                collect_heap_object_types_from_ty_inner(tcx, *inner, depth + 1, scan);
            } else {
                scan.unresolved = true;
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::RawPtr(type_and_mut) => {
            if depth == 0 {
                collect_heap_object_types_from_ty_inner(tcx, type_and_mut.ty, depth + 1, scan);
            } else {
                scan.unresolved = true;
            }
        }
        ty::Adt(adt, substs) => {
            if supported_heap_adt_def_id(tcx, adt.did()) {
                if let Some(owner) = compiler_heap_owner(tcx, ty) {
                    scan.owners.insert(owner);
                } else {
                    scan.unresolved = true;
                }
                // A supported container owns both its backing allocation and
                // any allocator-visible values stored in its generic payload.
                // Keeping both identities makes mutating methods and Drop fail
                // closed for shapes such as `Vec<String>`.
                for arg_ty in substs.types() {
                    collect_heap_object_types_from_ty_inner(tcx, arg_ty, depth + 1, scan);
                }
            } else if scan.borrowed_slice_iterators_are_nonowners
                && exact_core_borrowing_slice_iterator_def_id(tcx, adt.did())
            {
                // Iter/IterMut contain raw pointers plus PhantomData, but they
                // borrow rather than own the slice.  This exception is valid
                // only when scanning by-value hazards around a separately
                // attributed destination/receiver.  General and Drop scans
                // keep inspecting the full field graph and remain fail closed.
            } else if scan.borrowed_str_iterators_are_nonowners
                && exact_core_borrowing_str_iterator_def_id(tcx, adt.did())
            {
                // Split and SplitInclusive contain raw-pointer-backed string
                // search state, but borrow rather than own the source str.
                // As with slice Iter/IterMut, this exception is restricted to
                // the by-value hazard scan around an independently attributed
                // destination/receiver. General and Drop scans remain fail
                // closed.
            } else if clone_known_no_supported_owner_adt_def_id(tcx, adt.did()) {
                // PhantomData<T> does not store a T. In particular,
                // PhantomData<Vec<_>> must not manufacture a Vec owner.
            } else if clone_transparent_wrapper_def_id(tcx, adt.did()) {
                // Option and Result store their generic arguments directly.
                // Arbitrary custom ADTs do not: a generic can be phantom while
                // a non-generic private field owns another allocation.
                for arg_ty in substs.types() {
                    collect_heap_object_types_from_ty_inner(tcx, arg_ty, depth + 1, scan);
                }
            } else {
                // Inspect concrete fields instead of treating generic arguments
                // as stored owners.  In current rustc field types are exposed as
                // `Unnormalized<Ty>`; `skip_norm_wip()` is the same conservative
                // structural view already used by the actual-rustc Clone scan.
                #[cfg(unialloc_rustc_current)]
                for variant in adt.variants().iter() {
                    for field in variant.fields.iter() {
                        collect_heap_object_types_from_ty_inner(
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
                        collect_heap_object_types_from_ty_inner(
                            tcx,
                            field.ty(tcx, substs),
                            depth + 1,
                            scan,
                        );
                    }
                }
            }
        }
        ty::Tuple(fields) => {
            for field_ty in fields.iter() {
                collect_heap_object_types_from_ty_inner(tcx, field_ty, depth + 1, scan);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::Closure(_, substs) => {
            for upvar_ty in ty::UpvarArgs::Closure(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, scan);
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::Closure(_, substs) => {
            for upvar_ty in substs.as_closure().upvar_tys() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, scan);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::Coroutine(_, substs) => {
            for upvar_ty in ty::UpvarArgs::Coroutine(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, scan);
            }
        }
        #[cfg(unialloc_rustc_current)]
        ty::CoroutineClosure(_, substs) => {
            for upvar_ty in ty::UpvarArgs::CoroutineClosure(substs).upvar_tys().iter() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, scan);
            }
        }
        #[cfg(not(unialloc_rustc_current))]
        ty::Generator(_, substs, _) => {
            for upvar_ty in substs.as_generator().upvar_tys() {
                collect_heap_object_types_from_ty_inner(tcx, upvar_ty, depth + 1, scan);
            }
        }
        ty::Array(inner, _) | ty::Slice(inner) => {
            collect_heap_object_types_from_ty_inner(tcx, *inner, depth + 1, scan);
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
        // Aliases, dynamic objects, generic parameters, inference variables,
        // compiler errors, and other unmodeled leaves cannot prove a complete
        // owner graph. Unknown dominates any owner found in a sibling field.
        _ => scan.unresolved = true,
    }
}

#[derive(Default)]
struct PlainCloneHeapOwnerScan {
    owners: BTreeSet<CompilerHeapOwner>,
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

fn exact_core_clone_trait_item_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::clone::Clone::clone" | "std::clone::Clone::clone"
    )
}

fn exact_core_crate_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .sized_trait()
        .map_or(false, |sized_trait| sized_trait.krate == def_id.krate)
}

fn exact_alloc_crate_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .owned_box()
        .map_or(false, |owned_box| owned_box.krate == def_id.krate)
}

fn exact_std_crate_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.get_diagnostic_item(sym::HashMap)
        .map_or(false, |hash_map| hash_map.krate == def_id.krate)
}

#[cfg(unialloc_rustc_current)]
fn plain_clone_trait_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    exact_core_crate_def_id(tcx, trait_item_def_id)
        && exact_core_clone_trait_item_def_path(&tcx.def_path_str(trait_item_def_id))
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

fn exact_alloc_slice_into_vec_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::slice::{impl#0}::into_vec" | "std::slice::<impl [T]>::into_vec"
    )
}

fn exact_alloc_slice_into_vec_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    // Current rustc's diagnostic DefPath uses the `std` re-export spelling
    // even though the defining DefId belongs to `alloc`. This path/name check
    // is only a first filter; the structural proof below also binds the source
    // to the OwnedBox lang item and requires all three definitions to share
    // that exact CrateNum.
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_slice_into_vec_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_slice_to_vec_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::slice::<impl [T]>::to_vec" | "std::slice::<impl [T]>::to_vec"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::slice::{impl#")
        .and_then(|rest| rest.strip_suffix("}::to_vec"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_slice_to_vec_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && tcx.trait_item_of(def_id).is_none()
        && exact_alloc_slice_to_vec_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_vec_into_boxed_slice_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::vec::Vec::<T, A>::into_boxed_slice" | "std::vec::Vec::<T, A>::into_boxed_slice"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::vec::{impl#")
        .and_then(|rest| rest.strip_suffix("}::into_boxed_slice"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_vec_into_boxed_slice_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_vec_into_boxed_slice_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_string_into_bytes_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::string::String::into_bytes" | "std::string::String::into_bytes"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::string::{impl#")
        .and_then(|rest| rest.strip_suffix("}::into_bytes"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_string_into_bytes_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_string_into_bytes_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_cstring_into_bytes_with_nul_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::ffi::c_str::CString::into_bytes_with_nul"
            | "std::ffi::CString::into_bytes_with_nul"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::ffi::c_str::{impl#")
        .and_then(|rest| rest.strip_suffix("}::into_bytes_with_nul"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_cstring_into_bytes_with_nul_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_cstring_into_bytes_with_nul_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_string_into_boxed_str_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::string::String::into_boxed_str" | "std::string::String::into_boxed_str"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::string::{impl#")
        .and_then(|rest| rest.strip_suffix("}::into_boxed_str"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_string_into_boxed_str_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_string_into_boxed_str_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_str_into_string_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::str::str::into_string"
            | "alloc::str::<impl str>::into_string"
            | "std::str::<impl str>::into_string"
            | "std::primitive::str::into_string"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::str::{impl#")
        .and_then(|rest| rest.strip_suffix("}::into_string"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_str_into_string_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_str_into_string_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_into_iterator_into_iter_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::iter::traits::collect::IntoIterator::into_iter"
            | "core::iter::IntoIterator::into_iter"
            | "std::iter::IntoIterator::into_iter"
    )
}

fn exact_core_into_iterator_into_iter_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    tcx.lang_items().into_iter_fn() == Some(trait_item_def_id)
        && exact_core_into_iterator_into_iter_def_path(&tcx.def_path_str(trait_item_def_id))
}

fn exact_core_iterator_collect_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::iter::traits::iterator::Iterator::collect"
            | "core::iter::Iterator::collect"
            | "std::iter::Iterator::collect"
    )
}

fn exact_core_iterator_collect_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    let Some(iterator_trait_def_id) = tcx.lang_items().iterator_trait() else {
        return false;
    };
    tcx.trait_of_assoc(trait_item_def_id) == Some(iterator_trait_def_id)
        && exact_core_iterator_collect_def_path(&tcx.def_path_str(trait_item_def_id))
}

fn exact_core_iterator_zip_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::iter::traits::iterator::Iterator::zip"
            | "core::iter::Iterator::zip"
            | "std::iter::Iterator::zip"
    )
}

fn exact_core_iterator_zip_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    let Some(iterator_trait_def_id) = tcx.lang_items().iterator_trait() else {
        return false;
    };
    tcx.trait_of_assoc(trait_item_def_id) == Some(iterator_trait_def_id)
        && exact_core_iterator_zip_def_path(&tcx.def_path_str(trait_item_def_id))
}

#[cfg(unialloc_rustc_current)]
fn generic_arg_type<'tcx>(arg: &ty::GenericArg<'tcx>) -> Option<Ty<'tcx>> {
    arg.as_type()
}

#[cfg(not(unialloc_rustc_current))]
fn generic_arg_type<'tcx>(arg: &ty::GenericArg<'tcx>) -> Option<Ty<'tcx>> {
    match arg.unpack() {
        ty::GenericArgKind::Type(ty) => Some(ty),
        _ => None,
    }
}

fn exact_core_result_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::result::Result" | "std::result::Result"
    )
}

fn exact_core_borrowing_slice_iterator_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::slice::iter::Iter"
            | "core::slice::iter::IterMut"
            | "std::slice::Iter"
            | "std::slice::IterMut"
    )
}

fn exact_core_borrowing_slice_iterator_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .iterator_trait()
        .map_or(false, |iterator_trait| iterator_trait.krate == def_id.krate)
        && exact_core_borrowing_slice_iterator_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_slice_iter_mut_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::slice::iter::IterMut" | "std::slice::IterMut"
    )
}

fn exact_core_slice_iter_mut_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .iterator_trait()
        .map_or(false, |iterator_trait| iterator_trait.krate == def_id.krate)
        && exact_core_slice_iter_mut_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_zip_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::iter::adapters::zip::Zip" | "std::iter::Zip"
    )
}

fn exact_core_zip_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .iterator_trait()
        .map_or(false, |iterator_trait| iterator_trait.krate == def_id.krate)
        && exact_core_zip_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_copied_iterator_adapter_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::iter::adapters::copied::Copied" | "std::iter::Copied"
    )
}

fn exact_core_copied_iterator_adapter_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .iterator_trait()
        .map_or(false, |iterator_trait| iterator_trait.krate == def_id.krate)
        && exact_core_copied_iterator_adapter_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_borrowing_str_iterator_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::str::iter::Split"
            | "core::str::iter::SplitInclusive"
            | "std::str::Split"
            | "std::str::SplitInclusive"
    )
}

fn exact_core_borrowing_str_iterator_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.lang_items()
        .iterator_trait()
        .map_or(false, |iterator_trait| iterator_trait.krate == def_id.krate)
        && exact_core_borrowing_str_iterator_def_path(&tcx.def_path_str(def_id))
}

fn exact_result_destination_types<'tcx>(
    tcx: TyCtxt<'tcx>,
    destination_ty: Ty<'tcx>,
) -> Option<(Ty<'tcx>, Ty<'tcx>)> {
    let (adt, args) = match destination_ty.kind() {
        ty::Adt(adt, args) => (adt, args),
        _ => return None,
    };
    if !tcx.is_diagnostic_item(sym::Result, adt.did())
        || !exact_core_result_def_path(&tcx.def_path_str(adt.did()))
        || args.len() != 2
    {
        return None;
    }
    Some((
        generic_arg_type(args.get(0)?)?,
        generic_arg_type(args.get(1)?)?,
    ))
}

fn exact_alloc_box_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::boxed::Box" | "std::boxed::Box"
    )
}

fn exact_alloc_box_new_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::boxed::Box::<T>::new"
            | "std::boxed::Box::<T>::new"
            | "alloc::boxed::Box::<T, A>::new"
            | "std::boxed::Box::<T, A>::new"
            | "alloc::boxed::Box::new"
            | "std::boxed::Box::new"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::boxed::{impl#")
        .and_then(|rest| rest.strip_suffix("}::new"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_box_new_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id) && exact_alloc_box_new_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_drop_inert_wrapper_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::mem::maybe_uninit::MaybeUninit"
            | "std::mem::MaybeUninit"
            | "core::mem::manually_drop::ManuallyDrop"
            | "std::mem::ManuallyDrop"
            | "core::marker::PhantomData"
            | "std::marker::PhantomData"
    )
}

fn exact_core_drop_inert_wrapper_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_core_crate_def_id(tcx, def_id)
        && exact_core_drop_inert_wrapper_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_vec_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::vec::Vec" | "std::vec::Vec"
    )
}

fn exact_alloc_vec_from_elem_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::vec::from_elem" | "std::vec::from_elem"
    )
}

fn exact_alloc_vec_from_elem_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_vec_from_elem_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_vecdeque_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::collections::vec_deque::VecDeque" | "std::collections::VecDeque"
    )
}

fn exact_alloc_vecdeque_method_def_path(path: &str, method: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if [
        "alloc::collections::vec_deque::VecDeque::<T, A>::",
        "std::collections::VecDeque::<T, A>::",
        "alloc::collections::vec_deque::VecDeque::<T>::",
        "std::collections::VecDeque::<T>::",
    ]
    .iter()
    .any(|prefix| normalized.strip_prefix(prefix) == Some(method))
    {
        return true;
    }

    let impl_index = match normalized
        .strip_prefix("alloc::collections::vec_deque::{impl#")
        .and_then(|rest| rest.strip_suffix(&format!("}}::{method}")))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_vecdeque_method_def_id(tcx: TyCtxt<'_>, def_id: DefId, method: &str) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_vecdeque_method_def_path(&tcx.def_path_str(def_id), method)
}

fn exact_alloc_vecdeque_with_capacity_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_vecdeque_method_def_id(tcx, def_id, "with_capacity")
}

fn exact_alloc_vecdeque_capacity_only_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    [
        "reserve",
        "reserve_exact",
        "try_reserve",
        "try_reserve_exact",
        "shrink_to",
        "shrink_to_fit",
    ]
    .iter()
    .any(|method| exact_alloc_vecdeque_method_def_id(tcx, def_id, method))
}

fn exact_alloc_vec_with_capacity_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::vec::Vec::<T, A>::with_capacity"
            | "std::vec::Vec::<T, A>::with_capacity"
            | "alloc::vec::Vec::<T>::with_capacity"
            | "std::vec::Vec::<T>::with_capacity"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::vec::{impl#")
        .and_then(|rest| rest.strip_suffix("}::with_capacity"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_vec_with_capacity_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_vec_with_capacity_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_vec_capacity_only_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    if !exact_alloc_crate_def_id(tcx, def_id) || tcx.trait_item_of(def_id).is_some() {
        return false;
    }
    let def_path = tcx.def_path_str(def_id);
    VEC_CAPACITY_ONLY_METHODS.iter().any(|method| {
        callee_contains_current_impl_method(&def_path, "alloc::vec", method)
            || ["alloc::vec::Vec", "std::vec::Vec"]
                .iter()
                .any(|receiver| callee_contains_named_receiver_method(&def_path, receiver, method))
    })
}

fn exact_alloc_arc_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::sync::Arc" | "std::sync::Arc"
    )
}

fn exact_alloc_arc_new_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::sync::Arc::<T>::new"
            | "std::sync::Arc::<T>::new"
            | "alloc::sync::Arc::<T, A>::new"
            | "std::sync::Arc::<T, A>::new"
            | "alloc::sync::Arc::new"
            | "std::sync::Arc::new"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::sync::{impl#")
        .and_then(|rest| rest.strip_suffix("}::new"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_arc_new_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id) && exact_alloc_arc_new_def_path(&tcx.def_path_str(def_id))
}

fn exact_alloc_rc_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::rc::Rc" | "std::rc::Rc"
    )
}

fn exact_alloc_rc_new_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::rc::Rc::<T>::new"
            | "std::rc::Rc::<T>::new"
            | "alloc::rc::Rc::<T, A>::new"
            | "std::rc::Rc::<T, A>::new"
            | "alloc::rc::Rc::new"
            | "std::rc::Rc::new"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::rc::{impl#")
        .and_then(|rest| rest.strip_suffix("}::new"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_rc_new_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id) && exact_alloc_rc_new_def_path(&tcx.def_path_str(def_id))
}

fn exact_std_path_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::path::Path"
}

fn exact_std_pathbuf_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::path::PathBuf"
}

fn exact_std_dir_entry_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::fs::DirEntry"
}

fn exact_std_dir_entry_method_def_path(path: &str, method: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if normalized.strip_prefix("std::fs::DirEntry::") == Some(method) {
        return true;
    }

    let impl_index = match normalized
        .strip_prefix("std::fs::{impl#")
        .and_then(|rest| rest.strip_suffix(&format!("}}::{method}")))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_std_dir_entry_method_def_id(tcx: TyCtxt<'_>, def_id: DefId, method: &str) -> bool {
    exact_std_crate_def_id(tcx, def_id)
        && exact_std_dir_entry_method_def_path(&tcx.def_path_str(def_id), method)
}

fn exact_std_os_str_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "std::ffi::os_str::OsStr" | "std::ffi::OsStr"
    )
}

fn exact_std_path_method_def_path(path: &str, method: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if normalized.strip_prefix("std::path::Path::") == Some(method) {
        return true;
    }

    let impl_index = match normalized
        .strip_prefix("std::path::{impl#")
        .and_then(|rest| rest.strip_suffix(&format!("}}::{method}")))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_std_path_method_def_id(tcx: TyCtxt<'_>, def_id: DefId, method: &str) -> bool {
    exact_std_crate_def_id(tcx, def_id)
        && exact_std_path_method_def_path(&tcx.def_path_str(def_id), method)
}

fn exact_std_hash_map_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::collections::HashMap"
}

fn exact_std_hash_map_with_capacity_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::collections::HashMap::<K, V>::with_capacity"
}

fn exact_std_hash_map_with_capacity_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_std_crate_def_id(tcx, def_id)
        && exact_std_hash_map_with_capacity_def_path(&tcx.def_path_str(def_id))
}

fn exact_std_hash_map_method_def_path(path: &str, method: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if [
        "std::collections::HashMap::<K, V>::",
        "std::collections::HashMap::<K, V, S>::",
        "std::collections::HashMap::<K, V, S, A>::",
    ]
    .iter()
    .any(|prefix| normalized.strip_prefix(prefix) == Some(method))
    {
        return true;
    }

    let impl_index = match normalized
        .strip_prefix("std::collections::hash::map::{impl#")
        .and_then(|rest| rest.strip_suffix(&format!("}}::{method}")))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_std_hash_map_capacity_only_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_std_crate_def_id(tcx, def_id)
        && ["reserve", "try_reserve", "shrink_to", "shrink_to_fit"]
            .iter()
            .any(|method| exact_std_hash_map_method_def_path(&tcx.def_path_str(def_id), method))
}

fn exact_std_hash_set_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::collections::HashSet"
}

fn exact_std_hash_set_with_capacity_def_path(path: &str) -> bool {
    strip_rustc_crate_disambiguators(path) == "std::collections::HashSet::<T>::with_capacity"
}

fn exact_std_hash_set_with_capacity_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_std_crate_def_id(tcx, def_id)
        && exact_std_hash_set_with_capacity_def_path(&tcx.def_path_str(def_id))
}

fn exact_std_hash_map_random_state_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        // Current std exposes RandomState through std::hash; the pinned
        // nightly exposes the same std-owned type through hash_map.
        "std::hash::RandomState" | "std::collections::hash_map::RandomState"
    )
}

fn exact_alloc_vec_into_iter_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::vec::into_iter::IntoIter" | "alloc::vec::IntoIter" | "std::vec::IntoIter"
    )
}

fn exact_alloc_string_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::string::String" | "std::string::String"
    )
}

fn exact_alloc_string_with_capacity_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::string::String::with_capacity" | "std::string::String::with_capacity"
    ) {
        return true;
    }
    let impl_index = match normalized
        .strip_prefix("alloc::string::{impl#")
        .and_then(|rest| rest.strip_suffix("}::with_capacity"))
    {
        Some(index) => index,
        None => return false,
    };
    !impl_index.is_empty() && impl_index.bytes().all(|byte| byte.is_ascii_digit())
}

fn exact_alloc_string_with_capacity_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && exact_alloc_string_with_capacity_def_path(&tcx.def_path_str(def_id))
}

fn exact_core_from_from_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::convert::From::from" | "std::convert::From::from"
    )
}

fn exact_core_from_from_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    let Some(from_trait_def_id) = tcx.lang_items().from_trait() else {
        return false;
    };
    tcx.trait_of_assoc(trait_item_def_id) == Some(from_trait_def_id)
        && exact_core_from_from_def_path(&tcx.def_path_str(trait_item_def_id))
}

fn exact_alloc_to_owned_to_owned_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::borrow::ToOwned::to_owned" | "std::borrow::ToOwned::to_owned"
    )
}

fn exact_alloc_to_owned_to_owned_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    let trait_item_def_id = tcx.trait_item_of(def_id).unwrap_or(def_id);
    tcx.get_diagnostic_item(sym::to_owned_method) == Some(trait_item_def_id)
        && exact_alloc_to_owned_to_owned_def_path(&tcx.def_path_str(trait_item_def_id))
}

fn exact_alloc_cstring_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::ffi::c_str::CString" | "std::ffi::CString"
    )
}

fn exact_alloc_global_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "alloc::alloc::Global" | "std::alloc::Global"
    )
}

fn exact_alloc_adt_def_id(tcx: TyCtxt<'_>, def_id: DefId, exact_path: fn(&str) -> bool) -> bool {
    exact_alloc_crate_def_id(tcx, def_id) && exact_path(&tcx.def_path_str(def_id))
}

fn exact_box_slice_into_vec_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_alloc_slice_into_vec_def_id(tcx, callee_def_id)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(source_def.did())
        || source_args.len() != 2
    {
        return None;
    }
    let source_payload_ty = generic_arg_type(source_args.get(0)?)?;
    let source_element_ty = match source_payload_ty.kind() {
        ty::Slice(element_ty) => *element_ty,
        _ => return None,
    };
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_vec_def_path)
        || callee_def_id.krate != source_def.did().krate
        || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_element_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;

    if source_element_ty != destination_element_ty
        || source_allocator_ty != destination_allocator_ty
        || callee_generic_types != [source_element_ty, source_allocator_ty]
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: source_element_ty,
        allocator_ty: source_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_vec_into_boxed_slice_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_alloc_vec_into_boxed_slice_def_id(tcx, callee_def_id)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_vec_def_path)
        || source_args.len() != 2
    {
        return None;
    }
    let source_element_ty = generic_arg_type(source_args.get(0)?)?;
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(destination_def.did())
        || callee_def_id.krate != source_def.did().krate
        || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_payload_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_element_ty = match destination_payload_ty.kind() {
        ty::Slice(element_ty) => *element_ty,
        _ => return None,
    };
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;

    if source_element_ty != destination_element_ty
        || source_allocator_ty != destination_allocator_ty
        || callee_generic_types != [source_element_ty, source_allocator_ty]
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: source_element_ty,
        allocator_ty: source_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_string_into_bytes_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    let exact_inherent_into_bytes = exact_alloc_string_into_bytes_def_id(tcx, callee_def_id);
    let exact_from_string = exact_core_from_from_def_id(tcx, callee_def_id);
    if (!exact_inherent_into_bytes && !exact_from_string)
        || (exact_inherent_into_bytes && !callee_generic_types.is_empty())
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_string_def_path)
        || !source_args.is_empty()
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_vec_def_path)
        || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_element_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;
    let (allocator_def, allocator_args) = match destination_allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if destination_element_ty != tcx.types.u8
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || allocator_def.did().krate != source_def.did().krate
        || !allocator_args.is_empty()
    {
        return None;
    }

    // The inherent method has no type arguments and is defined in `alloc`.
    // `Vec::<u8>::from(String)` instead lowers to the exact `core::convert::From::from`
    // trait item with concrete `[Self = Vec<u8, Global>, T = String]` type
    // arguments. Match that monomorphized pair structurally rather than by
    // debug text so the current and pinned legacy printers may respectively
    // show or omit the default allocator without widening to generic/custom
    // conversions.
    if exact_from_string {
        if callee_generic_types != [destination_ty, source_ty] {
            return None;
        }
    } else if callee_def_id.krate != source_def.did().krate {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: destination_element_ty,
        allocator_ty: destination_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_cstring_into_bytes_with_nul_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_alloc_cstring_into_bytes_with_nul_def_id(tcx, callee_def_id)
        || !callee_generic_types.is_empty()
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_cstring_def_path)
        || !source_args.is_empty()
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_vec_def_path)
        || callee_def_id.krate != source_def.did().krate
        || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_element_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;
    let (allocator_def, allocator_args) = match destination_allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if destination_element_ty != tcx.types.u8
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || allocator_def.did().krate != source_def.did().krate
        || !allocator_args.is_empty()
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: destination_element_ty,
        allocator_ty: destination_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_string_into_boxed_str_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_alloc_string_into_boxed_str_def_id(tcx, callee_def_id)
        || !callee_generic_types.is_empty()
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_string_def_path)
        || !source_args.is_empty()
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(destination_def.did())
        || callee_def_id.krate != source_def.did().krate
        || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_payload_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;
    let (allocator_def, allocator_args) = match destination_allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if destination_payload_ty != tcx.types.str_
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || allocator_def.did().krate != source_def.did().krate
        || !allocator_args.is_empty()
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: destination_payload_ty,
        allocator_ty: destination_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_boxed_str_into_string_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_alloc_str_into_string_def_id(tcx, callee_def_id)
        || !callee_generic_types.is_empty()
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(source_def.did())
        || source_args.len() != 2
    {
        return None;
    }
    let source_payload_ty = generic_arg_type(source_args.get(0)?)?;
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;
    let (allocator_def, allocator_args) = match source_allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if source_payload_ty != tcx.types.str_
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || !allocator_args.is_empty()
        || !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_string_def_path)
        || !destination_args.is_empty()
        || callee_def_id.krate != source_def.did().krate
        || destination_def.did().krate != source_def.did().krate
        || allocator_def.did().krate != source_def.did().krate
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: source_payload_ty,
        allocator_ty: source_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

fn exact_vec_into_iter_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<SemanticOwnershipTransferProof<'tcx>> {
    if !exact_core_into_iterator_into_iter_def_id(tcx, callee_def_id)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(destination_ty)
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let source_ty = argument_tys[0];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_vec_def_path)
        || source_args.len() != 2
    {
        return None;
    }
    let source_element_ty = generic_arg_type(source_args.get(0)?)?;
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(
        tcx,
        destination_def.did(),
        exact_alloc_vec_into_iter_def_path,
    ) || destination_def.did().krate != source_def.did().krate
        || destination_args.len() != 2
    {
        return None;
    }
    let destination_element_ty = generic_arg_type(destination_args.get(0)?)?;
    let destination_allocator_ty = generic_arg_type(destination_args.get(1)?)?;

    if source_element_ty != destination_element_ty
        || source_allocator_ty != destination_allocator_ty
        || callee_generic_types != [source_ty]
    {
        return None;
    }

    Some(SemanticOwnershipTransferProof {
        element_ty: source_element_ty,
        allocator_ty: source_allocator_ty,
        old_owner_ty: source_ty,
        new_owner_ty: destination_ty,
        old_owner_type: format!("{:?}", source_ty),
        new_owner_type: format!("{:?}", destination_ty),
    })
}

/// Prove only the real-application `slice::IterMut<u8>.zip(Vec<u8, Global>)`
/// shape. `Iterator::zip` performs `Vec::into_iter` inside core, so the local
/// MIR has no direct ownership-transfer call to retarget. The proof binds the
/// core method/Zip/IterMut DefIds, the alloc Vec/IntoIter/Global DefIds, both
/// generic substitutions, and the exact destination before the rewrite splits
/// the call into the existing Vec -> IntoIter helper followed by zip(IntoIter).
fn exact_vec_into_iter_via_zip_transfer_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<(
    SemanticOwnershipTransferProof<'tcx>,
    SemanticOwnershipTransferCallShape<'tcx>,
)> {
    if !exact_core_iterator_zip_def_id(tcx, callee_def_id)
        || argument_tys.len() != 2
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys
            .iter()
            .any(|ty| clone_result_has_unresolved_params(*ty))
    {
        return None;
    }

    let iterator_ty = argument_tys[0];
    let (iterator_def, iterator_args) = match iterator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_core_slice_iter_mut_def_id(tcx, iterator_def.did())
        || iterator_args.types().collect::<Vec<_>>() != [tcx.types.u8]
    {
        return None;
    }

    let source_ty = argument_tys[1];
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_vec_def_path)
        || source_args.len() != 2
    {
        return None;
    }
    let source_element_ty = generic_arg_type(source_args.get(0)?)?;
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;
    let (allocator_def, allocator_args) = match source_allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if source_element_ty != tcx.types.u8
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || !allocator_args.is_empty()
        || allocator_def.did().krate != source_def.did().krate
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_core_zip_def_id(tcx, destination_def.did()) || destination_args.len() != 2 {
        return None;
    }
    let destination_iterator_ty = generic_arg_type(destination_args.get(0)?)?;
    let into_iter_ty = generic_arg_type(destination_args.get(1)?)?;
    let (into_iter_def, into_iter_args) = match into_iter_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if destination_iterator_ty != iterator_ty
        || !exact_alloc_adt_def_id(tcx, into_iter_def.did(), exact_alloc_vec_into_iter_def_path)
        || into_iter_def.did().krate != source_def.did().krate
        || into_iter_args.len() != 2
        || generic_arg_type(into_iter_args.get(0)?)? != source_element_ty
        || generic_arg_type(into_iter_args.get(1)?)? != source_allocator_ty
        || callee_generic_types != [iterator_ty, source_ty]
    {
        return None;
    }

    Some((
        SemanticOwnershipTransferProof {
            element_ty: source_element_ty,
            allocator_ty: source_allocator_ty,
            old_owner_ty: source_ty,
            new_owner_ty: into_iter_ty,
            old_owner_type: format!("{:?}", source_ty),
            new_owner_type: format!("{:?}", into_iter_ty),
        },
        SemanticOwnershipTransferCallShape::IteratorZip {
            zip_def_id: callee_def_id,
            iterator_ty,
            into_iter_ty,
        },
    ))
}

/// Return the sole allocator-visible owner only for the exact canonical Zip
/// shape paired with the hidden-transfer rewrite above. Arbitrary Zip-like
/// ADTs, `Iter`, non-u8 elements, custom allocators, and unresolved types keep
/// the general fail-closed Drop scan.
fn exact_zip_iter_mut_u8_into_iter_drop_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    drop_ty: Ty<'tcx>,
) -> Option<Ty<'tcx>> {
    let (zip_def, zip_args) = match drop_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_core_zip_def_id(tcx, zip_def.did()) || zip_args.len() != 2 {
        return None;
    }
    let iterator_ty = generic_arg_type(zip_args.get(0)?)?;
    let into_iter_ty = generic_arg_type(zip_args.get(1)?)?;
    let (iterator_def, iterator_args) = match iterator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_core_slice_iter_mut_def_id(tcx, iterator_def.did())
        || iterator_args.types().collect::<Vec<_>>() != [tcx.types.u8]
    {
        return None;
    }
    let (into_iter_def, into_iter_args) = match into_iter_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, into_iter_def.did(), exact_alloc_vec_into_iter_def_path)
        || into_iter_args.len() != 2
        || generic_arg_type(into_iter_args.get(0)?)? != tcx.types.u8
    {
        return None;
    }
    let allocator_ty = generic_arg_type(into_iter_args.get(1)?)?;
    let (allocator_def, allocator_args) = match allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || !allocator_args.is_empty()
        || allocator_def.did().krate != into_iter_def.did().krate
    {
        return None;
    }
    Some(into_iter_ty)
}

#[cfg(unialloc_rustc_current)]
fn exact_pointer_unsize_cast_kind(kind: &CastKind) -> bool {
    matches!(kind, CastKind::PointerCoercion(PointerCoercion::Unsize, _))
}

#[cfg(not(unialloc_rustc_current))]
fn exact_pointer_unsize_cast_kind(kind: &CastKind) -> bool {
    matches!(kind, CastKind::Pointer(PointerCast::Unsize))
}

/// Recover the allocator-record owner for the exact old-rustc list-form
/// `vec![...]` lowering:
///
/// `Box<[T; N]> --Pointer(Unsize)--> Box<[T]> --into_vec--> Vec<T>`.
///
/// The direct allocation is recorded under `Box<[T; N]>`.  Passing the
/// nominal `Box<[T]>` argument identity to the runtime helper therefore makes
/// the authenticated record reject the transfer.  Accept only an immediate,
/// move-only cast in the final semantic statement before the `into_vec`
/// terminator and prove the Box DefId, element, and allocator structurally.
/// Optimized MIR may append only lifetime bookkeeping (`StorageDead`) or
/// no-ops after that cast; do not skip any other statement kind.  Any less
/// exact provenance keeps the ordinary Box-slice expected identity and fails
/// closed at runtime if its record does not agree.
fn exact_immediate_box_array_unsize_owner_type<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    bb: BasicBlock,
    argument: &Operand<'tcx>,
    nominal_box_slice_ty: Ty<'tcx>,
    proof: &SemanticOwnershipTransferProof<'tcx>,
) -> Option<Ty<'tcx>> {
    let argument_place = match argument {
        Operand::Move(place) if place.projection.is_empty() => *place,
        _ => return None,
    };
    let statement = body[bb].statements.iter().rev().find(|statement| {
        !matches!(
            &statement.kind,
            StatementKind::StorageDead(_) | StatementKind::Nop
        )
    })?;
    let assigned = match &statement.kind {
        StatementKind::Assign(assigned) => assigned,
        _ => return None,
    };
    let (destination, rvalue) = &**assigned;
    if *destination != argument_place {
        return None;
    }
    let (cast_kind, source_operand, cast_ty) = match rvalue {
        Rvalue::Cast(cast_kind, source_operand, cast_ty) => (cast_kind, source_operand, *cast_ty),
        _ => return None,
    };
    if !exact_pointer_unsize_cast_kind(cast_kind) || cast_ty != nominal_box_slice_ty {
        return None;
    }
    let source_place = match source_operand {
        Operand::Move(place) if place.projection.is_empty() => place,
        _ => return None,
    };
    let source_ty = source_place.ty(&body.local_decls, tcx).ty;
    let (source_def, source_args) = match source_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, source_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(source_def.did())
        || source_args.len() != 2
    {
        return None;
    }
    let source_payload_ty = generic_arg_type(source_args.get(0)?)?;
    let source_element_ty = match source_payload_ty.kind() {
        ty::Array(element_ty, _) => *element_ty,
        _ => return None,
    };
    let source_allocator_ty = generic_arg_type(source_args.get(1)?)?;
    if source_element_ty != proof.element_ty || source_allocator_ty != proof.allocator_ty {
        return None;
    }
    Some(source_ty)
}

fn clone_transparent_wrapper_def_path(path: &str) -> bool {
    path == "core::option::Option"
        || path.ends_with("::core::option::Option")
        || path == "core::result::Result"
        || path.ends_with("::core::result::Result")
}

fn clone_transparent_wrapper_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_core_crate_def_id(tcx, def_id)
        && clone_transparent_wrapper_def_path(&tcx.def_path_str(def_id))
}

fn clone_known_no_supported_owner_adt(path: &str) -> bool {
    path == "core::marker::PhantomData"
        || path.ends_with("::core::marker::PhantomData")
        || path == "core::time::Duration"
        || path.ends_with("::core::time::Duration")
}

fn clone_known_no_supported_owner_adt_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_core_crate_def_id(tcx, def_id)
        && clone_known_no_supported_owner_adt(&tcx.def_path_str(def_id))
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

fn clone_reference_counted_field_is_nonallocating_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    exact_alloc_crate_def_id(tcx, def_id)
        && clone_reference_counted_field_is_nonallocating(&tcx.def_path_str(def_id))
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
            if clone_reference_counted_field_is_nonallocating_def_id(tcx, adt.did()) {
                // Do not inspect Arc<T>/Rc<T>'s generic argument: Clone copies
                // the reference-counted handle and does not invoke T::clone.
                return;
            }
            if supported_heap_adt_def_id(tcx, adt.did()) {
                if let Some(owner) = compiler_heap_owner(tcx, ty) {
                    scan.owners.insert(owner);
                } else {
                    scan.unresolved = true;
                }
                return;
            }
            if clone_known_no_supported_owner_adt_def_id(tcx, adt.did()) {
                // Do not inspect PhantomData<T>'s generic argument: it is not a
                // stored T and scanning it would make PhantomData<Vec<_>> look
                // like an owned Vec.  Duration is likewise a trusted scalar.
                return;
            }
            if clone_transparent_wrapper_def_id(tcx, adt.did()) {
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

#[cfg(unialloc_rustc_current)]
fn exact_core_clone_direct_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    let callee_def_id = callee_def_id?;
    let trait_item_def_id = tcx.trait_item_of(callee_def_id).unwrap_or(callee_def_id);
    if tcx.lang_items().clone_fn() != Some(trait_item_def_id)
        || !exact_core_crate_def_id(tcx, trait_item_def_id)
        || !exact_core_clone_trait_item_def_path(&tcx.def_path_str(trait_item_def_id))
        || callee_generic_types != [destination_ty]
        || argument_tys.len() != 1
    {
        return None;
    }
    match argument_tys[0].kind() {
        ty::Ref(_, self_ty, rustc_ast::Mutability::Not) if *self_ty == destination_ty => {}
        _ => return None,
    }

    let (owner_def, owner_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_string_def_path)
        && tcx.lang_items().string() == Some(owner_def.did())
        && owner_args.is_empty()
    {
        return Some(format!("{:?}", destination_ty));
    }
    if !exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_vec_def_path)
        || !tcx.is_diagnostic_item(sym::Vec, owner_def.did())
        || owner_args.len() != 2
        || generic_arg_type(owner_args.get(0)?)? != tcx.types.u8
    {
        return None;
    }
    let allocator_ty = generic_arg_type(owner_args.get(1)?)?;
    let (allocator_def, allocator_args) = match allocator_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if allocator_def.did().krate != owner_def.did().krate
        || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        || !allocator_args.is_empty()
    {
        return None;
    }
    Some(format!("{:?}", destination_ty))
}

#[cfg(not(unialloc_rustc_current))]
fn exact_core_clone_direct_owner<'tcx>(
    _tcx: TyCtxt<'tcx>,
    _callee_def_id: Option<DefId>,
    _callee_generic_types: &[Ty<'tcx>],
    _destination_ty: Ty<'tcx>,
    _argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    // The compatibility surface lacks the pinned rustc structural query
    // contract used above, so every plain Clone call remains audit-only.
    None
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
        1 => PlainCloneHeapClass::Single(owners.into_iter().next().unwrap().display),
        _ => {
            PlainCloneHeapClass::Ambiguous(owners.into_iter().map(|owner| owner.display).collect())
        }
    }
}

fn semantic_scope_argument_is_borrowed_or_raw(ty: Ty<'_>) -> bool {
    match ty.kind() {
        ty::Ref(_, _, _) => true,
        #[cfg(unialloc_rustc_current)]
        ty::RawPtr(_, _) => true,
        #[cfg(not(unialloc_rustc_current))]
        ty::RawPtr(_) => true,
        _ => false,
    }
}

fn semantic_scope_heap_class_with_known_attribution_and_by_value_hazards<'tcx>(
    tcx: TyCtxt<'tcx>,
    attributed_owner: CompilerHeapOwner,
    hazard_tys: &[Ty<'tcx>],
) -> SemanticScopeHeapClass {
    let mut owners = BTreeSet::from([attributed_owner.clone()]);
    for hazard_ty in hazard_tys {
        // Borrowed and raw-pointer arguments are not consumed owners. Their
        // pointees may be mutated, but they must not be re-attributed or make a
        // same-owner by-value call look ambiguous.
        if semantic_scope_argument_is_borrowed_or_raw(*hazard_ty) {
            continue;
        }
        let hazard_scan = heap_object_type_hazard_scan_from_ty(tcx, *hazard_ty);
        if hazard_scan.unresolved {
            return SemanticScopeHeapClass::Unresolved;
        }
        owners.extend(hazard_scan.owners);
    }

    if owners.len() == 1 {
        SemanticScopeHeapClass::Single(attributed_owner.display)
    } else {
        SemanticScopeHeapClass::Ambiguous(compiler_heap_owner_displays(&owners))
    }
}

fn semantic_scope_heap_class_with_by_value_hazards<'tcx>(
    tcx: TyCtxt<'tcx>,
    attribution_ty: Ty<'tcx>,
    hazard_tys: &[Ty<'tcx>],
) -> SemanticScopeHeapClass {
    let attribution_scan = heap_object_type_scan_from_ty(tcx, attribution_ty);
    if attribution_scan.unresolved {
        return SemanticScopeHeapClass::Unresolved;
    }
    let owners = attribution_scan.owners;
    if owners.is_empty() {
        return SemanticScopeHeapClass::Unresolved;
    }
    if owners.len() > 1 {
        return SemanticScopeHeapClass::Ambiguous(compiler_heap_owner_displays(&owners));
    }
    let attributed_owner = owners.iter().next().cloned().unwrap();
    semantic_scope_heap_class_with_known_attribution_and_by_value_hazards(
        tcx,
        attributed_owner,
        hazard_tys,
    )
}

fn semantic_scope_result_ok_heap_class<'tcx>(
    tcx: TyCtxt<'tcx>,
    ok_ty: Ty<'tcx>,
    err_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> SemanticScopeHeapClass {
    let ok_scan = heap_object_type_scan_from_ty(tcx, ok_ty);
    if ok_scan.unresolved || ok_scan.owners.is_empty() {
        return SemanticScopeHeapClass::Unresolved;
    }
    if ok_scan.owners.len() > 1 {
        return SemanticScopeHeapClass::Ambiguous(compiler_heap_owner_displays(&ok_scan.owners));
    }
    let attributed_owner = ok_scan.owners.into_iter().next().unwrap();
    let mut hazard_tys = Vec::with_capacity(argument_tys.len() + 1);
    hazard_tys.push(err_ty);
    hazard_tys.extend_from_slice(argument_tys);
    semantic_scope_heap_class_with_known_attribution_and_by_value_hazards(
        tcx,
        attributed_owner,
        &hazard_tys,
    )
}

fn direct_outer_vec_receiver_owner<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> Option<String> {
    let mut receiver_ty = ty;
    while let ty::Ref(_, inner, _) = receiver_ty.kind() {
        receiver_ty = *inner;
    }
    if clone_result_has_unresolved_params(receiver_ty) {
        return None;
    }
    let type_text = format!("{:?}", receiver_ty);
    let (owner_def, owner_args) = match receiver_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_vec_def_path)
        || !tcx.is_diagnostic_item(sym::Vec, owner_def.did())
        || !matches!(owner_args.len(), 1 | 2)
    {
        return None;
    }
    if owner_args.len() == 2 {
        let allocator_ty = generic_arg_type(owner_args.get(1)?)?;
        let (allocator_def, allocator_args) = match allocator_ty.kind() {
            ty::Adt(def, args) => (def, args),
            _ => return None,
        };
        if !allocator_args.is_empty()
            || allocator_def.did().krate != owner_def.did().krate
            || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        {
            return None;
        }
    }
    Some(type_text)
}

fn direct_outer_vec_destination_owner<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> Option<String> {
    if clone_result_has_unresolved_params(ty) {
        return None;
    }
    match ty.kind() {
        ty::Adt(adt, _) if exact_alloc_adt_def_id(tcx, adt.did(), exact_alloc_vec_def_path) => {
            Some(format!("{:?}", ty))
        }
        _ => None,
    }
}

fn direct_outer_vecdeque_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    mut owner_ty: Ty<'tcx>,
) -> Option<(String, DefId)> {
    while let ty::Ref(_, inner, _) = owner_ty.kind() {
        owner_ty = *inner;
    }
    if clone_result_has_unresolved_params(owner_ty) {
        return None;
    }

    let (owner_def, owner_args) = match owner_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_vecdeque_def_path)
        || !matches!(owner_args.len(), 1 | 2)
    {
        return None;
    }

    // Current VecDeque carries an explicit allocator parameter while the
    // pinned legacy surface does not. Exact public capacity APIs use Global;
    // allocator-specific variants must not inherit this direct-owner proof.
    if owner_args.len() == 2 {
        let allocator_ty = generic_arg_type(owner_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != owner_def.did().krate
        {
            return None;
        }
    }

    Some((format!("{:?}", owner_ty), owner_def.did()))
}

fn direct_outer_vecdeque_with_capacity_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_vecdeque_with_capacity_def_id(tcx, callee_def_id)
        || argument_tys.len() != 1
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }
    let (owner, owner_def_id) = direct_outer_vecdeque_owner(tcx, destination_ty)?;
    (owner_def_id.krate == callee_def_id.krate).then_some(owner)
}

fn direct_outer_vecdeque_capacity_receiver_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_vecdeque_capacity_only_def_id(tcx, callee_def_id)
        || !matches!(argument_tys.len(), 1 | 2)
        || argument_tys
            .get(1..)
            .unwrap_or_default()
            .iter()
            .any(|argument_ty| !matches!(argument_ty.kind(), ty::Uint(ty::UintTy::Usize)))
    {
        return None;
    }
    let (owner, owner_def_id) = direct_outer_vecdeque_owner(tcx, *argument_tys.first()?)?;
    (owner_def_id.krate == callee_def_id.krate).then_some(owner)
}

fn direct_outer_box_new_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_box_new_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || destination_args.is_empty()
        || generic_arg_type(destination_args.get(0)?)? != argument_tys[0]
    {
        return None;
    }

    // Box::new uses Global.  Preserve both the legacy Box<T> and current
    // Box<T, A> rustc surfaces, but do not broaden this proof to new_in.
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    } else if destination_args.len() != 1 {
        return None;
    }

    Some(format!("{:?}", destination_ty))
}

fn exact_global_box_payload<'tcx>(tcx: TyCtxt<'tcx>, owner_ty: Ty<'tcx>) -> Option<Ty<'tcx>> {
    let (owner_def, owner_args) = match owner_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(owner_def.did())
        || !matches!(owner_args.len(), 1 | 2)
    {
        return None;
    }
    if owner_args.len() == 2 {
        let allocator_ty = generic_arg_type(owner_args.get(1)?)?;
        let (allocator_def, allocator_args) = match allocator_ty.kind() {
            ty::Adt(def, args) => (def, args),
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != owner_def.did().krate
            || !allocator_args.is_empty()
        {
            return None;
        }
    }
    generic_arg_type(owner_args.get(0)?)
}

fn box_new_runtime_identity_owner_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<Ty<'tcx>> {
    // Route every exact Global Box::new through the same generic runtime ABI.
    // Generic and already-concrete MIR therefore both evaluate UniAlloc's
    // compiler TypeId contract after monomorphization instead of mixing that
    // identity with the older textual rustc_middle hash.
    if !exact_alloc_box_new_def_id(tcx, callee_def_id) || argument_tys.len() != 1 {
        return None;
    }
    let payload_ty = exact_global_box_payload(tcx, destination_ty)?;
    (payload_ty == argument_tys[0]).then_some(destination_ty)
}

fn vec_with_capacity_runtime_identity_owner_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<Ty<'tcx>> {
    // Route every exact Global Vec::with_capacity through the monomorphized
    // generic ABI. Definition-level MIR can contain Vec<T, Global>, while the
    // runtime helper is instantiated as Vec<Concrete, Global> by codegen. The
    // same route is used for already-concrete MIR so one Rust owner cannot be
    // split between runtime TypeId and the older compiler-hash namespace.
    if !exact_alloc_vec_with_capacity_def_id(tcx, callee_def_id)
        || argument_tys.len() != 1
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }
    let (owner_def, owner_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, owner_def.did(), exact_alloc_vec_def_path)
        || !tcx.is_diagnostic_item(sym::Vec, owner_def.did())
        || owner_def.did().krate != callee_def_id.krate
        || !matches!(owner_args.len(), 1 | 2)
    {
        return None;
    }

    let element_ty = generic_arg_type(owner_args.get(0)?)?;
    let allocator_ty = if owner_args.len() == 2 {
        let allocator_ty = generic_arg_type(owner_args.get(1)?)?;
        let (allocator_def, allocator_args) = match allocator_ty.kind() {
            ty::Adt(def, args) => (def, args),
            _ => return None,
        };
        if !allocator_args.is_empty()
            || allocator_def.did().krate != owner_def.did().krate
            || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        {
            return None;
        }
        Some(allocator_ty)
    } else {
        None
    };

    // Bind the canonical method's instantiated type arguments back to the
    // exact destination. Current rustc exposes only T for Global-only
    // `with_capacity`; compatible surfaces may also expose the allocator type.
    match callee_generic_types {
        [callee_element] if *callee_element == element_ty => {}
        [callee_element, callee_allocator]
            if *callee_element == element_ty && allocator_ty == Some(*callee_allocator) => {}
        _ => return None,
    }

    Some(destination_ty)
}

fn type_is_structurally_drop_inert<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>, depth: usize) -> bool {
    const MAX_DROP_INERT_DEPTH: usize = 8;
    if depth > MAX_DROP_INERT_DEPTH {
        return false;
    }
    match ty.kind() {
        ty::Bool
        | ty::Char
        | ty::Int(_)
        | ty::Uint(_)
        | ty::Float(_)
        | ty::Str
        | ty::Never
        | ty::FnDef(_, _)
        | ty::FnPtr(..)
        | ty::Ref(..) => true,
        #[cfg(unialloc_rustc_current)]
        ty::RawPtr(..) => true,
        #[cfg(not(unialloc_rustc_current))]
        ty::RawPtr(_) => true,
        ty::Array(element, _) | ty::Slice(element) => {
            type_is_structurally_drop_inert(tcx, *element, depth + 1)
        }
        ty::Tuple(fields) => fields
            .iter()
            .all(|field| type_is_structurally_drop_inert(tcx, field, depth + 1)),
        ty::Adt(adt, args) => {
            if adt.has_dtor(tcx) {
                return false;
            }
            if exact_core_drop_inert_wrapper_def_id(tcx, adt.did()) {
                return true;
            }
            adt.variants().iter().all(|variant| {
                variant.fields.iter().all(|field| {
                    #[cfg(unialloc_rustc_current)]
                    let field_ty = field.ty(tcx, args).skip_norm_wip();
                    #[cfg(not(unialloc_rustc_current))]
                    let field_ty = field.ty(tcx, args);
                    type_is_structurally_drop_inert(tcx, field_ty, depth + 1)
                })
            })
        }
        _ => false,
    }
}

fn drop_inert_box_runtime_identity_owner_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    owner_ty: Ty<'tcx>,
) -> Option<Ty<'tcx>> {
    let payload_ty = exact_global_box_payload(tcx, owner_ty)?;
    type_is_structurally_drop_inert(tcx, payload_ty, 0).then_some(owner_ty)
}

fn exact_box_drop_requires_allocation_recovery<'tcx>(
    tcx: TyCtxt<'tcx>,
    owner_ty: Ty<'tcx>,
) -> bool {
    exact_global_box_payload(tcx, owner_ty).is_some()
        && drop_inert_box_runtime_identity_owner_ty(tcx, owner_ty).is_none()
}

fn direct_outer_string_with_capacity_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_string_with_capacity_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }
    let destination_def = match destination_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_string_def_path)
        || destination_def.did().krate != callee_def_id.krate
    {
        return None;
    }
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_string_from_str_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_core_from_from_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
    {
        return None;
    }

    let destination_def = match destination_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_string_def_path) {
        return None;
    }
    if tcx.lang_items().string() != Some(destination_def.did()) {
        return None;
    }

    let source_is_str_ref = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => matches!(inner.kind(), ty::Str),
        _ => false,
    };
    if !source_is_str_ref {
        return None;
    }

    // Coherence prevents downstream crates from replacing the core From<&str>
    // implementation for alloc::String.  The exact trait DefId plus exact
    // source and destination types therefore proves the allocating conversion
    // without broadening arbitrary From::from calls into factory provenance.
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_string_from_str_to_owned_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_to_owned_to_owned_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
    {
        return None;
    }

    let destination_def = match destination_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_string_def_path)
        || tcx.lang_items().string() != Some(destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
    {
        return None;
    }

    let source_is_str_ref = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => matches!(inner.kind(), ty::Str),
        _ => false,
    };
    if !source_is_str_ref {
        return None;
    }

    // The exact alloc-owned ToOwned trait method plus &str and alloc::String
    // proves the standard byte-copy allocation. Slice, generic, custom
    // same-name, and other ToOwned monomorphizations retain the fail-closed
    // factory path below.
    Some(format!("{:?}", destination_ty))
}

fn immutable_ref_to_exact_std_path_component<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> bool {
    let inner = match ty.kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => *inner,
        _ => return false,
    };
    match inner.kind() {
        ty::Str => true,
        ty::Adt(def, args) if args.is_empty() => {
            exact_std_crate_def_id(tcx, def.did())
                && (exact_std_path_def_path(&tcx.def_path_str(def.did()))
                    || exact_std_os_str_def_path(&tcx.def_path_str(def.did())))
        }
        _ => false,
    }
}

fn direct_outer_std_path_factory_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if clone_result_has_unresolved_params(destination_ty) {
        return None;
    }

    let destination_def = match destination_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_std_crate_def_id(tcx, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !exact_std_pathbuf_def_path(&tcx.def_path_str(destination_def.did()))
    {
        return None;
    }

    let expected_argument_count = if exact_std_path_method_def_id(tcx, callee_def_id, "to_path_buf")
    {
        1
    } else if exact_std_path_method_def_id(tcx, callee_def_id, "join") {
        2
    } else {
        return None;
    };
    if argument_tys.len() != expected_argument_count
        || !argument_tys
            .iter()
            .all(|argument_ty| immutable_ref_to_exact_std_path_component(tcx, *argument_ty))
    {
        return None;
    }

    // The receiver must always be the canonical std Path. For join, the
    // second argument is limited to immutable references whose standard
    // AsRef<Path> implementation is callback-free: Path, OsStr, or str.
    let receiver_inner = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => *inner,
        _ => return None,
    };
    let receiver_def = match receiver_inner.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if receiver_def.did().krate != callee_def_id.krate
        || !exact_std_path_def_path(&tcx.def_path_str(receiver_def.did()))
    {
        return None;
    }

    Some(format!("{:?}", destination_ty))
}

fn direct_outer_std_dir_entry_path_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_std_dir_entry_method_def_id(tcx, callee_def_id, "path")
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
    {
        return None;
    }

    let destination_def = match destination_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if destination_def.did().krate != callee_def_id.krate
        || !exact_std_pathbuf_def_path(&tcx.def_path_str(destination_def.did()))
    {
        return None;
    }

    let receiver_inner = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => *inner,
        _ => return None,
    };
    let receiver_def = match receiver_inner.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if receiver_def.did().krate != callee_def_id.krate
        || !exact_std_dir_entry_def_path(&tcx.def_path_str(receiver_def.did()))
    {
        return None;
    }

    // The exact std method, canonical borrowed DirEntry receiver, and direct
    // PathBuf result prove that allocations inside the precompiled platform
    // implementation belong to the returned PathBuf. Generic wrappers and
    // same-name methods retain the fail-closed factory path below.
    Some(format!("{:?}", destination_ty))
}

fn callback_free_slice_to_vec_primitive_element(ty: Ty<'_>) -> bool {
    matches!(
        ty.kind(),
        ty::Bool | ty::Char | ty::Int(_) | ty::Uint(_) | ty::Float(_)
    )
}

fn direct_outer_vec_from_primitive_slice_to_vec_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_slice_to_vec_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_vec_def_path)
        || !tcx.is_diagnostic_item(sym::Vec, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !matches!(destination_args.len(), 1 | 2)
    {
        return None;
    }
    let destination_element = generic_arg_type(destination_args.get(0)?)?;
    if !callback_free_slice_to_vec_primitive_element(destination_element)
        || callee_generic_types != [destination_element]
    {
        return None;
    }

    let source_element = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => match inner.kind() {
            ty::Slice(element) => *element,
            _ => return None,
        },
        _ => return None,
    };
    if source_element != destination_element {
        return None;
    }

    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let (allocator_def, allocator_args) = match allocator_ty.kind() {
            ty::Adt(def, args) => (def, args),
            _ => return None,
        };
        if !allocator_args.is_empty()
            || allocator_def.did().krate != destination_def.did().krate
            || !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
        {
            return None;
        }
    }

    // Canonical slice::to_vec can invoke T::clone. Restrict the whole-call
    // scope to compiler primitive scalar types whose clone is callback-free
    // and drop-inert. Generic T, user Clone implementations, custom methods,
    // and allocator-specific factories retain the fail-closed path.
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_vec_u8_from_u8_slice_to_owned_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_to_owned_to_owned_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_vec_def_path)
        || !tcx.is_diagnostic_item(sym::Vec, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !matches!(destination_args.len(), 1 | 2)
    {
        return None;
    }

    let destination_element = generic_arg_type(destination_args.get(0)?)?;
    if !matches!(destination_element.kind(), ty::Uint(ty::UintTy::U8)) {
        return None;
    }

    let source_is_immutable_u8_slice = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => {
            matches!(inner.kind(), ty::Slice(element) if matches!(element.kind(), ty::Uint(ty::UintTy::U8)))
        }
        _ => false,
    };
    if !source_is_immutable_u8_slice {
        return None;
    }

    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    }

    // The exact alloc-owned ToOwned method, &[u8] receiver, and Global-backed
    // Vec<u8> destination prove a byte-copy allocation without running any
    // user-defined Clone callback. Other slice elements remain fail closed.
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_vec_u8_from_elem_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_vec_from_elem_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 2
        || argument_tys
            .iter()
            .any(|argument_ty| clone_result_has_unresolved_params(*argument_ty))
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::U8))
        || !matches!(argument_tys[1].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !tcx.is_diagnostic_item(sym::Vec, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !matches!(destination_args.len(), 1 | 2)
        || !matches!(
            generic_arg_type(destination_args.get(0)?)?.kind(),
            ty::Uint(ty::UintTy::U8)
        )
    {
        return None;
    }
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    }

    // The canonical Vec diagnostic item binds this proof to the sysroot alloc
    // crate; its exact implementation specializes u8 repetition without any
    // user Clone callback and creates only the returned Global-backed byte
    // vector. Generic T, other element types, custom Clone, same-name functions,
    // and --extern alloc spoofs retain the fail-closed factory path below.
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_vec_u8_from_copied_slice_iter_collect_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_core_iterator_collect_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !tcx.is_diagnostic_item(sym::Vec, destination_def.did())
        || !exact_alloc_crate_def_id(tcx, destination_def.did())
        || !matches!(destination_args.len(), 1 | 2)
        || !matches!(
            generic_arg_type(destination_args.get(0)?)?.kind(),
            ty::Uint(ty::UintTy::U8)
        )
    {
        return None;
    }
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    }

    let (copied_def, copied_args) = match argument_tys[0].kind() {
        ty::Adt(def, args) if exact_core_copied_iterator_adapter_def_id(tcx, def.did()) => {
            let path = strip_rustc_crate_disambiguators(&tcx.def_path_str(def.did()));
            if !matches!(
                path.as_str(),
                "core::iter::adapters::copied::Copied" | "std::iter::Copied"
            ) {
                return None;
            }
            (def, args)
        }
        _ => return None,
    };
    if !exact_core_crate_def_id(tcx, copied_def.did()) {
        return None;
    }
    let copied_types = copied_args.types().collect::<Vec<_>>();
    if copied_types.len() != 1 {
        return None;
    }
    let (iter_def, iter_args) = match copied_types[0].kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_core_crate_def_id(tcx, iter_def.did())
        || !matches!(
            strip_rustc_crate_disambiguators(&tcx.def_path_str(iter_def.did())).as_str(),
            "core::slice::iter::Iter" | "std::slice::Iter"
        )
    {
        return None;
    }
    let iter_types = iter_args.types().collect::<Vec<_>>();
    if iter_types.len() != 1 || !matches!(iter_types[0].kind(), ty::Uint(ty::UintTy::U8)) {
        return None;
    }

    // The canonical Vec diagnostic item prevents an external crate named
    // `alloc` from spoofing this destination and supplying an arbitrary
    // FromIterator callback. Exact core Copied<slice::Iter<u8>> has no user
    // callback, and coherence fixes canonical Vec<u8, Global>'s FromIterator
    // implementation. The call can only allocate the returned byte vector,
    // unlike arbitrary Iterator::collect.
    Some(format!("{:?}", destination_ty))
}

fn direct_outer_box_u8_slice_from_u8_slice_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_core_from_from_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_box_def_path)
        || tcx.lang_items().owned_box() != Some(destination_def.did())
        || !matches!(destination_args.len(), 1 | 2)
    {
        return None;
    }

    let destination_payload = generic_arg_type(destination_args.get(0)?)?;
    if !matches!(destination_payload.kind(), ty::Slice(element) if matches!(element.kind(), ty::Uint(ty::UintTy::U8)))
    {
        return None;
    }

    let source_is_immutable_u8_slice = match argument_tys[0].kind() {
        ty::Ref(_, inner, rustc_ast::Mutability::Not) => {
            matches!(inner.kind(), ty::Slice(element) if matches!(element.kind(), ty::Uint(ty::UintTy::U8)))
        }
        _ => false,
    };
    if !source_is_immutable_u8_slice {
        return None;
    }

    // Box<[T]>::from(&[T]) clones each element. Restrict the proof to u8 so
    // no user-defined Clone implementation can allocate or free unrelated
    // owners while the direct Box allocation scope is active.
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    }

    Some(format!("{:?}", destination_ty))
}

fn direct_outer_arc_new_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_arc_new_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_arc_def_path)
        || destination_def.did().krate != callee_def_id.krate
        || destination_args.is_empty()
        || generic_arg_type(destination_args.get(0)?)? != argument_tys[0]
    {
        return None;
    }

    // Current Arc has an explicit allocator parameter while the pinned legacy
    // Arc does not. Arc::new specifically returns Arc<T, Global>; reject any
    // future or custom shape rather than broadening this constructor proof.
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    } else if destination_args.len() != 1 {
        return None;
    }

    Some(format!("{:?}", destination_ty))
}

fn direct_outer_rc_new_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_alloc_rc_new_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || clone_result_has_unresolved_params(argument_tys[0])
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_alloc_adt_def_id(tcx, destination_def.did(), exact_alloc_rc_def_path)
        || destination_def.did().krate != callee_def_id.krate
        || destination_args.is_empty()
        || generic_arg_type(destination_args.get(0)?)? != argument_tys[0]
    {
        return None;
    }

    // Current Rc has an explicit allocator parameter while the pinned legacy
    // Rc does not. Rc::new specifically returns Rc<T, Global>; reject any
    // future or custom shape rather than broadening this constructor proof.
    if destination_args.len() == 2 {
        let allocator_ty = generic_arg_type(destination_args.get(1)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path)
            || allocator_def.did().krate != destination_def.did().krate
        {
            return None;
        }
    } else if destination_args.len() != 1 {
        return None;
    }

    Some(format!("{:?}", destination_ty))
}

fn direct_outer_std_hash_map_with_capacity_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_std_hash_map_with_capacity_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_std_crate_def_id(tcx, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !exact_std_hash_map_def_path(&tcx.def_path_str(destination_def.did()))
        || !matches!(destination_args.len(), 3 | 4)
    {
        return None;
    }

    // Both supported rustc surfaces expose K, V, RandomState in that order.
    // `with_capacity` has no hasher argument, so accepting any other hasher
    // would silently broaden this proof to with_capacity_and_hasher-like
    // semantics.
    let random_state_ty = generic_arg_type(destination_args.get(2)?)?;
    let random_state_def = match random_state_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_std_crate_def_id(tcx, random_state_def.did())
        || random_state_def.did().krate != destination_def.did().krate
        || !exact_std_hash_map_random_state_def_path(&tcx.def_path_str(random_state_def.did()))
    {
        return None;
    }

    // Current std exposes HashMap<K, V, S, A>; the pinned nightly exposes
    // HashMap<K, V, S>.  When A exists, exact with_capacity returns Global.
    if destination_args.len() == 4 {
        let allocator_ty = generic_arg_type(destination_args.get(3)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path) {
            return None;
        }
    }

    Some(format!("{:?}", destination_ty))
}

fn direct_outer_std_hash_set_with_capacity_destination_owner<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<String> {
    if !exact_std_hash_set_with_capacity_def_id(tcx, callee_def_id)
        || clone_result_has_unresolved_params(destination_ty)
        || argument_tys.len() != 1
        || !matches!(argument_tys[0].kind(), ty::Uint(ty::UintTy::Usize))
    {
        return None;
    }

    let (destination_def, destination_args) = match destination_ty.kind() {
        ty::Adt(def, args) => (def, args),
        _ => return None,
    };
    if !exact_std_crate_def_id(tcx, destination_def.did())
        || destination_def.did().krate != callee_def_id.krate
        || !exact_std_hash_set_def_path(&tcx.def_path_str(destination_def.did()))
        || !matches!(destination_args.len(), 2 | 3)
    {
        return None;
    }

    // Both supported rustc surfaces expose T, RandomState in that order.
    // `with_capacity` has no hasher argument, so accepting any other hasher
    // would silently broaden this proof to with_capacity_and_hasher-like
    // semantics.
    let random_state_ty = generic_arg_type(destination_args.get(1)?)?;
    let random_state_def = match random_state_ty.kind() {
        ty::Adt(def, args) if args.is_empty() => def,
        _ => return None,
    };
    if !exact_std_crate_def_id(tcx, random_state_def.did())
        || random_state_def.did().krate != destination_def.did().krate
        || !exact_std_hash_map_random_state_def_path(&tcx.def_path_str(random_state_def.did()))
    {
        return None;
    }

    // Current std exposes HashSet<T, S, A>; the pinned nightly exposes
    // HashSet<T, S>.  When A exists, exact with_capacity returns Global.
    if destination_args.len() == 3 {
        let allocator_ty = generic_arg_type(destination_args.get(2)?)?;
        let allocator_def = match allocator_ty.kind() {
            ty::Adt(def, args) if args.is_empty() => def,
            _ => return None,
        };
        if !exact_alloc_adt_def_id(tcx, allocator_def.did(), exact_alloc_global_def_path) {
            return None;
        }
    }

    Some(format!("{:?}", destination_ty))
}

fn fail_closed_unproven_factory(heap_class: SemanticScopeHeapClass) -> SemanticScopeHeapClass {
    if matches!(heap_class, SemanticScopeHeapClass::Single(_)) {
        SemanticScopeHeapClass::Unresolved
    } else {
        // Keep multiple-owner detail: a conflicting consumed argument remains
        // an explicit ambiguity even when the callee body is unproven.
        heap_class
    }
}

fn non_plain_semantic_scope_heap_class<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee_generic_types: &[Ty<'tcx>],
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
    callee: &str,
) -> SemanticScopeHeapClass {
    if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_vecdeque_with_capacity_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact VecDeque::with_capacity allocates only the direct ring buffer.
        // No element exists yet, so nested supported owners inside T cannot be
        // identities for this allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id
        .and_then(|def_id| direct_outer_vecdeque_capacity_receiver_owner(tcx, def_id, argument_tys))
    {
        // reserve*/shrink* capacity methods can only replace or release the
        // direct VecDeque ring buffer. Element-affecting methods and Drop keep
        // the full owner-graph fail-closed rule below.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_box_new_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Exact Box::new creates one Global-backed allocation for precisely the
        // moved payload type. Box::new_in and custom same-name functions are
        // excluded by DefId/path and destination-allocator checks.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_string_with_capacity_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact String::with_capacity allocates only the direct UTF-8 buffer.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_string_from_str_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Exact core From<&str> for alloc::String copies the source bytes into
        // the direct String buffer.  Other From conversions and arbitrary
        // String-returning factories retain the fail-closed path below.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_string_from_str_to_owned_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact alloc ToOwned::to_owned for &str copies bytes into one direct
        // String buffer. Other ToOwned monomorphizations and same-name methods
        // remain audit-only.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_std_dir_entry_path_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Exact std DirEntry::path returns one direct PathBuf while keeping its
        // receiver borrowed. This scope reaches the PathBuf allocation and
        // growth performed inside precompiled std without widening arbitrary
        // filesystem methods or iterator calls.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_std_path_factory_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Exact std Path::to_path_buf and Path::join copy into the direct
        // PathBuf buffer. Restrict join to canonical immutable borrowed Path,
        // OsStr, and str arguments so user AsRef callbacks and consumed owners
        // retain the fail-closed factory path below.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_vec_from_primitive_slice_to_vec_destination_owner(
            tcx,
            def_id,
            callee_generic_types,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact alloc slice::to_vec for primitive scalar elements has a
        // callback-free clone path and allocates only the returned Global Vec.
        // Generic or user-defined Clone element types remain audit-only.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_vec_u8_from_u8_slice_to_owned_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact alloc ToOwned::to_owned for &[u8] copies bytes into one direct
        // Global-backed Vec<u8> buffer. Other slice element types remain
        // audit-only because their Clone implementation can allocate.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_vec_u8_from_elem_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Exact alloc::vec::from_elem::<u8> uses the callback-free byte
        // specialization and allocates only the returned Global-backed Vec.
        // Generic T, other element types, and custom same-name functions remain
        // audit-only.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_vec_u8_from_copied_slice_iter_collect_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact Copied<slice::Iter<u8>>::collect::<Vec<u8, Global>>() has no
        // user callback and allocates only the destination byte-vector backing.
        // All other Iterator::collect shapes remain fail closed.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_box_u8_slice_from_u8_slice_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact core From<&[u8]> for Global-backed Box<[u8]> performs one
        // direct byte-buffer allocation. Other element types remain audit-only
        // because their Clone implementation can execute arbitrary allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_arc_new_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Arc::new creates one ref-counted allocation for the direct Arc<T>
        // destination. The moved T may itself contain supported owners, but
        // those allocations already exist before this exact constructor call
        // and are not identities for the new Arc allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_rc_new_destination_owner(tcx, def_id, destination_ty, argument_tys)
    }) {
        // Rc::new creates one ref-counted allocation for the direct Rc<T>
        // destination. The moved T may itself contain supported owners, but
        // those allocations already exist before this exact constructor call
        // and are not identities for the new Rc allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_std_hash_map_with_capacity_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact std HashMap::with_capacity allocates only the direct raw table.
        // K/V values do not exist yet, so nested supported owners inside K or V
        // cannot be identities for that allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if let Some(owner) = callee_def_id.and_then(|def_id| {
        direct_outer_std_hash_set_with_capacity_destination_owner(
            tcx,
            def_id,
            destination_ty,
            argument_tys,
        )
    }) {
        // Exact std HashSet::with_capacity allocates only the direct raw table.
        // Elements do not exist yet, so nested supported owners inside T cannot
        // be identities for that allocation.
        SemanticScopeHeapClass::Single(owner)
    } else if callee_def_id.map_or(false, |def_id| {
        exact_std_hash_map_capacity_only_def_id(tcx, def_id)
    }) {
        // HashMap capacity changes may rehash and invoke user Hash/Eq callbacks.
        // Those callbacks can allocate arbitrary unrelated objects, so even an
        // exact std DefId cannot soundly scope the whole call as the table's
        // allocation identity. Keep reserve*/shrink* calls explicit and
        // audit-only until a narrower internal allocation boundary is proven.
        SemanticScopeHeapClass::CallbackCapable
    } else if callee_def_id.map_or(false, |def_id| {
        exact_alloc_vec_with_capacity_def_id(tcx, def_id)
    }) {
        // Exact Vec::with_capacity creates only the direct destination Vec
        // backing. Nested heap-owning element types are not allocated here.
        // Keep every by-value argument as a fail-closed safety hazard.
        match direct_outer_vec_destination_owner(tcx, destination_ty) {
            Some(owner) => compiler_heap_owner(tcx, destination_ty)
                .filter(|identity| identity.display == owner)
                .map(|identity| {
                    semantic_scope_heap_class_with_known_attribution_and_by_value_hazards(
                        tcx,
                        identity,
                        argument_tys,
                    )
                })
                .unwrap_or(SemanticScopeHeapClass::Unresolved),
            None => SemanticScopeHeapClass::Unresolved,
        }
    } else if callee_def_id.map_or(false, |def_id| {
        exact_alloc_vec_capacity_only_def_id(tcx, def_id)
    }) {
        // These methods can allocate, reallocate, or free only the direct Vec
        // backing buffer; they never clone/drop elements or run element code.
        // Keep the later by-value hazard gate, but do not make nested supported
        // element owners (for example String in Vec<String>) ambiguous with the
        // outer buffer whose capacity is being changed.
        match argument_tys.first() {
            Some(receiver_ty) => match direct_outer_vec_receiver_owner(tcx, *receiver_ty) {
                Some(owner) => direct_supported_heap_owner_ty(tcx, *receiver_ty)
                    .and_then(|owner_ty| compiler_heap_owner(tcx, owner_ty))
                    .filter(|identity| identity.display == owner)
                    .map(|identity| {
                        semantic_scope_heap_class_with_known_attribution_and_by_value_hazards(
                            tcx,
                            identity,
                            &argument_tys[1..],
                        )
                    })
                    .unwrap_or(SemanticScopeHeapClass::Unresolved),
                None => SemanticScopeHeapClass::Unresolved,
            },
            None => SemanticScopeHeapClass::Unresolved,
        }
    } else if semantic_scope_receiver_heap_owner_call(callee) {
        // Generic receiver mutators/deallocators can invoke Iterator, Clone,
        // AsRef, Hash/Eq, or element Drop code. Exact callback-free capacity
        // operations are handled above; every broader whole-call candidate
        // remains audit-only so unrelated callback allocations cannot inherit
        // the receiver identity. This also rejects local same-name traits such
        // as `EvilReserve::reserve(&mut Vec<_>)`.
        SemanticScopeHeapClass::CallbackCapable
    } else if let Some((ok_ty, err_ty)) = exact_result_destination_types(tcx, destination_ty) {
        // A fallible factory returns the Ok payload.  The Err payload is not
        // an allocation identity for successful work inside the call, but it
        // remains a by-value safety hazard. Return structure alone is not an
        // allocation proof, so even a single Ok owner remains audit-only here.
        fail_closed_unproven_factory(semantic_scope_result_ok_heap_class(
            tcx,
            ok_ty,
            err_ty,
            argument_tys,
        ))
    } else {
        // A direct supported destination is still not allocation provenance: a
        // helper can return an existing or empty owner while allocating and
        // dropping an unrelated object internally. Scoping that helper from
        // its return type misattributes the transient allocation and can poison
        // recovery. Keep non-exact factories audit-only until an exact
        // constructor matcher or sound callee-body proof exists.
        let heap_class = if direct_supported_heap_object_destination(tcx, destination_ty) {
            // A consumed by-value owner remains a scope-safety hazard: the
            // callee can free it before producing the destination. Merge those
            // owner graphs only to reject conflicting/unresolved scopes; never
            // select an argument identity as the returned allocation identity.
            semantic_scope_heap_class_with_by_value_hazards(tcx, destination_ty, argument_tys)
        } else {
            match semantic_scope_heap_class_with_by_value_hazards(tcx, destination_ty, argument_tys)
            {
                // Preserve multiple-owner and structural hazard detail in the
                // audit, but never lower a nested single owner without exact
                // allocation provenance.
                SemanticScopeHeapClass::Single(_) => SemanticScopeHeapClass::Unresolved,
                other => other,
            }
        };
        fail_closed_unproven_factory(heap_class)
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

fn exact_scope_owner_ty_for_display<'tcx>(
    tcx: TyCtxt<'tcx>,
    semantic_object_type: &str,
    destination_ty: Ty<'tcx>,
    argument_tys: &[Ty<'tcx>],
) -> Option<Ty<'tcx>> {
    if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
        return None;
    }
    let mut owners = Vec::new();
    for candidate_ty in std::iter::once(destination_ty).chain(argument_tys.iter().copied()) {
        let Some(owner_ty) = direct_supported_heap_owner_ty(tcx, candidate_ty) else {
            continue;
        };
        if format!("{:?}", owner_ty) != semantic_object_type
            || compiler_semantic_type_id(tcx, owner_ty).is_none()
        {
            continue;
        }
        if !owners.iter().any(|existing| *existing == owner_ty) {
            owners.push(owner_ty);
        }
    }
    match owners.as_slice() {
        [owner_ty] => Some(*owner_ty),
        _ => None,
    }
}

fn unknown_heap_object_solution() -> HeapObjectSolution {
    HeapObjectSolution {
        object_type: UNKNOWN_HEAP_OBJECT_TYPE.to_string(),
        type_id_basis: "direct_allocator_recovery_delegated",
    }
}

fn ty_heap_object_audit_solution<'tcx>(
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
            type_id_basis: "rustc_middle_ty_destination_or_argument_audit_only",
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
    fn rustc_hash_mapping_matches_runtime_semantic_type_ids() {
        assert_eq!(
            runtime_semantic_type_id_from_rustc_hash(
                0x1e23_2110_29f6_f504_2b2d_e51d_27aa_ed2au128,
                Endian::Little,
            ),
            9_887_144_619_654_301_271,
        );
        assert_eq!(
            runtime_semantic_type_id_from_rustc_hash(
                0xec9d_afc1_6457_9586_1cd5_3ff6_d6ff_bf39u128,
                Endian::Little,
            ),
            756_081_824_842_802_164,
        );
        assert_eq!(
            runtime_semantic_type_id_from_rustc_hash(
                0x1e23_2110_29f6_f504_2b2d_e51d_27aa_ed2au128,
                Endian::Big,
            ),
            4_938_801_762_559_584_019,
        );
        assert_eq!(
            runtime_semantic_type_id_from_rustc_hash(
                0xec9d_afc1_6457_9586_1cd5_3ff6_d6ff_bf39u128,
                Endian::Big,
            ),
            15_752_639_195_746_882_450,
        );
    }

    #[test]
    fn lifetime_profile_parser_selects_only_exact_guarded_sites() {
        let profile = parse_lifetime_profile(
            PathBuf::from("profile.txt"),
            b"# generated from a prior audit\nunialloc-lifetime-profile-v1\n0x11 0x22 0x33 ephemeral\n68 85 102 long-lived\n",
        );
        assert!(profile.format_valid);
        assert_eq!(profile.invalid_line_count, 0);
        assert_eq!(profile.entries.len(), 2);
        assert_eq!(
            select_lifetime_hint(Some(&profile), 9, 0, 0x11, 0x22, 0x33),
            LifetimeHintSelection {
                hint: LIFETIME_HINT_EPHEMERAL,
                confidence: 100,
                basis: "profile_exact_match",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 9, 0, 68, 85, 102),
            LifetimeHintSelection {
                hint: LIFETIME_HINT_LONG_LIVED,
                confidence: 100,
                basis: "profile_exact_match",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 9, 0, 0x11, 0x22, 0x34),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_type_or_module_guard_mismatch",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 9, 0, 0x99, 0x22, 0x33),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_missing_entry",
            }
        );
    }

    #[test]
    fn lifetime_profile_duplicate_and_invalid_entries_fail_closed() {
        let profile = parse_lifetime_profile(
            PathBuf::from("profile.txt"),
            b"unialloc-lifetime-profile-v1\n1 2 3 1\n1 2 3 1\n4 5 6 77\nmalformed\n",
        );
        assert!(profile.format_valid);
        assert_eq!(profile.duplicate_key_count, 1);
        assert_eq!(profile.invalid_line_count, 2);
        assert_eq!(
            select_lifetime_hint(Some(&profile), 2, 0, 1, 2, 3),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_ambiguous_entry",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 2, 0, 4, 5, 6),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_invalid_entry",
            }
        );

        let wrong_format = parse_lifetime_profile(
            PathBuf::from("profile.txt"),
            b"unialloc-lifetime-profile-v0\n1 2 3 1\n",
        );
        assert!(!wrong_format.format_valid);
        assert_eq!(
            select_lifetime_hint(Some(&wrong_format), 2, 0, 1, 2, 3),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_invalid_format",
            }
        );
        assert_eq!(
            select_lifetime_hint(None, 2, 0, 1, 2, 3),
            LifetimeHintSelection {
                hint: 2,
                confidence: 100,
                basis: "manual_global_lifetime_hint",
            }
        );
    }

    #[test]
    fn lifetime_profile_v2_abstains_below_the_configured_confidence() {
        let profile = parse_lifetime_profile(
            PathBuf::from("profile-v2.txt"),
            b"unialloc-lifetime-profile-v2\n0x11 0x22 0x33 long-lived 95\n0x44 0x55 0x66 ephemeral 40\n0x77 0x88 0x99 long-lived 101\n",
        );
        assert!(profile.format_valid);
        assert_eq!(profile.invalid_line_count, 1);
        assert_eq!(profile.entries.len(), 3);
        assert_eq!(
            select_lifetime_hint(Some(&profile), 0, 80, 0x11, 0x22, 0x33),
            LifetimeHintSelection {
                hint: LIFETIME_HINT_LONG_LIVED,
                confidence: 95,
                basis: "profile_exact_match",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 0, 80, 0x44, 0x55, 0x66),
            LifetimeHintSelection {
                hint: 0,
                confidence: 40,
                basis: "profile_below_confidence_threshold",
            }
        );
        assert_eq!(
            select_lifetime_hint(Some(&profile), 0, 80, 0x77, 0x88, 0x99),
            LifetimeHintSelection {
                hint: 0,
                confidence: 0,
                basis: "profile_invalid_entry",
            }
        );
    }

    #[test]
    fn automatic_lifetime_selection_is_conservative_and_explicit_inputs_win() {
        let short = select_lifetime_hint_with_automatic_decision(
            None,
            0,
            0,
            true,
            Some(AutomaticLifetimeDecision::ExactLocalDropBeforePhaseBoundary),
            1,
            2,
            3,
        );
        assert_eq!(short.hint, LIFETIME_HINT_EPHEMERAL);
        assert_eq!(short.confidence, 100);
        assert!(automatic_lifetime_ephemeral_basis(short.basis));

        let long = select_lifetime_hint_with_automatic_decision(
            None,
            0,
            100,
            true,
            Some(AutomaticLifetimeDecision::ExactLocalDropAfterPhaseBoundary),
            1,
            2,
            3,
        );
        assert_eq!(long.hint, LIFETIME_HINT_LONG_LIVED);
        assert_eq!(long.confidence, 100);
        assert!(automatic_lifetime_long_lived_basis(long.basis));

        let unknown = select_lifetime_hint_with_automatic_decision(
            None,
            0,
            0,
            true,
            Some(AutomaticLifetimeDecision::InterveningCallMayAdvanceEpochUnknown),
            1,
            2,
            3,
        );
        assert_eq!(unknown.hint, 0);
        assert!(automatic_lifetime_unknown_basis(unknown.basis));

        let effectful_drop = select_lifetime_hint_with_automatic_decision(
            None,
            0,
            0,
            true,
            Some(AutomaticLifetimeDecision::EffectfulDropGlueUnknown),
            1,
            2,
            3,
        );
        assert_eq!(effectful_drop.hint, 0);
        assert_eq!(
            effectful_drop.basis,
            "automatic_effectful_drop_glue_unknown"
        );
        assert!(automatic_lifetime_unknown_basis(effectful_drop.basis));

        let manual = select_lifetime_hint_with_automatic_decision(
            None,
            LIFETIME_HINT_LONG_LIVED,
            0,
            true,
            Some(AutomaticLifetimeDecision::ExactLocalDropBeforePhaseBoundary),
            1,
            2,
            3,
        );
        assert_eq!(manual.hint, LIFETIME_HINT_LONG_LIVED);
        assert_eq!(manual.basis, "manual_global_lifetime_hint");
    }

    #[test]
    fn lifetime_epoch_boundary_path_is_exact() {
        assert!(exact_unialloc_lifetime_epoch_boundary_def_path(
            "unialloc[a11c]::alloc_api::lifetime_hugepage::lifetime_hugepage_advance_epoch"
        ));
        assert!(exact_unialloc_lifetime_epoch_boundary_def_path(
            "unialloc::lifetime_hugepage_advance_epoch"
        ));
        assert!(!exact_unialloc_lifetime_epoch_boundary_def_path(
            "app::lifetime_hugepage_advance_epoch"
        ));
        assert!(!exact_unialloc_lifetime_epoch_boundary_def_path(
            "unialloc::lifetime_hugepage_phase_flush_current_thread"
        ));
    }

    #[test]
    fn semantic_scope_default_recovery_hint_preserves_explicit_hints_and_local_pairs() {
        assert_eq!(
            semantic_scope_placement_hint_from_configured(0, false, "default", false),
            (
                PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "default_recovery_backed_semantic_scope"
            )
        );
        assert_eq!(
            semantic_scope_placement_hint_from_configured(0, false, "default", true),
            (0, false, "exact_local_no_recovery")
        );
        assert_eq!(
            semantic_scope_placement_hint_from_configured(7, false, "manual_placement_hint", false,),
            (
                7 | PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "manual_placement_hint_and_default_recovery_backed_semantic_scope"
            )
        );
        assert_eq!(
            semantic_scope_placement_hint_from_configured(
                PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "manual_cross_thread_recovery_hint",
                false,
            ),
            (
                PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "manual_cross_thread_recovery_hint"
            )
        );
        assert_eq!(
            semantic_scope_placement_hint_from_configured(
                PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "auto_cross_thread_escape",
                false,
            ),
            (
                PLACEMENT_HINT_CROSS_THREAD_RECOVERY,
                true,
                "auto_cross_thread_escape"
            )
        );
        assert_eq!(
            semantic_scope_placement_hint_from_configured(9, false, "manual_placement_hint", true,),
            (9, false, "manual_placement_hint")
        );
    }

    #[test]
    fn plain_clone_matcher_is_trait_exact_and_excludes_clone_from() {
        assert!(exact_core_clone_trait_item_def_path(
            "core[53a3]::clone::Clone::clone"
        ));
        // Current rustc prints the std re-export spelling for this core-owned
        // trait-item DefId; the exact DefId check also requires crate `core`.
        assert!(exact_core_clone_trait_item_def_path(
            "std::clone::Clone::clone"
        ));
        assert!(!exact_core_clone_trait_item_def_path(
            "my_crate::core::clone::Clone::clone"
        ));
        assert!(!exact_core_clone_trait_item_def_path(
            "core::clone::Clone::clone_from"
        ));
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
    fn box_slice_into_vec_matcher_is_def_path_exact_and_one_way() {
        assert!(exact_alloc_slice_to_vec_def_path(
            "alloc[d734]::slice::{impl#0}::to_vec"
        ));
        assert!(exact_alloc_slice_to_vec_def_path(
            "alloc::slice::{impl#27}::to_vec"
        ));
        assert!(exact_alloc_slice_to_vec_def_path(
            "std::slice::<impl [T]>::to_vec"
        ));
        assert!(!exact_alloc_slice_to_vec_def_path(
            "alloc::slice::{impl#0}::to_vec_unchecked"
        ));
        assert!(!exact_alloc_slice_to_vec_def_path(
            "alloc::slice::{impl#x}::to_vec"
        ));
        assert!(!exact_alloc_slice_to_vec_def_path(
            "my_crate::alloc::slice::{impl#0}::to_vec"
        ));
        assert!(exact_alloc_slice_into_vec_def_path(
            "alloc[d734]::slice::{impl#0}::into_vec"
        ));
        assert!(exact_alloc_slice_into_vec_def_path(
            "std::slice::<impl [T]>::into_vec"
        ));
        assert!(!exact_alloc_slice_into_vec_def_path(
            "alloc::slice::{impl#1}::into_vec"
        ));
        assert!(!exact_alloc_slice_into_vec_def_path(
            "alloc::slice::{impl#0}::into_vec_unchecked"
        ));
        assert!(!exact_alloc_slice_into_vec_def_path(
            "alloc::vec::{impl#2}::into_boxed_slice"
        ));
        assert!(!exact_alloc_slice_into_vec_def_path(
            "my_crate::alloc::slice::{impl#0}::into_vec"
        ));
        assert!(!exact_alloc_slice_into_vec_def_path(
            "my_crate::std::slice::<impl [T]>::into_vec"
        ));
        assert!(exact_alloc_vec_into_boxed_slice_def_path(
            "alloc[d734]::vec::{impl#1}::into_boxed_slice"
        ));
        assert!(exact_alloc_vec_into_boxed_slice_def_path(
            "alloc::vec::{impl#27}::into_boxed_slice"
        ));
        assert!(exact_alloc_vec_into_boxed_slice_def_path(
            "std::vec::Vec::<T, A>::into_boxed_slice"
        ));
        assert!(!exact_alloc_vec_into_boxed_slice_def_path(
            "alloc::vec::{impl#1}::into_boxed_slice_unchecked"
        ));
        assert!(!exact_alloc_vec_into_boxed_slice_def_path(
            "alloc::vec::{impl#x}::into_boxed_slice"
        ));
        assert!(!exact_alloc_vec_into_boxed_slice_def_path(
            "my_crate::alloc::vec::{impl#1}::into_boxed_slice"
        ));
        assert!(exact_alloc_string_into_bytes_def_path(
            "alloc[d734]::string::{impl#0}::into_bytes"
        ));
        assert!(exact_alloc_string_into_bytes_def_path(
            "alloc::string::{impl#17}::into_bytes"
        ));
        assert!(exact_alloc_string_into_bytes_def_path(
            "std::string::String::into_bytes"
        ));
        assert!(!exact_alloc_string_into_bytes_def_path(
            "alloc::string::{impl#0}::into_bytes_unchecked"
        ));
        assert!(!exact_alloc_string_into_bytes_def_path(
            "alloc::string::{impl#x}::into_bytes"
        ));
        assert!(!exact_alloc_string_into_bytes_def_path(
            "my_crate::alloc::string::{impl#0}::into_bytes"
        ));
        assert!(exact_core_from_from_def_path(
            "core[2f33]::convert::From::from"
        ));
        assert!(exact_core_from_from_def_path("std::convert::From::from"));
        assert!(!exact_core_from_from_def_path(
            "my_crate::core::convert::From::from"
        ));
        assert!(!exact_core_from_from_def_path(
            "core::convert::From::from_ref"
        ));
        assert!(exact_alloc_cstring_into_bytes_with_nul_def_path(
            "alloc[d734]::ffi::c_str::{impl#1}::into_bytes_with_nul"
        ));
        assert!(exact_alloc_cstring_into_bytes_with_nul_def_path(
            "alloc::ffi::c_str::{impl#17}::into_bytes_with_nul"
        ));
        assert!(exact_alloc_cstring_into_bytes_with_nul_def_path(
            "std::ffi::CString::into_bytes_with_nul"
        ));
        assert!(!exact_alloc_cstring_into_bytes_with_nul_def_path(
            "alloc::ffi::c_str::{impl#1}::into_bytes"
        ));
        assert!(!exact_alloc_cstring_into_bytes_with_nul_def_path(
            "alloc::ffi::c_str::{impl#x}::into_bytes_with_nul"
        ));
        assert!(!exact_alloc_cstring_into_bytes_with_nul_def_path(
            "my_crate::alloc::ffi::c_str::{impl#1}::into_bytes_with_nul"
        ));
        assert!(exact_alloc_string_into_boxed_str_def_path(
            "alloc[d734]::string::{impl#0}::into_boxed_str"
        ));
        assert!(exact_alloc_string_into_boxed_str_def_path(
            "alloc::string::{impl#17}::into_boxed_str"
        ));
        assert!(exact_alloc_string_into_boxed_str_def_path(
            "std::string::String::into_boxed_str"
        ));
        assert!(!exact_alloc_string_into_boxed_str_def_path(
            "alloc::string::{impl#0}::into_boxed_str_unchecked"
        ));
        assert!(!exact_alloc_string_into_boxed_str_def_path(
            "alloc::string::{impl#x}::into_boxed_str"
        ));
        assert!(!exact_alloc_string_into_boxed_str_def_path(
            "my_crate::alloc::string::{impl#0}::into_boxed_str"
        ));
        assert!(exact_alloc_str_into_string_def_path(
            "alloc[d734]::str::{impl#5}::into_string"
        ));
        assert!(exact_alloc_str_into_string_def_path(
            "alloc::str::<impl str>::into_string"
        ));
        assert!(exact_alloc_str_into_string_def_path(
            "std::str::<impl str>::into_string"
        ));
        assert!(exact_alloc_str_into_string_def_path(
            "std::primitive::str::into_string"
        ));
        assert!(!exact_alloc_str_into_string_def_path(
            "alloc::str::{impl#5}::into_string_unchecked"
        ));
        assert!(!exact_alloc_str_into_string_def_path(
            "alloc::str::{impl#x}::into_string"
        ));
        assert!(!exact_alloc_str_into_string_def_path(
            "my_crate::alloc::str::{impl#5}::into_string"
        ));
        assert!(exact_core_into_iterator_into_iter_def_path(
            "core[2f33]::iter::traits::collect::IntoIterator::into_iter"
        ));
        assert!(exact_core_into_iterator_into_iter_def_path(
            "std::iter::IntoIterator::into_iter"
        ));
        assert!(!exact_core_into_iterator_into_iter_def_path(
            "my_crate::core::iter::traits::collect::IntoIterator::into_iter"
        ));
        assert!(!exact_core_into_iterator_into_iter_def_path(
            "core::iter::traits::collect::IntoIterator::into_iter_extra"
        ));
        assert!(exact_core_iterator_collect_def_path(
            "core[2f33]::iter::traits::iterator::Iterator::collect"
        ));
        assert!(exact_core_iterator_collect_def_path(
            "std::iter::Iterator::collect"
        ));
        assert!(!exact_core_iterator_collect_def_path(
            "my_crate::core::iter::traits::iterator::Iterator::collect"
        ));
        assert!(!exact_core_iterator_collect_def_path(
            "core::iter::traits::iterator::Iterator::collect_extra"
        ));
        assert!(exact_core_borrowing_slice_iterator_def_path(
            "core[2f33]::slice::iter::Iter"
        ));
        assert!(exact_core_borrowing_slice_iterator_def_path(
            "core::slice::iter::IterMut"
        ));
        assert!(exact_core_borrowing_slice_iterator_def_path(
            "std::slice::Iter"
        ));
        assert!(!exact_core_borrowing_slice_iterator_def_path(
            "my_crate::core::slice::iter::Iter"
        ));
        assert!(!exact_core_borrowing_slice_iterator_def_path(
            "core::slice::iter::IterMutExtra"
        ));
        assert!(exact_core_copied_iterator_adapter_def_path(
            "core[2f33]::iter::adapters::copied::Copied"
        ));
        assert!(!exact_core_copied_iterator_adapter_def_path(
            "my_crate::core::iter::adapters::copied::Copied"
        ));
        assert!(!exact_core_copied_iterator_adapter_def_path(
            "core::iter::adapters::copied::CopiedExtra"
        ));
        assert!(exact_core_borrowing_str_iterator_def_path(
            "core[2f33]::str::iter::Split"
        ));
        assert!(exact_core_borrowing_str_iterator_def_path(
            "core::str::iter::SplitInclusive"
        ));
        assert!(exact_core_borrowing_str_iterator_def_path(
            "std::str::Split"
        ));
        assert!(!exact_core_borrowing_str_iterator_def_path(
            "my_crate::core::str::iter::Split"
        ));
        assert!(!exact_core_borrowing_str_iterator_def_path(
            "core::str::iter::SplitInclusiveExtra"
        ));
        assert!(exact_alloc_box_def_path("alloc[d734]::boxed::Box"));
        assert!(exact_alloc_box_def_path("std::boxed::Box"));
        assert!(!exact_alloc_box_def_path("my_crate::std::boxed::Box"));
        assert!(exact_alloc_box_new_def_path(
            "alloc[d734]::boxed::{impl#0}::new"
        ));
        assert!(exact_alloc_box_new_def_path("std::boxed::Box::<T>::new"));
        assert!(!exact_alloc_box_new_def_path(
            "alloc::boxed::{impl#0}::new_in"
        ));
        assert!(!exact_alloc_box_new_def_path(
            "my_crate::alloc::boxed::Box::<T>::new"
        ));
        assert!(exact_alloc_vec_def_path("alloc[d734]::vec::Vec"));
        assert!(exact_alloc_vec_def_path("std::vec::Vec"));
        assert!(!exact_alloc_vec_def_path("my_crate::std::vec::Vec"));
        assert!(exact_alloc_vec_from_elem_def_path(
            "alloc[d734]::vec::from_elem"
        ));
        assert!(exact_alloc_vec_from_elem_def_path("std::vec::from_elem"));
        assert!(!exact_alloc_vec_from_elem_def_path(
            "alloc::vec::from_elem_in"
        ));
        assert!(!exact_alloc_vec_from_elem_def_path(
            "alloc::vec::from_elem_extra"
        ));
        assert!(!exact_alloc_vec_from_elem_def_path(
            "my_crate::alloc::vec::from_elem"
        ));
        assert!(exact_alloc_vecdeque_def_path(
            "alloc[d734]::collections::vec_deque::VecDeque"
        ));
        assert!(exact_alloc_vecdeque_def_path("std::collections::VecDeque"));
        assert!(!exact_alloc_vecdeque_def_path(
            "my_crate::std::collections::VecDeque"
        ));
        assert!(exact_alloc_vecdeque_method_def_path(
            "alloc[d734]::collections::vec_deque::{impl#4}::with_capacity",
            "with_capacity"
        ));
        assert!(exact_alloc_vecdeque_method_def_path(
            "alloc::collections::vec_deque::{impl#5}::reserve_exact",
            "reserve_exact"
        ));
        assert!(exact_alloc_vecdeque_method_def_path(
            "std::collections::VecDeque::<T, A>::reserve_exact",
            "reserve_exact"
        ));
        assert!(!exact_alloc_vecdeque_method_def_path(
            "alloc::collections::vec_deque::{impl#4}::with_capacity_in",
            "with_capacity"
        ));
        assert!(!exact_alloc_vecdeque_method_def_path(
            "alloc::collections::vec_deque::{impl#5}::push_back",
            "reserve_exact"
        ));
        assert!(!exact_alloc_vecdeque_method_def_path(
            "my_crate::alloc::collections::vec_deque::{impl#5}::reserve_exact",
            "reserve_exact"
        ));
        assert!(exact_alloc_string_with_capacity_def_path(
            "alloc[d734]::string::{impl#0}::with_capacity"
        ));
        assert!(exact_alloc_string_with_capacity_def_path(
            "std::string::String::with_capacity"
        ));
        assert!(!exact_alloc_string_with_capacity_def_path(
            "alloc::string::{impl#0}::with_capacity_in"
        ));
        assert!(!exact_alloc_string_with_capacity_def_path(
            "my_crate::alloc::string::String::with_capacity"
        ));
        assert!(exact_alloc_arc_def_path("alloc[d734]::sync::Arc"));
        assert!(exact_alloc_arc_def_path("std::sync::Arc"));
        assert!(!exact_alloc_arc_def_path("my_crate::std::sync::Arc"));
        assert!(exact_alloc_arc_new_def_path(
            "alloc[d734]::sync::{impl#16}::new"
        ));
        assert!(exact_alloc_arc_new_def_path("std::sync::Arc::<T>::new"));
        assert!(!exact_alloc_arc_new_def_path(
            "alloc::sync::{impl#16}::new_in"
        ));
        assert!(!exact_alloc_arc_new_def_path(
            "my_crate::alloc::sync::Arc::<T>::new"
        ));
        assert!(exact_alloc_rc_def_path("alloc[d734]::rc::Rc"));
        assert!(exact_alloc_rc_def_path("std::rc::Rc"));
        assert!(!exact_alloc_rc_def_path("my_crate::std::rc::Rc"));
        assert!(exact_alloc_rc_new_def_path(
            "alloc[d734]::rc::{impl#9}::new"
        ));
        assert!(exact_alloc_rc_new_def_path("std::rc::Rc::<T>::new"));
        assert!(!exact_alloc_rc_new_def_path("alloc::rc::{impl#9}::new_in"));
        assert!(!exact_alloc_rc_new_def_path(
            "alloc::rc::{impl#9}::new_cyclic"
        ));
        assert!(!exact_alloc_rc_new_def_path("alloc::rc::Weak::<T>::new"));
        assert!(!exact_alloc_rc_new_def_path(
            "my_crate::alloc::rc::Rc::<T>::new"
        ));
        assert!(exact_std_hash_map_def_path(
            "std[efc3]::collections::HashMap"
        ));
        assert!(!exact_std_hash_map_def_path("hashbrown::map::HashMap"));
        assert!(exact_std_hash_map_with_capacity_def_path(
            "std[efc3]::collections::HashMap::<K, V>::with_capacity"
        ));
        assert!(!exact_std_hash_map_with_capacity_def_path(
            "std::collections::HashMap::<K, V>::with_capacity_and_hasher"
        ));
        assert!(!exact_std_hash_map_with_capacity_def_path(
            "HashMapLike::with_capacity"
        ));
        assert!(exact_std_hash_map_method_def_path(
            "std[efc3]::collections::hash::map::{impl#4}::reserve",
            "reserve"
        ));
        assert!(exact_std_hash_map_method_def_path(
            "std::collections::HashMap::<K, V, S, A>::try_reserve",
            "try_reserve"
        ));
        assert!(!exact_std_hash_map_method_def_path(
            "hashmap_reserve_callback_audit::reserve_with_panicking_hash",
            "reserve"
        ));
        assert!(!exact_std_hash_map_method_def_path(
            "std::collections::hash::map::{impl#4}::reserve_with_callback",
            "reserve"
        ));
        assert!(callee_contains_method_name(
            "std[efc3]::collections::hash::map::{impl#4}::reserve",
            "reserve"
        ));
        assert!(!callee_contains_method_name(
            "hashmap_reserve_callback_audit::reserve_with_panicking_hash",
            "reserve"
        ));
        assert!(exact_std_hash_set_def_path(
            "std[efc3]::collections::HashSet"
        ));
        assert!(!exact_std_hash_set_def_path("hashbrown::set::HashSet"));
        assert!(!exact_std_hash_set_def_path("indexmap::set::IndexSet"));
        assert!(exact_std_hash_set_with_capacity_def_path(
            "std[efc3]::collections::HashSet::<T>::with_capacity"
        ));
        assert!(!exact_std_hash_set_with_capacity_def_path(
            "std::collections::HashSet::<T>::with_capacity_and_hasher"
        ));
        assert!(!exact_std_hash_set_with_capacity_def_path(
            "std::collections::HashSet::<T>::with_hasher"
        ));
        assert!(!exact_std_hash_set_with_capacity_def_path(
            "HashSetLike::with_capacity"
        ));
        assert!(exact_std_hash_map_random_state_def_path(
            "std[efc3]::hash::RandomState"
        ));
        assert!(exact_std_hash_map_random_state_def_path(
            "std::collections::hash_map::RandomState"
        ));
        assert!(!exact_std_hash_map_random_state_def_path(
            "hashbrown::hash_map::DefaultHashBuilder"
        ));
        assert!(exact_alloc_vec_into_iter_def_path(
            "alloc[d734]::vec::into_iter::IntoIter"
        ));
        assert!(exact_alloc_vec_into_iter_def_path("std::vec::IntoIter"));
        assert!(!exact_alloc_vec_into_iter_def_path(
            "my_crate::std::vec::IntoIter"
        ));
        assert!(exact_alloc_string_def_path("alloc[d734]::string::String"));
        assert!(exact_alloc_string_def_path("std::string::String"));
        assert!(!exact_alloc_string_def_path(
            "my_crate::std::string::String"
        ));
        assert!(exact_alloc_to_owned_to_owned_def_path(
            "alloc[d734]::borrow::ToOwned::to_owned"
        ));
        assert!(exact_alloc_to_owned_to_owned_def_path(
            "std::borrow::ToOwned::to_owned"
        ));
        assert!(!exact_alloc_to_owned_to_owned_def_path(
            "my_crate::std::borrow::ToOwned::to_owned"
        ));
        assert!(!exact_alloc_to_owned_to_owned_def_path(
            "alloc::borrow::ToOwned::clone_into"
        ));
        assert!(exact_alloc_cstring_def_path(
            "alloc[d734]::ffi::c_str::CString"
        ));
        assert!(exact_alloc_cstring_def_path("std::ffi::CString"));
        assert!(!exact_alloc_cstring_def_path("my_crate::std::ffi::CString"));
        assert!(exact_alloc_global_def_path("alloc[d734]::alloc::Global"));
        assert!(exact_alloc_global_def_path("std::alloc::Global"));
        assert!(!exact_alloc_global_def_path("my_crate::std::alloc::Global"));
    }

    #[test]
    fn path_factory_matcher_is_std_method_exact() {
        assert!(exact_std_path_def_path("std[3e07]::path::Path"));
        assert!(exact_std_pathbuf_def_path("std[3e07]::path::PathBuf"));
        assert!(exact_std_os_str_def_path("std[3e07]::ffi::os_str::OsStr"));
        assert!(exact_std_path_method_def_path(
            "std[3e07]::path::{impl#70}::join",
            "join"
        ));
        assert!(exact_std_path_method_def_path(
            "std[3e07]::path::{impl#70}::to_path_buf",
            "to_path_buf"
        ));
        assert!(exact_std_path_method_def_path(
            "std::path::Path::join",
            "join"
        ));
        assert!(!exact_std_path_method_def_path(
            "my_crate::std::path::{impl#70}::join",
            "join"
        ));
        assert!(!exact_std_path_method_def_path(
            "std::thread::{impl#70}::join",
            "join"
        ));
        assert!(!exact_std_path_method_def_path(
            "std::path::{impl#70}::join_extra",
            "join"
        ));
        assert!(!exact_std_path_method_def_path(
            "std::path::{impl#x}::join",
            "join"
        ));
        assert!(!exact_std_path_def_path("my_crate::std::path::Path"));
        assert!(!exact_std_pathbuf_def_path("my_crate::std::path::PathBuf"));
        assert!(!exact_std_os_str_def_path(
            "my_crate::std::ffi::os_str::OsStr"
        ));
    }

    #[test]
    fn dir_entry_path_factory_matcher_is_std_method_exact() {
        assert!(exact_std_dir_entry_def_path("std[3e07]::fs::DirEntry"));
        assert!(exact_std_dir_entry_method_def_path(
            "std[3e07]::fs::{impl#38}::path",
            "path"
        ));
        assert!(exact_std_dir_entry_method_def_path(
            "std::fs::DirEntry::path",
            "path"
        ));
        assert!(!exact_std_dir_entry_method_def_path(
            "my_crate::std::fs::{impl#38}::path",
            "path"
        ));
        assert!(!exact_std_dir_entry_method_def_path(
            "std::fs::{impl#38}::path_extra",
            "path"
        ));
        assert!(!exact_std_dir_entry_method_def_path(
            "std::fs::{impl#x}::path",
            "path"
        ));
        assert!(!exact_std_dir_entry_def_path("my_crate::std::fs::DirEntry"));
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
    fn unresolved_direct_allocator_calls_delegate_recovery() {
        let (type_id, basis) = direct_allocator_type_id();
        assert_eq!(type_id, 0, "direct calls stay neutral without a CFG proof");
        assert_eq!(basis, "direct_allocator_recovery_delegated");

        let previous_direct_local = unsafe { DIRECT_LOCAL_METADATA_ABI };
        let previous_size_align_pairing = unsafe { DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP };
        unsafe {
            DIRECT_LOCAL_METADATA_ABI = true;
            DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP = false;
        }
        for kind in [
            DirectAllocatorCallKind::SizeAlignAlloc,
            DirectAllocatorCallKind::LayoutAlloc,
            DirectAllocatorCallKind::LayoutAllocZeroed,
            DirectAllocatorCallKind::LayoutRealloc,
            DirectAllocatorCallKind::LayoutDealloc,
            DirectAllocatorCallKind::GlobalAllocLayoutAlloc,
            DirectAllocatorCallKind::GlobalAllocLayoutAllocZeroed,
            DirectAllocatorCallKind::GlobalAllocLayoutRealloc,
            DirectAllocatorCallKind::GlobalAllocLayoutDealloc,
        ] {
            let symbol = direct_allocator_recovery_backed_replacement_symbol(kind);
            assert!(symbol.contains("_with_metadata"));
            assert!(
                !symbol.ends_with("_local"),
                "delegated recovery must never select the no-recovery local ABI: {}",
                symbol
            );
        }
        unsafe {
            DIRECT_LOCAL_METADATA_ABI = previous_direct_local;
            DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP = previous_size_align_pairing;
        }
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
        for method in VEC_CAPACITY_ONLY_METHODS {
            let current = format!(
                "Val(ZeroSized, FnDef(DefId(3:8610 ~ alloc[d734]::vec::{{impl#2}}::{}), [std::string::String, std::alloc::Global]))",
                method
            );
            let named = format!(
                "Vec::<std::string::String>::{}(move _14, const 4_usize)",
                method
            );
            assert!(semantic_scope_capacity_only_vec_receiver_call(&current));
            assert!(semantic_scope_capacity_only_vec_receiver_call(&named));
        }
        assert!(!semantic_scope_capacity_only_vec_receiver_call(
            vec_bare_push
        ));
        assert!(!semantic_scope_capacity_only_vec_receiver_call(
            "Vec::<std::string::String>::resize(move _14, const 4_usize, move _15)"
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

fn exact_core_layout_method_def_id(tcx: TyCtxt<'_>, def_id: Option<DefId>, method: &str) -> bool {
    def_id.map_or(false, |def_id| {
        exact_core_crate_def_id(tcx, def_id)
            && callee_mentions_rust_layout_impl_method(&tcx.def_path_str(def_id), method)
    })
}

fn exact_core_result_or_option_method_def_id(
    tcx: TyCtxt<'_>,
    def_id: Option<DefId>,
    method: &str,
) -> bool {
    def_id.map_or(false, |def_id| {
        if !exact_core_crate_def_id(tcx, def_id) {
            return false;
        }
        let path = strip_rustc_crate_disambiguators(&tcx.def_path_str(def_id));
        (path.starts_with("core::result::") || path.starts_with("core::option::"))
            && callee_mentions_exact_method(&path, method)
    })
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

fn layout_constructor_heap_object_solution<'tcx>(
    _tcx: TyCtxt<'tcx>,
    _callee_def_id: Option<DefId>,
    callee: &str,
    args: &[Operand<'tcx>],
    _callee_generic_types: &[Ty<'tcx>],
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
            type_id_basis: "rustc_middle_mir_layout_constructor_audit_only",
        });
    }

    // `for_value_raw` contains `for_value`; match the raw-pointer constructor
    // first so pointer/slice-derived Layout objects are not mislabeled as
    // reference-derived `for_value` objects.
    if let Some(value_ty) = extract_layout_angle_argument(callee, "for_value_raw") {
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::for_value_raw::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_constructor_audit_only",
        });
    }

    if let Some(value_ty) = extract_layout_angle_argument(callee, "for_value") {
        return Some(HeapObjectSolution {
            object_type: format!("std::alloc::Layout::for_value::<{}>", value_ty),
            type_id_basis: "rustc_middle_mir_layout_constructor_audit_only",
        });
    }

    None
}

fn layout_size_align_operand_heap_object_solution<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    callee_generic_types: &[Ty<'tcx>],
) -> Option<HeapObjectSolution> {
    // Many allocators manually rebuild a `Layout` from
    // `size_of::<T>()`/`align_of::<T>()` instead of starting from
    // `Layout::new::<T>()`.  MIR preserves these calls, so keep a typed
    // provenance token on the integer locals and let `Layout::from_size_align`
    // accept it only when both operands refer to the same `T`.
    extract_mem_angle_argument(callee, "size_of")
        .or_else(|| extract_mem_angle_argument(callee, "align_of"))
        .map(|value_ty| {
            let exact_method = if extract_mem_angle_argument(callee, "size_of").is_some() {
                "size_of"
            } else {
                "align_of"
            };
            let _ = (tcx, callee_def_id, exact_method, callee_generic_types);
            HeapObjectSolution {
                object_type: format!("std::mem::layout_of::<{}>", value_ty),
                type_id_basis: "rustc_middle_mir_layout_size_align_operand_audit_only",
            }
        })
}

fn layout_transformer_heap_object_solution(
    tcx: TyCtxt<'_>,
    callee_def_id: Option<DefId>,
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

    if LAYOUT_PRESERVING_TRANSFORMERS.iter().any(|method| {
        callee_mentions_rust_layout_method(callee, method)
            && exact_core_layout_method_def_id(tcx, callee_def_id, method)
    }) {
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
    tcx: TyCtxt<'_>,
    callee_def_id: Option<DefId>,
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
    let exact_constructor = ["from_size_align", "from_size_align_unchecked"]
        .iter()
        .any(|method| exact_core_layout_method_def_id(tcx, callee_def_id, method));
    if !reconstructs_layout || !exact_constructor {
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

    if size_solution.type_id_basis == "rustc_middle_mir_layout_size_align_operand_audit_only"
        && align_solution.type_id_basis == "rustc_middle_mir_layout_size_align_operand_audit_only"
    {
        return Some(HeapObjectSolution {
            object_type: format!(
                "std::alloc::Layout::from_size_align({})",
                size_solution.object_type
            ),
            type_id_basis: "rustc_middle_mir_layout_size_align_constructor_audit_only",
        });
    }

    Some(HeapObjectSolution {
        object_type: format!(
            "std::alloc::Layout::from_size_align({})",
            size_solution.object_type
        ),
        type_id_basis: "rustc_middle_mir_layout_reconstructed_audit_only",
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
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    args: &[Operand<'tcx>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    if result_layout_unwrap_call(callee)
        && ["expect", "unwrap"]
            .iter()
            .any(|method| exact_core_result_or_option_method_def_id(tcx, callee_def_id, method))
    {
        args.get(0)
            .and_then(|arg| operand_heap_object_solution(arg, provenance))
    } else {
        None
    }
}

fn fallible_layout_passthrough_heap_object_solution<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    args: &[Operand<'tcx>],
    provenance: &BTreeMap<String, HeapObjectSolution>,
) -> Option<HeapObjectSolution> {
    if !fallible_layout_passthrough_call(callee)
        || !["ok", "expect", "unwrap"]
            .iter()
            .any(|method| exact_core_result_or_option_method_def_id(tcx, callee_def_id, method))
    {
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
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    args: &[Operand<'tcx>],
    callee_generic_types: &[Ty<'tcx>],
    destination: &Place<'tcx>,
    provenance: &mut BTreeMap<String, HeapObjectSolution>,
) {
    let destination_key = place_text(destination);
    let solved = layout_constructor_heap_object_solution(
        tcx,
        callee_def_id,
        callee,
        args,
        callee_generic_types,
    )
    .or_else(|| {
        layout_size_align_operand_heap_object_solution(
            tcx,
            callee_def_id,
            callee,
            callee_generic_types,
        )
    })
    .or_else(|| {
        layout_transformer_heap_object_solution(tcx, callee_def_id, callee, args, provenance)
    })
    .or_else(|| {
        layout_reconstructed_heap_object_solution(tcx, callee_def_id, callee, args, provenance)
    })
    .or_else(|| layout_composite_heap_object_solution(callee, args, provenance))
    .or_else(|| {
        fallible_layout_passthrough_heap_object_solution(
            tcx,
            callee_def_id,
            callee,
            args,
            provenance,
        )
    })
    .or_else(|| {
        result_layout_unwrap_heap_object_solution(tcx, callee_def_id, callee, args, provenance)
    })
    .or_else(|| {
        let exact_preserving_query = ["size", "align"].iter().any(|method| {
            callee_mentions_rust_layout_method(callee, method)
                && exact_core_layout_method_def_id(tcx, callee_def_id, method)
        });
        if exact_preserving_query {
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
    if let Some(solved) = ty_heap_object_audit_solution(tcx, destination_ty, argument_tys, callee) {
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

fn semantic_scope_type_id(compiler_type_id: Option<u64>) -> (u64, &'static str) {
    compiler_type_id.map_or(
        (0, "rustc_type_identity_unresolved_recovery_only"),
        |type_id| (type_id, "rustc_type_id_hash_runtime_equivalent"),
    )
}

fn direct_allocator_type_id() -> (u64, &'static str) {
    // Direct allocator calls do not yet have a CFG-proven exact owner link.
    // Keep every such call neutral even when its audit-only display happens to
    // expose a plausible owner type.
    (0, "direct_allocator_recovery_delegated")
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

fn unique_unialloc_crate<'tcx>(tcx: TyCtxt<'tcx>) -> Option<DefId> {
    let mut matches = tcx
        .crates(())
        .iter()
        .copied()
        .filter(|krate| tcx.crate_name(*krate).as_str() == "unialloc");
    let unique = matches.next()?;
    if matches.next().is_some() {
        return None;
    }
    Some(unique.as_def_id())
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
    resolve_unialloc_alloc_metadata_abi_in_crate(
        tcx,
        unique_unialloc_crate(tcx)?,
        allow_size_align_local,
    )
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
    resolve_unialloc_layout_metadata_abi_in_crate(
        tcx,
        unique_unialloc_crate(tcx)?,
        kind,
        force_recovery_backed,
    )
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
        recovery_backed_layout_alloc: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutAlloc,
            true,
        ),
        recovery_backed_layout_alloc_zeroed: resolve_unialloc_layout_metadata_abi(
            tcx,
            DirectAllocatorCallKind::LayoutAllocZeroed,
            true,
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
        DirectAllocatorCallKind::SizeAlignAlloc if force_recovery_backed => {
            abis.conservative_size_align_alloc
        }
        DirectAllocatorCallKind::SizeAlignAlloc if allow_size_align_local => abis.size_align_alloc,
        DirectAllocatorCallKind::SizeAlignAlloc => abis.conservative_size_align_alloc,
        DirectAllocatorCallKind::LayoutAlloc if force_recovery_backed => {
            abis.recovery_backed_layout_alloc
        }
        DirectAllocatorCallKind::LayoutAllocZeroed if force_recovery_backed => {
            abis.recovery_backed_layout_alloc_zeroed
        }
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

fn trusted_unialloc_direct_allocator_crate(
    abis: DirectAllocatorReplacementAbis,
    kind: DirectAllocatorCallKind,
) -> Option<DefId> {
    direct_allocator_replacement_abi(abis, kind, false, false)
        .map(|abi| abi.def_id.krate.as_def_id())
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
            generic_push_def_id: child_def_id_in_alloc_api_or_type_isolation(
                tcx,
                crate_root,
                generic_semantic_scope_push_symbol(local_no_recovery),
            ),
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
    resolve_unialloc_semantic_scope_in_crate(tcx, unique_unialloc_crate(tcx)?, local_no_recovery)
}

fn resolve_unialloc_semantic_ownership_transfer_in_crate<'tcx>(
    tcx: TyCtxt<'tcx>,
    crate_root: DefId,
) -> Option<SemanticOwnershipTransferAbi> {
    let box_slice_into_vec_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_BOX_SLICE_INTO_VEC_SYMBOL,
    )?;
    let vec_into_boxed_slice_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_VEC_INTO_BOXED_SLICE_SYMBOL,
    )?;
    let string_into_bytes_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_STRING_INTO_BYTES_SYMBOL,
    )?;
    let string_into_boxed_str_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_STRING_INTO_BOXED_STR_SYMBOL,
    )?;
    let boxed_str_into_string_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_BOXED_STR_INTO_STRING_SYMBOL,
    )?;
    let cstring_into_bytes_with_nul_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_CSTRING_INTO_BYTES_WITH_NUL_SYMBOL,
    )?;
    let vec_into_iter_def_id = child_def_id_in_alloc_api_or_type_isolation(
        tcx,
        crate_root,
        SEMANTIC_VEC_INTO_ITER_SYMBOL,
    )?;
    Some(SemanticOwnershipTransferAbi {
        box_slice_into_vec_def_id,
        vec_into_boxed_slice_def_id,
        string_into_bytes_def_id,
        string_into_boxed_str_def_id,
        boxed_str_into_string_def_id,
        cstring_into_bytes_with_nul_def_id,
        vec_into_iter_def_id,
    })
}

fn resolve_unialloc_semantic_ownership_transfer<'tcx>(
    tcx: TyCtxt<'tcx>,
) -> Option<SemanticOwnershipTransferAbi> {
    resolve_unialloc_semantic_ownership_transfer_in_crate(tcx, unique_unialloc_crate(tcx)?)
}

fn semantic_scope_candidate_for_mir<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: Option<DefId>,
    callee: &str,
    destination_ty: Ty<'tcx>,
) -> bool {
    if direct_allocator_call(callee) {
        return false;
    }
    if semantic_scope_explicit_drop_call(callee) {
        return callee_def_id.map_or(false, |def_id| exact_core_mem_drop_def_id(tcx, def_id));
    }
    if semantic_scope_deallocation_like_call(callee) {
        return true;
    }
    if semantic_scope_known_non_allocating_call(callee)
        || semantic_scope_receiver_consuming_wait_call(callee)
    {
        return false;
    }
    if semantic_scope_receiver_mutating_allocation_like_call(callee) {
        return true;
    }

    // Result<T, E> carries two roles.  Only Ok(T) can be the returned
    // allocation identity; an owner reachable solely through Err(E) must not
    // wrap the whole call.  Unresolved Ok payloads remain explicit audit
    // candidates so later lowering still fails closed.
    if let Some((ok_ty, _)) = exact_result_destination_types(tcx, destination_ty) {
        let ok_scan = heap_object_type_scan_from_ty(tcx, ok_ty);
        return ok_scan.unresolved || !ok_scan.owners.is_empty();
    }

    if semantic_scope_allocation_like_call(callee)
        || semantic_scope_returns_owned_heap_container(callee)
    {
        return true;
    }

    // Do not rely only on rustc debug text for candidate discovery. Indirect
    // calls, generic factories, and wrapper functions may not expose an owned
    // heap return marker in the callee text, while the MIR destination still has
    // the exact rustc_middle type. Preserve the existing first-owner signal for
    // generic-argument hazards, then add the same complete owner-graph scan as
    // the classifier below: the older helper cannot inspect concrete fields on
    // current rustc and silently missed non-generic wrappers such as
    // `struct Buffer { bytes: Vec<u8> }`. Structurally unresolved destinations
    // remain explicit audit candidates; actual lowering still fails closed.
    let destination_scan = heap_object_type_scan_from_ty(tcx, destination_ty);
    heap_object_type_from_ty(tcx, destination_ty).is_some()
        || destination_scan.unresolved
        || !destination_scan.owners.is_empty()
}

fn semantic_scope_explicit_drop_call(callee: &str) -> bool {
    callee.contains("std::mem::drop") || callee.contains("core::mem::drop")
}

fn exact_core_mem_drop_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "core::mem::drop" | "std::mem::drop"
    )
}

fn exact_core_mem_drop_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.get_diagnostic_item(sym::mem_drop) == Some(def_id)
        && exact_core_mem_drop_def_path(&tcx.def_path_str(def_id))
}

fn drop_inert_box_reclaim_call_runtime_identity_owner_ty<'tcx>(
    tcx: TyCtxt<'tcx>,
    callee_def_id: DefId,
    argument_tys: &[Ty<'tcx>],
) -> Option<Ty<'tcx>> {
    if !exact_core_mem_drop_def_id(tcx, callee_def_id) || argument_tys.len() != 1 {
        return None;
    }
    drop_inert_box_runtime_identity_owner_ty(tcx, argument_tys[0])
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
        || [
            "reserve",
            "reserve_exact",
            "try_reserve",
            "try_reserve_exact",
        ]
        .iter()
        .any(|method| callee_contains_method_name(callee, method))
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

const VEC_CAPACITY_ONLY_METHODS: &[&str] = &[
    "reserve",
    "reserve_exact",
    "try_reserve",
    "try_reserve_exact",
    "shrink_to",
    "shrink_to_fit",
];

#[cfg(test)]
fn semantic_scope_capacity_only_vec_receiver_call(callee: &str) -> bool {
    VEC_CAPACITY_ONLY_METHODS.iter().any(|method| {
        callee_contains_current_impl_method(callee, "alloc::vec", method)
            || ["Vec", "std::vec::Vec", "alloc::vec::Vec"]
                .iter()
                .any(|receiver| callee_contains_named_receiver_method(callee, receiver, method))
    })
}

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
        || [
            "reserve",
            "reserve_exact",
            "try_reserve",
            "try_reserve_exact",
        ]
        .iter()
        .any(|method| callee_contains_method_name(callee, method))
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

fn callee_contains_method_name(callee: &str, method: &str) -> bool {
    let callee = strip_rustc_crate_disambiguators(callee);
    let marker = format!("::{method}");
    callee
        .match_indices(&marker)
        .any(|(idx, _)| path_marker_has_boundary(&callee, idx, &marker))
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
    UnwindAction::Terminate(UnwindTerminateReason::InCleanup)
}

#[cfg(not(unialloc_rustc_current))]
fn no_unwind() -> MirUnwind {
    None
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

#[cfg(unialloc_rustc_current)]
fn unwind_continues_outward(unwind: MirUnwind) -> bool {
    matches!(unwind, UnwindAction::Continue)
}

#[cfg(not(unialloc_rustc_current))]
fn unwind_continues_outward(unwind: MirUnwind) -> bool {
    unwind.is_none()
}

#[cfg(unialloc_rustc_current)]
fn unwind_resume_terminator_kind<'tcx>() -> TerminatorKind<'tcx> {
    TerminatorKind::UnwindResume
}

#[cfg(not(unialloc_rustc_current))]
fn unwind_resume_terminator_kind<'tcx>() -> TerminatorKind<'tcx> {
    TerminatorKind::Resume
}

fn push_unwind_resume_block<'tcx>(body: &mut Body<'tcx>, source_info: SourceInfo) -> BasicBlock {
    body.basic_blocks_mut().push(basic_block_data(
        Terminator {
            source_info,
            kind: unwind_resume_terminator_kind(),
        },
        true,
    ))
}

fn semantic_scope_unwind_continuation<'tcx>(
    body: &mut Body<'tcx>,
    source_info: SourceInfo,
    original_unwind: MirUnwind,
    original_is_cleanup: bool,
) -> Option<BasicBlock> {
    // A call or Drop that is already in a cleanup funclet must retain its
    // original double-unwind behavior.  In legacy MIR, `cleanup: None` means
    // outward unwind on a normal block but must not be expanded into a second
    // cleanup funclet here; doing so can give a Resume funclet two parents.
    if original_is_cleanup {
        return None;
    }
    unwind_cleanup_target(original_unwind).or_else(|| {
        unwind_continues_outward(original_unwind)
            .then(|| push_unwind_resume_block(body, source_info))
    })
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

fn unialloc_function_handle_with_two_types<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    first: Ty<'tcx>,
    second: Ty<'tcx>,
    span: Span,
) -> Operand<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        Operand::function_handle(tcx, def_id, [first.into(), second.into()], span)
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        Operand::function_handle(
            tcx,
            def_id,
            tcx.intern_substs(&[first.into(), second.into()]),
            span,
        )
    }
}

fn unialloc_function_handle_with_type<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    owner: Ty<'tcx>,
    span: Span,
) -> Operand<'tcx> {
    #[cfg(unialloc_rustc_current)]
    {
        Operand::function_handle(tcx, def_id, [owner.into()], span)
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        Operand::function_handle(tcx, def_id, tcx.intern_substs(&[owner.into()]), span)
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

fn call_terminator_kind_with_type<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    owner: Ty<'tcx>,
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
            func: unialloc_function_handle_with_type(tcx, def_id, owner, fn_span),
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
            func: unialloc_function_handle_with_type(tcx, def_id, owner, fn_span),
            args: make_call_args(args, fn_span),
            destination,
            target,
            cleanup: unwind,
            from_hir_call,
            fn_span,
        }
    }
}

fn call_terminator_kind_with_two_types<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    first: Ty<'tcx>,
    second: Ty<'tcx>,
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
            func: unialloc_function_handle_with_two_types(tcx, def_id, first, second, fn_span),
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
            func: unialloc_function_handle_with_two_types(tcx, def_id, first, second, fn_span),
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
    automatic_lifetime_decisions: BTreeMap<(String, String), AutomaticLifetimeDecision>,
    automatic_heap_lifetime_decisions: BTreeMap<(String, String), AutomaticHeapLifetimeDecision>,
    exact_zip_drop_pairs: BTreeSet<(String, String)>,
}

struct ExactZipDestinationWriteVisitor<'tcx> {
    destination: Place<'tcx>,
    initializer_block: BasicBlock,
    initializer_statement_index: usize,
    saw_initializer: bool,
    disqualified: bool,
}

fn exact_zip_destination_write_context(context: PlaceContext) -> bool {
    match context {
        PlaceContext::MutatingUse(
            MutatingUseContext::Store
            | MutatingUseContext::SetDiscriminant
            | MutatingUseContext::AsmOutput
            | MutatingUseContext::Call
            | MutatingUseContext::Yield,
        ) => true,
        #[cfg(not(unialloc_rustc_current))]
        PlaceContext::MutatingUse(MutatingUseContext::Deinit) => true,
        _ => false,
    }
}

impl<'tcx> Visitor<'tcx> for ExactZipDestinationWriteVisitor<'tcx> {
    fn visit_place(&mut self, place: &Place<'tcx>, context: PlaceContext, location: Location) {
        if self.disqualified
            || place.local != self.destination.local
            || !exact_zip_destination_write_context(context)
        {
            return;
        }

        let exact_initializer = *place == self.destination
            && location.block == self.initializer_block
            && location.statement_index == self.initializer_statement_index
            && matches!(context, PlaceContext::MutatingUse(MutatingUseContext::Call));
        if exact_initializer && !self.saw_initializer {
            self.saw_initializer = true;
        } else {
            // A second assignment/call destination/reinitialization anywhere in
            // the body means the rewritten Zip call is not the sole static
            // initializer for this unprojected local. Drop flags may select
            // initialized paths, but they do not make mixed initializers share
            // ownership provenance.
            self.disqualified = true;
        }
    }
}

/// Bind hidden Vec -> IntoIter Drop attribution to the exact MIR rewrite that
/// created the Zip value. The destination must be an unprojected local and the
/// newly inserted Zip call must be its only static initializer. This
/// deliberately rejects branch joins and later reassignments that reuse the
/// same local for an already-bound IntoIter, even when one sibling branch was
/// rewritten successfully. Only exact `(Drop block, place)` pairs are returned;
/// dry-run/planned candidates never call this helper.
fn exact_zip_drop_pairs_after_applied_rewrite<'tcx>(
    body: &Body<'tcx>,
    zip_block: BasicBlock,
    destination: Place<'tcx>,
) -> BTreeSet<(String, String)> {
    if !destination.projection.is_empty() {
        return BTreeSet::new();
    }

    let initializer_statement_index = body[zip_block].statements.len();
    let exact_zip_destination = match &body[zip_block].terminator().kind {
        TerminatorKind::Call {
            destination: rewritten_destination,
            ..
        } => *rewritten_destination == destination,
        _ => false,
    };
    if !exact_zip_destination {
        return BTreeSet::new();
    }

    let mut visitor = ExactZipDestinationWriteVisitor {
        destination,
        initializer_block: zip_block,
        initializer_statement_index,
        saw_initializer: false,
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
                return BTreeSet::new();
            }
        }
        if let Some(terminator) = &data.terminator {
            #[cfg(not(unialloc_rustc_current))]
            if matches!(
                &terminator.kind,
                TerminatorKind::DropAndReplace { place, .. }
                    if place.local == destination.local
            ) {
                return BTreeSet::new();
            }
            visitor.visit_terminator(
                terminator,
                Location {
                    block: bb,
                    statement_index: data.statements.len(),
                },
            );
            if visitor.disqualified {
                return BTreeSet::new();
            }
        }
    }
    if !visitor.saw_initializer {
        return BTreeSet::new();
    }

    body_basic_blocks!(body)
        .iter_enumerated()
        .filter_map(|(bb, data)| match &data.terminator {
            Some(Terminator {
                kind: TerminatorKind::Drop { place, .. },
                ..
            }) if *place == destination => {
                Some((format!("{:?}", bb), format!("{:?}", destination)))
            }
            _ => None,
        })
        .collect()
}

struct SemanticOwnerUseVisitor<'tcx> {
    owner: Place<'tcx>,
    allocation_block: BasicBlock,
    allocation_statement_index: usize,
    allow_read_only: bool,
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
        let read_only_owner_use = self.allow_read_only
            && exact_owner
            && matches!(
                context,
                PlaceContext::NonMutatingUse(
                    NonMutatingUseContext::Inspect
                        | NonMutatingUseContext::SharedBorrow
                        | NonMutatingUseContext::FakeBorrow
                        | NonMutatingUseContext::PlaceMention
                        | NonMutatingUseContext::Projection
                ) | PlaceContext::MutatingUse(
                    MutatingUseContext::Retag | MutatingUseContext::Projection
                )
            );

        // Shared borrows and read-only inspection preserve the allocation
        // owner under valid Rust. Moves, copies, mutable/raw borrows, stores,
        // and projected values can transfer or replace backing storage and
        // remain disqualifying for exact lifetime attribution.
        if !allocation_destination && !exact_drop && !bookkeeping_only && !read_only_owner_use {
            self.disqualified = true;
        }
    }
}

fn candidate_has_owner_uses<'tcx>(
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
    allow_read_only: bool,
) -> bool {
    if !candidate.destination.projection.is_empty() {
        return false;
    }

    let mut visitor = SemanticOwnerUseVisitor {
        owner: candidate.destination,
        allocation_block: candidate.bb,
        allocation_statement_index: body[candidate.bb].statements.len(),
        allow_read_only,
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

fn candidate_has_zero_alias_owner_uses<'tcx>(
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> bool {
    candidate_has_owner_uses(body, candidate, false)
}

fn candidate_has_stable_nonescaping_owner_uses<'tcx>(
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> bool {
    candidate_has_owner_uses(body, candidate, true)
}

struct ExactHeapOwnerUseVisitor<'tcx> {
    owner: Place<'tcx>,
    saw_use: bool,
}

impl<'tcx> Visitor<'tcx> for ExactHeapOwnerUseVisitor<'tcx> {
    fn visit_place(&mut self, place: &Place<'tcx>, context: PlaceContext, _location: Location) {
        if self.saw_use
            || place.local != self.owner.local
            || matches!(context, PlaceContext::NonUse(_))
        {
            return;
        }
        self.saw_use = true;
    }
}

fn statement_uses_exact_heap_owner<'tcx>(
    statement: &rustc_middle::mir::Statement<'tcx>,
    owner: Place<'tcx>,
    location: Location,
) -> bool {
    let mut visitor = ExactHeapOwnerUseVisitor {
        owner,
        saw_use: false,
    };
    visitor.visit_statement(statement, location);
    visitor.saw_use
}

fn terminator_uses_exact_heap_owner<'tcx>(
    terminator: &Terminator<'tcx>,
    owner: Place<'tcx>,
    location: Location,
) -> bool {
    let mut visitor = ExactHeapOwnerUseVisitor {
        owner,
        saw_use: false,
    };
    visitor.visit_terminator(terminator, location);
    visitor.saw_use
}

fn exact_unprojected_owner_move<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    statement: &rustc_middle::mir::Statement<'tcx>,
    owner: Place<'tcx>,
) -> Option<Place<'tcx>> {
    let assigned = match &statement.kind {
        StatementKind::Assign(assigned) => assigned,
        _ => return None,
    };
    let (destination, rvalue) = &**assigned;
    let source = rvalue_move_source(rvalue)?;
    if *source != owner
        || !source.projection.is_empty()
        || !destination.projection.is_empty()
        || destination.local == owner.local
        || destination.ty(&body.local_decls, tcx).ty != owner.ty(&body.local_decls, tcx).ty
    {
        return None;
    }
    Some(*destination)
}

#[cfg(unialloc_rustc_current)]
fn rvalue_move_source<'a, 'tcx>(rvalue: &'a Rvalue<'tcx>) -> Option<&'a Place<'tcx>> {
    match rvalue {
        Rvalue::Use(Operand::Move(source), _) => Some(source),
        _ => None,
    }
}

#[cfg(not(unialloc_rustc_current))]
fn rvalue_move_source<'a, 'tcx>(rvalue: &'a Rvalue<'tcx>) -> Option<&'a Place<'tcx>> {
    match rvalue {
        Rvalue::Use(Operand::Move(source)) => Some(source),
        _ => None,
    }
}

fn operand_is_exact_owner_consume<'tcx>(operand: &Operand<'tcx>, owner: Place<'tcx>) -> bool {
    matches!(operand, Operand::Move(place) | Operand::Copy(place) if *place == owner && place.projection.is_empty())
}

fn exact_core_mem_forget_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.crate_name(def_id.krate).as_str() == "core"
        && matches!(
            strip_rustc_crate_disambiguators(&tcx.def_path_str(def_id)).as_str(),
            "core::mem::forget" | "std::mem::forget"
        )
}

fn exact_alloc_box_leak_def_path(path: &str) -> bool {
    let normalized = strip_rustc_crate_disambiguators(path);
    if matches!(
        normalized.as_str(),
        "alloc::boxed::Box::<T>::leak"
            | "std::boxed::Box::<T>::leak"
            | "alloc::boxed::Box::<T, A>::leak"
            | "std::boxed::Box::<T, A>::leak"
            | "alloc::boxed::Box::leak"
            | "std::boxed::Box::leak"
    ) {
        return true;
    }
    let impl_index = normalized
        .strip_prefix("alloc::boxed::{impl#")
        .and_then(|rest| rest.strip_suffix("}::leak"));
    matches!(impl_index, Some(index) if !index.is_empty() && index.bytes().all(|byte| byte.is_ascii_digit()))
}

fn exact_alloc_box_leak_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.crate_name(def_id.krate).as_str() == "alloc"
        && exact_alloc_box_leak_def_path(&tcx.def_path_str(def_id))
}

fn exact_box_owner_ty(tcx: TyCtxt<'_>, owner_ty: Ty<'_>) -> bool {
    matches!(
        owner_ty.kind(),
        ty::Adt(def, _) if exact_alloc_adt_def_id(tcx, def.did(), exact_alloc_box_def_path)
            && tcx.lang_items().owned_box() == Some(def.did())
    )
}

#[cfg(unialloc_rustc_current)]
fn terminator_has_cleanup_or_unwind_edge(terminator: &Terminator<'_>) -> bool {
    let unwind = match &terminator.kind {
        TerminatorKind::Call { unwind, .. } | TerminatorKind::Drop { unwind, .. } => unwind,
        _ => return false,
    };
    !matches!(unwind, UnwindAction::Unreachable)
}

#[cfg(not(unialloc_rustc_current))]
fn terminator_has_cleanup_or_unwind_edge(terminator: &Terminator<'_>) -> bool {
    // Legacy MIR used `None` both for outward propagation and sites without a
    // materialized cleanup block. Conservatively retain Unknown for every
    // owner-live Call/Drop on that representation.
    matches!(
        &terminator.kind,
        TerminatorKind::Call { .. } | TerminatorKind::Drop { .. }
    )
}

fn refcounted_heap_owner_ty(tcx: TyCtxt<'_>, owner_ty: Ty<'_>) -> bool {
    matches!(
        owner_ty.kind(),
        ty::Adt(def, _)
            if exact_alloc_adt_def_id(tcx, def.did(), exact_alloc_arc_def_path)
                || exact_alloc_adt_def_id(tcx, def.did(), exact_alloc_rc_def_path)
    )
}

fn terminator_exact_bounded_process_long_decision<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    terminator: &Terminator<'tcx>,
    owner: Place<'tcx>,
) -> Option<AutomaticHeapLifetimeDecision> {
    let (func, args) = match &terminator.kind {
        TerminatorKind::Call { func, args, .. } => (func, call_arg_operands(args)),
        _ => return None,
    };
    if args.len() != 1 || !operand_is_exact_owner_consume(&args[0], owner) {
        return None;
    }
    let def_id = match func.ty(&body.local_decls, tcx).kind() {
        ty::FnDef(def_id, _) => *def_id,
        _ => return None,
    };
    if exact_core_mem_forget_def_id(tcx, def_id) {
        Some(AutomaticHeapLifetimeDecision::ExactMemForget)
    } else if exact_box_owner_ty(tcx, owner.ty(&body.local_decls, tcx).ty)
        && exact_alloc_box_leak_def_id(tcx, def_id)
    {
        Some(AutomaticHeapLifetimeDecision::ExactBoxLeak)
    } else {
        None
    }
}

#[cfg(unialloc_rustc_current)]
fn exact_box_new_requested_layout<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> Option<(u64, u64)> {
    let func = match &candidate.original_terminator.kind {
        TerminatorKind::Call { func, .. } => func,
        _ => return None,
    };
    let def_id = match func.ty(&body.local_decls, tcx).kind() {
        ty::FnDef(def_id, _) => *def_id,
        _ => return None,
    };
    if !exact_alloc_box_new_def_id(tcx, def_id) {
        return None;
    }
    let destination_ty = candidate.destination.ty(&body.local_decls, tcx).ty;
    let payload_ty = match destination_ty.kind() {
        ty::Adt(def, args)
            if exact_alloc_adt_def_id(tcx, def.did(), exact_alloc_box_def_path)
                && tcx.lang_items().owned_box() == Some(def.did()) =>
        {
            generic_arg_type(args.get(0)?)?
        }
        _ => return None,
    };
    if clone_result_has_unresolved_params(payload_ty) {
        return None;
    }
    let layout = tcx
        .layout_of(body.typing_env(tcx).as_query_input(payload_ty))
        .ok()?;
    Some((layout.size.bytes(), layout.align.abi.bytes()))
}

#[cfg(not(unialloc_rustc_current))]
fn exact_box_new_requested_layout<'tcx>(
    _tcx: TyCtxt<'tcx>,
    _body: &Body<'tcx>,
    _candidate: &SemanticScopeCandidate<'tcx>,
) -> Option<(u64, u64)> {
    None
}

fn rvalue_move_or_copy_source<'a, 'tcx>(rvalue: &'a Rvalue<'tcx>) -> Option<&'a Place<'tcx>> {
    #[cfg(unialloc_rustc_current)]
    match rvalue {
        Rvalue::Use(Operand::Move(source) | Operand::Copy(source), _) => Some(source),
        _ => None,
    }
    #[cfg(not(unialloc_rustc_current))]
    match rvalue {
        Rvalue::Use(Operand::Move(source) | Operand::Copy(source)) => Some(source),
        _ => None,
    }
}

fn operand_exact_place<'tcx>(operand: &Operand<'tcx>) -> Option<Place<'tcx>> {
    match operand {
        Operand::Move(place) | Operand::Copy(place) => Some(*place),
        _ => None,
    }
}

fn exact_ref_source_for_local<'tcx>(
    body: &Body<'tcx>,
    reference_local: Local,
) -> Option<Place<'tcx>> {
    let mut source = None;
    for data in body_basic_blocks!(body).iter() {
        for statement in &data.statements {
            let (destination, rvalue) = match &statement.kind {
                StatementKind::Assign(assigned) => &**assigned,
                _ => continue,
            };
            if !destination.projection.is_empty() || destination.local != reference_local {
                continue;
            }
            let candidate_source = match rvalue {
                Rvalue::Ref(_, _, place) if place.projection.is_empty() => Some(*place),
                _ => None,
            };
            match (source, candidate_source) {
                (None, Some(place)) => source = Some(place),
                (Some(existing), Some(place)) if existing == place => {}
                (_, _) => return None,
            }
        }
    }
    source
}

fn semantic_feature_owner_place<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    destination: Place<'tcx>,
    args: &[Operand<'tcx>],
    receiver_owned_allocation: bool,
) -> (Option<Place<'tcx>>, &'static str) {
    if receiver_owned_allocation {
        let receiver_place = args
            .first()
            .and_then(|operand| operand_exact_place(operand));
        if let Some(receiver_place) = receiver_place {
            let receiver_ty = receiver_place.ty(&body.local_decls, tcx).ty;
            if receiver_place.projection.is_empty() {
                if let Some(source) = exact_ref_source_for_local(body, receiver_place.local) {
                    let source_ty = source.ty(&body.local_decls, tcx).ty;
                    if direct_supported_heap_object_destination(tcx, source_ty) {
                        return (Some(source), "exact_receiver_ref_source");
                    }
                }
            }
            if receiver_place.projection.is_empty()
                && direct_supported_heap_object_destination(tcx, receiver_ty)
            {
                return (Some(receiver_place), "exact_receiver_operand");
            }
        }
        return (None, "receiver_owner_place_unresolved");
    }

    let destination_ty = destination.ty(&body.local_decls, tcx).ty;
    if destination.projection.is_empty()
        && direct_supported_heap_object_destination(tcx, destination_ty)
    {
        (Some(destination), "semantic_destination_owner")
    } else {
        (None, "destination_owner_place_unresolved")
    }
}

fn reachable_cfg_blocks<'tcx>(
    body: &Body<'tcx>,
    start: Option<BasicBlock>,
) -> BTreeSet<BasicBlock> {
    let mut reachable = BTreeSet::new();
    let mut pending = match start {
        Some(start) => vec![start],
        None => Vec::new(),
    };
    while let Some(bb) = pending.pop() {
        if !reachable.insert(bb) {
            continue;
        }
        if let Some(terminator) = &body[bb].terminator {
            pending.extend(terminator.successors());
        }
    }
    reachable
}

fn natural_loop_blocks_and_backedges<'tcx>(
    body: &Body<'tcx>,
) -> (BTreeSet<BasicBlock>, BTreeSet<(BasicBlock, BasicBlock)>) {
    let blocks = body_basic_blocks!(body);
    let dominators = blocks.dominators();
    let predecessors = blocks.predecessors();
    let mut loop_blocks = BTreeSet::new();
    let mut backedges = BTreeSet::new();

    for (from, data) in blocks.iter_enumerated() {
        if !dominators.is_reachable(from) {
            continue;
        }
        let Some(terminator) = &data.terminator else {
            continue;
        };
        for to in terminator.successors() {
            if !dominators.is_reachable(to) || !dominators.dominates(to, from) {
                continue;
            }
            backedges.insert((from, to));
            loop_blocks.insert(to);
            let mut pending = vec![from];
            while let Some(node) = pending.pop() {
                if !loop_blocks.insert(node) || node == to {
                    continue;
                }
                pending.extend(predecessors[node].iter().copied());
            }
        }
    }
    (loop_blocks, backedges)
}

struct AliasConsumingUseVisitor<'a> {
    aliases: &'a BTreeSet<Local>,
    saw_consuming_use: bool,
}

struct AliasMovingUseVisitor<'a> {
    aliases: &'a BTreeSet<Local>,
    saw_moving_use: bool,
}

impl<'tcx> Visitor<'tcx> for AliasMovingUseVisitor<'_> {
    fn visit_place(&mut self, place: &Place<'tcx>, context: PlaceContext, _location: Location) {
        if self.aliases.contains(&place.local)
            && place.projection.is_empty()
            && matches!(
                context,
                PlaceContext::NonMutatingUse(NonMutatingUseContext::Move)
            )
        {
            self.saw_moving_use = true;
        }
    }
}

impl<'tcx> Visitor<'tcx> for AliasConsumingUseVisitor<'_> {
    fn visit_place(&mut self, place: &Place<'tcx>, context: PlaceContext, _location: Location) {
        if self.aliases.contains(&place.local)
            && matches!(
                context,
                PlaceContext::NonMutatingUse(
                    NonMutatingUseContext::Move | NonMutatingUseContext::Copy
                )
            )
        {
            self.saw_consuming_use = true;
        }
    }
}

fn rvalue_consumes_owner_alias<'tcx>(
    rvalue: &Rvalue<'tcx>,
    aliases: &BTreeSet<Local>,
    location: Location,
) -> bool {
    let mut visitor = AliasConsumingUseVisitor {
        aliases,
        saw_consuming_use: false,
    };
    visitor.visit_rvalue(rvalue, location);
    visitor.saw_consuming_use
}

fn rvalue_moves_owner_alias<'tcx>(
    rvalue: &Rvalue<'tcx>,
    aliases: &BTreeSet<Local>,
    location: Location,
) -> bool {
    let mut visitor = AliasMovingUseVisitor {
        aliases,
        saw_moving_use: false,
    };
    visitor.visit_rvalue(rvalue, location);
    visitor.saw_moving_use
}

fn terminator_has_yield_or_await(terminator: &Terminator<'_>) -> bool {
    matches!(&terminator.kind, TerminatorKind::Yield { .. })
}

fn sorted_block_labels(blocks: impl IntoIterator<Item = BasicBlock>) -> Vec<String> {
    blocks
        .into_iter()
        .map(|block| format!("{:?}", block))
        .collect()
}

fn owner_live_opaque_call_blocks<'tcx>(
    body: &Body<'tcx>,
    start: Option<BasicBlock>,
    owner_carriers: &BTreeSet<Local>,
) -> BTreeSet<BasicBlock> {
    let mut opaque_calls = BTreeSet::new();
    let mut visited = BTreeSet::new();
    let mut pending = start.into_iter().collect::<Vec<_>>();
    while let Some(bb) = pending.pop() {
        if !visited.insert(bb) || body[bb].is_cleanup {
            continue;
        }
        let Some(terminator) = &body[bb].terminator else {
            continue;
        };
        let owner_reaches_terminal = match &terminator.kind {
            TerminatorKind::Drop { place, .. } => {
                owner_carriers.contains(&place.local) && place.projection.is_empty()
            }
            TerminatorKind::Call { args, .. } => call_arg_operands(args).iter().any(|operand| {
                operand_exact_place(operand)
                    .map(|place| {
                        owner_carriers.contains(&place.local) && place.projection.is_empty()
                    })
                    .unwrap_or(false)
            }),
            TerminatorKind::Return => owner_carriers.contains(&RETURN_PLACE),
            _ => false,
        };
        if owner_reaches_terminal {
            continue;
        }
        if matches!(&terminator.kind, TerminatorKind::Call { .. }) {
            opaque_calls.insert(bb);
        }
        pending.extend(
            terminator
                .successors()
                .filter(|successor| !body[*successor].is_cleanup),
        );
    }
    opaque_calls
}

fn semantic_lifetime_feature_export<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> SemanticLifetimeFeatureExport {
    let reachable = reachable_cfg_blocks(body, candidate.original_target);
    let (natural_loop_blocks, backedges) = natural_loop_blocks_and_backedges(body);
    let mut reachable_normal = BTreeSet::new();
    let mut reachable_cleanup = BTreeSet::new();
    for bb in &reachable {
        if body[*bb].is_cleanup {
            reachable_cleanup.insert(*bb);
        } else {
            reachable_normal.insert(*bb);
        }
    }

    let mut aliases = BTreeSet::new();
    if let Some(owner) = candidate.feature_owner {
        aliases.insert(owner.local);
    }
    let mut changed = true;
    while changed {
        changed = false;
        for bb in &reachable {
            for (statement_index, statement) in body[*bb].statements.iter().enumerate() {
                let (destination, rvalue) = match &statement.kind {
                    StatementKind::Assign(assigned) => &**assigned,
                    _ => continue,
                };
                // A whole-carrier Move transfers ownership into its destination
                // even when rustc wraps the owner in an aggregate or writes it
                // through a projection. A projected extraction may select a
                // different field of a multi-owner carrier, so it stays an
                // ambiguous store sink instead of extending the carrier set.
                // Copy/borrow uses never extend the carrier set.
                if rvalue_moves_owner_alias(
                    rvalue,
                    &aliases,
                    Location {
                        block: *bb,
                        statement_index,
                    },
                ) && aliases.insert(destination.local)
                {
                    changed = true;
                }
            }
        }
    }
    let owner_live_opaque_call_blocks =
        owner_live_opaque_call_blocks(body, candidate.original_target, &aliases);

    let mut owner_move_count = 0usize;
    let mut return_sink_blocks = BTreeSet::new();
    let mut escape_sink_blocks = BTreeSet::new();
    let mut store_sink_blocks = BTreeSet::new();
    let mut normal_drop_blocks = BTreeSet::new();
    let mut cleanup_drop_blocks = BTreeSet::new();
    let mut function_has_yield_or_await = false;
    let mut reachable_yield_or_await = false;

    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        if data
            .terminator
            .as_ref()
            .map(terminator_has_yield_or_await)
            .unwrap_or(false)
        {
            function_has_yield_or_await = true;
            if reachable.contains(&bb) {
                reachable_yield_or_await = true;
            }
        }
        if !reachable.contains(&bb) {
            continue;
        }
        for (statement_index, statement) in data.statements.iter().enumerate() {
            let (destination, rvalue) = match &statement.kind {
                StatementKind::Assign(assigned) => &**assigned,
                _ => continue,
            };
            let exact_alias_move = rvalue_move_or_copy_source(rvalue).filter(|source| {
                aliases.contains(&source.local)
                    && source.projection.is_empty()
                    && destination.projection.is_empty()
                    && destination.ty(&body.local_decls, tcx).ty
                        == source.ty(&body.local_decls, tcx).ty
            });
            let moves_owner_carrier = rvalue_moves_owner_alias(
                rvalue,
                &aliases,
                Location {
                    block: bb,
                    statement_index,
                },
            );
            if exact_alias_move.is_some() || moves_owner_carrier {
                owner_move_count += 1;
                if destination.local == RETURN_PLACE {
                    return_sink_blocks.insert(bb);
                }
                if exact_alias_move.is_none() {
                    store_sink_blocks.insert(bb);
                }
            } else if rvalue_consumes_owner_alias(
                rvalue,
                &aliases,
                Location {
                    block: bb,
                    statement_index,
                },
            ) {
                store_sink_blocks.insert(bb);
            }
        }
        let Some(terminator) = &data.terminator else {
            continue;
        };
        match &terminator.kind {
            TerminatorKind::Drop { place, .. }
                if aliases.contains(&place.local) && place.projection.is_empty() =>
            {
                if data.is_cleanup {
                    cleanup_drop_blocks.insert(bb);
                } else {
                    normal_drop_blocks.insert(bb);
                }
            }
            TerminatorKind::Call { func, args, .. } => {
                let def_id = match func.ty(&body.local_decls, tcx).kind() {
                    ty::FnDef(def_id, _) => Some(*def_id),
                    _ => None,
                };
                let owner_argument = call_arg_operands(args).iter().any(|operand| {
                    operand_exact_place(operand)
                        .map(|place| aliases.contains(&place.local) && place.projection.is_empty())
                        .unwrap_or(false)
                });
                if owner_argument {
                    if def_id
                        .map(|def_id| exact_core_mem_drop_def_id(tcx, def_id))
                        .unwrap_or(false)
                    {
                        if data.is_cleanup {
                            cleanup_drop_blocks.insert(bb);
                        } else {
                            normal_drop_blocks.insert(bb);
                        }
                    } else {
                        escape_sink_blocks.insert(bb);
                    }
                }
            }
            TerminatorKind::Return if aliases.contains(&RETURN_PLACE) => {
                return_sink_blocks.insert(bb);
            }
            _ => {}
        }
    }

    let nonlinear_normal_cfg = reachable_normal.iter().any(|bb| {
        body[*bb]
            .terminator
            .as_ref()
            .map(|terminator| {
                terminator
                    .successors()
                    .filter(|successor| !body[*successor].is_cleanup)
                    .count()
                    > 1
            })
            .unwrap_or(false)
    });
    // This is a normal-path CFG fact only. Cleanup/unwind is exported in its
    // own field and independently forces the automatic hint to Unknown.
    let exact_drop_path = normal_drop_blocks.len() == 1
        && !nonlinear_normal_cfg
        && return_sink_blocks.is_empty()
        && escape_sink_blocks.is_empty()
        && store_sink_blocks.is_empty();
    let conditional_drop_path = !exact_drop_path && !normal_drop_blocks.is_empty();
    let (requested_size_bytes, requested_align_bytes, requested_layout_basis) =
        match exact_box_new_requested_layout(tcx, body, candidate) {
            Some((size, align)) => (Some(size), Some(align), "exact_box_new_payload_layout"),
            None => (None, None, "dynamic_or_unproven_requested_layout"),
        };
    let (normal_successor_count, cleanup_successor_count) = candidate
        .original_terminator
        .successors()
        .fold((0usize, 0usize), |(normal, cleanup), successor| {
            if body[successor].is_cleanup {
                (normal, cleanup + 1)
            } else {
                (normal + 1, cleanup)
            }
        });
    #[cfg(unialloc_rustc_current)]
    let analysis_phase = if marker_free_heap_preoptimization_rewrite_enabled() {
        "post_borrowck_pre_optimization"
    } else {
        "optimized_mir"
    };
    #[cfg(not(unialloc_rustc_current))]
    let analysis_phase = "optimized_mir";

    SemanticLifetimeFeatureExport {
        analysis_phase,
        owner_place: candidate.feature_owner.map(|place| format!("{:?}", place)),
        owner_place_basis: candidate.feature_owner_basis,
        requested_size_bytes,
        requested_align_bytes,
        requested_layout_basis,
        owner_move_count,
        return_sink: !return_sink_blocks.is_empty(),
        escape_sink: !escape_sink_blocks.is_empty(),
        store_sink: !store_sink_blocks.is_empty(),
        owner_live_opaque_call: !owner_live_opaque_call_blocks.is_empty(),
        allocation_in_natural_loop: natural_loop_blocks.contains(&candidate.bb),
        reachable_backedge_after_allocation: backedges
            .iter()
            .any(|(from, to)| reachable.contains(from) && reachable.contains(to)),
        function_has_yield_or_await,
        reachable_yield_or_await,
        receiver_owned_allocation: candidate.receiver_owned_allocation,
        exact_drop_path,
        conditional_drop_path,
        cleanup_drop_path: !cleanup_drop_blocks.is_empty(),
        normal_drop_blocks: sorted_block_labels(normal_drop_blocks),
        cleanup_drop_blocks: sorted_block_labels(cleanup_drop_blocks),
        return_sink_blocks: sorted_block_labels(return_sink_blocks),
        escape_sink_blocks: sorted_block_labels(escape_sink_blocks),
        store_sink_blocks: sorted_block_labels(store_sink_blocks),
        owner_live_opaque_call_blocks: sorted_block_labels(owner_live_opaque_call_blocks),
        reachable_normal_blocks: sorted_block_labels(reachable_normal),
        reachable_cleanup_blocks: sorted_block_labels(reachable_cleanup),
        normal_successor_count,
        cleanup_successor_count,
    }
}

#[derive(Clone, Debug)]
struct AutomaticHeapLifetimeTrace {
    decision: AutomaticHeapLifetimeDecision,
    terminal_owner_place: Option<String>,
}

fn candidate_marker_free_heap_lifetime_decision<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> AutomaticHeapLifetimeTrace {
    let destination_ty = candidate.destination.ty(&body.local_decls, tcx).ty;
    if !direct_supported_heap_object_destination(tcx, destination_ty)
        || format!("{:?}", destination_ty) != candidate.semantic_object_type
        || !candidate.destination.projection.is_empty()
    {
        return AutomaticHeapLifetimeTrace {
            decision: AutomaticHeapLifetimeDecision::UnsupportedOwnerUnknown,
            terminal_owner_place: None,
        };
    }
    if refcounted_heap_owner_ty(tcx, destination_ty) {
        return AutomaticHeapLifetimeTrace {
            decision: AutomaticHeapLifetimeDecision::RefcountedOwnerUnknown,
            terminal_owner_place: None,
        };
    }

    let mut owner = candidate.destination;
    let mut owner_move_count = 0usize;
    let mut bb = match candidate.original_target {
        Some(bb) => bb,
        None => {
            return AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::MissingTerminalUnknown,
                terminal_owner_place: None,
            }
        }
    };
    let mut visited = BTreeSet::new();

    loop {
        if !visited.insert(bb) {
            return AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::NonlinearControlFlowUnknown,
                terminal_owner_place: None,
            };
        }
        let data = &body[bb];
        if data.is_cleanup {
            return AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::MissingTerminalUnknown,
                terminal_owner_place: None,
            };
        }

        for (statement_index, statement) in data.statements.iter().enumerate() {
            if let Some(next_owner) = exact_unprojected_owner_move(tcx, body, statement, owner) {
                owner = next_owner;
                owner_move_count += 1;
                continue;
            }
            if statement_uses_exact_heap_owner(
                statement,
                owner,
                Location {
                    block: bb,
                    statement_index,
                },
            ) {
                return AutomaticHeapLifetimeTrace {
                    decision: AutomaticHeapLifetimeDecision::AliasOrEscapeUnknown,
                    terminal_owner_place: None,
                };
            }
        }

        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => {
                return AutomaticHeapLifetimeTrace {
                    decision: AutomaticHeapLifetimeDecision::MissingTerminalUnknown,
                    terminal_owner_place: None,
                }
            }
        };
        let bounded_process_long_terminal =
            terminator_exact_bounded_process_long_decision(tcx, body, terminator, owner);
        if let Some(decision) = bounded_process_long_terminal {
            return AutomaticHeapLifetimeTrace {
                decision,
                terminal_owner_place: None,
            };
        }
        // Releasing the exact owner is itself the terminal lifetime event.
        // An unwind edge on that Drop cannot make later normal-path work part
        // of the object's lifetime, because the owner has already entered its
        // consuming release operation.
        if let TerminatorKind::Drop { place, .. } = &terminator.kind {
            if *place == owner {
                return AutomaticHeapLifetimeTrace {
                    decision: if owner_move_count > 0 {
                        AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop
                    } else {
                        AutomaticHeapLifetimeDecision::ExactLocalDrop
                    },
                    terminal_owner_place: Some(format!("{:?}", owner)),
                };
            }
        }
        if let TerminatorKind::Call { func, args, .. } = &terminator.kind {
            let def_id = match func.ty(&body.local_decls, tcx).kind() {
                ty::FnDef(def_id, _) => Some(*def_id),
                _ => None,
            };
            let args = call_arg_operands(args);
            let exact_owner_consumer =
                args.len() == 1 && operand_is_exact_owner_consume(&args[0], owner);
            if exact_owner_consumer
                && def_id
                    .map(|def_id| exact_core_mem_drop_def_id(tcx, def_id))
                    .unwrap_or(false)
            {
                return AutomaticHeapLifetimeTrace {
                    decision: if owner_move_count > 1 {
                        AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop
                    } else {
                        AutomaticHeapLifetimeDecision::ExactLocalDrop
                    },
                    terminal_owner_place: None,
                };
            }
        }
        if terminator_has_cleanup_or_unwind_edge(terminator) {
            // The owner is live across this potentially-unwinding operation.
            // A normal-path Drop/forget says nothing about cleanup behavior;
            // retain Unknown until every normal and cleanup terminal has an
            // exact process-long proof.
            return AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::CleanupOrUnwindUnknown,
                terminal_owner_place: None,
            };
        }
        if let TerminatorKind::Drop { place, .. } = &terminator.kind {
            if *place == owner {
                return AutomaticHeapLifetimeTrace {
                    decision: if owner_move_count > 0 {
                        AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop
                    } else {
                        AutomaticHeapLifetimeDecision::ExactLocalDrop
                    },
                    terminal_owner_place: Some(format!("{:?}", owner)),
                };
            }
        }
        if let TerminatorKind::Call { func, args, .. } = &terminator.kind {
            let def_id = match func.ty(&body.local_decls, tcx).kind() {
                ty::FnDef(def_id, _) => Some(*def_id),
                _ => None,
            };
            let args = call_arg_operands(args);
            let exact_owner_consumer =
                args.len() == 1 && operand_is_exact_owner_consume(&args[0], owner);
            if exact_owner_consumer {
                if def_id
                    .map(|def_id| exact_core_mem_drop_def_id(tcx, def_id))
                    .unwrap_or(false)
                {
                    return AutomaticHeapLifetimeTrace {
                        // Drop elaboration lowers an explicit `drop(owner)` to
                        // one compiler-generated argument temporary. Count
                        // that final move as call plumbing; any earlier owner
                        // move is the bounded source-level move chain.
                        decision: if owner_move_count > 1 {
                            AutomaticHeapLifetimeDecision::ExactLocalMoveChainDrop
                        } else {
                            AutomaticHeapLifetimeDecision::ExactLocalDrop
                        },
                        terminal_owner_place: None,
                    };
                }
                if def_id
                    .map(|def_id| exact_core_mem_forget_def_id(tcx, def_id))
                    .unwrap_or(false)
                {
                    return AutomaticHeapLifetimeTrace {
                        decision: AutomaticHeapLifetimeDecision::ExactMemForget,
                        terminal_owner_place: None,
                    };
                }
                if exact_box_owner_ty(tcx, owner.ty(&body.local_decls, tcx).ty)
                    && def_id
                        .map(|def_id| exact_alloc_box_leak_def_id(tcx, def_id))
                        .unwrap_or(false)
                {
                    return AutomaticHeapLifetimeTrace {
                        decision: AutomaticHeapLifetimeDecision::ExactBoxLeak,
                        terminal_owner_place: None,
                    };
                }
            }
        }
        if terminator_uses_exact_heap_owner(
            terminator,
            owner,
            Location {
                block: bb,
                statement_index: data.statements.len(),
            },
        ) {
            return AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::AliasOrEscapeUnknown,
                terminal_owner_place: None,
            };
        }

        let successors = terminator.successors().collect::<Vec<_>>();
        if successors.len() != 1 {
            return AutomaticHeapLifetimeTrace {
                decision: if successors.is_empty() {
                    AutomaticHeapLifetimeDecision::MissingTerminalUnknown
                } else {
                    AutomaticHeapLifetimeDecision::NonlinearControlFlowUnknown
                },
                terminal_owner_place: None,
            };
        }
        bb = successors[0];
    }
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
        if matches!(
            &terminator.kind,
            TerminatorKind::Drop { place, .. } if *place == candidate.destination
        ) {
            return true;
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

fn exact_unialloc_lifetime_epoch_boundary_def_path(path: &str) -> bool {
    matches!(
        strip_rustc_crate_disambiguators(path).as_str(),
        "unialloc::lifetime_hugepage_advance_epoch"
            | "unialloc::alloc_api::lifetime_hugepage_advance_epoch"
            | "unialloc::alloc_api::lifetime_hugepage::lifetime_hugepage_advance_epoch"
    )
}

fn exact_unialloc_lifetime_epoch_boundary_def_id(tcx: TyCtxt<'_>, def_id: DefId) -> bool {
    tcx.crate_name(def_id.krate).as_str() == "unialloc"
        && exact_unialloc_lifetime_epoch_boundary_def_path(&tcx.def_path_str(def_id))
}

fn terminator_is_exact_unialloc_lifetime_epoch_boundary<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    terminator: &Terminator<'tcx>,
) -> bool {
    let func = match &terminator.kind {
        TerminatorKind::Call { func, .. } => func,
        _ => return false,
    };
    match func.ty(&body.local_decls, tcx).kind() {
        ty::FnDef(def_id, _) => exact_unialloc_lifetime_epoch_boundary_def_id(tcx, *def_id),
        _ => false,
    }
}

fn type_drop_glue_may_execute<'tcx>(tcx: TyCtxt<'tcx>, ty: Ty<'tcx>) -> bool {
    // `needs_drop` requires a fully resolved typing environment. Optimized MIR
    // is normally monomorphized here; unresolved inputs abstain rather than
    // risking either an ICE or an unsound Ephemeral classification.
    if clone_result_has_unresolved_params(ty) {
        return true;
    }
    #[cfg(unialloc_rustc_current)]
    {
        ty.needs_drop(tcx, ty::TypingEnv::fully_monomorphized())
    }
    #[cfg(not(unialloc_rustc_current))]
    {
        ty.needs_drop(tcx, ty::ParamEnv::reveal_all())
    }
}

fn direct_owner_generic_drop_glue_may_be_effectful<'tcx>(
    tcx: TyCtxt<'tcx>,
    owner_ty: Ty<'tcx>,
) -> bool {
    let generic_types = match owner_ty.kind() {
        ty::Adt(_, substs) => substs.types(),
        // The caller accepts only direct supported owner ADTs. Retain a
        // conservative result if that contract changes in a future rustc.
        _ => return true,
    };

    generic_types
        .into_iter()
        .any(|nested_ty| type_drop_glue_may_execute(tcx, nested_ty))
}

fn lifetime_terminator_is_transparent_control_only<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    terminator: &Terminator<'tcx>,
) -> bool {
    match &terminator.kind {
        // These terminators only choose the next MIR block. SwitchInt and
        // Assert operands are already evaluated values; an Assert unwind edge
        // is handled independently by the pre-boundary cleanup-edge gate.
        TerminatorKind::Goto { .. }
        | TerminatorKind::SwitchInt { .. }
        | TerminatorKind::Assert { .. }
        | TerminatorKind::FalseEdge { .. }
        | TerminatorKind::FalseUnwind { .. } => true,
        // Drop is transparent only when rustc proves that no drop glue runs.
        // A Drop whose glue runs can invoke arbitrary application code.
        TerminatorKind::Drop { place, .. } => {
            let drop_ty = place.ty(&body.local_decls, tcx).ty;
            !type_drop_glue_may_execute(tcx, drop_ty)
        }
        // Exact epoch Calls are recognized and consumed as boundaries before
        // this predicate is queried. Every other Call is opaque. All remaining
        // current and legacy MIR variants, including InlineAsm and Yield, stay
        // fail-closed without enumerating version-specific variant names.
        _ => false,
    }
}

fn candidate_linear_lifetime_decision<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    candidate: &SemanticScopeCandidate<'tcx>,
) -> AutomaticLifetimeDecision {
    let destination_ty = candidate.destination.ty(&body.local_decls, tcx).ty;
    if !direct_supported_heap_object_destination(tcx, destination_ty)
        || format!("{:?}", destination_ty) != candidate.semantic_object_type
    {
        // Semantic scopes also cover receiver-owned reallocations and
        // deallocations. Their call destination can be `()`, `Option<T>`, or a
        // temporary guard/iterator whose Drop says nothing about the receiver
        // allocation's lifetime. Until the pass records an exact receiver
        // owner Place, classify only direct destination-owning constructors.
        return AutomaticLifetimeDecision::UnsupportedSiteUnknown;
    }
    if !candidate_has_stable_nonescaping_owner_uses(body, candidate) {
        return AutomaticLifetimeDecision::AliasOrEscapeUnknown;
    }
    let mut bb = match candidate.original_target {
        Some(bb) => bb,
        None => return AutomaticLifetimeDecision::MissingOrCleanupDropUnknown,
    };
    let mut visited = BTreeSet::new();
    let mut crossed_phase_boundary = false;
    let mut saw_hidden_effect = false;
    let mut saw_pre_boundary_cleanup_edge = false;

    loop {
        if !visited.insert(bb) {
            return AutomaticLifetimeDecision::NonlinearControlFlowUnknown;
        }
        let data = &body[bb];
        if data.is_cleanup {
            return AutomaticLifetimeDecision::MissingOrCleanupDropUnknown;
        }
        let terminator = match &data.terminator {
            Some(terminator) => terminator,
            None => return AutomaticLifetimeDecision::MissingOrCleanupDropUnknown,
        };

        if let TerminatorKind::Drop { place, .. } = &terminator.kind {
            if *place == candidate.destination {
                return if crossed_phase_boundary {
                    if saw_pre_boundary_cleanup_edge {
                        AutomaticLifetimeDecision::CleanupBeforeBoundaryUnknown
                    } else {
                        AutomaticLifetimeDecision::ExactLocalDropAfterPhaseBoundary
                    }
                } else if direct_owner_generic_drop_glue_may_be_effectful(tcx, destination_ty) {
                    // Dropping a Box<T>, Vec<T>, Arc<T>, or another supported
                    // owner can execute T::drop before releasing the backing
                    // allocation. That user Drop glue can advance the process
                    // epoch while the owner remains live, so the absence of an
                    // explicit MIR boundary in this body cannot prove short
                    // lifetime. A boundary already crossed still proves Long.
                    AutomaticLifetimeDecision::EffectfulDropGlueUnknown
                } else if saw_hidden_effect {
                    // Without interprocedural and terminator-effect summaries,
                    // an opaque operation can hide the process-wide epoch API.
                    AutomaticLifetimeDecision::InterveningCallMayAdvanceEpochUnknown
                } else if saw_pre_boundary_cleanup_edge {
                    // A cleanup path can drop the owner before the observed
                    // normal-path boundary. Ephemeral requires every reachable
                    // pre-boundary path to stay free of such an early exit.
                    AutomaticLifetimeDecision::CleanupBeforeBoundaryUnknown
                } else {
                    AutomaticLifetimeDecision::ExactLocalDropBeforePhaseBoundary
                };
            }
        }

        let exact_phase_boundary =
            terminator_is_exact_unialloc_lifetime_epoch_boundary(tcx, body, terminator);
        if !crossed_phase_boundary && !exact_phase_boundary {
            if !lifetime_terminator_is_transparent_control_only(tcx, body, terminator) {
                saw_hidden_effect = true;
            }
            if terminator
                .successors()
                .any(|successor| body[successor].is_cleanup)
            {
                saw_pre_boundary_cleanup_edge = true;
            }
        }
        if exact_phase_boundary {
            crossed_phase_boundary = true;
        }

        let normal_successors = terminator
            .successors()
            .filter(|successor| !body[*successor].is_cleanup)
            .collect::<Vec<_>>();
        if normal_successors.len() != 1 {
            return AutomaticLifetimeDecision::NonlinearControlFlowUnknown;
        }
        bb = normal_successors[0];
    }
}

fn solved_semantic_drop_pairs<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
) -> BTreeSet<(String, String)> {
    body_basic_blocks!(body)
        .iter()
        .filter_map(|data| {
            let place = match &data.terminator {
                Some(Terminator {
                    kind: TerminatorKind::Drop { place, .. },
                    ..
                }) => place,
                _ => return None,
            };
            let place_ty = place.ty(&body.local_decls, tcx).ty;
            if type_contains_generic_param(tcx, place_ty) {
                return None;
            }
            let scan = heap_object_type_scan_from_ty(tcx, place_ty);
            if scan.unresolved || scan.owners.len() != 1 {
                return None;
            }
            Some((
                scan.owners.into_iter().next().unwrap().display,
                format!("{:?}", place),
            ))
        })
        .collect()
}

fn exact_semantic_local_ownership_proof<'tcx>(
    tcx: TyCtxt<'tcx>,
    body: &Body<'tcx>,
    candidates: &[SemanticScopeCandidate<'tcx>],
) -> SemanticLocalOwnershipProof {
    if !direct_local_size_align_with_semantic_drop_requested()
        && !auto_lifetime_classifier_enabled()
        && !auto_heap_lifetime_inference_enabled()
        && !auto_rust_lifetime_prior_enabled()
    {
        return SemanticLocalOwnershipProof::default();
    }

    let mut candidate_counts: BTreeMap<(String, String), usize> = BTreeMap::new();
    let mut candidate_pairs: BTreeMap<
        String,
        Vec<((String, String), AutomaticLifetimeDecision, bool)>,
    > = BTreeMap::new();
    let mut heap_candidate_traces: Vec<((String, String), String, AutomaticHeapLifetimeTrace)> =
        Vec::new();
    let solved_drop_pairs = solved_semantic_drop_pairs(tcx, body);

    for candidate in candidates {
        if candidate.semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE {
            continue;
        }
        let key = (
            candidate.semantic_object_type.clone(),
            candidate.destination_place.clone(),
        );
        let drop_requires_allocation_recovery = candidate
            .runtime_identity_owner_ty
            .map(|owner_ty| exact_box_drop_requires_allocation_recovery(tcx, owner_ty))
            .unwrap_or(false);
        let local_abi_proven = !candidate.original_is_cleanup
            && !drop_requires_allocation_recovery
            && candidate_has_linear_exact_drop(body, candidate)
            && solved_drop_pairs.contains(&key);
        *candidate_counts.entry(key.clone()).or_insert(0) += 1;
        let heap_trace = if candidate.original_is_cleanup {
            AutomaticHeapLifetimeTrace {
                decision: AutomaticHeapLifetimeDecision::MissingTerminalUnknown,
                terminal_owner_place: None,
            }
        } else {
            candidate_marker_free_heap_lifetime_decision(tcx, body, candidate)
        };
        heap_candidate_traces.push((
            key.clone(),
            candidate.semantic_object_type.clone(),
            heap_trace,
        ));
        candidate_pairs
            .entry(candidate.semantic_object_type.clone())
            .or_default()
            .push((
                key,
                if candidate.original_is_cleanup {
                    AutomaticLifetimeDecision::MissingOrCleanupDropUnknown
                } else {
                    candidate_linear_lifetime_decision(tcx, body, candidate)
                },
                local_abi_proven,
            ));
    }

    let mut proof = SemanticLocalOwnershipProof::default();
    let mut heap_terminal_counts: BTreeMap<(String, String), usize> = BTreeMap::new();
    for (_, semantic_object_type, trace) in &heap_candidate_traces {
        if let Some(terminal_owner_place) = &trace.terminal_owner_place {
            *heap_terminal_counts
                .entry((semantic_object_type.clone(), terminal_owner_place.clone()))
                .or_insert(0) += 1;
        }
    }
    for (pair, semantic_object_type, trace) in &heap_candidate_traces {
        let decision = if candidate_counts.get(pair) == Some(&1) {
            trace.decision
        } else {
            AutomaticHeapLifetimeDecision::AmbiguousOwnerSiteUnknown
        };
        proof
            .automatic_heap_lifetime_decisions
            .insert(pair.clone(), decision);
        if let Some(terminal_owner_place) = &trace.terminal_owner_place {
            let terminal_pair = (semantic_object_type.clone(), terminal_owner_place.clone());
            let terminal_decision = if heap_terminal_counts.get(&terminal_pair) == Some(&1) {
                decision
            } else {
                AutomaticHeapLifetimeDecision::AmbiguousOwnerSiteUnknown
            };
            proof
                .automatic_heap_lifetime_decisions
                .insert(terminal_pair, terminal_decision);
        }
    }
    for pairs in candidate_pairs.values() {
        for (pair, decision, _) in pairs {
            if candidate_counts.get(pair) == Some(&1) {
                proof
                    .automatic_lifetime_decisions
                    .insert(pair.clone(), *decision);
            } else {
                proof.automatic_lifetime_decisions.insert(
                    pair.clone(),
                    AutomaticLifetimeDecision::AmbiguousOwnerSiteUnknown,
                );
            }
        }
    }
    for pairs in candidate_pairs.into_values() {
        if pairs.iter().any(|(pair, _, local_abi_proven)| {
            !local_abi_proven || candidate_counts.get(pair) != Some(&1)
        }) {
            continue;
        }
        for (pair, _, local_abi_proven) in pairs {
            proof.allocation_pairs.insert(pair.clone());
            proof.drop_pairs.insert(pair.clone());
            debug_assert!(local_abi_proven);
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
    semantic_ownership_transfer_abi: Option<SemanticOwnershipTransferAbi>,
    records: &mut Vec<RewriteRecord>,
) {
    let function_name = tcx.def_path_str(def_id);
    let authentication_abis =
        replacement_abis.unwrap_or_else(|| resolve_unialloc_direct_allocator_abis(tcx));
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
        let (callee_def_id, callee_generic_types) = match func.ty(&body.local_decls, tcx).kind() {
            ty::FnDef(def_id, args) => (Some(*def_id), args.types().collect::<Vec<_>>()),
            _ => (None, Vec::new()),
        };
        let call_kind = callee_def_id.map_or(DirectAllocatorCallKind::Unsupported, |def_id| {
            authenticated_direct_allocator_call_kind(tcx, def_id)
        });
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
            if !direct_allocator_global_receiver_supported(
                tcx,
                call_kind,
                callee_def_id.expect("authenticated direct allocator call has a FnDef"),
                trusted_unialloc_direct_allocator_crate(authentication_abis, call_kind),
                &argument_tys,
            ) || (direct_allocator_is_global_receiver_call(call_kind)
                && layout_solution.is_none())
            {
                propagate_layout_like_call_provenance(
                    tcx,
                    callee_def_id,
                    &callee,
                    &arg_operands,
                    &callee_generic_types,
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
                source_span: tcx.sess.source_map().span_to_diagnostic_string(*fn_span),
                fn_span: *fn_span,
            });
        }
        propagate_layout_like_call_provenance(
            tcx,
            callee_def_id,
            &callee,
            &arg_operands,
            &callee_generic_types,
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
        let (type_id, type_id_basis) = direct_allocator_type_id();
        let recovery_delegated = type_id_basis == "direct_allocator_recovery_delegated";
        // Optimized MIR no longer exposes a sound owner link from a raw
        // size/align allocator destination to the eventual heap-owner Drop.
        // A same-type Drop count is not an ownership proof, so this branch
        // remains recovery-backed until an exact destination link exists.
        let allow_size_align_local = !(call_kind == DirectAllocatorCallKind::SizeAlignAlloc
            && direct_local_size_align_with_semantic_drop_requested());
        let configured_policy_flags = lowering_policy_flags();
        let configured_module_id = lowering_module_id();
        let lifetime_selection = lowering_lifetime_hint_for_site(
            callsite,
            type_id,
            configured_module_id,
            Some(AutomaticLifetimeDecision::UnsupportedSiteUnknown),
            Some(AutomaticHeapLifetimeDecision::UnsupportedOwnerUnknown),
            None,
        );
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
            lifetime_hint_confidence,
            lifetime_hint_basis,
            placement_hint,
            cross_thread_recovery_hint,
            placement_hint_basis,
        ) = if recovery_delegated {
            (
                0,
                0,
                0,
                0,
                "direct_allocator_recovery_delegated_neutral",
                0,
                false,
                "direct_allocator_recovery_delegated_neutral",
            )
        } else {
            (
                configured_module_id,
                configured_policy_flags,
                lifetime_selection.hint,
                lifetime_selection.confidence,
                lifetime_selection.basis,
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
                        lifetime_hint_confidence,
                        lifetime_hint_basis,
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
                        lifetime_analysis_features: None,
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
            lifetime_hint_confidence,
            lifetime_hint_basis,
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
            lifetime_analysis_features: None,
        });
    }
    let local_ownership = record_or_rewrite_semantic_scope_candidates(
        tcx,
        def_id,
        body,
        semantic_scope_rewrite,
        semantic_scope_abi,
        semantic_scope_local_abi,
        semantic_ownership_transfer_abi,
        records,
    );
    record_or_rewrite_semantic_drop_candidates(
        tcx,
        def_id,
        body,
        semantic_scope_rewrite,
        semantic_scope_abi,
        semantic_scope_local_abi,
        &local_ownership,
        &local_ownership.exact_zip_drop_pairs,
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
    original_unwind: MirUnwind,
    from_hir_call: MirCallSource,
    is_cleanup: bool,
) -> BasicBlock {
    let pop_unit_local = push_internal_local(body, unit_ty(tcx), fn_span);
    let pop_unit_place = Place::from(pop_unit_local);
    let unwind = if is_cleanup {
        no_unwind()
    } else {
        original_unwind
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

fn record_or_rewrite_semantic_ownership_transfers<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    body: &mut Body<'tcx>,
    semantic_scope_rewrite: bool,
    transfer_abi: Option<SemanticOwnershipTransferAbi>,
    records: &mut Vec<RewriteRecord>,
) -> (BTreeSet<BasicBlock>, BTreeSet<(String, String)>) {
    let function_name = tcx.def_path_str(def_id);
    let mut candidates = Vec::new();

    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
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
        if target.is_none() || (semantic_scope_rewrite && transfer_abi.is_none()) {
            continue;
        }

        let (callee_def_id, callee_generic_types) = match func.ty(&body.local_decls, tcx).kind() {
            ty::FnDef(def_id, args) => (*def_id, args.types().collect::<Vec<_>>()),
            _ => continue,
        };
        let arg_operands = call_arg_operands(args);
        let argument_tys = arg_operands
            .iter()
            .map(|arg| arg.ty(&body.local_decls, tcx))
            .collect::<Vec<_>>();
        let destination_ty = destination.ty(&body.local_decls, tcx).ty;
        let (kind, proof, call_shape) = if let Some(proof) = exact_box_slice_into_vec_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::BoxSliceIntoVec,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_vec_into_boxed_slice_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::VecIntoBoxedSlice,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_string_into_bytes_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::StringIntoBytes,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_string_into_boxed_str_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::StringIntoBoxedStr,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_boxed_str_into_string_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::BoxedStrIntoString,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_cstring_into_bytes_with_nul_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::CStringIntoBytesWithNul,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some(proof) = exact_vec_into_iter_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::VecIntoIter,
                proof,
                SemanticOwnershipTransferCallShape::Direct,
            )
        } else if let Some((proof, call_shape)) = exact_vec_into_iter_via_zip_transfer_proof(
            tcx,
            callee_def_id,
            &callee_generic_types,
            destination_ty,
            &argument_tys,
        ) {
            (
                SemanticOwnershipTransferKind::VecIntoIterViaZip,
                proof,
                call_shape,
            )
        } else {
            continue;
        };
        if kind == SemanticOwnershipTransferKind::VecIntoIterViaZip && data.is_cleanup {
            continue;
        }
        let (expected_old_owner_ty, expected_old_owner_type, expected_old_owner_basis) = match kind
        {
            SemanticOwnershipTransferKind::BoxSliceIntoVec => {
                match exact_immediate_box_array_unsize_owner_type(
                    tcx,
                    body,
                    bb,
                    &arg_operands[0],
                    argument_tys[0],
                    &proof,
                ) {
                    Some(owner_ty) => (
                        owner_ty,
                        format!("{:?}", owner_ty),
                        "exact_immediate_box_array_unsize",
                    ),
                    None => (
                        proof.old_owner_ty,
                        proof.old_owner_type.clone(),
                        "exact_box_slice_argument",
                    ),
                }
            }
            SemanticOwnershipTransferKind::VecIntoBoxedSlice => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_vec_argument",
            ),
            SemanticOwnershipTransferKind::StringIntoBytes => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_string_argument",
            ),
            SemanticOwnershipTransferKind::StringIntoBoxedStr => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_string_argument",
            ),
            SemanticOwnershipTransferKind::BoxedStrIntoString => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_boxed_str_argument",
            ),
            SemanticOwnershipTransferKind::CStringIntoBytesWithNul => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_cstring_argument",
            ),
            SemanticOwnershipTransferKind::VecIntoIter => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_vec_argument",
            ),
            SemanticOwnershipTransferKind::VecIntoIterViaZip => (
                proof.old_owner_ty,
                proof.old_owner_type.clone(),
                "exact_vec_zip_argument",
            ),
        };

        candidates.push(SemanticOwnershipTransferCandidate {
            bb,
            callee: callee_text(func),
            call_arguments: arg_operands
                .iter()
                .map(|arg| format!("{:?}", arg))
                .collect(),
            argument_types: argument_tys
                .iter()
                .map(|arg_ty| format!("{:?}", arg_ty))
                .collect(),
            destination: *destination,
            destination_place: format!("{:?}", destination),
            destination_type: format!("{:?}", destination_ty),
            kind,
            call_shape,
            proof,
            expected_old_owner_ty,
            expected_old_owner_type,
            expected_old_owner_basis,
            source_span: tcx.sess.source_map().span_to_diagnostic_string(*fn_span),
            fn_span: *fn_span,
        });
    }

    let mut handled_blocks = BTreeSet::new();
    let mut exact_zip_drop_pairs = BTreeSet::new();
    for candidate in candidates {
        let basic_block = format!("{:?}", candidate.bb);
        let key = format!(
            "semantic-ownership-transfer\0{}\0{}\0{}\0{}\0{}\0{}",
            function_name,
            basic_block,
            candidate.source_span,
            candidate.callee,
            candidate.proof.old_owner_type,
            candidate.proof.new_owner_type,
        );
        let callsite = nonzero_fnv1a64_text(&key);
        let Some(old_type_id) = compiler_semantic_type_id(tcx, candidate.expected_old_owner_ty)
        else {
            continue;
        };
        let Some(new_type_id) = compiler_semantic_type_id(tcx, candidate.proof.new_owner_ty) else {
            continue;
        };
        let module_id = lowering_module_id();
        let mut rewrite_status = "semantic_ownership_transfer_rewrite_planned";
        let mut replacement_resolution_status = "not_requested_dry_run";

        if semantic_scope_rewrite {
            let still_exact = {
                let terminator = body[candidate.bb].terminator();
                let (func, args, destination, target) = match &terminator.kind {
                    TerminatorKind::Call {
                        func,
                        args,
                        destination,
                        target,
                        ..
                    } => (func, args, destination, target),
                    _ => continue,
                };
                if target.is_none() {
                    false
                } else {
                    let (callee_def_id, callee_generic_types) =
                        match func.ty(&body.local_decls, tcx).kind() {
                            ty::FnDef(def_id, args) => (*def_id, args.types().collect::<Vec<_>>()),
                            _ => continue,
                        };
                    let arg_operands = call_arg_operands(args);
                    let argument_tys = arg_operands
                        .iter()
                        .map(|arg| arg.ty(&body.local_decls, tcx))
                        .collect::<Vec<_>>();
                    let destination_ty = destination.ty(&body.local_decls, tcx).ty;
                    let proof = match candidate.kind {
                        SemanticOwnershipTransferKind::BoxSliceIntoVec => {
                            exact_box_slice_into_vec_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::VecIntoBoxedSlice => {
                            exact_vec_into_boxed_slice_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::StringIntoBytes => {
                            exact_string_into_bytes_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::StringIntoBoxedStr => {
                            exact_string_into_boxed_str_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::BoxedStrIntoString => {
                            exact_boxed_str_into_string_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::CStringIntoBytesWithNul => {
                            exact_cstring_into_bytes_with_nul_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::VecIntoIter => {
                            exact_vec_into_iter_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                        }
                        SemanticOwnershipTransferKind::VecIntoIterViaZip => {
                            exact_vec_into_iter_via_zip_transfer_proof(
                                tcx,
                                callee_def_id,
                                &callee_generic_types,
                                destination_ty,
                                &argument_tys,
                            )
                            .map(|(proof, _)| proof)
                        }
                    };
                    proof.map_or(false, |proof| {
                        proof.element_ty == candidate.proof.element_ty
                            && proof.allocator_ty == candidate.proof.allocator_ty
                            && proof.old_owner_ty == candidate.proof.old_owner_ty
                            && proof.new_owner_ty == candidate.proof.new_owner_ty
                            && proof.old_owner_type == candidate.proof.old_owner_type
                            && proof.new_owner_type == candidate.proof.new_owner_type
                    })
                }
            };
            if !still_exact {
                // Do not consume this block. The ordinary semantic-scope scan
                // below will retain its existing ambiguous fail-closed audit.
                continue;
            }

            let abi = match transfer_abi {
                Some(abi) => abi,
                None => continue,
            };
            let transfer_def_id = match candidate.kind {
                SemanticOwnershipTransferKind::BoxSliceIntoVec => abi.box_slice_into_vec_def_id,
                SemanticOwnershipTransferKind::VecIntoBoxedSlice => abi.vec_into_boxed_slice_def_id,
                SemanticOwnershipTransferKind::StringIntoBytes => abi.string_into_bytes_def_id,
                SemanticOwnershipTransferKind::StringIntoBoxedStr => {
                    abi.string_into_boxed_str_def_id
                }
                SemanticOwnershipTransferKind::BoxedStrIntoString => {
                    abi.boxed_str_into_string_def_id
                }
                SemanticOwnershipTransferKind::CStringIntoBytesWithNul => {
                    abi.cstring_into_bytes_with_nul_def_id
                }
                SemanticOwnershipTransferKind::VecIntoIter => abi.vec_into_iter_def_id,
                SemanticOwnershipTransferKind::VecIntoIterViaZip => abi.vec_into_iter_def_id,
            };
            let applied_zip_rewrite = match candidate.call_shape.clone() {
                SemanticOwnershipTransferCallShape::Direct => {
                    let terminator = body[candidate.bb].terminator_mut();
                    let (func, args) = match &mut terminator.kind {
                        TerminatorKind::Call { func, args, .. } if args.len() == 1 => (func, args),
                        _ => continue,
                    };
                    let rewritten_args = vec![
                        clone_call_arg_operand(args, 0),
                        const_u64_operand(tcx, old_type_id, candidate.fn_span),
                        const_u64_operand(tcx, new_type_id, candidate.fn_span),
                    ];
                    *func = match candidate.kind {
                        SemanticOwnershipTransferKind::StringIntoBytes
                        | SemanticOwnershipTransferKind::StringIntoBoxedStr
                        | SemanticOwnershipTransferKind::BoxedStrIntoString
                        | SemanticOwnershipTransferKind::CStringIntoBytesWithNul => {
                            unialloc_function_handle(tcx, transfer_def_id, candidate.fn_span)
                        }
                        SemanticOwnershipTransferKind::BoxSliceIntoVec
                        | SemanticOwnershipTransferKind::VecIntoBoxedSlice
                        | SemanticOwnershipTransferKind::VecIntoIter => {
                            unialloc_function_handle_with_two_types(
                                tcx,
                                transfer_def_id,
                                candidate.proof.element_ty,
                                candidate.proof.allocator_ty,
                                candidate.fn_span,
                            )
                        }
                        SemanticOwnershipTransferKind::VecIntoIterViaZip => continue,
                    };
                    *args = make_call_args(rewritten_args, candidate.fn_span);
                    None
                }
                SemanticOwnershipTransferCallShape::IteratorZip {
                    zip_def_id,
                    iterator_ty,
                    into_iter_ty,
                } => {
                    let mut zip_terminator = body[candidate.bb].terminator().clone();
                    let source_info = zip_terminator.source_info;
                    let (iterator_arg, vec_arg) = match &zip_terminator.kind {
                        TerminatorKind::Call { args, .. } if args.len() == 2 => (
                            clone_call_arg_operand(args, 0),
                            clone_call_arg_operand(args, 1),
                        ),
                        _ => continue,
                    };
                    #[cfg(unialloc_rustc_current)]
                    let (original_unwind, original_call_source) = match &zip_terminator.kind {
                        TerminatorKind::Call {
                            unwind,
                            call_source,
                            ..
                        } => (*unwind, *call_source),
                        _ => continue,
                    };
                    #[cfg(not(unialloc_rustc_current))]
                    let (original_unwind, original_call_source) = match &zip_terminator.kind {
                        TerminatorKind::Call {
                            cleanup,
                            from_hir_call,
                            ..
                        } => (*cleanup, *from_hir_call),
                        _ => continue,
                    };

                    let into_iter_local =
                        push_internal_local(body, into_iter_ty, candidate.fn_span);
                    let into_iter_place = Place::from(into_iter_local);
                    if let TerminatorKind::Call { func, args, .. } = &mut zip_terminator.kind {
                        *func = unialloc_function_handle_with_two_types(
                            tcx,
                            zip_def_id,
                            iterator_ty,
                            into_iter_ty,
                            candidate.fn_span,
                        );
                        *args = make_call_args(
                            vec![iterator_arg, Operand::Move(into_iter_place)],
                            candidate.fn_span,
                        );
                    } else {
                        continue;
                    }
                    let zip_block = body
                        .basic_blocks_mut()
                        .push(basic_block_data(zip_terminator, false));
                    handled_blocks.insert(zip_block);

                    let transfer_call = call_terminator_kind_with_two_types(
                        tcx,
                        transfer_def_id,
                        candidate.proof.element_ty,
                        candidate.proof.allocator_ty,
                        vec![
                            vec_arg,
                            const_u64_operand(tcx, old_type_id, candidate.fn_span),
                            const_u64_operand(tcx, new_type_id, candidate.fn_span),
                        ],
                        into_iter_place,
                        Some(zip_block),
                        original_unwind,
                        original_call_source,
                        candidate.fn_span,
                    );
                    body[candidate.bb].terminator_mut().kind = transfer_call;
                    body[candidate.bb].terminator_mut().source_info = source_info;
                    Some(zip_block)
                }
            };
            rewrite_status = "actual_semantic_ownership_transfer_rewrite_applied";
            if let Some(zip_block) = applied_zip_rewrite {
                exact_zip_drop_pairs.extend(exact_zip_drop_pairs_after_applied_rewrite(
                    body,
                    zip_block,
                    candidate.destination,
                ));
            }
            replacement_resolution_status = match candidate.kind {
                SemanticOwnershipTransferKind::BoxSliceIntoVec => {
                    "resolved_unialloc_semantic_box_slice_into_vec"
                }
                SemanticOwnershipTransferKind::VecIntoBoxedSlice => {
                    "resolved_unialloc_semantic_vec_into_boxed_slice"
                }
                SemanticOwnershipTransferKind::StringIntoBytes => {
                    "resolved_unialloc_semantic_string_into_bytes"
                }
                SemanticOwnershipTransferKind::StringIntoBoxedStr => {
                    "resolved_unialloc_semantic_string_into_boxed_str"
                }
                SemanticOwnershipTransferKind::BoxedStrIntoString => {
                    "resolved_unialloc_semantic_boxed_str_into_string"
                }
                SemanticOwnershipTransferKind::CStringIntoBytesWithNul => {
                    "resolved_unialloc_semantic_cstring_into_bytes_with_nul"
                }
                SemanticOwnershipTransferKind::VecIntoIter => {
                    "resolved_unialloc_semantic_vec_into_iter"
                }
                SemanticOwnershipTransferKind::VecIntoIterViaZip => {
                    "resolved_unialloc_semantic_vec_into_iter_via_zip"
                }
            };
        }

        let (type_id_basis, replacement_symbol, replacement_preview, metadata_pairing_contract) =
            match candidate.kind {
                SemanticOwnershipTransferKind::BoxSliceIntoVec => (
                    "rustc_middle_exact_box_slice_into_vec_owner_transfer",
                    SEMANTIC_BOX_SLICE_INTO_VEC_SYMBOL,
                    format!(
                        "Retarget exact pointer-preserving Box<[T], A> -> Vec<T, A> ownership transfer; old_owner_type={}; old_type_id={}; argument_owner_type={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.proof.old_owner_type,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_owner_identity_rebind",
                ),
                SemanticOwnershipTransferKind::VecIntoBoxedSlice => (
                    "rustc_middle_exact_vec_into_boxed_slice_owner_transfer",
                    SEMANTIC_VEC_INTO_BOXED_SLICE_SYMBOL,
                    format!(
                        "Retarget exact shrink-aware Vec<T, A> -> Box<[T], A> ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "shrink_aware_owner_identity_transfer",
                ),
                SemanticOwnershipTransferKind::StringIntoBytes => (
                    "rustc_middle_exact_string_into_bytes_owner_transfer",
                    SEMANTIC_STRING_INTO_BYTES_SYMBOL,
                    format!(
                        "Retarget exact pointer-preserving String -> Vec<u8, Global> ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_owner_identity_rebind",
                ),
                SemanticOwnershipTransferKind::StringIntoBoxedStr => (
                    "rustc_middle_exact_string_into_boxed_str_owner_transfer",
                    SEMANTIC_STRING_INTO_BOXED_STR_SYMBOL,
                    format!(
                        "Retarget exact shrink-aware String -> Box<str, Global> ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "shrink_aware_owner_identity_transfer",
                ),
                SemanticOwnershipTransferKind::BoxedStrIntoString => (
                    "rustc_middle_exact_boxed_str_into_string_owner_transfer",
                    SEMANTIC_BOXED_STR_INTO_STRING_SYMBOL,
                    format!(
                        "Retarget exact pointer-preserving Box<str, Global> -> String ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_owner_identity_rebind",
                ),
                SemanticOwnershipTransferKind::CStringIntoBytesWithNul => (
                    "rustc_middle_exact_cstring_into_bytes_with_nul_owner_transfer",
                    SEMANTIC_CSTRING_INTO_BYTES_WITH_NUL_SYMBOL,
                    format!(
                        "Retarget exact pointer-preserving CString -> Vec<u8, Global> ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_owner_identity_rebind",
                ),
                SemanticOwnershipTransferKind::VecIntoIter => (
                    "rustc_middle_exact_vec_into_iter_owner_transfer",
                    SEMANTIC_VEC_INTO_ITER_SYMBOL,
                    format!(
                        "Retarget exact pointer-preserving Vec<T, A> -> IntoIter<T, A> ownership transfer; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_owner_identity_rebind",
                ),
                SemanticOwnershipTransferKind::VecIntoIterViaZip => (
                    "rustc_middle_exact_vec_into_iter_via_zip_owner_transfer",
                    SEMANTIC_VEC_INTO_ITER_SYMBOL,
                    format!(
                        "Split exact core Iterator::zip<canonical Vec<u8, Global>> into pointer-preserving Vec -> IntoIter ownership transfer followed by zip<IntoIter>; old_owner_type={}; old_type_id={}; old_owner_basis={}; new_owner_type={}; new_type_id={}",
                        candidate.expected_old_owner_type,
                        old_type_id,
                        candidate.expected_old_owner_basis,
                        candidate.proof.new_owner_type,
                        new_type_id,
                    ),
                    "pointer_preserving_hidden_zip_owner_identity_rebind",
                ),
            };

        records.push(RewriteRecord {
            allocation_site_id: format!(
                "rustc-driver-mir-semantic-ownership-transfer:{:016x}",
                callsite
            ),
            type_id: new_type_id,
            module_id,
            flags: lowering_policy_flags(),
            lifetime_hint: 0,
            lifetime_hint_confidence: 0,
            lifetime_hint_basis: "preserved_by_ownership_transfer_rebind",
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
            source_span: candidate.source_span,
            callee: candidate.callee,
            destination_place: candidate.destination_place,
            destination_type: candidate.destination_type,
            call_arguments: candidate.call_arguments,
            argument_types: candidate.argument_types,
            semantic_object_type: candidate.proof.new_owner_type.clone(),
            type_id_basis,
            size_operand: None,
            align_operand: None,
            rewrite_status,
            replacement_symbol,
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted: false,
            metadata_pairing_contract,
            lowering_kind: "semantic_ownership_transfer_rewrite",
            lifetime_analysis_features: None,
        });
        handled_blocks.insert(candidate.bb);
    }

    (handled_blocks, exact_zip_drop_pairs)
}

fn record_or_rewrite_semantic_scope_candidates<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: DefId,
    body: &mut Body<'tcx>,
    semantic_scope_rewrite: bool,
    semantic_scope_abi: Option<SemanticScopeAbi>,
    semantic_scope_local_abi: Option<SemanticScopeAbi>,
    semantic_ownership_transfer_abi: Option<SemanticOwnershipTransferAbi>,
    records: &mut Vec<RewriteRecord>,
) -> SemanticLocalOwnershipProof {
    let function_name = tcx.def_path_str(def_id);
    let cross_thread_escape = body_contains_cross_thread_escape_call(body);
    let cross_thread_escape_heap_object_types = if cross_thread_escape {
        body_cross_thread_escape_heap_object_types(tcx, body)
    } else {
        BTreeSet::new()
    };
    let (ownership_transfer_blocks, exact_zip_drop_pairs) =
        record_or_rewrite_semantic_ownership_transfers(
            tcx,
            def_id,
            body,
            semantic_scope_rewrite,
            semantic_ownership_transfer_abi,
            records,
        );
    let mut candidate_blocks = Vec::new();
    for (bb, data) in body_basic_blocks!(body).iter_enumerated() {
        if ownership_transfer_blocks.contains(&bb) {
            continue;
        }
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
        let (callee_def_id, callee_generic_types) = match func.ty(&body.local_decls, tcx).kind() {
            ty::FnDef(def_id, args) => (Some(*def_id), args.types().collect::<Vec<Ty<'tcx>>>()),
            _ => (None, Vec::new()),
        };
        let destination_ty = destination.ty(&body.local_decls, tcx).ty;
        let arg_operands = call_arg_operands(args);
        let argument_tys = arg_operands
            .iter()
            .map(|arg| arg.ty(&body.local_decls, tcx))
            .collect::<Vec<_>>();
        let mut plain_clone_heap_class =
            plain_clone_heap_class(tcx, callee_def_id, &callee, destination_ty);
        if !matches!(&plain_clone_heap_class, PlainCloneHeapClass::NotPlainClone) {
            // The pinned structural proof admits only core Clone::clone for
            // exact Global-backed byte Vec and String. Their standard Clone
            // implementations create at most the direct returned buffer and
            // preserve the textual owner identity used by the matching Drop.
            // Every broader/custom Clone surface stays explicit audit-only.
            plain_clone_heap_class = exact_core_clone_direct_owner(
                tcx,
                callee_def_id,
                &callee_generic_types,
                destination_ty,
                &argument_tys,
            )
            .map(PlainCloneHeapClass::Single)
            .unwrap_or(PlainCloneHeapClass::Unresolved);
        }
        let non_plain_heap_class =
            if matches!(&plain_clone_heap_class, PlainCloneHeapClass::NotPlainClone) {
                Some(non_plain_semantic_scope_heap_class(
                    tcx,
                    callee_def_id,
                    &callee_generic_types,
                    destination_ty,
                    &argument_tys,
                    &callee,
                ))
            } else {
                None
            };
        let runtime_identity_owner_ty = callee_def_id
            .and_then(|def_id| {
                vec_with_capacity_runtime_identity_owner_ty(
                    tcx,
                    def_id,
                    &callee_generic_types,
                    destination_ty,
                    &argument_tys,
                )
            })
            .or_else(|| {
                callee_def_id.and_then(|def_id| {
                    box_new_runtime_identity_owner_ty(tcx, def_id, destination_ty, &argument_tys)
                })
            })
            .or_else(|| {
                callee_def_id.and_then(|def_id| {
                    drop_inert_box_reclaim_call_runtime_identity_owner_ty(
                        tcx,
                        def_id,
                        &argument_tys,
                    )
                })
            });
        if target.is_none()
            || (matches!(&plain_clone_heap_class, PlainCloneHeapClass::NotPlainClone)
                && runtime_identity_owner_ty.is_none()
                && !semantic_scope_candidate_for_mir(tcx, callee_def_id, &callee, destination_ty))
        {
            continue;
        }
        let destination_type = format!("{:?}", destination_ty);
        let argument_types = argument_tys
            .iter()
            .map(|arg_ty| format!("{:?}", arg_ty))
            .collect::<Vec<_>>();
        let semantic_object_type = if let Some(owner_ty) = runtime_identity_owner_ty {
            format!("{:?}", owner_ty)
        } else {
            match &plain_clone_heap_class {
                PlainCloneHeapClass::Single(owner) => owner.clone(),
                PlainCloneHeapClass::DefiniteNoSupportedOwner
                | PlainCloneHeapClass::Ambiguous(_)
                | PlainCloneHeapClass::Unresolved => UNKNOWN_HEAP_OBJECT_TYPE.to_string(),
                PlainCloneHeapClass::NotPlainClone => match &non_plain_heap_class {
                    Some(SemanticScopeHeapClass::Single(owner)) => owner.clone(),
                    Some(SemanticScopeHeapClass::Ambiguous(_))
                    | Some(SemanticScopeHeapClass::CallbackCapable)
                    | Some(SemanticScopeHeapClass::Unresolved)
                    | None => UNKNOWN_HEAP_OBJECT_TYPE.to_string(),
                },
            }
        };
        let receiver_owned_allocation =
            semantic_scope_receiver_mutating_allocation_like_call(&callee);
        let (feature_owner, feature_owner_basis) = semantic_feature_owner_place(
            tcx,
            body,
            *destination,
            &arg_operands,
            receiver_owned_allocation,
        );
        let compiler_type_id = runtime_identity_owner_ty
            .or_else(|| {
                exact_scope_owner_ty_for_display(
                    tcx,
                    &semantic_object_type,
                    destination_ty,
                    &argument_tys,
                )
            })
            .and_then(|owner_ty| compiler_semantic_type_id(tcx, owner_ty));
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
            compiler_type_id,
            runtime_identity_owner_ty,
            plain_clone_heap_class,
            non_plain_heap_class,
            original_target: *target,
            original_unwind,
            original_from_hir_call: from_hir_call,
            fn_span: *fn_span,
            original_terminator: terminator.clone(),
            feature_owner,
            feature_owner_basis,
            receiver_owned_allocation,
            lifetime_analysis_features: None,
        });
    }

    for candidate in &mut candidate_blocks {
        candidate.lifetime_analysis_features =
            Some(semantic_lifetime_feature_export(tcx, body, candidate));
    }

    // The local ABI is a per-owner optimization, not a per-type heuristic.
    // Authorize the type group only when every allocation destination follows
    // one non-cleanup normal path, is not moved or passed to a call, and reaches
    // its exact destination-matched Drop before any normal exit.
    let mut local_ownership = exact_semantic_local_ownership_proof(tcx, body, &candidate_blocks);
    local_ownership.exact_zip_drop_pairs = exact_zip_drop_pairs;

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
        compiler_type_id,
        runtime_identity_owner_ty,
        plain_clone_heap_class,
        non_plain_heap_class,
        original_target,
        original_unwind,
        original_from_hir_call,
        fn_span,
        mut original_terminator,
        feature_owner: _,
        feature_owner_basis: _,
        receiver_owned_allocation: _,
        lifetime_analysis_features,
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
        // Every exact Global Box constructor/reclaim route still uses the
        // monomorphized runtime helper when rewriting.  A concrete owner Ty can
        // also expose the exact runtime-equivalent compiler identity to the
        // audit/profile key, which lets runtime ground truth join this site.
        // Generic definition-level MIR keeps type_id=0 until monomorphization;
        // textual `K`/`V`/`T` placeholders never become allocator authority.
        let (type_id, type_id_basis) =
            if runtime_identity_owner_ty.is_some() && compiler_type_id.is_none() {
                (0, "monomorphized_compiler_type_id_runtime")
            } else {
                semantic_scope_type_id(compiler_type_id)
            };
        let policy_flags = lowering_policy_flags();
        let module_id = lowering_module_id();
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
                module_id: lowering_module_id(),
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_skipped_candidate",
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
                semantic_object_type: semantic_object_type.clone(),
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
                lifetime_analysis_features: lifetime_analysis_features.clone(),
            });
            continue;
        }
        let ambiguous_owners = match (&plain_clone_heap_class, &non_plain_heap_class) {
            (PlainCloneHeapClass::Ambiguous(owners), _) => Some((owners, true)),
            (_, Some(SemanticScopeHeapClass::Ambiguous(owners))) => Some((owners, false)),
            _ => None,
        };
        if let Some((owners, plain_clone)) = ambiguous_owners {
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-scope-ambiguous:{:016x}",
                    callsite
                ),
                type_id,
                module_id: lowering_module_id(),
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_skipped_candidate",
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
                semantic_object_type: semantic_object_type.clone(),
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
                replacement_preview: if plain_clone {
                    format!(
                        "Skipped semantic-scope lowering because the Clone result has multiple supported heap owners: {}",
                        owners.join(", ")
                    )
                } else {
                    format!(
                        "Skipped semantic-scope lowering because the selected MIR receiver/destination has multiple supported heap owners: {}",
                        owners.join(", ")
                    )
                },
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "audit_only_ambiguous_heap_object_type",
                lowering_kind: "semantic_scope_unsolved_heap_object_candidate",
                lifetime_analysis_features: lifetime_analysis_features.clone(),
            });
            continue;
        }
        if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE
            || (runtime_identity_owner_ty.is_none() && compiler_type_id.is_none())
        {
            // Do not turn non-heap iterator/Bencher/helper calls into typed
            // allocation evidence.  Keep an explicit audit row so the solver
            // gap is visible instead of silently disappearing from reports.
            let callback_capable = matches!(
                &non_plain_heap_class,
                Some(SemanticScopeHeapClass::CallbackCapable)
            );
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-scope-{}:{:016x}",
                    if callback_capable {
                        "callback-capable"
                    } else {
                        "unsolved"
                    },
                    callsite
                ),
                type_id,
                module_id: lowering_module_id(),
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_skipped_candidate",
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
                semantic_object_type: semantic_object_type.clone(),
                type_id_basis,
                size_operand: None,
                align_operand: None,
                rewrite_status: if callback_capable {
                    "semantic_scope_rewrite_skipped_callback_capable_receiver"
                } else {
                    "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
                },
                replacement_symbol: if lowering_metadata_hints_requested() {
                    "__unialloc_semantic_scope_push_hints"
                } else {
                    "__unialloc_semantic_scope_push"
                },
                replacement_resolution_status: if callback_capable {
                    "exact_receiver_call_callback_capable_not_lowered"
                } else if semantic_object_type != UNKNOWN_HEAP_OBJECT_TYPE {
                    "rustc_exact_type_identity_not_solved"
                } else {
                    "rustc_middle_heap_object_type_not_solved"
                },
                replacement_preview: if callback_capable {
                    "Skipped semantic-scope lowering because the exact receiver call may execute user callbacks with unrelated allocations"
                        .to_string()
                } else if semantic_object_type != UNKNOWN_HEAP_OBJECT_TYPE {
                    "Skipped semantic-scope lowering because no exact compiler TypeId identity was recovered for the displayed heap owner"
                        .to_string()
                } else {
                    "Skipped semantic-scope lowering because rustc_middle did not solve a supported heap object type"
                        .to_string()
                },
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: if callback_capable {
                    "audit_only_callback_capable_receiver"
                } else {
                    "audit_only_unresolved_heap_object_type"
                },
                lowering_kind: if callback_capable {
                    "semantic_scope_callback_capable_receiver_skipped"
                } else {
                    "semantic_scope_unsolved_heap_object_candidate"
                },
                lifetime_analysis_features: lifetime_analysis_features.clone(),
            });
            continue;
        }
        let proven_local_pair = local_ownership
            .allocation_pairs
            .contains(&(semantic_object_type.clone(), destination_place.clone()));
        let automatic_lifetime_decision = local_ownership
            .automatic_lifetime_decisions
            .get(&(semantic_object_type.clone(), destination_place.clone()))
            .copied();
        let automatic_heap_lifetime_decision = local_ownership
            .automatic_heap_lifetime_decisions
            .get(&(semantic_object_type.clone(), destination_place.clone()))
            .copied();
        // The CFG classifier does not depend on the numeric type-id namespace.
        // Runtime compiler-TypeId scopes may therefore retain its result. An
        // explicit profile remains fail-closed because type_id=0 cannot match.
        let lifetime_selection = lowering_lifetime_hint_for_site(
            callsite,
            type_id,
            module_id,
            automatic_lifetime_decision,
            automatic_heap_lifetime_decision,
            lifetime_analysis_features.as_ref(),
        );
        let lifetime_hint = lifetime_selection.hint;
        let lifetime_hint_confidence = lifetime_selection.confidence;
        let lifetime_hint_basis = lifetime_selection.basis;
        let drop_requires_allocation_recovery = runtime_identity_owner_ty
            .map(|owner_ty| exact_box_drop_requires_allocation_recovery(tcx, owner_ty))
            .unwrap_or(false);
        let exact_local_pair = direct_local_size_align_with_semantic_drop_requested()
            && proven_local_pair
            && !drop_requires_allocation_recovery
            && local_no_recovery_lifetime_source_safe();
        let selected_semantic_scope_abi = if exact_local_pair {
            semantic_scope_local_abi.or(semantic_scope_abi)
        } else {
            semantic_scope_abi
        };
        let scope_uses_local_no_recovery = selected_semantic_scope_abi
            .map(|scope_abi| scope_abi.local_no_recovery)
            .unwrap_or(exact_local_pair);
        let (placement_hint, cross_thread_recovery_hint, placement_hint_basis) =
            lowering_semantic_scope_placement_hint_for_body(
                candidate_cross_thread_escape,
                scope_uses_local_no_recovery,
            );
        let mut rewrite_status = if runtime_identity_owner_ty.is_some() {
            "semantic_scope_generic_type_rewrite_planned"
        } else {
            "semantic_scope_enter_exit_rewrite_planned"
        };
        let mut replacement_resolution_status = "not_requested_dry_run";
        let mut replacement_preview = format!(
            "Wrap {} with semantic scope metadata(type_id={}, module_id={}, flags={}, lifetime_hint={}, placement_hint={}, callsite={}) / __unialloc_semantic_scope_pop(); semantic_object_type={}",
            callee,
            type_id,
            lowering_module_id(),
            policy_flags,
            lifetime_hint,
            placement_hint,
            callsite,
            semantic_object_type
        );
        let mut semantic_scope_unwind_pop_inserted = false;
        if semantic_scope_rewrite {
            let resolved_scope_abi = selected_semantic_scope_abi.filter(|scope_abi| {
                runtime_identity_owner_ty.is_none() || scope_abi.generic_push_def_id.is_some()
            });
            if let (Some(scope_abi), Some(original_target)) = (resolved_scope_abi, original_target)
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
                    original_unwind,
                    original_from_hir_call,
                    original_is_cleanup,
                );

                let unwind_continuation = semantic_scope_unwind_continuation(
                    body,
                    source_info,
                    original_unwind,
                    original_is_cleanup,
                );
                let cleanup_exit_block = unwind_continuation.map(|cleanup_target| {
                    push_semantic_scope_pop_block(
                        tcx,
                        body,
                        scope_abi.pop_def_id,
                        source_info,
                        fn_span,
                        cleanup_target,
                        no_unwind(),
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

                let mut push_args = Vec::new();
                if runtime_identity_owner_ty.is_none() {
                    push_args.push(const_u64_operand(tcx, type_id, fn_span));
                }
                push_args.push(const_u64_operand(tcx, lowering_module_id(), fn_span));
                push_args.push(const_u32_operand(tcx, policy_flags, fn_span));
                if scope_abi.supports_hints {
                    push_args.push(const_u16_operand(tcx, lifetime_hint, fn_span));
                    push_args.push(const_u16_operand(tcx, placement_hint, fn_span));
                }
                push_args.push(const_u64_operand(tcx, callsite, fn_span));
                body[bb].terminator_mut().kind = if let Some(owner_ty) = runtime_identity_owner_ty {
                    call_terminator_kind_with_type(
                        tcx,
                        scope_abi
                            .generic_push_def_id
                            .expect("generic scope ABI prevalidated"),
                        owner_ty,
                        push_args,
                        push_unit_place,
                        Some(call_block),
                        inserted_call_unwind(original_unwind, original_is_cleanup),
                        original_from_hir_call,
                        fn_span,
                    )
                } else {
                    call_terminator_kind(
                        tcx,
                        scope_abi.push_def_id,
                        push_args,
                        push_unit_place,
                        Some(call_block),
                        inserted_call_unwind(original_unwind, original_is_cleanup),
                        original_from_hir_call,
                        fn_span,
                    )
                };
                rewrite_status = if runtime_identity_owner_ty.is_some() {
                    "actual_semantic_scope_generic_type_rewrite_applied"
                } else {
                    "actual_semantic_scope_enter_exit_rewrite_applied"
                };
                replacement_resolution_status = if runtime_identity_owner_ty.is_some() {
                    generic_semantic_scope_resolution_status(
                        scope_abi.supports_hints,
                        scope_abi.local_no_recovery,
                    )
                } else {
                    semantic_scope_resolution_status(scope_abi)
                };
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
                rewrite_status = if runtime_identity_owner_ty.is_some() {
                    "actual_semantic_scope_generic_type_rewrite_requested_symbol_unresolved"
                } else {
                    "actual_semantic_scope_enter_exit_rewrite_requested_symbol_unresolved"
                };
                replacement_resolution_status = if runtime_identity_owner_ty.is_some() {
                    generic_semantic_scope_unresolved_status(
                        lowering_metadata_hints_requested(),
                        exact_local_pair,
                    )
                } else {
                    semantic_scope_unresolved_status(exact_local_pair)
                };
            }
        }

        records.push(RewriteRecord {
            allocation_site_id: format!("rustc-driver-mir-semantic-scope:{:016x}", callsite),
            type_id,
            module_id: lowering_module_id(),
            flags: policy_flags,
            lifetime_hint,
            lifetime_hint_confidence,
            lifetime_hint_basis,
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
            replacement_symbol: if runtime_identity_owner_ty.is_some() {
                generic_semantic_scope_push_symbol(
                    selected_semantic_scope_abi
                        .map(|abi| abi.local_no_recovery)
                        .unwrap_or(exact_local_pair),
                )
            } else {
                selected_semantic_scope_abi
                    .map(|abi| abi.push_symbol)
                    .unwrap_or_else(|| semantic_scope_push_symbol(exact_local_pair))
            },
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted,
            metadata_pairing_contract: if runtime_identity_owner_ty.is_some()
                && scope_uses_local_no_recovery
            {
                "semantic_scope_monomorphized_runtime_type_metadata_local_no_recovery"
            } else if runtime_identity_owner_ty.is_some() {
                "semantic_scope_monomorphized_runtime_type_metadata"
            } else {
                "semantic_scope_active_metadata"
            },
            lowering_kind: "semantic_scope_enter_exit_rewrite",
            lifetime_analysis_features,
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
    local_ownership: &SemanticLocalOwnershipProof,
    exact_zip_drop_pairs: &BTreeSet<(String, String)>,
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
        let drop_place = format!("{:?}", place);
        let drop_type = format!("{:?}", place_ty);
        let runtime_identity_owner_ty = drop_inert_box_runtime_identity_owner_ty(tcx, place_ty);
        let recovery_only_exact_box_drop =
            exact_box_drop_requires_allocation_recovery(tcx, place_ty);
        let exact_zip_owner = exact_zip_drop_pairs
            .contains(&(format!("{:?}", bb), drop_place.clone()))
            .then(|| exact_zip_iter_mut_u8_into_iter_drop_owner(tcx, place_ty))
            .flatten();
        let (drop_owner_graph_unresolved, heap_owner_types) =
            if runtime_identity_owner_ty.is_some() || recovery_only_exact_box_drop {
                // This exact Global Box is handled by the compiler-TypeId runtime
                // helper below for both generic and concrete MIR. The owner graph
                // therefore cannot introduce a second textual identity namespace.
                (false, BTreeSet::new())
            } else {
                match exact_zip_owner {
                    Some(owner_ty) => match compiler_heap_owner(tcx, owner_ty) {
                        Some(owner) => (false, BTreeSet::from([owner])),
                        None => (true, BTreeSet::new()),
                    },
                    None => {
                        let heap_owner_scan = heap_object_type_scan_from_ty(tcx, place_ty);
                        (heap_owner_scan.unresolved, heap_owner_scan.owners)
                    }
                }
            };
        let direct_supported_owner = direct_supported_heap_object_destination(tcx, place_ty);
        let recovery_only_effectful_owner_drop = !recovery_only_exact_box_drop
            && exact_zip_owner.is_none()
            && !drop_owner_graph_unresolved
            && heap_owner_types.len() == 1
            && if direct_supported_owner {
                !supported_owner_uses_canonical_global_allocator(tcx, place_ty)
                    || direct_owner_generic_drop_glue_may_be_effectful(tcx, place_ty)
            } else {
                // A wrapper's own Drop implementation can run arbitrary code
                // before releasing its single nested owner. Keep such scopes
                // recovery-backed even when the nested owner graph is unique.
                true
            };
        let drop_type_has_multiple_heap_owners =
            !drop_owner_graph_unresolved && heap_owner_types.len() > 1;
        let semantic_object_type = if let Some(owner_ty) = runtime_identity_owner_ty {
            format!("{:?}", owner_ty)
        } else if recovery_only_exact_box_drop {
            format!("{:?}", place_ty)
        } else if drop_owner_graph_unresolved {
            UNKNOWN_HEAP_OBJECT_TYPE.to_string()
        } else if drop_type_has_multiple_heap_owners {
            format!(
                "multiple_heap_owners({})",
                heap_owner_types
                    .iter()
                    .map(|owner| owner.display.clone())
                    .collect::<Vec<_>>()
                    .join(",")
            )
        } else {
            heap_owner_types
                .iter()
                .next()
                .map(|owner| owner.display.clone())
                .unwrap_or_else(|| UNKNOWN_HEAP_OBJECT_TYPE.to_string())
        };
        let compiler_type_id = runtime_identity_owner_ty
            .and_then(|owner_ty| compiler_semantic_type_id(tcx, owner_ty))
            .or_else(|| {
                (!recovery_only_exact_box_drop
                    && !recovery_only_effectful_owner_drop
                    && heap_owner_types.len() == 1)
                    .then(|| heap_owner_types.iter().next().map(|owner| owner.type_id))
                    .flatten()
            });
        let drop_type_has_generic_param = type_contains_generic_param(tcx, place_ty);
        candidate_blocks.push(SemanticDropCandidate {
            bb,
            original_is_cleanup: data.is_cleanup,
            drop_place,
            drop_type,
            semantic_object_type,
            compiler_type_id,
            runtime_identity_owner_ty,
            recovery_only_exact_box_drop,
            recovery_only_effectful_owner_drop,
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
        compiler_type_id,
        runtime_identity_owner_ty,
        recovery_only_exact_box_drop,
        recovery_only_effectful_owner_drop,
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
        let (type_id, type_id_basis) =
            if runtime_identity_owner_ty.is_some() && compiler_type_id.is_none() {
                (0, "monomorphized_compiler_type_id_runtime")
            } else if recovery_only_exact_box_drop || recovery_only_effectful_owner_drop {
                (0, "authenticated_allocation_recovery_record")
            } else {
                semantic_scope_type_id(compiler_type_id)
            };
        let policy_flags = lowering_policy_flags();
        let module_id = lowering_module_id();
        if recovery_only_exact_box_drop || recovery_only_effectful_owner_drop {
            // A dropful payload or wrapper may execute arbitrary user code
            // before the backing storage is reclaimed. Wrapping the entire
            // Drop would tag inner frees with the outer owner identity. Leave
            // the Drop unwrapped and use allocation-side pointer recovery.
            let recovery_kind = if recovery_only_exact_box_drop {
                "exact-box"
            } else {
                "effectful-owner"
            };
            records.push(RewriteRecord {
                allocation_site_id: format!(
                    "rustc-driver-mir-semantic-drop-{}-recovery:{:016x}",
                    recovery_kind, callsite
                ),
                type_id,
                module_id,
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_recovery_authoritative",
                placement_hint: 0,
                cross_thread_recovery_hint: false,
                placement_hint_basis: "allocation_recovery_record_authoritative",
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
                rewrite_status: if recovery_only_exact_box_drop {
                    "semantic_scope_drop_rewrite_skipped_exact_box_recovery"
                } else {
                    "semantic_scope_drop_rewrite_skipped_effectful_owner_recovery"
                },
                replacement_symbol: "authenticated_allocation_recovery_record",
                replacement_resolution_status: if recovery_only_exact_box_drop {
                    "exact_box_drop_uses_authenticated_allocation_recovery"
                } else {
                    "effectful_owner_drop_uses_authenticated_allocation_recovery"
                },
                replacement_preview: if recovery_only_exact_box_drop {
                    "Leave dropful exact Box Drop unwrapped; recover the outer allocation identity from its authenticated allocation record"
                } else {
                    "Leave effectful owner Drop unwrapped; recover backing allocations from authenticated allocation records"
                }
                .to_string(),
                semantic_scope_unwind_pop_inserted: false,
                metadata_pairing_contract: "allocation_scope_to_authenticated_recovery_record",
                lowering_kind: if recovery_only_exact_box_drop {
                    "semantic_scope_drop_exact_box_recovery_skipped"
                } else {
                    "semantic_scope_drop_effectful_owner_recovery_skipped"
                },
                lifetime_analysis_features: None,
            });
            continue;
        }
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
                module_id: lowering_module_id(),
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_skipped_candidate",
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
                lifetime_analysis_features: None,
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
        if semantic_object_type == UNKNOWN_HEAP_OBJECT_TYPE
            || (runtime_identity_owner_ty.is_none() && compiler_type_id.is_none())
        {
            let (
                allocation_site_prefix,
                rewrite_status,
                replacement_resolution_status,
                replacement_preview,
                metadata_pairing_contract,
                lowering_kind,
            ) = if semantic_object_type != UNKNOWN_HEAP_OBJECT_TYPE {
                (
                    "rustc-driver-mir-semantic-drop-unresolved-identity",
                    "semantic_scope_drop_rewrite_skipped_unresolved_type_identity",
                    "rustc_exact_drop_type_identity_not_solved",
                    format!(
                        "Skipped drop-scope lowering for {} because no exact compiler TypeId identity was recovered for displayed owner `{}`",
                        drop_place, semantic_object_type
                    ),
                    "audit_only_unresolved_drop_type_identity",
                    "semantic_scope_drop_unresolved_type_identity_skipped",
                )
            } else if drop_type_has_generic_param {
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
                module_id: lowering_module_id(),
                flags: policy_flags,
                lifetime_hint: 0,
                lifetime_hint_confidence: 0,
                lifetime_hint_basis: "not_applicable_skipped_candidate",
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
                lifetime_analysis_features: None,
            });
            continue;
        }

        let pair = (semantic_object_type.clone(), drop_place.clone());
        let proven_local_pair = local_ownership.drop_pairs.contains(&pair);
        let automatic_lifetime_decision = local_ownership
            .automatic_lifetime_decisions
            .get(&pair)
            .copied();
        let automatic_heap_lifetime_decision = local_ownership
            .automatic_heap_lifetime_decisions
            .get(&pair)
            .copied();
        let lifetime_selection = lowering_lifetime_hint_for_site(
            callsite,
            type_id,
            module_id,
            automatic_lifetime_decision,
            automatic_heap_lifetime_decision,
            None,
        );
        let lifetime_hint = lifetime_selection.hint;
        let lifetime_hint_confidence = lifetime_selection.confidence;
        let lifetime_hint_basis = lifetime_selection.basis;
        let exact_local_pair = direct_local_size_align_with_semantic_drop_requested()
            && proven_local_pair
            && local_no_recovery_lifetime_source_safe();
        let selected_semantic_scope_abi = if exact_local_pair {
            semantic_scope_local_abi.or(semantic_scope_abi)
        } else {
            semantic_scope_abi
        };
        let scope_uses_local_no_recovery = selected_semantic_scope_abi
            .map(|scope_abi| scope_abi.local_no_recovery)
            .unwrap_or(exact_local_pair);
        let (placement_hint, cross_thread_recovery_hint, placement_hint_basis) =
            lowering_semantic_scope_placement_hint_for_body(
                candidate_cross_thread_escape,
                scope_uses_local_no_recovery,
            );
        let mut rewrite_status = if runtime_identity_owner_ty.is_some() {
            "semantic_scope_drop_generic_type_rewrite_planned"
        } else {
            "semantic_scope_drop_rewrite_planned"
        };
        let mut replacement_resolution_status = "not_requested_dry_run";
        let mut replacement_preview = format!(
            "Wrap Drop of {} with semantic scope metadata(type_id={}, module_id={}, flags={}, lifetime_hint={}, placement_hint={}, callsite={}) / __unialloc_semantic_scope_pop(); semantic_object_type={}",
            drop_place,
            type_id,
            lowering_module_id(),
            policy_flags,
            lifetime_hint,
            placement_hint,
            callsite,
            semantic_object_type
        );
        let mut semantic_scope_unwind_pop_inserted = false;
        if semantic_scope_rewrite {
            let resolved_scope_abi = selected_semantic_scope_abi.filter(|scope_abi| {
                runtime_identity_owner_ty.is_none() || scope_abi.generic_push_def_id.is_some()
            });
            if let Some(scope_abi) = resolved_scope_abi {
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
                    original_unwind,
                    synthetic_call_source(),
                    original_is_cleanup,
                );

                let unwind_continuation = semantic_scope_unwind_continuation(
                    body,
                    source_info,
                    original_unwind,
                    original_is_cleanup,
                );
                let cleanup_exit_block = unwind_continuation.map(|cleanup_target| {
                    push_semantic_scope_pop_block(
                        tcx,
                        body,
                        scope_abi.pop_def_id,
                        source_info,
                        fn_span,
                        cleanup_target,
                        no_unwind(),
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

                let mut push_args = Vec::new();
                if runtime_identity_owner_ty.is_none() {
                    push_args.push(const_u64_operand(tcx, type_id, fn_span));
                }
                push_args.push(const_u64_operand(tcx, lowering_module_id(), fn_span));
                push_args.push(const_u32_operand(tcx, policy_flags, fn_span));
                if scope_abi.supports_hints {
                    push_args.push(const_u16_operand(tcx, lifetime_hint, fn_span));
                    push_args.push(const_u16_operand(tcx, placement_hint, fn_span));
                }
                push_args.push(const_u64_operand(tcx, callsite, fn_span));
                body[bb].terminator_mut().kind = if let Some(owner_ty) = runtime_identity_owner_ty {
                    call_terminator_kind_with_type(
                        tcx,
                        scope_abi
                            .generic_push_def_id
                            .expect("generic drop scope ABI prevalidated"),
                        owner_ty,
                        push_args,
                        push_unit_place,
                        Some(drop_block),
                        inserted_call_unwind(original_unwind, original_is_cleanup),
                        synthetic_call_source(),
                        fn_span,
                    )
                } else {
                    call_terminator_kind(
                        tcx,
                        scope_abi.push_def_id,
                        push_args,
                        push_unit_place,
                        Some(drop_block),
                        inserted_call_unwind(original_unwind, original_is_cleanup),
                        synthetic_call_source(),
                        fn_span,
                    )
                };
                rewrite_status = if runtime_identity_owner_ty.is_some() {
                    "actual_semantic_scope_drop_generic_type_rewrite_applied"
                } else {
                    "actual_semantic_scope_drop_rewrite_applied"
                };
                replacement_resolution_status = if runtime_identity_owner_ty.is_some() {
                    generic_semantic_scope_resolution_status(
                        scope_abi.supports_hints,
                        scope_abi.local_no_recovery,
                    )
                } else {
                    semantic_scope_resolution_status(scope_abi)
                };
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
                rewrite_status = if runtime_identity_owner_ty.is_some() {
                    "actual_semantic_scope_drop_generic_type_rewrite_requested_symbol_unresolved"
                } else {
                    "actual_semantic_scope_drop_rewrite_requested_symbol_unresolved"
                };
                replacement_resolution_status = if runtime_identity_owner_ty.is_some() {
                    generic_semantic_scope_unresolved_status(
                        lowering_metadata_hints_requested(),
                        exact_local_pair,
                    )
                } else {
                    semantic_scope_unresolved_status(exact_local_pair)
                };
            }
        }

        records.push(RewriteRecord {
            allocation_site_id: format!("rustc-driver-mir-semantic-drop:{:016x}", callsite),
            type_id,
            module_id: lowering_module_id(),
            flags: policy_flags,
            lifetime_hint,
            lifetime_hint_confidence,
            lifetime_hint_basis,
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
            replacement_symbol: if runtime_identity_owner_ty.is_some() {
                generic_semantic_scope_push_symbol(
                    selected_semantic_scope_abi
                        .map(|abi| abi.local_no_recovery)
                        .unwrap_or(exact_local_pair),
                )
            } else {
                selected_semantic_scope_abi
                    .map(|abi| abi.push_symbol)
                    .unwrap_or_else(|| semantic_scope_push_symbol(exact_local_pair))
            },
            replacement_resolution_status,
            replacement_preview,
            semantic_scope_unwind_pop_inserted,
            metadata_pairing_contract: if runtime_identity_owner_ty.is_some()
                && scope_uses_local_no_recovery
            {
                "semantic_scope_drop_monomorphized_runtime_type_metadata_local_no_recovery"
            } else if runtime_identity_owner_ty.is_some() {
                "semantic_scope_drop_monomorphized_runtime_type_metadata"
            } else {
                "semantic_scope_drop_active_metadata"
            },
            lowering_kind: "semantic_scope_drop_rewrite",
            lifetime_analysis_features: None,
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
    // Marker-free lifetime scopes are inserted in the post-borrowck,
    // pre-optimization provider below. Returning the normally optimized body
    // here preserves rustc's release MIR pipeline while avoiding a second
    // instrumentation/audit pass over the already-instrumented function.
    #[cfg(unialloc_rustc_current)]
    if marker_free_heap_preoptimization_rewrite_enabled() {
        return original;
    }
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
    let semantic_ownership_transfer_abi = if semantic_scope_rewrite {
        resolve_unialloc_semantic_ownership_transfer(tcx)
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
                    semantic_ownership_transfer_abi,
                    &mut guard,
                );
            }
        }
    }
    tcx.arena.alloc(cloned)
}

#[cfg(unialloc_rustc_current)]
fn marker_free_heap_preoptimization_rewrite_enabled() -> bool {
    unsafe {
        (AUTO_HEAP_LIFETIME_INFERENCE || AUTO_RUST_LIFETIME_PRIOR)
            && ACTUAL_SEMANTIC_SCOPE_REWRITE
            && !ACTUAL_MIR_REWRITE
    }
}

/// Instrument exact marker-free ownership while the MIR still contains the
/// source-level constructor and consuming call. MIR optimization level 2+
/// inlines Vec constructors, mem::forget, and Box::leak, erasing precisely the
/// DefId/owner edges needed by the bounded proof. This provider runs after
/// borrow checking and drop elaboration, then returns the same `Steal` cache to
/// rustc so the ordinary release optimization pipeline remains unchanged.
#[cfg(unialloc_rustc_current)]
#[cfg_attr(unialloc_rustc_current, allow(static_mut_refs))]
fn mir_drops_elaborated_with_marker_free_heap_rewrite<'tcx>(
    tcx: TyCtxt<'tcx>,
    def_id: LocalDefId,
) -> &'tcx Steal<Body<'tcx>> {
    let original = unsafe {
        ORIGINAL_MIR_DROPS_ELABORATED_AND_CONST_CHECKED
            .expect("original mir_drops_elaborated_and_const_checked provider not installed")(
            tcx, def_id,
        )
    };
    if !marker_free_heap_preoptimization_rewrite_enabled() {
        return original;
    }

    let semantic_scope_abi = resolve_unialloc_semantic_scope(tcx, false);
    let semantic_scope_local_abi = if direct_local_size_align_with_semantic_drop_requested() {
        resolve_unialloc_semantic_scope(tcx, true)
    } else {
        None
    };
    let semantic_ownership_transfer_abi = resolve_unialloc_semantic_ownership_transfer(tcx);
    let mut body = original.risky_hack_borrow_mut();
    unsafe {
        if let Some(records) = &RECORDS {
            if let Ok(mut guard) = records.lock() {
                record_or_rewrite_candidates(
                    tcx,
                    def_id.to_def_id(),
                    &mut body,
                    false,
                    None,
                    true,
                    semantic_scope_abi,
                    semantic_scope_local_abi,
                    semantic_ownership_transfer_abi,
                    &mut guard,
                );
            }
        }
    }
    drop(body);
    original
}

impl Callbacks for RewriteDryRunCallbacks {
    #[cfg(unialloc_rustc_current)]
    fn config(&mut self, config: &mut interface::Config) {
        config.override_queries = Some(|_sess, providers: &mut Providers| {
            unsafe {
                ORIGINAL_OPTIMIZED_MIR = Some(providers.queries.optimized_mir);
                ORIGINAL_MIR_DROPS_ELABORATED_AND_CONST_CHECKED =
                    Some(providers.queries.mir_drops_elaborated_and_const_checked);
            }
            providers.queries.optimized_mir = optimized_mir_with_rewrite_dry_run;
            providers.queries.mir_drops_elaborated_and_const_checked =
                mir_drops_elaborated_with_marker_free_heap_rewrite;
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
    let semantic_ownership_transfer_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_ownership_transfer_rewrite")
        .count();
    let semantic_ownership_transfer_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
        })
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
    let semantic_scope_callback_capable_skipped_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_callback_capable_receiver_skipped")
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
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_generic_type_rewrite_applied"
            )
        })
        .count();
    let semantic_scope_deallocation_like_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_generic_type_rewrite_applied"
            ) && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_drop_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_drop_rewrite_applied"
                    | "actual_semantic_scope_drop_generic_type_rewrite_applied"
            )
        })
        .count();
    let semantic_scope_unwind_pop_inserted_count = records
        .iter()
        .filter(|record| record.semantic_scope_unwind_pop_inserted)
        .count();
    let cross_thread_recovery_hint_count = records
        .iter()
        .filter(|record| record.cross_thread_recovery_hint)
        .count();
    let automatic_lifetime_ephemeral_count = records
        .iter()
        .filter(|record| automatic_lifetime_ephemeral_basis(record.lifetime_hint_basis))
        .count();
    let automatic_lifetime_long_lived_count = records
        .iter()
        .filter(|record| automatic_lifetime_long_lived_basis(record.lifetime_hint_basis))
        .count();
    let automatic_lifetime_unknown_count = records
        .iter()
        .filter(|record| automatic_lifetime_unknown_basis(record.lifetime_hint_basis))
        .count();
    let automatic_heap_exact_drop_fact_count = records
        .iter()
        .filter(|record| {
            record
                .lifetime_analysis_features
                .as_ref()
                .map(|features| features.exact_drop_path)
                .unwrap_or(false)
        })
        .count();
    let automatic_heap_bounded_process_long_oracle_count = records
        .iter()
        .filter(|record| {
            automatic_heap_lifetime_bounded_process_long_oracle_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_heap_unknown_count = records
        .iter()
        .filter(|record| automatic_heap_lifetime_unknown_basis(record.lifetime_hint_basis))
        .count();
    let automatic_lifetime_candidate_allocation_site_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_enter_exit_rewrite")
        .count();
    let automatic_heap_exact_drop_fact_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && record
                    .lifetime_analysis_features
                    .as_ref()
                    .map(|features| features.exact_drop_path)
                    .unwrap_or(false)
        })
        .count();
    let automatic_heap_bounded_process_long_oracle_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_heap_lifetime_bounded_process_long_oracle_basis(
                    record.lifetime_hint_basis,
                )
        })
        .count();
    let automatic_heap_unknown_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_heap_lifetime_unknown_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_rust_lifetime_prior_short_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_rust_lifetime_prior_short_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_rust_lifetime_prior_long_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_rust_lifetime_prior_long_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_rust_lifetime_prior_unknown_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_rust_lifetime_prior_unknown_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_eligible_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_eligible_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_ephemeral_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_ephemeral_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_long_lived_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_long_lived_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_unknown_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_unknown_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_unsupported_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_unsupported_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_abstained_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_unknown_basis(record.lifetime_hint_basis)
                && !automatic_lifetime_unsupported_basis(record.lifetime_hint_basis)
        })
        .count();
    let mut automatic_lifetime_unknown_allocation_site_reasons: BTreeMap<&str, usize> =
        BTreeMap::new();
    for record in records.iter().filter(|record| {
        record.lowering_kind == "semantic_scope_enter_exit_rewrite"
            && automatic_lifetime_unknown_basis(record.lifetime_hint_basis)
    }) {
        *automatic_lifetime_unknown_allocation_site_reasons
            .entry(record.lifetime_hint_basis)
            .or_insert(0) += 1;
    }
    let lifetime_profile_match_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_exact_match")
        .count();
    let lifetime_profile_abstention_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_below_confidence_threshold")
        .count();
    let lifetime_profile_miss_count = records
        .iter()
        .filter(|record| {
            record.lifetime_hint_basis.starts_with("profile_")
                && record.lifetime_hint_basis != "profile_exact_match"
                && record.lifetime_hint_basis != "profile_below_confidence_threshold"
        })
        .count();
    let lifetime_profile_guard_mismatch_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_type_or_module_guard_mismatch")
        .count();
    let lifetime_profile_ambiguous_match_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_ambiguous_entry")
        .count();
    let lifetime_profile_unique_entry_count = cli
        .lifetime_profile
        .as_ref()
        .map(|profile| {
            profile
                .entries
                .values()
                .filter(|entry| matches!(entry, LifetimeProfileEntry::Unique { .. }))
                .count()
        })
        .unwrap_or(0);
    let lifetime_profile_ambiguous_entry_count = cli
        .lifetime_profile
        .as_ref()
        .map(|profile| {
            profile
                .entries
                .values()
                .filter(|entry| matches!(entry, LifetimeProfileEntry::Ambiguous))
                .count()
        })
        .unwrap_or(0);
    let metadata_hints_requested = cli.lifetime_profile.is_some()
        || cli.lifetime_hint != 0
        || cli.auto_lifetime_classifier
        || cli.auto_heap_lifetime_inference
        || cli.auto_rust_lifetime_prior
        || cli.placement_hint != 0
        || cli.auto_cross_thread_recovery_hint;
    let local_semantic_scope_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_drop_rewrite_applied"
                    | "actual_semantic_scope_generic_type_rewrite_applied"
                    | "actual_semantic_scope_drop_generic_type_rewrite_applied"
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
                    | "actual_semantic_scope_generic_type_rewrite_applied"
                    | "actual_semantic_scope_drop_generic_type_rewrite_applied"
            ) && !record.replacement_symbol.ends_with("_local")
        })
        .count();
    let actual_allocator_call_replacement = cli.actual_rewrite && rewrite_applied_count > 0;
    let actual_semantic_scope_rewrite =
        cli.semantic_scope_rewrite && semantic_scope_rewrite_applied_count > 0;
    let actual_semantic_ownership_transfer_rewrite =
        cli.semantic_scope_rewrite && semantic_ownership_transfer_rewrite_applied_count > 0;
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
    let box_slice_into_vec_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_BOX_SLICE_INTO_VEC_SYMBOL
    });
    let vec_into_boxed_slice_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_VEC_INTO_BOXED_SLICE_SYMBOL
    });
    let string_into_bytes_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_STRING_INTO_BYTES_SYMBOL
    });
    let string_into_boxed_str_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_STRING_INTO_BOXED_STR_SYMBOL
    });
    let boxed_str_into_string_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_BOXED_STR_INTO_STRING_SYMBOL
    });
    let cstring_into_bytes_with_nul_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_CSTRING_INTO_BYTES_WITH_NUL_SYMBOL
    });
    let vec_into_iter_rewrite_applied = records.iter().any(|record| {
        record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
            && record.replacement_symbol == SEMANTIC_VEC_INTO_ITER_SYMBOL
    });
    let semantic_ownership_transfer_replacement_resolution_status = if !cli.semantic_scope_rewrite {
        "not_requested_dry_run"
    } else {
        match (
            box_slice_into_vec_rewrite_applied,
            vec_into_boxed_slice_rewrite_applied,
            string_into_bytes_rewrite_applied,
            string_into_boxed_str_rewrite_applied,
            boxed_str_into_string_rewrite_applied,
            cstring_into_bytes_with_nul_rewrite_applied,
            vec_into_iter_rewrite_applied,
        ) {
            (true, false, false, false, false, false, false) => {
                "resolved_unialloc_semantic_box_slice_into_vec"
            }
            (false, true, false, false, false, false, false) => {
                "resolved_unialloc_semantic_vec_into_boxed_slice"
            }
            (false, false, true, false, false, false, false) => {
                "resolved_unialloc_semantic_string_into_bytes"
            }
            (false, false, false, true, false, false, false) => {
                "resolved_unialloc_semantic_string_into_boxed_str"
            }
            (false, false, false, false, true, false, false) => {
                "resolved_unialloc_semantic_boxed_str_into_string"
            }
            (false, false, false, false, false, true, false) => {
                "resolved_unialloc_semantic_cstring_into_bytes_with_nul"
            }
            (false, false, false, false, false, false, true) => {
                "resolved_unialloc_semantic_vec_into_iter"
            }
            (false, false, false, false, false, false, false) => {
                "no_supported_semantic_ownership_transfer_candidates"
            }
            _ => "resolved_unialloc_semantic_ownership_transfers",
        }
    };
    let replacement_resolution_status = if cli.actual_rewrite {
        direct_replacement_resolution_status
    } else if cli.semantic_scope_rewrite {
        if semantic_scope_rewrite_applied_count > 0 {
            semantic_scope_replacement_resolution_status
        } else {
            semantic_ownership_transfer_replacement_resolution_status
        }
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
    let marker_free_heap_preoptimization_rewrite = cfg!(unialloc_rustc_current)
        && (cli.auto_heap_lifetime_inference || cli.auto_rust_lifetime_prior)
        && cli.semantic_scope_rewrite
        && !cli.actual_rewrite;
    let _ = writeln!(
        json,
        "    \"marker_free_heap_preoptimization_rewrite\": {},",
        marker_free_heap_preoptimization_rewrite
    );
    json.push_str(
        "    \"marker_free_heap_analysis_query\": \"mir_drops_elaborated_and_const_checked\",\n",
    );
    json.push_str("    \"release_mir_optimization_preserved\": true,\n");
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
    let _ = writeln!(
        json,
        "    \"actual_semantic_ownership_transfer_rewrite_requested\": {},",
        cli.semantic_scope_rewrite
    );
    let _ = writeln!(json, "    \"policy_flags\": {},", cli.policy_flags);
    let _ = writeln!(json, "    \"lifetime_hint\": {},", cli.lifetime_hint);
    let _ = writeln!(
        json,
        "    \"lifetime_profile_confidence_threshold\": {},",
        cli.lifetime_confidence_threshold
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_enabled\": {},",
        cli.lifetime_profile.is_some()
    );
    match &cli.lifetime_profile {
        Some(profile) => {
            let _ = writeln!(
                json,
                "    \"lifetime_profile_path\": \"{}\",",
                json_escape(&profile.path.display().to_string())
            );
            let _ = writeln!(
                json,
                "    \"lifetime_profile_digest\": \"{:016x}\",",
                profile.raw_digest
            );
            let _ = writeln!(
                json,
                "    \"lifetime_profile_format_valid\": {},",
                profile.format_valid
            );
            let _ = writeln!(
                json,
                "    \"lifetime_profile_source_entry_line_count\": {},",
                profile.source_entry_line_count
            );
            let _ = writeln!(
                json,
                "    \"lifetime_profile_invalid_line_count\": {},",
                profile.invalid_line_count
            );
            let _ = writeln!(
                json,
                "    \"lifetime_profile_duplicate_key_count\": {},",
                profile.duplicate_key_count
            );
        }
        None => {
            json.push_str("    \"lifetime_profile_path\": null,\n");
            json.push_str("    \"lifetime_profile_digest\": null,\n");
            json.push_str("    \"lifetime_profile_format_valid\": false,\n");
            json.push_str("    \"lifetime_profile_source_entry_line_count\": 0,\n");
            json.push_str("    \"lifetime_profile_invalid_line_count\": 0,\n");
            json.push_str("    \"lifetime_profile_duplicate_key_count\": 0,\n");
        }
    }
    let _ = writeln!(
        json,
        "    \"lifetime_profile_format\": \"{}\",",
        cli.lifetime_profile
            .as_ref()
            .and_then(|profile| profile.format)
            .unwrap_or(if cli.lifetime_profile.is_some() {
                LIFETIME_PROFILE_UNSUPPORTED_FORMAT
            } else {
                LIFETIME_PROFILE_FORMAT_V1
            })
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_binding\": \"{}\",",
        LIFETIME_PROFILE_BINDING
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_digest_algorithm\": \"{}\",",
        LIFETIME_PROFILE_DIGEST_ALGORITHM
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_unique_entry_count\": {},",
        lifetime_profile_unique_entry_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_ambiguous_entry_count\": {},",
        lifetime_profile_ambiguous_entry_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_match_count\": {},",
        lifetime_profile_match_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_abstention_count\": {},",
        lifetime_profile_abstention_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_miss_count\": {},",
        lifetime_profile_miss_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_guard_mismatch_count\": {},",
        lifetime_profile_guard_mismatch_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_ambiguous_match_count\": {},",
        lifetime_profile_ambiguous_match_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_classifier_enabled\": {},",
        cli.auto_lifetime_classifier
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_classifier_precedence\": \"{}\",",
        AUTOMATIC_LIFETIME_CLASSIFIER_PRECEDENCE
    );
    json.push_str(
        "    \"automatic_lifetime_phase_boundary\": \"exact DefId unialloc::lifetime_hugepage_advance_epoch on unique normal MIR path\",\n",
    );
    json.push_str(
        "    \"automatic_lifetime_pairing_contract\": \"same per-owner decision for semantic allocation and matching Drop scopes\",\n",
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_ephemeral_count\": {},",
        automatic_lifetime_ephemeral_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_long_lived_count\": {},",
        automatic_lifetime_long_lived_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_unknown_count\": {},",
        automatic_lifetime_unknown_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_candidate_allocation_site_count\": {},",
        automatic_lifetime_candidate_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_eligible_allocation_site_count\": {},",
        automatic_lifetime_eligible_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_ephemeral_allocation_site_count\": {},",
        automatic_lifetime_ephemeral_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_long_lived_allocation_site_count\": {},",
        automatic_lifetime_long_lived_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_abstained_allocation_site_count\": {},",
        automatic_lifetime_abstained_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_unsupported_allocation_site_count\": {},",
        automatic_lifetime_unsupported_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_unknown_allocation_site_count\": {},",
        automatic_lifetime_unknown_allocation_site_count
    );
    json.push_str("    \"automatic_lifetime_unknown_allocation_site_reasons\": {");
    for (index, (basis, count)) in automatic_lifetime_unknown_allocation_site_reasons
        .iter()
        .enumerate()
    {
        if index > 0 {
            json.push_str(", ");
        }
        let _ = write!(json, "\"{}\": {}", json_escape(basis), count);
    }
    json.push_str("},\n");
    let _ = writeln!(
        json,
        "    \"automatic_heap_lifetime_inference_enabled\": {},",
        cli.auto_heap_lifetime_inference
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_enabled\": {},",
        cli.auto_rust_lifetime_prior
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_precedence\": \"{}\",",
        AUTOMATIC_RUST_LIFETIME_PRIOR_PRECEDENCE
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_short_confidence\": {},",
        RUST_LIFETIME_PRIOR_SHORT_CONFIDENCE
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_long_confidence\": {},",
        RUST_LIFETIME_PRIOR_LONG_CONFIDENCE
    );
    json.push_str(
        "    \"automatic_rust_lifetime_prior_contract\": \"Advisory MIR ownership prior: all-path local release and cleanup-free receiver ownership may emit Short; propagated owner carriers reaching return or consuming escape may emit Long. Owner-live cleanup/unwind, opaque calls before Drop, raw loop/yield reachability, and unsupported ownership abstain. Exact per-site runtime observation remains authoritative\",\n",
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_short_allocation_site_count\": {},",
        automatic_rust_lifetime_prior_short_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_long_allocation_site_count\": {},",
        automatic_rust_lifetime_prior_long_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_rust_lifetime_prior_unknown_allocation_site_count\": {},",
        automatic_rust_lifetime_prior_unknown_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_lifetime_inference_precedence\": \"{}\",",
        AUTOMATIC_HEAP_LIFETIME_INFERENCE_PRECEDENCE
    );
    json.push_str(
        "    \"automatic_heap_lifetime_inference_contract\": \"Exact Drop is exported only as an eventual-release fact with hint=0/confidence=0; it makes no short-duration, pressure-bound, or allocator-phase claim. 0xA102 is a bounded process-long oracle for exact core mem::forget/alloc Box::leak smoke patterns and is excluded from real-program Long solution claims. Owner-live Call/Drop cleanup edges, branches, aliases, refcounted owners, and unsupported sinks abstain. Analysis runs post-borrowck before ordinary release MIR optimization\",\n",
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_bounded_process_long_oracle_hint\": {},",
        LIFETIME_HINT_BOUNDED_PROCESS_LONG_ORACLE
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_exact_drop_fact_count\": {},",
        automatic_heap_exact_drop_fact_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_bounded_process_long_oracle_count\": {},",
        automatic_heap_bounded_process_long_oracle_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_unknown_count\": {},",
        automatic_heap_unknown_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_exact_drop_fact_allocation_site_count\": {},",
        automatic_heap_exact_drop_fact_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_bounded_process_long_oracle_allocation_site_count\": {},",
        automatic_heap_bounded_process_long_oracle_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_unknown_allocation_site_count\": {},",
        automatic_heap_unknown_allocation_site_count
    );
    let _ = writeln!(json, "    \"placement_hint\": {},", cli.placement_hint);
    let _ = writeln!(json, "    \"module_id\": {},", lowering_module_id());
    let _ = writeln!(
        json,
        "    \"module_id_algorithm\": \"{}\",",
        MODULE_ID_ALGORITHM
    );
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
        "    \"actual_semantic_ownership_transfer_rewrite\": {},",
        actual_semantic_ownership_transfer_rewrite
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
        "    \"semantic_ownership_transfer_candidate_count\": {},",
        semantic_ownership_transfer_candidate_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_ownership_transfer_rewrite_applied_count\": {},",
        semantic_ownership_transfer_rewrite_applied_count
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
        "    \"semantic_scope_callback_capable_skipped_count\": {},",
        semantic_scope_callback_capable_skipped_count
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
    let _ = writeln!(
        json,
        "    \"semantic_ownership_transfer_replacement_resolution_status\": \"{}\",",
        semantic_ownership_transfer_replacement_resolution_status
    );
    json.push_str("    \"claim_grade\": false,\n");
    json.push_str("    \"notes\": [\n");
    json.push_str("      \"This installs a real rustc query override and returns cloned MIR bodies to rustc.\",\n");
    json.push_str("      \"Dry-run is the default; explicit actual modes can retarget direct allocator calls or insert semantic-scope enter/exit calls.\"\n");
    json.push_str("    ]\n");
    json.push_str("  },\n");
    json.push_str("  \"lowering_contract\": {\n");
    json.push_str("    \"target_allocator_abi\": \"__unialloc_alloc_with_metadata[_hints](size, align, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite), Rust-ABI __unialloc_{alloc,alloc_zeroed,realloc,dealloc}_layout_with_metadata[_hints] plus optional no-recovery alloc/alloc_zeroed/realloc/dealloc _local variants for explicit paired Layout lowerings; size/align __unialloc_alloc_with_metadata[_hints] remains recovery-backed because exchange_malloc has no direct paired dealloc rewrite (original Layout operands, type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite); explicit GlobalAlloc receiver calls are lowered only when the receiver type is UniAlloc/RustAllocator\",\n");
    json.push_str("    \"semantic_scope_abi\": \"__unialloc_semantic_scope_push[_hints][_local](type_id, module_id, flags, [lifetime_hint, placement_hint,] callsite) / __unialloc_semantic_scope_pop(); _local is selected only for a single normal path with no owner move/call before the exact destination Drop; every recovery-backed scope records CROSS_THREAD_RECOVERY by default, and the plain non-hints runtime ABI supplies that default bit\",\n");
    json.push_str("    \"semantic_ownership_transfer_abi\": \"exact DefId and structural owner proofs retarget Box<[T], A> -> Vec<T, A> to __unialloc_semantic_box_slice_into_vec, shrink-aware Vec<T, A> -> Box<[T], A> to __unialloc_semantic_vec_into_boxed_slice, String -> Vec<u8, Global> to __unialloc_semantic_string_into_bytes, shrink-aware String -> Box<str, Global> to __unialloc_semantic_string_into_boxed_str, Box<str, Global> -> String to __unialloc_semantic_boxed_str_into_string, CString -> Vec<u8, Global> to __unialloc_semantic_cstring_into_bytes_with_nul, and Vec<T, A> -> IntoIter<T, A> to __unialloc_semantic_vec_into_iter; no generic semantic allocation scope is inserted and any proof or symbol-resolution failure retains the ordinary ambiguous fail-closed path\",\n");
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
        "    \"semantic_ownership_transfer_candidate_count\": {},",
        semantic_ownership_transfer_candidate_count
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
        "    \"semantic_scope_callback_capable_skipped_count\": {},",
        semantic_scope_callback_capable_skipped_count
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
    let _ = writeln!(
        json,
        "    \"lifetime_profile_match_count\": {},",
        lifetime_profile_match_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_abstention_count\": {},",
        lifetime_profile_abstention_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_miss_count\": {},",
        lifetime_profile_miss_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_guard_mismatch_count\": {},",
        lifetime_profile_guard_mismatch_count
    );
    let _ = writeln!(
        json,
        "    \"lifetime_profile_ambiguous_match_count\": {},",
        lifetime_profile_ambiguous_match_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_exact_drop_fact_allocation_site_count\": {},",
        automatic_heap_exact_drop_fact_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_bounded_process_long_oracle_allocation_site_count\": {},",
        automatic_heap_bounded_process_long_oracle_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_heap_unknown_allocation_site_count\": {},",
        automatic_heap_unknown_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_candidate_allocation_site_count\": {},",
        automatic_lifetime_candidate_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_eligible_allocation_site_count\": {},",
        automatic_lifetime_eligible_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_ephemeral_allocation_site_count\": {},",
        automatic_lifetime_ephemeral_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_long_lived_allocation_site_count\": {},",
        automatic_lifetime_long_lived_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_abstained_allocation_site_count\": {},",
        automatic_lifetime_abstained_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_unsupported_allocation_site_count\": {},",
        automatic_lifetime_unsupported_allocation_site_count
    );
    let _ = writeln!(
        json,
        "    \"automatic_lifetime_unknown_allocation_site_count\": {},",
        automatic_lifetime_unknown_allocation_site_count
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
        "    \"actual_semantic_ownership_transfer_rewrite_requested\": {},",
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
        "    \"actual_semantic_ownership_transfer_rewrite\": {},",
        actual_semantic_ownership_transfer_rewrite
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
        "    \"semantic_ownership_transfer_rewrite_applied_count\": {},",
        semantic_ownership_transfer_rewrite_applied_count
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
    let _ = writeln!(
        json,
        "    \"semantic_ownership_transfer_replacement_resolution_status\": \"{}\",",
        semantic_ownership_transfer_replacement_resolution_status
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
        let _ = writeln!(
            json,
            "      \"lifetime_hint_confidence\": {},",
            record.lifetime_hint_confidence
        );
        let _ = writeln!(
            json,
            "      \"lifetime_hint_basis\": \"{}\",",
            record.lifetime_hint_basis
        );
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
        push_semantic_lifetime_features_json(&mut json, record);
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
    let marker_free_heap_preoptimization_rewrite = cfg!(unialloc_rustc_current)
        && (cli.auto_heap_lifetime_inference || cli.auto_rust_lifetime_prior)
        && cli.semantic_scope_rewrite
        && !cli.actual_rewrite;
    let _ = writeln!(
        text,
        "marker_free_heap_preoptimization_rewrite: {}",
        marker_free_heap_preoptimization_rewrite
    );
    text.push_str("marker_free_heap_analysis_query: mir_drops_elaborated_and_const_checked\n");
    text.push_str("release_mir_optimization_preserved: true\n");
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
    let semantic_ownership_transfer_candidate_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_ownership_transfer_rewrite")
        .count();
    let semantic_ownership_transfer_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            record.rewrite_status == "actual_semantic_ownership_transfer_rewrite_applied"
        })
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
    let semantic_scope_callback_capable_skipped_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_callback_capable_receiver_skipped")
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
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_generic_type_rewrite_applied"
            )
        })
        .count();
    let semantic_scope_deallocation_like_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_enter_exit_rewrite_applied"
                    | "actual_semantic_scope_generic_type_rewrite_applied"
            ) && semantic_scope_deallocation_like_call(&record.callee)
        })
        .count();
    let semantic_scope_drop_rewrite_applied_count = records
        .iter()
        .filter(|record| {
            matches!(
                record.rewrite_status,
                "actual_semantic_scope_drop_rewrite_applied"
                    | "actual_semantic_scope_drop_generic_type_rewrite_applied"
            )
        })
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
    let _ = writeln!(
        text,
        "actual_semantic_ownership_transfer_rewrite_requested: {}",
        cli.semantic_scope_rewrite
    );
    let _ = writeln!(text, "policy_flags: {}", cli.policy_flags);
    let _ = writeln!(text, "lifetime_hint: {}", cli.lifetime_hint);
    let _ = writeln!(
        text,
        "lifetime_profile_confidence_threshold: {}",
        cli.lifetime_confidence_threshold
    );
    let lifetime_profile_match_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_exact_match")
        .count();
    let lifetime_profile_abstention_count = records
        .iter()
        .filter(|record| record.lifetime_hint_basis == "profile_below_confidence_threshold")
        .count();
    let lifetime_profile_miss_count = records
        .iter()
        .filter(|record| {
            record.lifetime_hint_basis.starts_with("profile_")
                && record.lifetime_hint_basis != "profile_exact_match"
                && record.lifetime_hint_basis != "profile_below_confidence_threshold"
        })
        .count();
    let _ = writeln!(
        text,
        "lifetime_profile_enabled: {}",
        cli.lifetime_profile.is_some()
    );
    let _ = writeln!(
        text,
        "lifetime_profile_format: {}",
        cli.lifetime_profile
            .as_ref()
            .and_then(|profile| profile.format)
            .unwrap_or(if cli.lifetime_profile.is_some() {
                LIFETIME_PROFILE_UNSUPPORTED_FORMAT
            } else {
                LIFETIME_PROFILE_FORMAT_V1
            })
    );
    let _ = writeln!(
        text,
        "lifetime_profile_binding: {}",
        LIFETIME_PROFILE_BINDING
    );
    let _ = writeln!(
        text,
        "lifetime_profile_digest_algorithm: {}",
        LIFETIME_PROFILE_DIGEST_ALGORITHM
    );
    if let Some(profile) = &cli.lifetime_profile {
        let _ = writeln!(text, "lifetime_profile_path: {}", profile.path.display());
        let _ = writeln!(text, "lifetime_profile_digest: {:016x}", profile.raw_digest);
        let _ = writeln!(
            text,
            "lifetime_profile_format_valid: {}",
            profile.format_valid
        );
        let _ = writeln!(
            text,
            "lifetime_profile_invalid_line_count: {}",
            profile.invalid_line_count
        );
        let _ = writeln!(
            text,
            "lifetime_profile_duplicate_key_count: {}",
            profile.duplicate_key_count
        );
    }
    let _ = writeln!(
        text,
        "lifetime_profile_match_count: {}",
        lifetime_profile_match_count
    );
    let _ = writeln!(
        text,
        "lifetime_profile_abstention_count: {}",
        lifetime_profile_abstention_count
    );
    let _ = writeln!(
        text,
        "lifetime_profile_miss_count: {}",
        lifetime_profile_miss_count
    );
    let automatic_lifetime_ephemeral_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_ephemeral_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_long_lived_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_long_lived_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_unknown_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_unknown_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_candidate_allocation_site_count = records
        .iter()
        .filter(|record| record.lowering_kind == "semantic_scope_enter_exit_rewrite")
        .count();
    let automatic_lifetime_eligible_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_eligible_basis(record.lifetime_hint_basis)
        })
        .count();
    let automatic_lifetime_unsupported_allocation_site_count = records
        .iter()
        .filter(|record| {
            record.lowering_kind == "semantic_scope_enter_exit_rewrite"
                && automatic_lifetime_unsupported_basis(record.lifetime_hint_basis)
        })
        .count();
    let _ = writeln!(
        text,
        "automatic_lifetime_classifier_enabled: {}",
        cli.auto_lifetime_classifier
    );
    let _ = writeln!(
        text,
        "automatic_rust_lifetime_prior_enabled: {}",
        cli.auto_rust_lifetime_prior
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_classifier_precedence: {}",
        AUTOMATIC_LIFETIME_CLASSIFIER_PRECEDENCE
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_candidate_allocation_site_count: {}",
        automatic_lifetime_candidate_allocation_site_count
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_eligible_allocation_site_count: {}",
        automatic_lifetime_eligible_allocation_site_count
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_ephemeral_allocation_site_count: {}",
        automatic_lifetime_ephemeral_allocation_site_count
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_long_lived_allocation_site_count: {}",
        automatic_lifetime_long_lived_allocation_site_count
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_unsupported_allocation_site_count: {}",
        automatic_lifetime_unsupported_allocation_site_count
    );
    let _ = writeln!(
        text,
        "automatic_lifetime_unknown_allocation_site_count: {}",
        automatic_lifetime_unknown_allocation_site_count
    );
    let _ = writeln!(text, "placement_hint: {}", cli.placement_hint);
    let _ = writeln!(text, "module_id: {}", lowering_module_id());
    let _ = writeln!(text, "module_id_algorithm: {}", MODULE_ID_ALGORITHM);
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
        "actual_semantic_ownership_transfer_rewrite: {}",
        cli.semantic_scope_rewrite && semantic_ownership_transfer_rewrite_applied_count > 0
    );
    let _ = writeln!(
        text,
        "semantic_scope_candidate_count: {}",
        semantic_scope_candidate_count
    );
    let _ = writeln!(
        text,
        "semantic_ownership_transfer_candidate_count: {}",
        semantic_ownership_transfer_candidate_count
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
        "semantic_scope_callback_capable_skipped_count: {}",
        semantic_scope_callback_capable_skipped_count
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
        "semantic_ownership_transfer_rewrite_applied_count: {}",
        semantic_ownership_transfer_rewrite_applied_count
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
    text.push_str("semantic_ownership_transfer_abi: exact DefId and structural owner proofs retarget Box<[T], A> -> Vec<T, A> to __unialloc_semantic_box_slice_into_vec, shrink-aware Vec<T, A> -> Box<[T], A> to __unialloc_semantic_vec_into_boxed_slice, String -> Vec<u8, Global> to __unialloc_semantic_string_into_bytes, shrink-aware String -> Box<str, Global> to __unialloc_semantic_string_into_boxed_str, Box<str, Global> -> String to __unialloc_semantic_boxed_str_into_string, CString -> Vec<u8, Global> to __unialloc_semantic_cstring_into_bytes_with_nul, and Vec<T, A> -> IntoIter<T, A> to __unialloc_semantic_vec_into_iter; proof or symbol-resolution failure retains the ambiguous fail-closed path\n");
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
    if let Some(profile) = cli.lifetime_profile.clone() {
        if LIFETIME_PROFILE.set(profile).is_err() {
            eprintln!("lifetime profile was initialized more than once");
            process::exit(2);
        }
    }
    unsafe {
        RECORDS = Some(Mutex::new(Vec::new()));
        ACTUAL_MIR_REWRITE = cli.actual_rewrite;
        ACTUAL_SEMANTIC_SCOPE_REWRITE = cli.semantic_scope_rewrite;
        LOWERING_MODULE_ID = lowering_module_id_from_rustc_args(&cli.rustc_args);
        LOWERING_POLICY_FLAGS = cli.policy_flags;
        LOWERING_LIFETIME_HINT = cli.lifetime_hint;
        LOWERING_LIFETIME_CONFIDENCE_THRESHOLD = cli.lifetime_confidence_threshold;
        AUTO_LIFETIME_CLASSIFIER = cli.auto_lifetime_classifier;
        AUTO_HEAP_LIFETIME_INFERENCE = cli.auto_heap_lifetime_inference;
        AUTO_RUST_LIFETIME_PRIOR = cli.auto_rust_lifetime_prior;
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
