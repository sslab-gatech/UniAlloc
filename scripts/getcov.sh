#!/bin/bash
set -euxo;

# if [[ `uname -m` == 'arm64' ]]; then
# exit 0;
# fi

rustup component add llvm-tools-preview
cargo install cargo-binutils

RUSTFLAGS="-Zinstrument-coverage" \
    LLVM_PROFILE_FILE="json5format-%m.profraw" \
    cargo test --tests

cargo profdata -- merge \
    -sparse ./*/json5format-*.profraw -o json5format.profdata

COVERAGE=$(cargo cov -- report --ignore-filename-regex='/.cargo/registry|hashbrown|/library/std|tests/|/usr/local/cargo/registry' \
    $( \
      for file in \
        $( \
          RUSTFLAGS="-Zinstrument-coverage" \
            cargo test --tests --no-run --message-format=json \
              | jq -r "select(.profile.test == true) | .filenames[]" \
              | grep -v dSYM - \
        ); \
      do \
        printf "%s %s " -object $file; \
      done \
    ) \
	--instr-profile=json5format.profdata --summary-only | tail -1 | egrep  "[0-9]{2,3}\.[0-9]{2}\%"  | awk -F'  *' '{print $10}')

echo "Coverage:" $COVERAGE
# math the last percentage
# [0-9]{2,3}\.[0-9]{2}\%(?![\s\S]*?[0-9]{2,3}\.[0-9]{2}\%)

find . -name "json5format*" | xargs rm
