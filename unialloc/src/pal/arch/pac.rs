/// ARM v8.3 pointer authentication
/// https://www.qualcomm.com/media/documents/files/whitepaper-pointer-authentication-on-armv8-3.pdf
use crate::*;

/// Sign the pointer under the given context
///
/// For example, by using the address of the pointer as context,
/// we can safely bind the pointer to a specific address
#[inline]
pub fn pacib(ptr: usize, context: usize) -> usize {
    debug_assert!(ptr <= 1 << 40);
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

/// simply strip the signed pointer
pub use xpaci as strip_unchecked;

/// check the ownership of signed pointer `ptr`
///
/// return
/// error: 0
/// success: unsigned ptr
#[inline]
pub fn strip_checked(ptr: usize, owner: usize) -> usize {
    // TODO: rewrite it with ASM
    let unsigned = xpaci(ptr);
    let auth_res = autib(ptr, owner);

    if unsigned == auth_res {
        unsigned
    } else {
        panic!(
            "[PAC] Potential attacks on signed pointers!
            ptr (signed): 0x{:x}, owner: 0x{:x}, auth_res: 0x{:x}",
            ptr, owner, auth_res
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::*;

    #[test]
    fn it_works() {
        let err_val = 0x4000000000000000;
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let signed_ptr = pacib(ptr, 0);
        let wrong_sign = autib(ptr, 0);
        println!("wrong sign: {:x}", wrong_sign);
        println!("correct sign: {:x}", autib(signed_ptr, 0));
        assert_eq!(wrong_sign, err_val + ptr);
        assert_eq!(ptr, autib(signed_ptr, 0));
        assert_eq!(ptr, xpaci(signed_ptr));
    }

    #[test]
    fn non_zero_context_test() {
        let err_val = 0x4000000000000000;
        let x = 0xdeadbeefusize;
        let ptr = &x as *const _ as usize;
        let signed_ptr = pacib(ptr, 0xdeadbeef);
        let wrong_sign = autib(ptr, 0xdeadbeef);
        assert_eq!(wrong_sign, err_val + ptr);
        assert_eq!(ptr, autib(signed_ptr, 0xdeadbeef));
        assert_eq!(ptr, xpaci(signed_ptr));
    }

    #[test]
    fn random_test() {
        let x = pacib(0x280000000, 0x280000020);
        println!("{:x}", x);
        println!("{:x}", autib(x, 0x280000020));
    }
}
