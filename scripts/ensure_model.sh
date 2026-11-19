#!/usr/bin/env bash
# Ollama 모델이 있는지 확인하고, 없으면 자동으로 받는다.
# 사용법:
#   bash scripts/ensure_model.sh              # config/settings.yaml 의 ollama.model 사용
#   bash scripts/ensure_model.sh qwen2.5:14b  # 모델 직접 지정
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

# 1) 모델 이름 결정 (인자 > settings.yaml)
MODEL="${1:-}"
if [ -z "$MODEL" ]; then
  MODEL="$(grep -E '^\s*model:' config/settings.yaml | head -n1 | sed -E 's/^\s*model:\s*"?([^"#]+)"?.*/\1/' | xargs)"
fi
[ -n "$MODEL" ] || { echo "모델 이름을 찾지 못했습니다."; exit 1; }

# 2) ollama 설치 확인
if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama 가 설치되어 있지 않아 설치합니다."
  curl -fsSL https://ollama.com/install.sh | sh
fi

# 3) 서버가 꺼져 있으면 실행
if ! curl -fs "$HOST/api/tags" >/dev/null 2>&1; then
  echo "ollama 서버를 시작합니다."
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
    sudo systemctl start ollama
  else
    nohup ollama serve >/tmp/ollama.log 2>&1 &
  fi
  for _ in $(seq 1 30); do
    curl -fs "$HOST/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fs "$HOST/api/tags" >/dev/null 2>&1 || { echo "ollama 서버 시작 실패"; exit 1; }
fi

# 4) 모델 존재 확인 후 없으면 pull (태그 생략 시 :latest 로 비교)
WANT="$MODEL"
case "$WANT" in *:*) ;; *) WANT="$WANT:latest" ;; esac

if ollama list | awk 'NR>1 {print $1}' | grep -Fxq "$WANT"; then
  echo "모델 '$MODEL' 이(가) 이미 설치되어 있습니다."
else
  echo "모델 '$MODEL' 이(가) 없어 다운로드합니다."
  ollama pull "$MODEL"
  echo "설치 완료: $MODEL"
fi
