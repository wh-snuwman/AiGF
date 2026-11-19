#!/usr/bin/env bash
# 우분투/WSL 초기 설치 스크립트.  사용법: bash scripts/setup_ubuntu.sh
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${1:-gemma3:12b}"

echo "[1/4] 시스템 패키지"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip curl

echo "[2/4] Ollama 설치"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "[3/4] 모델 다운로드: $MODEL"
# systemd 가 없는 WSL 환경이면 백그라운드로 서버를 띄운다
if ! curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 5
fi
ollama pull "$MODEL"

echo "[4/4] 파이썬 가상환경"
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

[ -f .env ] || cp .env.example .env
echo
echo "완료! .env 에 DISCORD_TOKEN 을 넣고 아래로 실행하세요:"
echo "  .venv/bin/python -m bot.main"
