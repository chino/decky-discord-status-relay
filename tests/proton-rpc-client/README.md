# Proton rich presence test client

A tiny Windows program that sends a Discord status through `\\.\pipe\discord-ipc-0`, for
checking the Proton feature end to end without a game that uses Discord.

Build:

    docker run --rm -v "$PWD:/src" -w /src debian:bookworm-20241202-slim sh -c \
      'apt-get update -qq && apt-get install -y -qq gcc-mingw-w64-x86-64 && x86_64-w64-mingw32-gcc -O2 -mwindows -o rpcwin.exe rpcwin.c'

Test: add `rpcwin.exe` to Steam as a non-Steam game, force Proton, and **launch it from the
library** (a launch through `SteamClient.Apps.RunGame` leaves Steam stuck on "Cancel" after
it exits). The window reports whether the status went through; Discord should show
"rpc-bridge test from Proton". Optional launch option: a Discord application ID.
