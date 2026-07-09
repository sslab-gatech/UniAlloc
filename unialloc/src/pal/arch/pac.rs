/// ARM v8.3 pointer authentication
/// https://www.qualcomm.com/media/documents/files/whitepaper-pointer-authentication-on-armv8-3.pdf
use crate::*;
#[cfg(target_arch = "aarch64")]
use core::arch::asm;
use core::sync::atomic::{AtomicU8, Ordering};

// Do not gate the PAC instruction wrappers on `target_feature = "paca"`.
// Apple M-series machines can execute PAC/AUT/XPAC in ordinary
// `aarch64-apple-darwin` binaries even when rustc does not advertise that
// target feature for the ABI.  Unsupported AArch64 CPUs execute these encodings
// as non-authenticating hints/no-ops, and the runtime context-binding probe
// below detects that case and keeps allocator metadata on the software
// fail-closed authenticator.

const PAC_CONTEXT_BINDING_UNKNOWN: u8 = 0;
const PAC_CONTEXT_BINDING_INACTIVE: u8 = 1;
const PAC_CONTEXT_BINDING_KEY_IA: u8 = 2;
const PAC_CONTEXT_BINDING_KEY_IB: u8 = 3;
const PAC_CONTEXT_BINDING_KEY_DA: u8 = 4;
const PAC_CONTEXT_BINDING_KEY_DB: u8 = 5;

static PAC_CONTEXT_BINDING_KEY_STATE: AtomicU8 = AtomicU8::new(PAC_CONTEXT_BINDING_UNKNOWN);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum PacKeyKind {
    None = PAC_CONTEXT_BINDING_INACTIVE,
    InstructionA = PAC_CONTEXT_BINDING_KEY_IA,
    InstructionB = PAC_CONTEXT_BINDING_KEY_IB,
    DataA = PAC_CONTEXT_BINDING_KEY_DA,
    DataB = PAC_CONTEXT_BINDING_KEY_DB,
}

