//! Standalone form of the public `Parc::project` witness from pared issue #2.

#![forbid(unsafe_code)]

use pared::sync::Parc;

fn main() {
    let value = "Hello World!".to_owned();
    let projection = Parc::new(&()).project(|_| value.as_str());

    eprintln!("projection before drop={:?}", &*projection);
    drop(value);
    eprintln!("projection after drop={:?}", &*projection);
}
