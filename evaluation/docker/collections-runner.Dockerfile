# Docker runner for UniAlloc paper Collections cells.
# Build example:
#   docker build -f evaluation/docker/collections-runner.Dockerfile \
#     -t unialloc-collections-runner:nightly-2022-07-01 .
FROM rust:1.64-bullseye
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates build-essential pkg-config cmake libclang-rt-16-dev \
    && rm -rf /var/lib/apt/lists/*
RUN rustup toolchain install nightly-2022-07-01
WORKDIR /work
