#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Show spans printed by the local OpenTelemetry Collector debug exporter.

Usage: scripts/inspect_traces.sh [--since DURATION] [--follow]

Examples:
  scripts/inspect_traces.sh
  scripts/inspect_traces.sh --since 30m --follow

Start the local stack first with `make local-up`. The Collector's debug output
is temporary container logging, not a trace database or UI.
EOF
}

since=5m
follow=false
while (($#)); do
  case "$1" in
    --since)
      (($# >= 2)) || { echo "--since requires a duration" >&2; exit 2; }
      since=$2
      shift 2
      ;;
    --follow|-f)
      follow=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$since" =~ ^[0-9]+[smhd]$ ]] || {
  echo "duration must be an integer followed by s, m, h, or d" >&2
  exit 2
}

compose=(docker compose -p scalable-rag-eval --profile eval-local --env-file .env.example -f docker-compose.yml)
args=(logs --no-color --since "$since")
if [[ "$follow" == true ]]; then
  args+=(--follow)
fi
"${compose[@]}" "${args[@]}" otel-collector
