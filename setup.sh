#!/bin/bash
# recorder-ingest 一键安装：依赖 + whisper 模型
set -e

echo "==> 安装 whisper-cpp 和 ffmpeg"
brew install whisper-cpp ffmpeg

MODEL_DIR="$HOME/Documents/kimi/models"
MODEL="$MODEL_DIR/ggml-small.bin"
mkdir -p "$MODEL_DIR"

if [ -f "$MODEL" ]; then
  echo "==> 模型已存在，跳过下载: $MODEL"
else
  echo "==> 下载 whisper small 模型（约 466MB，优先国内镜像）"
  # 想要更高准确率可换成 ggml-medium.bin（约 1.5GB）
  curl -L -C - --connect-timeout 15 -o "$MODEL" \
    https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-small.bin \
  || curl -L -C - -o "$MODEL" \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
fi

echo "==> 完成。运行 python3 ingest.py 即可开始入库。"
