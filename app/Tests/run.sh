#!/usr/bin/env bash
# Тесты чистой Swift-логики (без AppKit/CoreAudio): собрать и запустить.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${TMPDIR:-/tmp}/meetmind-swift-tests"
swiftc -O -o "$OUT" "$DIR/../Sources/MeetMind/CallDetectionMachine.swift" "$DIR/../Sources/MeetMind/IslandModel.swift" "$DIR/../Sources/MeetMind/AudioMix.swift" "$DIR/../Sources/MeetMind/RuntimeLogic.swift" "$DIR/main.swift"
"$OUT"
