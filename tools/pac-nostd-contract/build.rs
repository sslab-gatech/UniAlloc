use std::env;

fn main() {
    println!("cargo:rustc-check-cfg=cfg(unialloc_target_arm64e)");
    if env::var("TARGET").ok().as_deref() == Some("arm64e-apple-darwin") {
        println!("cargo:rustc-cfg=unialloc_target_arm64e");
    }
}
