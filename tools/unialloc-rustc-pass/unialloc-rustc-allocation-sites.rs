#![feature(rustc_private)]

//! Standalone rustc_driver allocation-site extractor for UniAlloc C002 work.
//!
//! This is intentionally kept outside the Cargo workspace. Build it directly with
//! the rustc toolchain that provides `rustc-dev`, then run it as a thin rustc
//! driver over a Rust source/target. The pass is analysis-only: it emits a
//! machine-auditable MIR allocation-site/type map, but does not yet rewrite MIR
//! or pass metadata into UniAlloc. It also resolves the MIR destination-owner
//! layout for audit/debugging, but actual allocator size/align must come from
//! the allocator-call MIR operands during a future MIR rewrite.

extern crate rustc_driver;
extern crate rustc_hir;
extern crate rustc_interface;
extern crate rustc_middle;

use rustc_driver::{run_compiler, Callbacks, Compilation};
use rustc_hir::def::DefKind;
use rustc_interface::interface;
use rustc_middle::mir::{Operand, TerminatorKind};
use rustc_middle::ty::{Ty, TyCtxt, TypingEnv};

use std::collections::BTreeSet;
use std::env;
use std::fmt::Write as FmtWrite;
use std::fs;
use std::path::PathBuf;
use std::process;

const PASS_NAME: &str = "unialloc-rustc-driver-mir-allocation-site-pass";
const TYPE_ID_ALGORITHM: &str =
    "fnv1a64(allocation-site-object-type-id-v2 NUL function NUL mir_location NUL source_span NUL object_type NUL callee)";
const OBJECT_TYPE_ID_ALGORITHM: &str = "fnv1a64(object_type NUL callee)";
const LOWERING_MODULE_ID: u64 = 0xC002_DA00_0000_0001;
const LOWERING_POLICY_FLAGS: u32 = 0x3;
const STD_BENCH_HARNESS_SOURCE: &str = "unialloc/benches/lib.rs";

const NO_INSTRUMENT_SOURCE_SUFFIXES: &[&str] = &[STD_BENCH_HARNESS_SOURCE];

const NO_INSTRUMENT_FUNCTION_MARKERS: &[&str] = &[
    "__unialloc_lowered_site",
    "__unialloc_rustc_driver_lowered_site",
    "__unialloc_semantic_scope_enter",
    "__unialloc_semantic_scope_exit",
    "RustcDriverLoweredScopeGuard",
    "aaa_semantic_auto_metadata_",
    "zzz_semantic_auto_metadata_",
    "semantic_stats_",
    "semantic_type_stats_",
];

const ALLOCATION_CALLEE_MARKERS: &[&str] = &[
    "::with_capacity",
    "::new",
    "::from",
    "::insert",
    "::push_back",
    "::push_front",
    "Vec::<",
    "VecDeque::<",
    "BinaryHeap::<",
    "BTreeMap::<",
    "BTreeSet::<",
    "LinkedList::<",
    "HashMap::<",
    "HashSet::<",
    "Box::<",
    "String as From",
    "String::with_capacity",
    "String::from",
    "RawVec",
    "alloc::alloc",
    "exchange_malloc",
];

#[derive(Debug)]
struct Cli {
    type_map_out: PathBuf,
    pass_log_out: Option<PathBuf>,
    rustc_args: Vec<String>,
    continue_compilation: bool,
}

#[derive(Clone, Debug)]
struct AllocationSiteRecord {
    allocation_site_id: String,
    type_id: u64,
    object_type_id: u64,
    object_type: String,
    allocation_effect: &'static str,
    allocator_call_kind: &'static str,
    call_argument_count: usize,
    call_arguments: Vec<String>,
    destination_layout_size: Option<u64>,
    destination_layout_align: Option<u64>,
    destination_layout_status: &'static str,
    mir_location: String,
    source_span: String,
    mir_function: String,
    callee: String,
    destination_place: String,
    module_id: u64,
    policy_flags: u32,
    callsite: u64,
    compiler_pass: &'static str,
    lowering_status: &'static str,
    lowering_eligible: bool,
    no_instrument: bool,
    lowering_rejection_reason: Option<&'static str>,
    type_id_algorithm: &'static str,
}

#[derive(Copy, Clone, Debug)]
struct LoweringEligibility {
    lowering_eligible: bool,
    no_instrument: bool,
    rejection_reason: Option<&'static str>,
}

fn destination_layout_resolved(record: &AllocationSiteRecord) -> bool {
    record.destination_layout_size.is_some() && record.destination_layout_align.is_some()
}

fn allocator_metadata_operands_ready(record: &AllocationSiteRecord) -> bool {
    record.lowering_eligible && record.allocator_call_kind == "direct_alloc_size_align"
}

#[derive(Default)]
struct AllocationSiteCollector {
    records: Vec<AllocationSiteRecord>,
    continue_compilation: bool,
}

