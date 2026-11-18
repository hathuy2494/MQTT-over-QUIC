#!/bin/sh
# Native Linux dependencies only. Does not start an experiment or change NetEm.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
deps=${DEPS:-"$here/deps"}
jobs=${JOBS:-2}
mkdir -p "$deps"
fetch() {
  directory=$1 repository=$2 revision=$3
  if [ ! -d "$directory" ]; then
    git init "$directory"
    git -C "$directory" remote add origin "$repository"
    git -C "$directory" fetch --depth 1 origin "$revision"
    git -C "$directory" checkout --detach FETCH_HEAD
  fi
  actual=$(git -C "$directory" rev-parse HEAD)
  if [ "$actual" != "$revision" ]; then
    echo "Wrong dependency revision in $directory: $actual; expected $revision" >&2
    exit 2
  fi
}
# LSQUIC v4.6.3 + official BoringSSL commit exposing the TLS 1.3 cipher-list API.
# New pin is required: older BoringSSL could not restrict the ClientHello to AES128.
fetch "$deps/boringssl" https://github.com/google/boringssl.git ebd922adc708a48b0cccfcdf81e854a3fdc1b7b6
fetch "$deps/lsquic" https://github.com/litespeedtech/lsquic.git 0fa4f361094c1899b7bd664e5ee96e82e0af7e18
git -C "$deps/lsquic" submodule update --init --recursive --depth 1
cmake -S "$deps/boringssl" -B "$deps/boringssl/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
cmake --build "$deps/boringssl/build" --target ssl crypto -j "$jobs"
cmake -S "$deps/lsquic" -B "$deps/lsquic/build" -DCMAKE_BUILD_TYPE=Release -DLIBSSL_DIR="$deps/boringssl" -DLIBSSL_LIB="$deps/boringssl/build" -DLSQUIC_LIBSSL=BORINGSSL -DLSQUIC_BIN=OFF -DLSQUIC_TESTS=OFF -DLSQUIC_SHARED_LIB=OFF
cmake --build "$deps/lsquic/build" -j "$jobs"
printf 'Dependencies ready. Next: make -C "%s" DEPS="%s"\n' "$here" "$deps"
