#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Inspect locally persisted evaluation evidence in PostgreSQL.

Usage: scripts/inspect_evidence.sh [--run-id UUID] [--limit N] [--envelope] [--refs]

Examples:
  scripts/inspect_evidence.sh
  scripts/inspect_evidence.sh --run-id 00000000-0000-0000-0000-000000000000 --envelope --refs

Requires the opt-in synthetic evidence stack: `make local-evidence-up`.
The default local stack keeps evidence in memory and has no evidence tables.
Payload bytes live in the local versioned S3 emulator; --refs prints their
object keys and versions, not their contents.
EOF
}

run_id=
limit=10
show_envelope=false
show_refs=false
while (($#)); do
  case "$1" in
    --run-id)
      (($# >= 2)) || { echo "--run-id requires a UUID" >&2; exit 2; }
      run_id=$2
      shift 2
      ;;
    --limit)
      (($# >= 2)) || { echo "--limit requires a positive integer" >&2; exit 2; }
      limit=$2
      shift 2
      ;;
    --envelope)
      show_envelope=true
      shift
      ;;
    --refs)
      show_refs=true
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

[[ "$limit" =~ ^[1-9][0-9]*$ ]] || { echo "limit must be a positive integer" >&2; exit 2; }
if [[ -n "$run_id" && ! "$run_id" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then
  echo "run ID must be a UUID" >&2
  exit 2
fi

env_file=.env.example
if [[ -f .env.evidence ]]; then
  env_file=.env.evidence
fi
compose=(docker compose -p scalable-rag-eval --profile eval-local --env-file "$env_file" -f docker-compose.yml -f docker-compose.evaluation.yml)
psql=("${compose[@]}" exec -T postgres psql -X -P pager=off -U ragadmin -d rag_db)
filter=TRUE
if [[ -n "$run_id" ]]; then
  # The value is constrained to a UUID above before it is inserted into SQL.
  filter="r.run_id = '$run_id'::uuid"
fi

echo "Persisted evaluation runs"
"${psql[@]}" -c "SELECT r.run_id, r.export_version, r.run_schema_version, r.completeness,
       COALESCE(o.state, 'unknown') AS delivery_state, r.completed_at,
       count(p.id) AS payload_refs
FROM evaluation_runs r
LEFT JOIN evaluation_outbox o ON o.run_pk = r.id
LEFT JOIN evaluation_payload_refs p ON p.run_pk = r.id
WHERE $filter
GROUP BY r.id, o.state
ORDER BY r.completed_at DESC
LIMIT $limit;"

if [[ "$show_envelope" == true ]]; then
  echo "Safe export envelope"
  "${psql[@]}" -c "SELECT r.run_id, jsonb_pretty(convert_from(r.envelope, 'UTF8')::jsonb) AS envelope
FROM evaluation_runs r
WHERE $filter
ORDER BY r.completed_at DESC
LIMIT $limit;"
fi

if [[ "$show_refs" == true ]]; then
  echo "Payload references (object contents are not read)"
  "${psql[@]}" -c "SELECT r.run_id, p.kind, p.ordinal, p.item_ordinal, p.bucket,
       p.object_key, p.version_id, p.byte_size, p.sha256, p.expires_at
FROM evaluation_payload_refs p
JOIN evaluation_runs r ON r.id = p.run_pk
WHERE $filter
ORDER BY r.completed_at DESC, p.kind, p.ordinal, p.item_ordinal
LIMIT $limit;"
fi