fn usage() -> &'static str {
    "Usage:\n  RUSTC_BOOTSTRAP=1 rustc +$(cat rust-toolchain) tools/unialloc-rustc-pass/unialloc-rustc-allocation-sites.rs -o /tmp/unialloc-rustc-allocation-sites\n  DYLD_LIBRARY_PATH=$(rustc +$(cat rust-toolchain) --print sysroot)/lib /tmp/unialloc-rustc-allocation-sites \\\n    --unialloc-type-map-out /tmp/type-map.json -- --sysroot $(rustc +$(cat rust-toolchain) --print sysroot) --edition=2021 input.rs\n\nOptions:\n  --unialloc-type-map-out <path>   JSON allocation-site/type map output. Defaults to env UNIALLOC_TYPE_MAP_OUT or ./unialloc-rustc-allocation-sites.json\n  --unialloc-pass-log-out <path>   Optional text pass log output. Defaults to env UNIALLOC_PASS_LOG_OUT if set\n  --unialloc-type-map-dir <dir>    Wrapper-friendly output directory. Defaults to env UNIALLOC_TYPE_MAP_DIR if set\n  --unialloc-pass-log-dir <dir>    Wrapper-friendly pass-log directory. Defaults to env UNIALLOC_PASS_LOG_DIR if set\n  --unialloc-continue-compilation  Continue rustc after analysis. Defaults to env UNIALLOC_CONTINUE_COMPILATION truthiness\n  --unialloc-stop-after-analysis   Stop rustc after analysis; direct-source default\n  --unialloc-help                  Print this help\n\nEverything after `--` is passed to rustc_driver. Without `--`, remaining arguments are treated as rustc/wrapper arguments.\n"
}

fn parse_cli() -> Result<Cli, String> {
    let raw: Vec<String> = env::args().collect();
    let mut type_map_out = env::var_os("UNIALLOC_TYPE_MAP_OUT").map(PathBuf::from);
    let mut pass_log_out = env::var_os("UNIALLOC_PASS_LOG_OUT").map(PathBuf::from);
    let mut type_map_dir = env::var_os("UNIALLOC_TYPE_MAP_DIR").map(PathBuf::from);
    let mut pass_log_dir = env::var_os("UNIALLOC_PASS_LOG_DIR").map(PathBuf::from);
    let mut continue_compilation = env_truthy("UNIALLOC_CONTINUE_COMPILATION");
    let mut passthrough: Vec<String> = Vec::new();
    let mut saw_dashdash = false;

    let mut i = 1;
    while i < raw.len() {
        match raw[i].as_str() {
            "--unialloc-help" | "-h" | "--help" if passthrough.is_empty() => {
                println!("{}", usage());
                process::exit(0);
            }
            "--unialloc-type-map-out" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-type-map-out requires a path".to_string());
                }
                type_map_out = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-pass-log-out" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-pass-log-out requires a path".to_string());
                }
                pass_log_out = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-type-map-dir" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-type-map-dir requires a directory".to_string());
                }
                type_map_dir = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-pass-log-dir" => {
                i += 1;
                if i >= raw.len() {
                    return Err("--unialloc-pass-log-dir requires a directory".to_string());
                }
                pass_log_dir = Some(PathBuf::from(&raw[i]));
            }
            "--unialloc-continue-compilation" => {
                continue_compilation = true;
            }
            "--unialloc-stop-after-analysis" => {
                continue_compilation = false;
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

    let mut rustc_args = Vec::new();
    if saw_dashdash || !looks_like_rustc_argv0(&passthrough[0]) {
        rustc_args.push("rustc".to_string());
    }
    rustc_args.extend(passthrough);
    inject_sysroot_if_missing(&mut rustc_args);

    Ok(Cli {
        type_map_out: type_map_out
            .or_else(|| {
                type_map_dir
                    .as_ref()
                    .map(|dir| derived_output_path(dir, &rustc_args, "json"))
            })
            .unwrap_or_else(|| PathBuf::from("unialloc-rustc-allocation-sites.json")),
        pass_log_out: pass_log_out.or_else(|| {
            pass_log_dir
                .as_ref()
                .map(|dir| derived_output_path(dir, &rustc_args, "log"))
        }),
        rustc_args,
        continue_compilation,
    })
}

fn env_truthy(name: &str) -> bool {
    match env::var(name) {
        Ok(value) => {
            let lowered = value.trim().to_ascii_lowercase();
            !(lowered.is_empty()
                || lowered == "0"
                || lowered == "false"
                || lowered == "no"
                || lowered == "off")
        }
        Err(_) => false,
    }
}

