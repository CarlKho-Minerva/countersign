#!/bin/sh
# Build both images from the repository root.
set -eu
cd "$(dirname "$0")/.."
docker build -q -f sandbox/Dockerfile.proxy -t countersign-proxy .
docker build -q -f sandbox/Dockerfile.box -t countersign-box .
