//! RUSTSEC-2022-0070 preallocated-context UAF witness.
//!
//! ASan must instrument the bundled C dependency as well as Rust code; a
//! Rust-only sanitizer build can miss the failing load in libsecp256k1.

use secp256k1::Secp256k1;

fn make_bad_context() -> Secp256k1<secp256k1::AllPreallocated<'static>> {
    let mut storage = Box::new([secp256k1::ffi::types::AlignedType::ZERO; 1024]);
    Secp256k1::<secp256k1::AllPreallocated<'static>>::preallocated_gen_new(&mut *storage)
        .expect("context construction must succeed")
}

fn main() {
    let context = make_bad_context();
    let secret =
        secp256k1::SecretKey::from_slice(b"release the nasal daemons!!!!!!!").expect("valid key");
    let public = secp256k1::PublicKey::from_secret_key(&context, &secret);
    println!("{public}");
}
