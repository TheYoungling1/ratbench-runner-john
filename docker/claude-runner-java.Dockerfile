# docker/claude-runner-java.Dockerfile
# The WORKBENCH image for Java repos in the `claudecode-dockerfile` lane. This is where the agent
# explores and compiles live; it is NOT the base of the Dockerfile the agent emits (that is
# JAVA_PROFILE.default_base, also maven:3-eclipse-temurin-17) and it is never measured.
#
# maven:3-eclipse-temurin-17 sets JAVA_HOME=/opt/java/openjdk, which is exactly the default
# java.py falls back to, and puts mvn on the login-shell PATH. It ships NO gradle binary, and that
# is deliberate: JavaLanguage prefers ./gradlew, which 12 of the 13 Gradle repos in rat_java50.json
# ship. The RAT reference image apt-installs gradle, but measured that is Gradle 4.4.1 (2017),
# which cannot build a modern project — so baking it in would add ~150MB of false confidence. The
# agent has curl and sudo and is told by the prompt to fetch a real Gradle when a repo needs one.
FROM maven:3-eclipse-temurin-17

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates sudo \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root. The agent needs
# a writable ~/.m2, which its own home provides.
#
# No `-u 1000` here, unlike the python and rust workbenches. This base is UBUNTU-derived and ships
# a stock `ubuntu` user already holding uid 1000, so pinning it makes useradd exit 4 and the build
# fail. Nothing in the harness cares about the number: the producer addresses this account purely
# by name (`docker exec -u agent`, `chown -R agent:agent /testbed`), so letting useradd pick the
# next free uid is correct and keeps us from deleting a user the base image put there.
RUN useradd -m -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# The image default user intentionally stays root; the producer selects `agent` per-exec.
WORKDIR /testbed
