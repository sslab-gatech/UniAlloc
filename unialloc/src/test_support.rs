extern crate std;

use std::process::Command;
use std::string::String;

const ISOLATED_TEST_CHILD_MARKER: &str = "UNIALLOC_ISOLATED_TEST_CHILD=";

fn proves_exactly_one_test_ran(stdout: &str, test_name: &str) -> bool {
    let marker = [ISOLATED_TEST_CHILD_MARKER, test_name].concat();
    stdout
        .lines()
        .flat_map(str::split_whitespace)
        .any(|token| token == marker)
        && stdout.lines().any(|line| {
            line.trim()
                .starts_with("test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured;")
        })
}

/// Re-executes one libtest case when process-wide state must be isolated.
///
/// Returns `true` in the parent after the child succeeds and `false` inside
/// the selected child test so the caller can run the test body exactly once.
pub(crate) fn run_test_in_fresh_process(child_env: &str, test_name: &str) -> bool {
    if std::env::var(child_env).ok().as_deref() == Some(test_name) {
        std::println!("{ISOLATED_TEST_CHILD_MARKER}{test_name}");
        return false;
    }

    let output = Command::new(std::env::current_exe().expect("current allocator test executable"))
        .args(["--exact", test_name, "--nocapture", "--test-threads=1"])
        .env(child_env, test_name)
        .env("RUST_BACKTRACE", "0")
        .output()
        .expect("spawn isolated allocator test");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    assert!(
        output.status.success(),
        "isolated test {} failed\nstdout:\n{}\nstderr:\n{}",
        test_name,
        stdout,
        stderr,
    );
    assert!(
        proves_exactly_one_test_ran(&stdout, test_name),
        "isolated test {} did not prove that exactly one selected test ran\n\
         stdout:\n{}\nstderr:\n{}",
        test_name,
        stdout,
        stderr,
    );
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_NAME: &str = "test_support::tests::example";

    #[test]
    fn exact_child_proof_accepts_one_selected_test() {
        let stdout = "\
running 1 test\n\
test test_support::tests::example ... UNIALLOC_ISOLATED_TEST_CHILD=test_support::tests::example\n\
ok\n\n\
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 10 filtered out\n";

        assert!(proves_exactly_one_test_ran(stdout, TEST_NAME));
    }

    #[test]
    fn exact_child_proof_rejects_zero_tests() {
        let stdout = "\
running 0 tests\n\n\
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 11 filtered out\n";

        assert!(!proves_exactly_one_test_ran(stdout, TEST_NAME));
    }

    #[test]
    fn exact_child_proof_rejects_an_unrelated_marker() {
        let stdout = "\
running 1 test\n\
UNIALLOC_ISOLATED_TEST_CHILD=test_support::tests::other\n\n\
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 10 filtered out\n";

        assert!(!proves_exactly_one_test_ran(stdout, TEST_NAME));
    }

    #[test]
    fn exact_child_proof_rejects_a_test_name_prefix_match() {
        let stdout = "\
running 1 test\n\
UNIALLOC_ISOLATED_TEST_CHILD=test_support::tests::example_extra\n\n\
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 10 filtered out\n";

        assert!(!proves_exactly_one_test_ran(stdout, TEST_NAME));
    }

    #[test]
    fn exact_child_proof_rejects_multiple_tests() {
        let stdout = "\
running 2 tests\n\
UNIALLOC_ISOLATED_TEST_CHILD=test_support::tests::example\n\n\
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 9 filtered out\n";

        assert!(!proves_exactly_one_test_ran(stdout, TEST_NAME));
    }
}
