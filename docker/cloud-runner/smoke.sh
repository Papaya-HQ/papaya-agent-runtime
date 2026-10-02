#!/usr/bin/env bash
# Smoke-test a built cloud runner image: the tools are there, the runtime runs,
# and without its token the runner refuses to start and names what it needs.
#   docker/cloud-runner/smoke.sh <image> [expected version]
set -euo pipefail
image="$1"
expected="${2:-}"
run() { docker run --rm --entrypoint "$1" "$image" "${@:2}"; }

run claude --version
run codex --version
run gh --version | head -1
run git --version
version="$(run ppy version | tail -1 | awk '{print $NF}')"
echo "ppy version: ${version}"
if [ -n "$expected" ] && [ "$version" != "$expected" ]; then
  echo "::error::the image reports ${version}, not ${expected}"
  exit 1
fi
run /opt/papaya-agent-runtime/.venv/bin/python -c "import papaya_agent_client.cloud_host"

set +e
output="$(docker run --rm -v papaya-runner-smoke:/data "$image" 2>&1)"
status=$?
set -e
docker volume rm -f papaya-runner-smoke >/dev/null
echo "$output" | tail -5
if [ "$status" -eq 0 ] || ! grep -q PAPAYA_AGENT_TOKEN <<<"$output"; then
  echo "::error::the runner started without PAPAYA_AGENT_TOKEN (exit ${status})"
  exit 1
fi
echo "smoke test passed"
