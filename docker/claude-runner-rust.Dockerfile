# docker/claude-runner-rust.Dockerfile
# The WORKBENCH image for Rust repos in the `claudecode-dockerfile` lane. This is where the agent
# explores and compiles live; it is NOT the base of the Dockerfile the agent emits (that is
# RUST_PROFILE.default_base, also rust:1) and it is never measured.
#
# rust:1 (bookworm), not -slim or alpine: RustLanguage.ensure_cmd curls a prebuilt cargo-nextest,
# and rust.py hardcodes PATH=/usr/local/cargo/bin, which is the full image's layout.
FROM rust:1

# System basics the agent commonly needs, plus Node for the CLI. pkg-config/libssl-dev/cmake are
# the three that unblock the majority of -sys crates (openssl-sys, ring, prost/protobuf builds);
# without them the agent burns turns rediscovering them on almost every non-trivial crate.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential ca-certificates sudo pkg-config libssl-dev cmake \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# NOTE: no chmod on CARGO_HOME/RUSTUP_HOME is needed — the official rust image already makes
# /usr/local/cargo and /usr/local/rustup world-writable (verified: drwxrwxrwx), so the
# unprivileged `agent` user can fetch crates and let rustup install a pinned toolchain.
# The image default user intentionally stays root; the producer selects `agent` per-exec.
WORKDIR /testbed