fn looks_like_rustc_argv0(value: &str) -> bool {
    let lowered = value
        .rsplit('/')
        .next()
        .unwrap_or(value)
        .to_ascii_lowercase();
    lowered == "rustc" || lowered.starts_with("rustc-") || lowered.starts_with("rustc_driver")
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

fn derive_sysroot_from_rustc_argv0(argv0: &str) -> Option<String> {
    let path = PathBuf::from(argv0);
    if path.file_name().and_then(|name| name.to_str()) != Some("rustc") {
        return None;
    }
    let bin = path.parent()?;
    if bin.file_name().and_then(|name| name.to_str()) != Some("bin") {
        return None;
    }
    Some(bin.parent()?.to_string_lossy().into_owned())
}

fn crate_name_from_rustc_args(args: &[String]) -> String {
    let mut idx = 0;
    while idx < args.len() {
        if args[idx] == "--crate-name" {
            if let Some(name) = args.get(idx + 1) {
                return sanitize_filename(name);
            }
        }
        idx += 1;
    }
    "rustc-input".to_string()
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

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3u64);
    }
    hash
}

fn fnv1a64_text(text: &str) -> u64 {
    fnv1a64(text.as_bytes())
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

fn push_json_string_array(
    json: &mut String,
    indent: &str,
    name: &str,
    values: &[&str],
    trailing_comma: bool,
) {
    let _ = writeln!(json, "{}\"{}\": [", indent, name);
    for (idx, value) in values.iter().enumerate() {
        let comma = if idx + 1 == values.len() { "" } else { "," };
        let _ = writeln!(json, "{}  \"{}\"{}", indent, json_escape(value), comma);
    }
    let _ = writeln!(json, "{}]{}", indent, if trailing_comma { "," } else { "" });
}

fn write_json_owned_string_array(
    json: &mut String,
    indent: &str,
    name: &str,
    values: &[String],
    trailing_comma: bool,
) {
    let _ = writeln!(json, "{}\"{}\": [", indent, name);
    for (idx, value) in values.iter().enumerate() {
        let comma = if idx + 1 == values.len() { "" } else { "," };
        let _ = writeln!(json, "{}  \"{}\"{}", indent, json_escape(value), comma);
    }
    let _ = writeln!(json, "{}]{}", indent, if trailing_comma { "," } else { "" });
}

fn normalize_type_text(raw: String) -> String {
    let collapsed = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        "<unknown>".to_string()
    } else {
        collapsed
    }
}

fn callee_text(func: &Operand<'_>) -> String {
    match func {
        Operand::Constant(constant) => format!("{:?}", constant.const_),
        other => format!("{:?}", other),
    }
}

fn callee_looks_allocating(callee: &str, destination_type: &str) -> bool {
    let mut text = String::with_capacity(callee.len() + destination_type.len() + 1);
    text.push_str(callee);
    text.push(' ');
    text.push_str(destination_type);
    ALLOCATION_CALLEE_MARKERS
        .iter()
        .any(|marker| text.contains(marker))
}

fn classify_allocation_effect(callee: &str) -> (&'static str, &'static str) {
    if callee.contains("alloc::alloc::exchange_malloc")
        || callee.contains("alloc::alloc::alloc")
        || callee.contains("alloc::alloc::realloc")
        || callee.contains("alloc::alloc::GlobalAlloc::alloc")
    {
        return ("direct_allocator_call", "direct_alloc_size_align");
    }
    if callee.contains("::with_capacity") || callee.contains("try_allocate_in") {
        return ("capacity_allocation_constructor", "callee_internal_layout");
    }
    if callee.contains("::insert")
        || callee.contains("::push")
        || callee.contains("::push_back")
        || callee.contains("::push_front")
        || callee.contains("::extend")
    {
        return ("collection_growth_may_allocate", "callee_internal_layout");
    }
    if callee.contains("::new") {
        return (
            "constructor_no_immediate_heap_allocation",
            "not_allocator_call",
        );
    }
    (
        "semantic_operation_may_reference_heap_type",
        "not_allocator_call",
    )
}

fn object_type_from_callee(callee: &str) -> Option<String> {
    let collection_names = [
        ("VecDeque::<", "std::collections::VecDeque"),
        ("BinaryHeap::<", "std::collections::BinaryHeap"),
        ("BTreeMap::<", "std::collections::BTreeMap"),
        ("BTreeSet::<", "std::collections::BTreeSet"),
        ("LinkedList::<", "std::collections::LinkedList"),
        ("HashMap::<", "std::collections::HashMap"),
        ("HashSet::<", "std::collections::HashSet"),
        ("Vec::<", "std::vec::Vec"),
        ("Box::<", "std::boxed::Box"),
    ];
    for (needle, namespace) in collection_names {
        if let Some(start) = callee.find(needle) {
            let inner_start = start + needle.len();
            if let Some(rel_end) = callee[inner_start..].find(">") {
                let inner = &callee[inner_start..inner_start + rel_end];
                return Some(format!("{}<{}>", namespace, inner));
            }
        }
    }
    if callee.contains("String::with_capacity")
        || callee.contains("String::from")
        || callee.contains("String as From")
    {
        return Some("std::string::String".to_string());
    }
    if callee.contains("exchange_malloc") || callee.contains("alloc::alloc") {
        return Some("raw-allocation".to_string());
    }
    None
}

