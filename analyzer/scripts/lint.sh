#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(dirname "$(realpath "${BASH_SOURCE[0]}")")
cd "$SCRIPT_DIR/../"

clj-kondo --lint deps.edn dev src test
