#!/usr/bin/env bash
# Convert recorded/TTS prompts into the two formats Asterisk serves to the SIM7600 (8 kHz and 16 kHz).
# usage: convert_prompts.sh INPUT_DIR OUTPUT_SOUNDS_DIR     (INPUT_DIR/<lang>/<name>.<any audio>)
# result: OUTPUT/<lang>/<name>.wav (8 kHz mono PCM16) and OUTPUT/<lang>/<name>.sln16 (raw 16 kHz)
# ARI plays them as sound:chaar/<lang>/<name>; Asterisk picks the cheapest format to the call codec.
set -euo pipefail
in="${1:?input dir}"; out="${2:?output dir}"
find "$in" -type f \( -name '*.wav' -o -name '*.mp3' -o -name '*.m4a' -o -name '*.ogg' -o -name '*.flac' \) | while read -r f; do
  rel="${f#"$in"/}"; base="${rel%.*}"
  mkdir -p "$out/$(dirname "$base")"
  ffmpeg -nostdin -loglevel error -y -i "$f" -ac 1 -ar 8000  -c:a pcm_s16le "$out/$base.wav"
  ffmpeg -nostdin -loglevel error -y -i "$f" -ac 1 -ar 16000 -f s16le        "$out/$base.sln16"
  echo "ok $base"
done