fn object_type_for_call(destination_type: String, callee: &str) -> String {
    let dest = normalize_type_text(destination_type);
    if dest != "()"
        && dest != "bool"
        && !dest.starts_with("std::option::Option")
        && dest != "<unknown>"
    {
        return dest;
    }
    object_type_from_callee(callee).unwrap_or(dest)
}

fn destination_layout_for_type<'tcx>(
    tcx: TyCtxt<'tcx>,
    typing_env: TypingEnv<'tcx>,
    ty: Ty<'tcx>,
) -> (Option<u64>, Option<u64>, &'static str) {
    match tcx.layout_of(typing_env.as_query_input(ty)) {
        Ok(layout) => (
            Some(layout.size.bytes()),
            Some(layout.align.abi.bytes()),
            "resolved",
        ),
        Err(_) => (None, None, "unresolved"),
    }
}

fn source_span_path_text(source_span: &str) -> &str {
    source_span.split(':').next().unwrap_or(source_span)
}

fn source_span_matches_suffix(source_span: &str, suffix: &str) -> bool {
    let path = source_span_path_text(source_span);
    path == suffix || path.ends_with(&format!("/{}", suffix))
}

fn lowering_eligibility(
    source_span: &str,
    mir_function: &str,
    callee: &str,
) -> LoweringEligibility {
    if source_span.trim().is_empty() {
        return LoweringEligibility {
            lowering_eligible: false,
            no_instrument: false,
            rejection_reason: Some("missing_source_span"),
        };
    }
    if NO_INSTRUMENT_SOURCE_SUFFIXES
        .iter()
        .any(|suffix| source_span_matches_suffix(source_span, suffix))
    {
        return LoweringEligibility {
            lowering_eligible: false,
            no_instrument: true,
            rejection_reason: Some("no_instrument_source"),
        };
    }
    if NO_INSTRUMENT_FUNCTION_MARKERS
        .iter()
        .any(|marker| mir_function.contains(marker) || callee.contains(marker))
    {
        return LoweringEligibility {
            lowering_eligible: false,
            no_instrument: true,
            rejection_reason: Some("no_instrument_function"),
        };
    }
    LoweringEligibility {
        lowering_eligible: true,
        no_instrument: false,
        rejection_reason: None,
    }
}

fn inspect_crate<'tcx>(tcx: TyCtxt<'tcx>, records: &mut Vec<AllocationSiteRecord>) {
    for def_id in tcx.hir_body_owners() {
        match tcx.def_kind(def_id) {
            DefKind::Fn | DefKind::AssocFn | DefKind::Closure | DefKind::SyntheticCoroutineBody => {
            }
            _ => continue,
        }
        let mir = tcx.optimized_mir(def_id);
        let typing_env = mir.typing_env(tcx);
        let function_name = tcx.def_path_str(def_id.to_def_id());
        for (bb, data) in mir.basic_blocks.iter_enumerated() {
            let terminator = match &data.terminator {
                Some(terminator) => terminator,
                None => continue,
            };
            let (func, args, destination, fn_span) = match &terminator.kind {
                TerminatorKind::Call {
                    func,
                    args,
                    destination,
                    fn_span,
                    ..
                } => (func, args, destination, fn_span),
                _ => continue,
            };
            let destination_place = format!("{:?}", destination);
            let destination_ty = destination.ty(&mir.local_decls, tcx).ty;
            let destination_type = format!("{:?}", destination_ty);
            let callee = callee_text(&func);
            if !callee_looks_allocating(&callee, &destination_type) {
                continue;
            }
            let object_type = object_type_for_call(destination_type, &callee);
            let (allocation_effect, allocator_call_kind) = classify_allocation_effect(&callee);
            let call_arguments = args
                .iter()
                .map(|arg| format!("{:?}", arg))
                .collect::<Vec<_>>();
            let (destination_layout_size, destination_layout_align, destination_layout_status) =
                destination_layout_for_type(tcx, typing_env, destination_ty);
            let source_span = tcx.sess.source_map().span_to_diagnostic_string(*fn_span);
            let mir_location = format!("{}::{:?}:{}", function_name, bb, destination_place);
            let object_type_key = format!("{}\0{}", object_type, callee);
            let object_type_id = fnv1a64_text(&object_type_key);
            let site_key = format!(
                "{}\0{}\0{}\0{}",
                function_name, mir_location, source_span, object_type_key
            );
            let callsite = fnv1a64_text(&site_key);
            let type_id = fnv1a64_text(&format!("allocation-site-object-type-id-v2\0{}", site_key));
            let allocation_site_id = format!("rustc-driver-mir:{:016x}", callsite);
            let eligibility = lowering_eligibility(&source_span, &function_name, &callee);
            records.push(AllocationSiteRecord {
                allocation_site_id,
                type_id,
                object_type_id,
                object_type,
                allocation_effect,
                allocator_call_kind,
                call_argument_count: call_arguments.len(),
                call_arguments,
                destination_layout_size,
                destination_layout_align,
                destination_layout_status,
                mir_location,
                source_span,
                mir_function: function_name.clone(),
                callee,
                destination_place,
                module_id: LOWERING_MODULE_ID,
                policy_flags: LOWERING_POLICY_FLAGS,
                callsite,
                compiler_pass: PASS_NAME,
                lowering_status: "analysis_only",
                lowering_eligible: eligibility.lowering_eligible,
                no_instrument: eligibility.no_instrument,
                lowering_rejection_reason: eligibility.rejection_reason,
                type_id_algorithm: TYPE_ID_ALGORITHM,
            });
        }
    }
}

