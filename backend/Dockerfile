# Builds rpc-bridge's bridge.exe for the "Proton games" feature.
# The decky CLI builds this image and runs it with backend/ mounted at /backend
# and an empty directory at /backend/out; whatever lands in /backend/out ships as bin/.
FROM debian:bookworm-20241202-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git make \
      gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64 \
 && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/bin/sh", "/backend/entrypoint.sh"]
