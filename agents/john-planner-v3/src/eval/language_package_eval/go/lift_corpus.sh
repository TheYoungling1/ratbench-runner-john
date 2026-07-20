#!/usr/bin/env bash
# Lift manifest-only Go corpus into GO_SMOKE_ROOT. Manifests only — no clones.
# Each repo pinned to a tag so `go list -m all` is reproducible.
set -euo pipefail
ROOT="${GO_SMOKE_ROOT:-outputs/graph_fidelity/_smoke_go}"
mkdir -p "$ROOT"

lift() {  # <name> <owner/repo> <tag> <subpath>
  local name="$1" repo="$2" tag="$3" sub="${4:-}"
  local base="https://raw.githubusercontent.com/$repo/$tag/$sub"
  mkdir -p "$ROOT/$name"
  curl -fsSL "${base}go.mod" -o "$ROOT/$name/go.mod"
  curl -fsSL "${base}go.sum" -o "$ROOT/$name/go.sum" || echo "  (no go.sum for $name)"
  echo "lifted $name <- $repo@$tag ${sub:+($sub)}"
}

# PROXY-VERIFIED go directives (2026-07-05) — labels reflect REALITY, not intent:
lift viper       spf13/viper           v1.18.2   # anchor: go 1.18 (>=1.17) VERIFIED
lift prometheus  prometheus/prometheus v2.51.0   # large: verify go>=1.17 AND no local replace (Step 2)
lift cobra       spf13/cobra           v1.8.0    # go 1.15 -> RESOLVE-REQUIRED axis (verified)
lift uuid        google/uuid           v1.6.0    # NO go directive -> RESOLVE-REQUIRED axis (verified)
echo "NOTE: verify each go.mod's 'go' directive after lift (Step 2) — the draft mis-picked cobra/uuid."
echo "NOTE: go.work(#3), registry-replace(#5), vendored(#4) constructed in Step 3; a"
echo "      verified >=1.17 tiny/zero-dep entry is OPTIONAL (unit tests already cover empty closure)."
