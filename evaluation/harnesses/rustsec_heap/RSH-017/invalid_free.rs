//! RUSTSEC-2022-0070 invalid stack-free witness.

fn main() {
    let mut storage = [secp256k1::ffi::types::AlignedType::ZERO; 1024];
    let context = secp256k1::Secp256k1::<secp256k1::All>::preallocated_gen_new(&mut storage)
        .expect("context construction must succeed");
    let secret =
        secp256k1::SecretKey::from_slice(b"release the nasal daemons!!!!!!!").expect("valid key");
    let public = secp256k1::PublicKey::from_secret_key(&context, &secret);
    println!("{public}");
}
