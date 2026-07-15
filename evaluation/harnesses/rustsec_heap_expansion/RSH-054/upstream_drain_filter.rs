#![feature(drain_filter)]

use std::panic::catch_unwind;

struct PrintOnDrop {
    id: u8
}

impl Drop for PrintOnDrop {
    fn drop(&mut self) {
        println!("dropped: {}", self.id)
    }
}

fn main() {
    println!("-- start --");
    let _ = catch_unwind(move || {
        let mut a: Vec<_> = [0, 1, 4, 5, 6].iter()
            .map(|&id| PrintOnDrop { id })
            .collect::<Vec<_>>();
        
        let drain = a.drain_filter(|dc| {
            if dc.id == 4 { panic!("let's say a unwrap went wrong"); }
            dc.id < 4
        });
        
        drain.for_each(::std::mem::drop);
    });
    println!("-- end --");
    //output:
    // -- start --
    // dropped: 0    <-\
    // dropped: 1       \_ this is a double drop
    // dropped: 0  _  <-/
    // dropped: 5   \------ here 4 got leaked (kind fine)  
    // dropped: 6
    // -- end --
    
}