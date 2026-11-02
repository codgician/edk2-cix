#!/usr/bin/env bash
# shellcheck shell=bash
# Run the Pi agent read-only on a prompt file inside this repository. The guard
# extension (.github/pi/ci-guard.ts) limits writes to .ai-state/result and
# strips credentials from the agent's shell. `--check` runs the guard self-test.
set -euo pipefail

input=${1:?prompt file or --check is required}

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
runtime_dir=$(mktemp -d)
trap 'rm -rf "$runtime_dir"' EXIT
cp "$repository_root/.github/pi/models.json" "$runtime_dir/models.json"
cp "$repository_root/.github/pi/settings.json" "$runtime_dir/settings.json"
export PI_CODING_AGENT_DIR="$runtime_dir"
export PI_OFFLINE=1
export PI_TELEMETRY=0

if [[ "$input" == "--check" ]]; then
  # `--list-models` does not load extensions; a print run does, and the guard
  # stops it after its self-test with a fixed message instead of calling a model.
  export PI_GUARD_SELF_TEST=1
  cd -- "$repository_root"
  set +e
  output=$(DENDRO_API_KEY=self-test pi \
    --print \
    --no-session \
    --offline \
    --no-approve \
    --no-extensions \
    --extension "$repository_root/.github/pi/ci-guard.ts" \
    self-test 2>&1)
  set -e
  printf '%s\n' "$output"
  if [[ "$output" != *"Pi CI guard self-test passed"* ]]; then
    echo "Pi CI guard self-test did not pass" >&2
    exit 1
  fi
  exit 0
fi

: "${DENDRO_API_KEY:?DENDRO_API_KEY is required}"

prompt_path=$(realpath -- "$input")
case "$prompt_path" in
  "$repository_root"/*) ;;
  *)
    echo "Prompt file must be inside the repository: $input" >&2
    exit 2
    ;;
esac

prompt=$(<"$prompt_path")
cd -- "$repository_root"

pi \
  --print \
  --no-session \
  --offline \
  --no-approve \
  --no-extensions \
  --extension "$repository_root/.github/pi/ci-guard.ts" \
  --no-skills \
  --no-prompt-templates \
  --no-themes \
  --no-context-files \
  --tools read,bash,write,grep,find,ls \
  "$prompt"
