#!/usr/bin/env bash
# BREAK-GLASS ONLY. Releases are normally cut by release-please: merge the
# "chore: release X.Y.Z" PR it maintains (see CONTRIBUTING.md). Use this manual
# path only if the release-please workflow is broken and you must ship. Bumping
# the version here by hand will desync .release-please-manifest.json — update it
# to match afterwards.
#
# Cut a release: bump the version, roll the changelog, commit, tag.
#
#   scripts/release.sh 1.0.1
#
# Then push with:  git push origin main --follow-tags
# which triggers the GitHub Actions workflow (build image -> GHCR -> Fly).
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 X.Y.Z"; exit 1; }
VER="$1"
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z"; exit 1; }
DATE="$(date +%F)"

# 1. pyproject version
python3 - "$VER" <<'PY'
import re, sys, pathlib
ver = sys.argv[1]
p = pathlib.Path("pyproject.toml")
p.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{ver}"', p.read_text(), count=1))
PY

# 2. changelog: promote [Unreleased] to the new version, add fresh [Unreleased]
python3 - "$VER" "$DATE" <<'PY'
import sys, pathlib
ver, date = sys.argv[1], sys.argv[2]
p = pathlib.Path("CHANGELOG.md")
t = p.read_text()
assert "## [Unreleased]" in t, "no [Unreleased] section in CHANGELOG.md"
t = t.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{ver}] - {date}", 1)
p.write_text(t)
PY

git add pyproject.toml CHANGELOG.md
git commit -m "Release v$VER"
git tag -a "v$VER" -m "v$VER"
echo
echo "Tagged v$VER. Push it with:"
echo "    git push origin main --follow-tags"
