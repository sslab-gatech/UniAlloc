# Docker/Wine runner for UniAlloc Windows platform_allocator_workload evidence.
# Build example:
#   docker build --platform linux/amd64 -f evaluation/docker/windows-wine-runner.Dockerfile \
#     -t unialloc-windows-wine-runner:bookworm evaluation/docker/windows-wine-runner-context
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wine wine64 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENTRYPOINT ["/usr/bin/wine"]