impl Callbacks for AllocationSiteCollector {
    fn after_analysis<'tcx>(
        &mut self,
        _compiler: &interface::Compiler,
        tcx: TyCtxt<'tcx>,
    ) -> Compilation {
        inspect_crate(tcx, &mut self.records);
        if self.continue_compilation {
            Compilation::Continue
        } else {
            Compilation::Stop
        }
    }
}

fn write_json(cli: &Cli, records: &[AllocationSiteRecord]) -> Result<(), String> {
    if let Some(parent) = cli.type_map_out.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)
                .map_err(|err| format!("create {}: {}", parent.display(), err))?;
        }
    }

    let lowering_eligible_count = records
        .iter()
        .filter(|record| record.lowering_eligible)
        .count();
    let no_instrument_count = records.iter().filter(|record| record.no_instrument).count();
    let direct_allocator_call_count = records
        .iter()
        .filter(|record| record.allocation_effect == "direct_allocator_call")
        .count();
    let semantic_scope_metadata_ready_count = lowering_eligible_count;
    let metadata_operands_ready_count = records
        .iter()
        .filter(|record| allocator_metadata_operands_ready(record))
        .count();
    let layout_resolved_count = records
        .iter()
        .filter(|record| destination_layout_resolved(record))
        .count();
    let layout_resolved_lowering_eligible_count = records
        .iter()
        .filter(|record| record.lowering_eligible && destination_layout_resolved(record))
        .count();
    let unique_type_id_count = records
        .iter()
        .map(|record| record.type_id)
        .collect::<BTreeSet<_>>()
        .len();
    let unique_object_type_id_count = records
        .iter()
        .map(|record| record.object_type_id)
        .collect::<BTreeSet<_>>()
        .len();

    let mut json = String::new();
    json.push_str("{\n");
    json.push_str("  \"schema_version\": 1,\n");
    let _ = writeln!(json, "  \"source\": \"{}\",", PASS_NAME);
    json.push_str("  \"type_id_basis\": \"compiler-assigned-allocation-site-object-type-id\",\n");
    let _ = writeln!(
        json,
        "  \"type_id_algorithm\": \"{}\",",
        json_escape(TYPE_ID_ALGORITHM)
    );
    let _ = writeln!(
        json,
        "  \"object_type_id_algorithm\": \"{}\",",
        json_escape(OBJECT_TYPE_ID_ALGORITHM)
    );
    let _ = writeln!(json, "  \"lowering_module_id\": {},", LOWERING_MODULE_ID);
    let _ = writeln!(
        json,
        "  \"lowering_policy_flags\": {},",
        LOWERING_POLICY_FLAGS
    );
    json.push_str("  \"compiler_pass\": {\n");
    let _ = writeln!(json, "    \"name\": \"{}\",", PASS_NAME);
    json.push_str("    \"kind\": \"rustc_driver_mir_after_analysis\",\n");
    json.push_str("    \"lowering_status\": \"analysis_only\",\n");
    let _ = writeln!(
        json,
        "    \"rustc_continued_after_analysis\": {},",
        if cli.continue_compilation {
            "true"
        } else {
            "false"
        }
    );
    json.push_str("    \"source_changes_to_benchmarks\": false,\n");
    json.push_str("    \"claim_grade\": false,\n");
    json.push_str("    \"notes\": [\n");
    json.push_str("      \"This is a real rustc_driver pass over optimized MIR, not a text-only mock parser.\",\n");
    json.push_str("      \"It currently emits allocation-site/type-map evidence only; MIR rewriting and allocator metadata lowering remain future work.\"\n");
    json.push_str("    ]\n");
    json.push_str("  },\n");
    json.push_str("  \"lowering_contract\": {\n");
    json.push_str("    \"runtime_metadata_delivery\": \"semantic_scope_enter_exit\",\n");
    json.push_str("    \"direct_allocator_abi\": \"__unialloc_alloc_with_metadata(size, align, type_id, module_id, flags, callsite)\",\n");
    json.push_str("    \"direct_allocator_abi_size_align_basis\": \"existing allocator-call MIR size/align operands; destination layout is audit/debug evidence only\",\n");
    json.push_str("    \"destination_layout_basis\": \"rustc_middle::ty::layout_of(destination_place_type)\",\n");
    json.push_str(
        "    \"eligibility_basis\": \"rustc_driver_mir_source_span_and_no_instrument_contract\",\n",
    );
    json.push_str("    \"no_instrument_policy\": \"records marked no_instrument or lowering_eligible=false must not be source-span/MIR lowered\",\n");
    push_json_string_array(
        &mut json,
        "    ",
        "no_instrument_source_suffixes",
        NO_INSTRUMENT_SOURCE_SUFFIXES,
        true,
    );
    push_json_string_array(
        &mut json,
        "    ",
        "no_instrument_function_markers",
        NO_INSTRUMENT_FUNCTION_MARKERS,
        false,
    );
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
    let _ = writeln!(json, "    \"allocation_site_count\": {},", records.len());
    let _ = writeln!(
        json,
        "    \"unique_allocation_site_type_id_count\": {},",
        unique_type_id_count
    );
    let _ = writeln!(
        json,
        "    \"unique_object_type_id_count\": {},",
        unique_object_type_id_count
    );
    let _ = writeln!(
        json,
        "    \"lowering_eligible_allocation_site_count\": {},",
        lowering_eligible_count
    );
    let _ = writeln!(
        json,
        "    \"no_instrument_allocation_site_count\": {},",
        no_instrument_count
    );
    let _ = writeln!(
        json,
        "    \"layout_resolved_allocation_site_count\": {},",
        layout_resolved_count
    );
    let _ = writeln!(
        json,
        "    \"layout_resolved_lowering_eligible_allocation_site_count\": {},",
        layout_resolved_lowering_eligible_count
    );
    let _ = writeln!(
        json,
        "    \"direct_allocator_call_site_count\": {},",
        direct_allocator_call_count
    );
    let _ = writeln!(
        json,
        "    \"semantic_scope_metadata_operands_ready_allocation_site_count\": {},",
        semantic_scope_metadata_ready_count
    );
    let _ = writeln!(
        json,
        "    \"direct_allocator_metadata_operands_ready_allocation_site_count\": {},",
        metadata_operands_ready_count
    );
    json.push_str("    \"claim_grade\": false,\n");
    json.push_str("    \"complete_for_claim\": false\n");
    json.push_str("  },\n");
    json.push_str("  \"allocation_sites\": [\n");
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
        let _ = writeln!(
            json,
            "      \"type_id_hex\": \"0x{:016x}\",",
            record.type_id
        );
        let _ = writeln!(json, "      \"object_type_id\": {},", record.object_type_id);
        let _ = writeln!(
            json,
            "      \"object_type_id_hex\": \"0x{:016x}\",",
            record.object_type_id
        );
        let _ = writeln!(
            json,
            "      \"object_type\": \"{}\",",
            json_escape(&record.object_type)
        );
        let _ = writeln!(
            json,
            "      \"allocation_effect\": \"{}\",",
            record.allocation_effect
        );
        let _ = writeln!(
            json,
            "      \"allocator_call_kind\": \"{}\",",
            record.allocator_call_kind
        );
        let _ = writeln!(
            json,
            "      \"call_argument_count\": {},",
            record.call_argument_count
        );
        write_json_owned_string_array(
            &mut json,
            "      ",
            "call_arguments",
            &record.call_arguments,
            true,
        );
        json.push_str("      \"layout_subject\": \"mir_destination_place_type\",\n");
        match record.destination_layout_size {
            Some(size) => {
                let _ = writeln!(json, "      \"layout_size\": {},", size);
                let _ = writeln!(json, "      \"destination_layout_size\": {},", size);
            }
            None => {
                json.push_str("      \"layout_size\": null,\n");
                json.push_str("      \"destination_layout_size\": null,\n");
            }
        }
        match record.destination_layout_align {
            Some(align) => {
                let _ = writeln!(json, "      \"layout_align\": {},", align);
                let _ = writeln!(json, "      \"destination_layout_align\": {},", align);
            }
            None => {
                json.push_str("      \"layout_align\": null,\n");
                json.push_str("      \"destination_layout_align\": null,\n");
            }
        }
        let _ = writeln!(
            json,
            "      \"layout_status\": \"{}\",",
            record.destination_layout_status
        );
        let _ = writeln!(
            json,
            "      \"destination_layout_status\": \"{}\",",
            record.destination_layout_status
        );
        let _ = writeln!(
            json,
            "      \"mir_location\": \"{}\",",
            json_escape(&record.mir_location)
        );
        let _ = writeln!(
            json,
            "      \"source_span\": \"{}\",",
            json_escape(&record.source_span)
        );
        let _ = writeln!(
            json,
            "      \"mir_function\": \"{}\",",
            json_escape(&record.mir_function)
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
        let _ = writeln!(json, "      \"module_id\": {},", record.module_id);
        let _ = writeln!(
            json,
            "      \"module_id_hex\": \"0x{:016x}\",",
            record.module_id
        );
        let _ = writeln!(json, "      \"policy_flags\": {},", record.policy_flags);
        let _ = writeln!(json, "      \"callsite\": {},", record.callsite);
        let _ = writeln!(
            json,
            "      \"callsite_hex\": \"0x{:016x}\",",
            record.callsite
        );
        let _ = writeln!(
            json,
            "      \"compiler_pass\": \"{}\",",
            record.compiler_pass
        );
        let _ = writeln!(
            json,
            "      \"lowering_status\": \"{}\",",
            record.lowering_status
        );
        let _ = writeln!(
            json,
            "      \"lowering_eligible\": {},",
            if record.lowering_eligible {
                "true"
            } else {
                "false"
            }
        );
        let _ = writeln!(
            json,
            "      \"no_instrument\": {},",
            if record.no_instrument {
                "true"
            } else {
                "false"
            }
        );
        match record.lowering_rejection_reason {
            Some(reason) => {
                let _ = writeln!(
                    json,
                    "      \"lowering_rejection_reason\": \"{}\",",
                    json_escape(reason)
                );
            }
            None => json.push_str("      \"lowering_rejection_reason\": null,\n"),
        }
        json.push_str("      \"lowering_plan\": {\n");
        let _ = writeln!(
            json,
            "        \"status\": \"{}\",",
            if record.lowering_eligible {
                "planned"
            } else {
                "blocked"
            }
        );
        match record.lowering_rejection_reason {
            Some(reason) => {
                let _ = writeln!(json, "        \"blocker\": \"{}\",", json_escape(reason));
            }
            None => json.push_str("        \"blocker\": null,\n"),
        }
        json.push_str("        \"strategy\": \"semantic_scope_enter_exit\",\n");
        json.push_str("        \"enter_symbol\": \"__unialloc_semantic_scope_enter\",\n");
        json.push_str("        \"exit_symbol\": \"__unialloc_semantic_scope_exit\",\n");
        let _ = writeln!(json, "        \"type_id\": {},", record.type_id);
        let _ = writeln!(json, "        \"module_id\": {},", record.module_id);
        let _ = writeln!(json, "        \"flags\": {},", record.policy_flags);
        let _ = writeln!(json, "        \"callsite\": {}", record.callsite);
        json.push_str("      },\n");
        json.push_str("      \"direct_allocator_abi_plan\": {\n");
        let _ = writeln!(
            json,
            "        \"status\": \"{}\",",
            if allocator_metadata_operands_ready(record) {
                "metadata_operands_ready"
            } else {
                "blocked"
            }
        );
        let abi_blocker = if !record.lowering_eligible {
            record
                .lowering_rejection_reason
                .unwrap_or("not_lowering_eligible")
        } else if !allocator_metadata_operands_ready(record) {
            record.allocator_call_kind
        } else {
            ""
        };
        if abi_blocker.is_empty() {
            json.push_str("        \"blocker\": null,\n");
        } else {
            let _ = writeln!(
                json,
                "        \"blocker\": \"{}\",",
                json_escape(abi_blocker)
            );
        }
        json.push_str("        \"alloc_symbol\": \"__unialloc_alloc_with_metadata\",\n");
        json.push_str("        \"dealloc_symbol\": \"__unialloc_dealloc_with_metadata\",\n");
        json.push_str("        \"realloc_symbol\": \"__unialloc_realloc_with_split_metadata\",\n");
        json.push_str("        \"lowering_target\": \"allocator_call_mir_rewrite\",\n");
        let size_source = if allocator_metadata_operands_ready(record) {
            "mir_call_arg_0_runtime_size_operand"
        } else {
            "blocked_until_direct_allocator_call_mir"
        };
        let align_source = if allocator_metadata_operands_ready(record) {
            "mir_call_arg_1_runtime_align_operand"
        } else {
            "blocked_until_direct_allocator_call_mir"
        };
        let _ = writeln!(json, "        \"size_source\": \"{}\",", size_source);
        let _ = writeln!(json, "        \"align_source\": \"{}\",", align_source);
        let _ = writeln!(
            json,
            "        \"size_operand\": {},",
            match record.call_arguments.get(0) {
                Some(value) if allocator_metadata_operands_ready(record) =>
                    format!("\"{}\"", json_escape(value)),
                _ => "null".to_string(),
            }
        );
        let _ = writeln!(
            json,
            "        \"align_operand\": {},",
            match record.call_arguments.get(1) {
                Some(value) if allocator_metadata_operands_ready(record) =>
                    format!("\"{}\"", json_escape(value)),
                _ => "null".to_string(),
            }
        );
        json.push_str("        \"allocator_mir_rewrite_required\": true,\n");
        json.push_str("        \"size\": null,\n");
        json.push_str("        \"align\": null,\n");
        match record.destination_layout_size {
            Some(size) => {
                let _ = writeln!(json, "        \"destination_layout_size\": {},", size);
            }
            None => json.push_str("        \"destination_layout_size\": null,\n"),
        }
        match record.destination_layout_align {
            Some(align) => {
                let _ = writeln!(json, "        \"destination_layout_align\": {},", align);
            }
            None => json.push_str("        \"destination_layout_align\": null,\n"),
        }
        let _ = writeln!(json, "        \"type_id\": {},", record.type_id);
        let _ = writeln!(json, "        \"module_id\": {},", record.module_id);
        let _ = writeln!(json, "        \"flags\": {},", record.policy_flags);
        let _ = writeln!(json, "        \"callsite\": {}", record.callsite);
        json.push_str("      },\n");
        let _ = writeln!(
            json,
            "      \"type_id_algorithm\": \"{}\"",
            json_escape(record.type_id_algorithm)
        );
        json.push_str("    }");
    }
    json.push_str("\n  ]\n");
    json.push_str("}\n");

    fs::write(&cli.type_map_out, json)
        .map_err(|err| format!("write {}: {}", cli.type_map_out.display(), err))?;
    Ok(())
}

