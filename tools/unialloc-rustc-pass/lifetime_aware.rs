//! Pure lifetime-aware policy for the UniAlloc rustc driver.
//!
//! This module owns profile parsing, precedence, lifetime decisions, hint
//! selection, and requested-layout admission. MIR ownership/type solving and
//! MIR surgery remain in the shared driver engine.

use super::{fnv1a64, parse_u16, parse_u64, SemanticLifetimeFeatureExport};
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

pub(super) const LIFETIME_PROFILE_FORMAT_V1: &str = "unialloc-lifetime-profile-v1";
pub(super) const LIFETIME_PROFILE_FORMAT_V2: &str = "unialloc-lifetime-profile-v2";
pub(super) const LIFETIME_PROFILE_UNSUPPORTED_FORMAT: &str = "unsupported";
pub(super) const LIFETIME_PROFILE_V1_CONFIDENCE: u8 = 100;
pub(super) const LIFETIME_PROFILE_BINDING: &str = "exact(callsite,type_id,module_id)";
pub(super) const LIFETIME_PROFILE_DIGEST_ALGORITHM: &str = "fnv1a64-raw-bytes";
pub(super) const LIFETIME_HINT_EPHEMERAL: u16 = 1;
pub(super) const LIFETIME_HINT_LONG_LIVED: u16 = 2;
// Compiler-directed Long placement targets the allocator's page-backed
// small-object range. Keep this explicit pass-side contract synchronized with
// `unialloc::size_class::tc_size_class::MAX_SIZE`.
pub(super) const COMPILER_DIRECTED_LONG_MIN_REQUESTED_BYTES: u64 = 4 * 1024;
pub(super) const COMPILER_DIRECTED_LONG_MAX_REQUESTED_BYTES: u64 = 28_032;
/// Bounded process-long oracle used only for exact `mem::forget`/`Box::leak`
/// smoke patterns. This tag is deliberately outside real-program Long claims.
pub(super) const LIFETIME_HINT_BOUNDED_PROCESS_LONG_ORACLE: u16 = 0xA102;
pub(super) const RUST_LIFETIME_PRIOR_SHORT_CONFIDENCE: u8 = 85;
pub(super) const RUST_LIFETIME_PRIOR_LONG_CONFIDENCE: u8 = 70;
pub(super) const AUTOMATIC_LIFETIME_CLASSIFIER_PRECEDENCE: &str =
    "exact_profile>manual_global>automatic>Unknown";
pub(super) const AUTOMATIC_HEAP_LIFETIME_INFERENCE_PRECEDENCE: &str =
    "exact_profile>manual_global>automatic_heap>automatic_epoch>Unknown";