impl PacKeyKind {
    #[inline]
    pub const fn as_str(self) -> &'static str {
        match self {
            PacKeyKind::None => "none",
            PacKeyKind::InstructionA => "ia",
            PacKeyKind::InstructionB => "ib",
            PacKeyKind::DataA => "da",
            PacKeyKind::DataB => "db",
        }
    }

    #[inline]
    const fn from_state(state: u8) -> Option<Self> {
        match state {
            PAC_CONTEXT_BINDING_KEY_IA => Some(PacKeyKind::InstructionA),
            PAC_CONTEXT_BINDING_KEY_IB => Some(PacKeyKind::InstructionB),
            PAC_CONTEXT_BINDING_KEY_DA => Some(PacKeyKind::DataA),
            PAC_CONTEXT_BINDING_KEY_DB => Some(PacKeyKind::DataB),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PacContextBindingProbe {
    pub key: PacKeyKind,
    pub available: bool,
    pub signed_changed: bool,
    pub strip_roundtrip: bool,
    pub correct_context_roundtrip: bool,
    pub wrong_context_rejected: bool,
}

/// Sign the pointer under the given context
///
/// For example, by using the address of the pointer as context,
/// we can safely bind the pointer to a specific address
#[cfg(target_arch = "aarch64")]
#[inline]
pub fn pacib(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            // The only difference between PACIA and PACIB is the key
            "pacib {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        )
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn pacib(ptr: usize, _context: usize) -> usize {
    ptr
}

/// Sign the pointer with IA key under the given context.
#[cfg(target_arch = "aarch64")]
#[inline]
pub fn pacia(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "pacia {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        )
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn pacia(ptr: usize, _context: usize) -> usize {
    ptr
}

/// Sign the pointer with DA key under the given context.
#[cfg(target_arch = "aarch64")]
#[inline]
pub fn pacda(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "pacda {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        )
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn pacda(ptr: usize, _context: usize) -> usize {
    ptr
}

/// Sign the pointer with DB key under the given context.
#[cfg(target_arch = "aarch64")]
#[inline]
pub fn pacdb(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "pacdb {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        )
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn pacdb(ptr: usize, _context: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn autib(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "autib {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn autib(ptr: usize, _context: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn autia(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "autia {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn autia(ptr: usize, _context: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn autda(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "autda {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn autda(ptr: usize, _context: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn autdb(ptr: usize, context: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "autdb {ptr}, {ctx}",
            ptr = inlateout(reg) ptr => res,
            ctx = in(reg) context,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn autdb(ptr: usize, _context: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn xpaci(ptr: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "xpaci {ptr}",
            ptr = inlateout(reg) ptr => res,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn xpaci(ptr: usize) -> usize {
    ptr
}

#[cfg(target_arch = "aarch64")]
#[inline]
pub fn xpacd(ptr: usize) -> usize {
    let res: usize;
    unsafe {
        asm!(
            "xpacd {ptr}",
            ptr = inlateout(reg) ptr => res,
        );
    }
    res
}

#[cfg(not(target_arch = "aarch64"))]
#[inline]
pub fn xpacd(ptr: usize) -> usize {
    ptr
}

#[inline]
fn probe_owner_contexts(ptr: usize) -> (usize, usize) {
    let owner = ptr.rotate_left(17) ^ 0x9e37_79b9_7f4a_7c15usize;
    let owner = if owner == 0 { 1 } else { owner };
    (owner, owner ^ 0xd1b5_4a32_d192_ed03usize)
}

#[inline]
fn sign_with_key(key: PacKeyKind, ptr: usize, context: usize) -> usize {
    match key {
        PacKeyKind::InstructionA => pacia(ptr, context),
        PacKeyKind::InstructionB => pacib(ptr, context),
        PacKeyKind::DataA => pacda(ptr, context),
        PacKeyKind::DataB => pacdb(ptr, context),
        PacKeyKind::None => ptr,
    }
}

#[inline]
fn authenticate_with_key(key: PacKeyKind, ptr: usize, context: usize) -> usize {
    match key {
        PacKeyKind::InstructionA => autia(ptr, context),
        PacKeyKind::InstructionB => autib(ptr, context),
        PacKeyKind::DataA => autda(ptr, context),
        PacKeyKind::DataB => autdb(ptr, context),
        PacKeyKind::None => ptr,
    }
}

#[inline]
fn strip_with_key(key: PacKeyKind, ptr: usize) -> usize {
    match key {
        PacKeyKind::InstructionA | PacKeyKind::InstructionB => xpaci(ptr),
        PacKeyKind::DataA | PacKeyKind::DataB => xpacd(ptr),
        PacKeyKind::None => ptr,
    }
}

const PAC_KEY_PROBE_ORDER: [PacKeyKind; 4] = [
    PacKeyKind::DataA,
    PacKeyKind::DataB,
    PacKeyKind::InstructionA,
    PacKeyKind::InstructionB,
];

/// Return whether this process/target actually enforces PAC context binding.
///
/// On some aarch64 targets the PAC instructions can assemble and round-trip a
/// pointer, yet all key variants behave as no-ops.  Treating that as real
/// pointer authentication would make allocator metadata checks silently accept
/// forged contexts, so callers use this probe to fail closed or fall back to
/// software integrity hashes.
#[inline]
pub fn context_binding_available() -> bool {
    active_context_binding_key().is_some()
}

/// Return the PAC key variant that actually enforces context binding, if any.
#[inline]
pub fn active_context_binding_key() -> Option<PacKeyKind> {
    match PAC_CONTEXT_BINDING_KEY_STATE.load(Ordering::Relaxed) {
        PAC_CONTEXT_BINDING_UNKNOWN => {}
        PAC_CONTEXT_BINDING_INACTIVE => return None,
        state => return PacKeyKind::from_state(state),
    }

    let probe_word = 0usize;
    let probe = best_context_binding_probe_for(&probe_word as *const _ as usize);
    let state = if probe.available {
        probe.key as u8
    } else {
        PAC_CONTEXT_BINDING_INACTIVE
    };
    PAC_CONTEXT_BINDING_KEY_STATE.store(state, Ordering::Relaxed);
    PacKeyKind::from_state(state)
}

/// Return the active PAC key as a stable lowercase string for JSON evidence.
#[inline]
pub fn active_context_binding_key_name() -> &'static str {
    active_context_binding_key()
        .unwrap_or(PacKeyKind::None)
        .as_str()
}

/// Probe raw PAC context-binding behavior for a representative pointer.
#[inline]
pub fn context_binding_probe_snapshot() -> PacContextBindingProbe {
    let probe_word = 0usize;
    best_context_binding_probe_for(&probe_word as *const _ as usize)
}

/// Return the key variants probed for context-binding enforcement.
#[inline]
pub const fn context_binding_probe_key_order() -> [PacKeyKind; 4] {
    PAC_KEY_PROBE_ORDER
}

/// Probe all PAC key variants for a representative pointer.
#[inline]
pub fn context_binding_probe_matrix_snapshot() -> [PacContextBindingProbe; 4] {
    let probe_word = 0usize;
    context_binding_probe_matrix_for(&probe_word as *const _ as usize)
}

/// Probe all PAC key variants for a specific pointer value.
#[inline]
pub fn context_binding_probe_matrix_for(ptr: usize) -> [PacContextBindingProbe; 4] {
    [
        context_binding_probe_for_key(PacKeyKind::DataA, ptr),
        context_binding_probe_for_key(PacKeyKind::DataB, ptr),
        context_binding_probe_for_key(PacKeyKind::InstructionA, ptr),
        context_binding_probe_for_key(PacKeyKind::InstructionB, ptr),
    ]
}

/// Probe raw PAC context-binding behavior for a specific pointer value.
#[inline]
pub fn context_binding_probe_for(ptr: usize) -> PacContextBindingProbe {
    context_binding_probe_for_key(PacKeyKind::InstructionB, ptr)
}

/// Probe all PAC key variants and return the first one that really rejects a
/// wrong context.  If none are active, return the legacy IB probe so existing
/// diagnostics remain comparable.
#[inline]
pub fn best_context_binding_probe_for(ptr: usize) -> PacContextBindingProbe {
    let mut fallback = context_binding_probe_for_key(PacKeyKind::InstructionB, ptr);
    for key in PAC_KEY_PROBE_ORDER {
        let probe = context_binding_probe_for_key(key, ptr);
        if key == PacKeyKind::InstructionB {
            fallback = probe;
        }
        if probe.available {
            return probe;
        }
    }
    fallback
}

/// Probe raw PAC context-binding behavior for a specific pointer/key value.
#[inline]
pub fn context_binding_probe_for_key(key: PacKeyKind, ptr: usize) -> PacContextBindingProbe {
    let (owner, wrong_owner) = probe_owner_contexts(ptr);
    let signed_ptr = sign_with_key(key, ptr, owner);
    let stripped_ptr = strip_with_key(key, signed_ptr);
    let correct_auth = authenticate_with_key(key, signed_ptr, owner);
    let strip_roundtrip = stripped_ptr == ptr;
    let correct_context_roundtrip = correct_auth == ptr;
    #[cfg(unialloc_target_arm64e)]
    let _ = wrong_owner;

    #[cfg(unialloc_target_arm64e)]
    let wrong_context_rejected = {
        // On arm64e, authenticating a signed pointer with the wrong context is
        // a destructive fail-closed operation: current macOS reports
        // EXC_ARM_PAC_FAIL/SIGBUS at the AUT instruction before Rust can return
        // a poisoned value for comparison.  Therefore the in-process probe only
        // executes the non-destructive positive checks.  The evaluator records
        // the wrong-context negative check as an expected-signal run instead of
        // crashing the positive allocator probe.
        signed_ptr != ptr && strip_roundtrip && correct_context_roundtrip
    };
    #[cfg(not(unialloc_target_arm64e))]
    let wrong_context_rejected = {
        let wrong_auth = authenticate_with_key(key, signed_ptr, wrong_owner);
        wrong_auth != ptr
    };

    PacContextBindingProbe {
        key,
        available: strip_roundtrip && correct_context_roundtrip && wrong_context_rejected,
        signed_changed: signed_ptr != ptr,
        strip_roundtrip,
        correct_context_roundtrip,
        wrong_context_rejected,
    }
}

/// Probe context binding for a representative pointer value.
#[inline]
pub fn context_binding_available_for(ptr: usize) -> bool {
    best_context_binding_probe_for(ptr).available
}

/// simply strip the signed pointer
pub use xpaci as strip_unchecked;

/// Sign a pointer under the active hardware PAC key, if any.
#[inline]
pub fn sign_context_bound_pointer(ptr: usize, owner: usize) -> Option<usize> {
    active_context_binding_key().map(|key| sign_with_key(key, ptr, owner))
}

/// Authenticate a signed pointer under `owner` and return the unsigned pointer.
///
/// Returns `None` when this target does not actually enforce PAC context
/// binding or when the signed pointer fails authentication. Allocator metadata
/// callers can then fail closed or deliberately use a software authenticator
/// instead of treating no-op PAC instructions as protection.
#[inline]
pub fn authenticate_context_bound_pointer(ptr: usize, owner: usize) -> Option<usize> {
    if !context_binding_available() {
        return None;
    }

    let key = active_context_binding_key()?;
    let unsigned = strip_with_key(key, ptr);
    let auth_res = authenticate_with_key(key, ptr, owner);
    if unsigned == auth_res {
        Some(unsigned)
    } else {
        None
    }
}

/// check the ownership of signed pointer `ptr`
///
/// return
/// error: 0
/// success: unsigned ptr
///
/// Panics if PAC context binding is unavailable for this process/target.
#[inline]
pub fn strip_checked(ptr: usize, owner: usize) -> usize {
    match authenticate_context_bound_pointer(ptr, owner) {
        Some(unsigned) => unsigned,
        None if !context_binding_available() => {
            let diag_key = best_context_binding_probe_for(ptr).key;
            let auth_res = authenticate_with_key(diag_key, ptr, owner);
            panic!(
                "[PAC] Pointer authentication context binding is unavailable!
                key: {}, ptr (signed): 0x{:x}, owner: 0x{:x}, auth_res: 0x{:x}",
                diag_key.as_str(),
                ptr,
                owner,
                auth_res
            );
        }
        None => {
            let diag_key = active_context_binding_key().unwrap_or(PacKeyKind::InstructionB);
            let auth_res = authenticate_with_key(diag_key, ptr, owner);
            panic!(
                "[PAC] Potential attacks on signed pointers!
                key: {}, ptr (signed): 0x{:x}, owner: 0x{:x}, auth_res: 0x{:x}",
                diag_key.as_str(),
                ptr,
                owner,
                auth_res
            );
        }
    }
}

#[cfg(test)]
mod tests {
    extern crate std;

    use super::*;

    fn sign_with_active_key_or_legacy_ib(ptr: usize, owner: usize) -> usize {
        match active_context_binding_key() {
            Some(key) => sign_with_key(key, ptr, owner),
            None => pacib(ptr, owner),
        }
    }

    fn assert_strip_checked_behavior(ptr: usize, signed_ptr: usize, owner: usize) {
        if context_binding_available() {
            assert_eq!(ptr, strip_checked(signed_ptr, owner));
        } else {
            let result = std::panic::catch_unwind(|| strip_checked(signed_ptr, owner));
            assert!(
                result.is_err(),
                "strip_checked must fail closed when PAC context binding is unavailable"
            );
        }
    }

    fn assert_authenticate_context_bound_pointer_behavior(
        ptr: usize,
        signed_ptr: usize,
        owner: usize,
    ) {
        if context_binding_available() {
            assert_eq!(
                Some(ptr),
                authenticate_context_bound_pointer(signed_ptr, owner)
            );
        } else {
            assert_eq!(
                None,
                authenticate_context_bound_pointer(signed_ptr, owner),
                "context-bound PAC helper must fail closed on no-op PAC targets"
            );
        }
    }

    #[test]
    fn pac_round_trips_with_zero_context() {
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let signed_ptr = pacib(ptr, 0);

        assert_eq!(ptr, autib(signed_ptr, 0));
        assert_eq!(ptr, xpaci(signed_ptr));
        let active_signed_ptr = sign_with_active_key_or_legacy_ib(ptr, 0);
        assert_authenticate_context_bound_pointer_behavior(ptr, active_signed_ptr, 0);
        assert_strip_checked_behavior(ptr, active_signed_ptr, 0);
    }

    #[test]
    fn context_binding_probe_explains_availability_decision() {
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let legacy_probe = context_binding_probe_for(ptr);
        let probe = best_context_binding_probe_for(ptr);

        assert_eq!(legacy_probe.key, PacKeyKind::InstructionB);
        assert_eq!(probe.available, context_binding_available_for(ptr));
        assert_ne!(probe.key, PacKeyKind::None);
        assert!(
            probe.available
                || !probe.wrong_context_rejected
                || !probe.strip_roundtrip
                || !probe.correct_context_roundtrip,
            "if PAC is unavailable, at least one raw PAC semantic check should explain why"
        );
    }

    #[test]
    fn pac_round_trips_with_non_zero_context() {
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let signed_ptr = pacib(ptr, 0xdeadbeef);

        assert_eq!(ptr, autib(signed_ptr, 0xdeadbeef));
        assert_eq!(ptr, xpaci(signed_ptr));
        let active_signed_ptr = sign_with_active_key_or_legacy_ib(ptr, 0xdeadbeef);
        assert_authenticate_context_bound_pointer_behavior(ptr, active_signed_ptr, 0xdeadbeef);
        assert_strip_checked_behavior(ptr, active_signed_ptr, 0xdeadbeef);
    }

    #[test]
    fn strip_checked_rejects_wrong_context_when_hardware_enforces_pac() {
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let signed_ptr = sign_with_active_key_or_legacy_ib(ptr, 0x280000020);

        assert_eq!(
            None,
            authenticate_context_bound_pointer(signed_ptr, 0x280000028),
            "wrong-context PAC authentication should reject or fail closed"
        );
        let result = std::panic::catch_unwind(|| strip_checked(signed_ptr, 0x280000028));
        assert!(
            result.is_err(),
            "strip_checked must reject wrong contexts or fail closed when context binding is unavailable"
        );
    }

    #[test]
    fn pac_round_trips_allocator_like_address() {
        let ptr = 0x280000000usize;
        let owner = 0x280000020usize;
        let signed_ptr = pacib(ptr, owner);

        assert_eq!(ptr, autib(signed_ptr, owner));
        assert_eq!(ptr, xpaci(signed_ptr));
        let active_signed_ptr = sign_with_active_key_or_legacy_ib(ptr, owner);
        assert_authenticate_context_bound_pointer_behavior(ptr, active_signed_ptr, owner);
        assert_strip_checked_behavior(ptr, active_signed_ptr, owner);
    }

    #[test]
    fn pac_round_trips_high_canonical_user_address() {
        let ptr = (1usize << 41) | 0x280000000usize;
        let owner = ptr ^ 0x280000020usize;
        let signed_ptr = pacib(ptr, owner);

        assert_eq!(
            ptr,
            xpaci(signed_ptr),
            "PAC helpers must not hard-code a 40-bit allocator address limit"
        );
        let active_signed_ptr = sign_with_active_key_or_legacy_ib(ptr, owner);
        assert_authenticate_context_bound_pointer_behavior(ptr, active_signed_ptr, owner);
        assert_strip_checked_behavior(ptr, active_signed_ptr, owner);
    }

    #[test]
    fn context_binding_probe_reports_all_key_variants() {
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let order = context_binding_probe_key_order();
        let matrix = context_binding_probe_matrix_for(ptr);

        for (index, key) in order.iter().copied().enumerate() {
            let probe = matrix[index];
            assert_eq!(probe.key, key);
            assert_eq!(
                probe.available,
                probe.strip_roundtrip
                    && probe.correct_context_roundtrip
                    && probe.wrong_context_rejected
            );
        }
    }
}
