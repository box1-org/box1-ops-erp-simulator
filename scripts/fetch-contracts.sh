#!/bin/sh
# Fetch box1-ops-contracts at the commit pinned in contracts.lock into .contracts/.
# Tests read the ERP1 golden fixtures from there; they are never copied into this repository.
set -eu
cd "$(dirname "$0")/.."
sha=$(sed -n 's/^commit=//p' contracts.lock)
url=$(sed -n 's/^repository=//p' contracts.lock)
if [ ! -d .contracts/.git ]; then git clone --quiet --filter=blob:none --no-checkout "$url" .contracts; fi
git -C .contracts fetch --quiet origin "$sha" 2>/dev/null || git -C .contracts fetch --quiet origin
git -C .contracts -c advice.detachedHead=false checkout --quiet "$sha"
echo "box1-ops-contracts at $(git -C .contracts rev-parse HEAD)"