fn write_pass_log(cli: &Cli, records: &[AllocationSiteRecord]) -> Result<(), String> {
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
    let lowering_eligible_count = records
        .iter()
        .filter(|record| record.lowering_eligible)
        .count();
    let no_instrument_count = records.iter().filter(|record| record.no_instrument).count();
    let direct_allocator_call_count = records
        .iter()
        .filter(|record| record.allocation_effect == "direct_allocator_call")
        .count();
    let semantic_scope_metadata_ready_count = lowering_eligible_count;
    let metadata_operands_ready_count = records
        .iter()
        .filter(|record| allocator_metadata_operands_ready(record))
        .count();
    let layout_resolved_count = records
        .iter()
        .filter(|record| destination_layout_resolved(record))
        .count();
    let layout_resolved_lowering_eligible_count = records
        .iter()
        .filter(|record| record.lowering_eligible && destination_layout_resolved(record))
        .count();
    let unique_type_id_count = records
        .iter()
        .map(|record| record.type_id)
        .collect::<BTreeSet<_>>()
        .len();
    let unique_object_type_id_count = records
        .iter()
        .map(|record| record.object_type_id)
        .collect::<BTreeSet<_>>()
        .len();
    let _ = writeln!(text, "label: {}", PASS_NAME);
    text.push_str("compiler_pass_kind: rustc_driver_mir_after_analysis\n");
    text.push_str("lowering_status: analysis_only\n");
    let _ = writeln!(
        text,
        "rustc_continued_after_analysis: {}",
        if cli.continue_compilation {
            "true"
        } else {
            "false"
        }
    );
    text.push_str("claim_grade: false\n");
    let _ = writeln!(text, "type_map: {}", cli.type_map_out.display());
    let _ = writeln!(text, "allocation_site_count: {}", records.len());
    let _ = writeln!(
        text,
        "unique_allocation_site_type_id_count: {}",
        unique_type_id_count
    );
    let _ = writeln!(
        text,
        "unique_object_type_id_count: {}",
        unique_object_type_id_count
    );
    let _ = writeln!(
        text,
        "lowering_eligible_allocation_site_count: {}",
        lowering_eligible_count
    );
    let _ = writeln!(
        text,
        "no_instrument_allocation_site_count: {}",
        no_instrument_count
    );
    let _ = writeln!(
        text,
        "layout_resolved_allocation_site_count: {}",
        layout_resolved_count
    );
    let _ = writeln!(
        text,
        "layout_resolved_lowering_eligible_allocation_site_count: {}",
        layout_resolved_lowering_eligible_count
    );
    let _ = writeln!(
        text,
        "direct_allocator_call_site_count: {}",
        direct_allocator_call_count
    );
    let _ = writeln!(
        text,
        "semantic_scope_metadata_operands_ready_allocation_site_count: {}",
        semantic_scope_metadata_ready_count
    );
    let _ = writeln!(
        text,
        "direct_allocator_metadata_operands_ready_allocation_site_count: {}",
        metadata_operands_ready_count
    );
    text.push_str(
        "direct_allocator_abi: __unialloc_alloc_with_metadata(size, align, type_id, module_id, flags, callsite)\n",
    );
    text.push_str(
        "direct_allocator_size_align_basis: existing allocator-call MIR operands; destination layout is audit/debug evidence only\n",
    );
    text.push_str(
        "note: real rustc_driver optimized-MIR analysis; no MIR rewrite/allocator lowering yet\n",
    );
    fs::write(path, text).map_err(|err| format!("write {}: {}", path.display(), err))?;
    Ok(())
}

fn main() {
    let cli = match parse_cli() {
        Ok(cli) => cli,
        Err(err) => {
            eprintln!("{}", err);
            process::exit(2);
        }
    };

    let mut collector = AllocationSiteCollector {
        records: Vec::new(),
        continue_compilation: cli.continue_compilation,
    };
    run_compiler(&cli.rustc_args, &mut collector);

    if let Err(err) =
        write_json(&cli, &collector.records).and_then(|_| write_pass_log(&cli, &collector.records))
    {
        eprintln!("{}", err);
        process::exit(1);
    }

    eprintln!(
        "{}: wrote {} allocation-site rows to {}",
        PASS_NAME,
        collector.records.len(),
        cli.type_map_out.display()
    );
}
