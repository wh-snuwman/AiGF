#!/usr/bin/env bash
# systemd 없이 `ollama serve` 를 직접 실행할 때 쓰는 스크립트 (tmux 등에서 실행).
# 봇에 맞는 튜닝 환경변수와 모델 저장 경로를 설정한 뒤 서버를 (재)시작한다.
#
# 사용법:
#   bash scripts/run_ollama.sh            # 이미 떠 있는 ollama 가 있으면 종료하고 이 설정으로 다시 시작
#   OLLAMA_MODELS=/원하는/경로 bash scripts/run_ollama.sh   # 모델 경로 직접 지정
set -euo pipefail

# 모델 저장 경로: 지정이 없으면 HDD 로 옮겨둔 경로를 자동 사용
if [ -z "${OLLAMA_MODELS:-}" ] && [ -d /mnt/hdd/ollama/models ]; then
  export OLLAMA_MODELS=/mnt/hdd/ollama/models
fi

# 속도 튜닝 (이미 export 해둔 값이 있으면 그 값을 우선)
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"        # Flash Attention
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"          # KV 캐시 8비트 (VRAM 절약)
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-24h}"                 # 모델 상주
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"               # 동시 처리 1개
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-2}"     # 메인 모델 + 문법 교정 소형 모델 동시 상주 (1이면 호출마다 서로 교체돼 매우 느려짐)

if pgrep -x ollama >/dev/null 2>&1; then
  echo "이미 실행 중인 ollama 를 종료하고 새 설정으로 다시 시작합니다."
  pkill -x ollama || true
  sleep 2
fi

echo "== ollama serve 시작 =="
env | grep '^OLLAMA_' | sort
echo
echo "(종료: Ctrl+C. 이 창을 닫아도 계속 돌게 하려면 tmux 안에서 실행하세요)"
exec ollama serve
