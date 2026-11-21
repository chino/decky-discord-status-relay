#!/bin/sh
# Clones rpc-bridge at a pinned commit and builds bridge.exe.
set -eu
# Fork of EnderIce2/rpc-bridge v1.4.1.3 plus the Proton fix (upstream PR pending).
REPO=https://github.com/chino/rpc-bridge
COMMIT=6bdd258d1814b705bee03b6b3a0706f4e1f4c307   # fix/staging-check-wine-11.5

work=$(mktemp -d)
git clone --quiet "$REPO" "$work/rpc-bridge"
cd "$work/rpc-bridge"
git -c advice.detachedHead=false checkout --quiet "$COMMIT"
make -s build

mkdir -p /backend/out
cp build/bridge.exe /backend/out/bridge.exe
cp LICENSE /backend/out/LICENSE.rpc-bridge
echo "built $(sha256sum /backend/out/bridge.exe)"
