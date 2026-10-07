#!/usr/bin/env bash
# Supply-chain and secret scans (ADR 0011). Exit code != 0 when anything is found.
#   bash scripts/security-scan.sh
# Needs: uv (python -m uv), npm, docker. CI wiring: Phase 13.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATUS=0

echo "== Backend dependencies (pip-audit, locked versions) =="
REQ="$(mktemp)"
(cd "$ROOT/backend" && python -m uv export --frozen --no-hashes --no-emit-project > "$REQ")
python -m uv tool run pip-audit -r "$REQ" --progress-spinner off || STATUS=1
rm -f "$REQ"

echo "== Visual dependencies (npm audit: shipped, then all) =="
(cd "$ROOT/visual" && npm audit --omit=dev && npm audit) || STATUS=1

echo "== Secrets (gitleaks: git history, then working tree) =="
for mode in git dir; do
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/repo" zricethezav/gitleaks:latest \
        "$mode" /repo --config /repo/.gitleaks.toml --no-banner --redact || STATUS=1
done

[ "$STATUS" -eq 0 ] && echo "All security scans clean." || echo "Security scans found issues."
exit "$STATUS"
