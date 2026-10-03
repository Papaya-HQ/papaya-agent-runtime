#!/usr/bin/env bash
# The cloud runner's entrypoint: make the /data layout, then become `ppy serve --cloud`.
# The volume is empty on a runner's first boot, so the directories are made here,
# not in the image, where the volume would hide them.
set -euo pipefail
# HOME is pinned here, not only in the image's ENV: Maritime's VM init starts this
# with its own HOME, which replaced the image's, so Claude Code, Codex and gh signed
# in on the root disk and a redeploy would have lost every sign-in.
export HOME=/data/home
mkdir -p /data/ppy "$PAPAYA_AGENT_HOME" "$HOME" "$UV_CACHE_DIR"
chmod 700 "$PAPAYA_AGENT_HOME" "$HOME"
cd /opt/papaya-agent-runtime
exec ./bin/ppy serve --cloud "$@"
