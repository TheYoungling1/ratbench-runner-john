# docker/claude-runner.Dockerfile
# The WORKBENCH image for the Claude Code lanes (`claudecode`, `claudecode-dockerfile`).
# This is where the agent explores and installs live; it is NOT the base of the Dockerfile the
# agent emits (that is CLAUDE_DOCKERFILE_BASE, default python:3.11) and it is never measured.
FROM python:3.11-slim

# System basics the agent commonly needs, plus Node for the CLI.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential ca-certificates sudo \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root.
# Passwordless sudo lets the agent install system-wide where a repo needs it.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# NOTE: the image default user intentionally stays root. The producer selects the unprivileged
# `agent` user per-exec via `docker exec -u agent` for the Claude Code agent, while container
# setup steps (docker cp of the repo, chown) require root and run as the default user.
WORKDIR /testbed
