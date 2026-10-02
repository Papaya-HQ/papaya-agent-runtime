# The Papaya Agent Runtime as a cloud runner Papaya hosts (PAP-334).
#
# A cloud provider (Maritime first) boots this as a microVM, sleeps it when it is
# idle, and wakes it when Papaya's doorbell rings. It runs `ppy serve --cloud`
# from a checkout of this repository at /opt/papaya-agent-runtime: the same
# runtime, pointed at the same checkout, as a person's own machine runs. The
# owner signs Claude Code (or Codex) and GitHub in themselves, in the runner's
# sign-in terminal (`ppy setup`); see src/papaya_agent_runtime/cloud.py.
#
# PUBLIC by necessity (the provider holds no registry credentials), so nothing
# secret is baked in: the connection token arrives as a provider secret at boot,
# and every sign-in is made on the running VM and kept on its /data disk.
#
# Debian, not Alpine: Maritime's VM init (/sbin/maritime-init) is a glibc binary
# and panics on a musl image ("failed (error -2)", seen 2026-10-02).
#
# Build from the repository root, with the full git history so the runtime can
# read its version from its tag (src/papaya_agent_runtime/version.py):
#   docker buildx build --platform linux/amd64 -f docker/cloud-runner/Dockerfile -t papaya-agent-runtime:dev .
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

# git and gh for the work; bash, procps and ripgrep for the harnesses.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gnupg openssh-client procps ripgrep \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# The harness CLIs, unmodified, from their publishers' own packages. Corepack puts
# pnpm and yarn on the PATH (fetched at the version a repository pins, on first
# use), so a JavaScript repository's own checks can run.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && corepack enable

# uv, and the Python it manages, outside /data so the volume does not hide them.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy

# The checkout, its environment built the runtime's own way (`ppy env sync`):
# from the lockfile, so the client comes from PyPI at the version uv.lock pins,
# and stamped, so `ppy serve` finds it current and never rebuilds it at boot.
WORKDIR /opt/papaya-agent-runtime
COPY . .
# A build from a `git worktree` has a .git FILE pointing outside the build
# context; dropped, the runtime reports a fallback version instead of failing.
RUN if [ -f .git ]; then rm .git; fi \
 && UV_CACHE_DIR=/tmp/uv-cache ./bin/ppy env sync \
 && rm -rf /tmp/uv-cache \
 && git -C / config --system --add safe.directory /opt/papaya-agent-runtime \
 && rm -rf /opt/papaya-agent-runtime/.ppy \
 && ln -s /data/ppy /opt/papaya-agent-runtime/.ppy
# On PATH as the checkout's own launcher: bin/ppy finds its checkout from its own
# path, so a symlink elsewhere would point it at the wrong one.
ENV PATH="/opt/papaya-agent-runtime/bin:${PATH}"

# Everything that must outlive the VM sleeping lives under /data: the runtime's
# state, the client home, and HOME itself, where Claude Code, Codex and gh keep
# the sign-ins the owner makes in the terminal. The state stays where it is on
# a laptop, the checkout's own `.ppy`, as a link to /data/ppy: every path the
# runtime's prompts name (`.ppy/memory/...` in the runtime directory) is then true.
ENV PAPAYA_AGENT_HOME=/data/papaya-agent \
    HOME=/data/home \
    UV_CACHE_DIR=/data/uv-cache \
    PORT=18789 \
    IS_SANDBOX=1 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 18789

COPY docker/cloud-runner/entrypoint.sh /usr/local/bin/papaya-cloud-runner
ENTRYPOINT ["papaya-cloud-runner"]
