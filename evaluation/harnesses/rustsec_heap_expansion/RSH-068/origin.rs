use pared::sync::Parc;

fn main() {
    let s = "Hello World!".to_owned();
    let x = Parc::new(&());
    let x = x.project(|_| s.as_str());

    println!("{:?}", &*x);
    drop(s);
    println!("{:?}", &*x);
}
