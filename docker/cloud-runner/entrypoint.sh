#!/usr/bin/env bash
# The cloud runner's entrypoint: make the /data layout, then become `ppy serve --cloud`.
# The volume is empty on a runner's first boot, so the directories are made here,
# not in the image, where the volume would hide them.
set -euo pipefail
mkdir -p "$PPY_HOME" "$PAPAYA_AGENT_HOME" "$HOME" "$UV_CACHE_DIR"
chmod 700 "$PAPAYA_AGENT_HOME" "$HOME"
cd /opt/papaya-agent-runtime
exec ./bin/ppy serve --cloud "$@"
