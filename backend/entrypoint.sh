#!/bin/sh
# Clones rpc-bridge at a pinned commit, applies our Proton patch and builds bridge.exe.
set -eu
REPO=https://github.com/EnderIce2/rpc-bridge
COMMIT=6aa60772a5241c7bd1b8d38f026b2a5168e0f93c   # v1.4.1.3

work=$(mktemp -d)
git clone --quiet "$REPO" "$work/rpc-bridge"
cd "$work/rpc-bridge"
git -c advice.detachedHead=false checkout --quiet "$COMMIT"
git apply /backend/rpc-bridge-proton.patch
make -s build

mkdir -p /backend/out
cp build/bridge.exe /backend/out/bridge.exe
cp LICENSE /backend/out/LICENSE.rpc-bridge
echo "built $(sha256sum /backend/out/bridge.exe)"
