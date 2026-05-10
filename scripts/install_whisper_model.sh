#!/usr/bin/env bash
# install_whisper_model.sh — fetch whisper.cpp + ggml model for VOICE_BACKEND=whisper.
#
# Usage:
#   ./scripts/install_whisper_model.sh           # default model = medium (~1.5GB)
#   MODEL=small ./scripts/install_whisper_model.sh
#
# Models (HuggingFace ggerganov/whisper.cpp):
#   tiny    (~75MB)   — English-only fast
#   base    (~142MB)  — English-only good
#   small   (~488MB)  — multilingual decent
#   medium  (~1.5GB)  — multilingual + Russian quality (recommended)
#   large   (~3.0GB)  — best quality, exceeds 2GB target
#
# After install, set VOICE_BACKEND=whisper in your .env (or VOICE_BACKEND=auto
# on Linux). On macOS auto routes to whisper too in the current build.

set -euo pipefail

MODEL="${MODEL:-medium}"
CCBOT_DIR_DEFAULT="${HOME}/.ccbot"
TARGET_DIR="${CCBOT_DIR:-$CCBOT_DIR_DEFAULT}/models"
TARGET_FILE="${TARGET_DIR}/ggml-${MODEL}.bin"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${MODEL}.bin"

case "$MODEL" in
  tiny|base|small|medium|large|tiny.en|base.en|small.en|medium.en) ;;
  *) echo "ERROR: unknown MODEL '$MODEL'. Try tiny|base|small|medium|large." >&2; exit 1 ;;
esac

# Ensure whisper-cli (the binary the bot shells out to) is available.
if ! command -v whisper-cli >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo ">>> Installing whisper-cpp via Homebrew…"
    brew install whisper-cpp
  else
    echo "ERROR: whisper-cli not found and Homebrew unavailable." >&2
    echo "Install whisper.cpp from https://github.com/ggerganov/whisper.cpp" >&2
    exit 1
  fi
fi

mkdir -p "$TARGET_DIR"

if [[ -f "$TARGET_FILE" ]]; then
  echo ">>> $TARGET_FILE already exists, skipping download."
else
  echo ">>> Downloading $MODEL → $TARGET_FILE"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --progress-bar "$URL" -o "$TARGET_FILE.partial"
  else
    wget --show-progress -O "$TARGET_FILE.partial" "$URL"
  fi
  mv "$TARGET_FILE.partial" "$TARGET_FILE"
fi

# Quick sanity check — refuse the file when it's clearly wrong (HTML 404 page).
file_head=$(head -c 4 "$TARGET_FILE" | od -An -c | tr -d ' ')
if [[ "${#file_head}" -lt 1 ]]; then
  echo "WARN: model file looks empty; investigate $TARGET_FILE" >&2
  exit 2
fi

echo
echo "Model installed: $TARGET_FILE"
echo
echo "Add to your .env:"
echo "  VOICE_BACKEND=whisper"
echo "  WHISPER_MODEL_PATH=$TARGET_FILE"
echo