pub(super) const AUTOMATIC_RUST_LIFETIME_PRIOR_PRECEDENCE: &str =
    "exact_profile>manual_global>bounded_process_long_oracle>automatic_rust_lifetime_prior>automatic_heap_fact>automatic_epoch>Unknown";

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(super) struct LifetimeProfileKey {
    pub(super) callsite: u64,
    pub(super) type_id: u64,
    pub(super) module_id: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum LifetimeProfileEntry {
    Unique { hint: u16, confidence: u8 },
    Invalid,
    Ambiguous,
}

#[derive(Clone, Debug)]
pub(super) struct LifetimeProfile {
    pub(super) path: PathBuf,
    pub(super) raw_digest: u64,
    pub(super) format: Option<&'static str>,
    pub(super) format_valid: bool,
    pub(super) source_entry_line_count: usize,
    pub(super) invalid_line_count: usize,
    pub(super) duplicate_key_count: usize,
    pub(super) entries: BTreeMap<LifetimeProfileKey, LifetimeProfileEntry>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct LifetimeHintSelection {
    pub(super) hint: u16,
    pub(super) confidence: u8,
    pub(super) basis: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum AutomaticLifetimeDecision {
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
pub(super) enum AutomaticHeapLifetimeDecision {
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
pub(super) enum AutomaticRustLifetimePriorDecision {
    AllPathLocalReleaseShort,
    ReceiverOwnedShort,
    BorrowedVecReserveLong,
    ReturnLong,
    EscapeLong,
    CleanupOrUnwindUnknown,
    OwnerLiveCallUnknown,
}


pub(super) fn parse_profile_lifetime_hint(value: &str) -> Result<u16, String> {
    match value.trim() {
        "1" | "ephemeral" => Ok(LIFETIME_HINT_EPHEMERAL),
        "2" | "long-lived" | "long_lived" => Ok(LIFETIME_HINT_LONG_LIVED),
        _ => Err(format!(
            "invalid lifetime class `{}`; expected ephemeral/1 or long-lived/2",
            value
        )),
    }
}

pub(super) fn parse_profile_confidence(value: &str) -> Result<u8, String> {
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

pub(super) fn parse_lifetime_confidence_threshold(value: &str) -> Result<u8, String> {
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

pub(super) fn insert_lifetime_profile_entry(
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

pub(super) fn parse_lifetime_profile(path: PathBuf, raw: &[u8]) -> LifetimeProfile {
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

pub(super) fn load_lifetime_profile(path: PathBuf) -> Result<LifetimeProfile, String> {
    let raw = fs::read(&path)
        .map_err(|err| format!("read lifetime profile {}: {}", path.display(), err))?;
    Ok(parse_lifetime_profile(path, &raw))
}

pub(super) fn select_lifetime_hint(
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

pub(super) fn select_lifetime_hint_with_automatic_decision(
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

pub(super) fn automatic_heap_lifetime_selection(
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

pub(super) fn automatic_rust_lifetime_prior_decision(
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

    // An exact Global Vec reserve on a direct mutable argument allocates only
    // caller-owned backing storage. That storage necessarily survives this
    // call boundary. The semantic scope carries Long to the allocator, where
    // the actual runtime Layout admits only 4 KiB..=28,032-byte allocations.
    if features.borrowed_vec_reserve_prior_eligible {
        return Some(AutomaticRustLifetimePriorDecision::BorrowedVecReserveLong);
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

pub(super) fn automatic_rust_lifetime_prior_selection(
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
        AutomaticRustLifetimePriorDecision::BorrowedVecReserveLong => LifetimeHintSelection {
            hint: LIFETIME_HINT_LONG_LIVED,
            confidence: RUST_LIFETIME_PRIOR_LONG_CONFIDENCE,
            basis: "automatic_rust_lifetime_prior_borrowed_vec_reserve_long",
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

pub(super) fn automatic_rust_lifetime_prior_joinable_layout(features: &SemanticLifetimeFeatureExport) -> bool {
    // Ownership flow can predict a useful class while the scoped call still
    // performs dynamic or nested allocations. Route an advisory prior only
    // when the compiler audit can name the same exact-layout cohort that the
    // runtime observer will validate. The ownership facts remain exported for
    // ground-truth analysis when this deployment gate abstains.
    (features.borrowed_vec_reserve_prior_eligible
        && features.requested_layout_basis == "exact_borrowed_vec_reserve_runtime_layout"
        && features.requested_size_bytes.is_none()
        && features.requested_align_bytes.is_none())
        || (matches!(features.requested_size_bytes, Some(size) if size > 0)
            && matches!(
                features.requested_align_bytes,
                Some(align) if align.is_power_of_two()
            ))
}

pub(super) fn automatic_rust_lifetime_prior_direct_long_layout_admitted(
    features: &SemanticLifetimeFeatureExport,
) -> bool {
    (features.borrowed_vec_reserve_prior_eligible
        && features.requested_layout_basis == "exact_borrowed_vec_reserve_runtime_layout"
        && features.requested_size_bytes.is_none()
        && features.requested_align_bytes.is_none())
        || (matches!(
            features.requested_layout_basis,
            "exact_box_new_payload_layout" | "exact_vec_with_capacity_requested_layout"
        ) && matches!(
            features.requested_size_bytes,
            Some(size)
                if (COMPILER_DIRECTED_LONG_MIN_REQUESTED_BYTES
                    ..=COMPILER_DIRECTED_LONG_MAX_REQUESTED_BYTES)
                    .contains(&size)
        ) && matches!(
            features.requested_align_bytes,
            Some(align) if align.is_power_of_two()
        ))
}

pub(super) fn automatic_rust_lifetime_prior_unjoinable_layout_selection(
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

pub(super) fn automatic_rust_lifetime_prior_out_of_band_long_layout_selection(
    selection: LifetimeHintSelection,
) -> LifetimeHintSelection {
    let basis = match selection.basis {
        "automatic_rust_lifetime_prior_return_long" => {
            "automatic_rust_lifetime_prior_return_long_out_of_band_layout_unknown"
        }
        "automatic_rust_lifetime_prior_escape_long" => {
            "automatic_rust_lifetime_prior_escape_long_out_of_band_layout_unknown"
        }
        _ => return selection,
    };
    LifetimeHintSelection {
        hint: 0,
        confidence: 0,
        basis,
    }
}

pub(super) fn select_lifetime_hint_with_heap_inference(
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
            let features = lifetime_features.expect("Rust lifetime prior requires feature export");
            let selection = if selection.hint != 0
                && !automatic_rust_lifetime_prior_joinable_layout(features)
            {
                automatic_rust_lifetime_prior_unjoinable_layout_selection(selection)
            } else if selection.hint == LIFETIME_HINT_LONG_LIVED
                && !automatic_rust_lifetime_prior_direct_long_layout_admitted(features)
            {
                automatic_rust_lifetime_prior_out_of_band_long_layout_selection(selection)
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

pub(super) fn automatic_heap_lifetime_bounded_process_long_oracle_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_heap_exact_mem_forget" | "automatic_heap_exact_box_leak"
    )
}

pub(super) fn automatic_heap_lifetime_unknown_basis(basis: &str) -> bool {
    basis == "automatic_heap_below_confidence_threshold"
        || (basis.starts_with("automatic_heap_") && basis.ends_with("_unknown"))
}

pub(super) fn automatic_rust_lifetime_prior_short_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_rust_lifetime_prior_all_path_local_release_short"
            | "automatic_rust_lifetime_prior_receiver_owned_short"
    )
}

pub(super) fn automatic_rust_lifetime_prior_long_basis(basis: &str) -> bool {
    matches!(
        basis,
        "automatic_rust_lifetime_prior_return_long"
            | "automatic_rust_lifetime_prior_escape_long"
            | "automatic_rust_lifetime_prior_borrowed_vec_reserve_long"
    )
}

pub(super) fn automatic_rust_lifetime_prior_unknown_basis(basis: &str) -> bool {
    basis == "automatic_rust_lifetime_prior_below_confidence_threshold"
        || (basis.starts_with("automatic_rust_lifetime_prior_") && basis.ends_with("_unknown"))
}

pub(super) fn automatic_lifetime_ephemeral_basis(basis: &str) -> bool {
    basis == "automatic_exact_local_drop_before_phase_boundary"
}

pub(super) fn automatic_lifetime_long_lived_basis(basis: &str) -> bool {
    basis == "automatic_exact_local_drop_after_phase_boundary"
}

pub(super) fn automatic_lifetime_unknown_basis(basis: &str) -> bool {
    basis == "automatic_below_confidence_threshold"
        || (basis.starts_with("automatic_")
            && !basis.starts_with("automatic_heap_")
            && !basis.starts_with("automatic_rust_lifetime_prior_")
            && basis.ends_with("_unknown"))
}

pub(super) fn automatic_lifetime_unsupported_basis(basis: &str) -> bool {
    basis == "automatic_unsupported_site_unknown"
}

pub(super) fn automatic_lifetime_eligible_basis(basis: &str) -> bool {
    automatic_lifetime_ephemeral_basis(basis)
        || automatic_lifetime_long_lived_basis(basis)
        || (automatic_lifetime_unknown_basis(basis) && !automatic_lifetime_unsupported_basis(basis))
}
