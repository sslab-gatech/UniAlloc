use heapless::consts::*;
use heapless::Vec;

#[derive(Debug)]
struct Foo {
    n: u32,
}

impl Foo {
    fn new(n: u32) -> Self {
        Self { n }
    }
}

impl Clone for Foo {
    fn clone(&self) -> Self {
        println!("cloning {:?}", self);
        Self {
            n: self.n,
        }
    }
}

impl Drop for Foo {
    fn drop(&mut self) {
        println!("Dropping {:?}", self);
    }
}

fn main() {
    println!("Hello, world!");
    let mut v: Vec<Foo, U16> = Vec::new();
    v.push(Foo::new(1)).unwrap();
    v.push(Foo::new(2)).unwrap();
    v.push(Foo::new(3)).unwrap();

    let mut i = v.into_iter();

    let item = i.next();
    println!("popped {:?}", item);
    core::mem::drop(item);

    println!("cloning iter");

    let mut j = i.clone();
}
