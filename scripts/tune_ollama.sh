#!/usr/bin/env bash
# Ollama 서버를 이 봇에 맞게 빠르게 튜닝한다. (systemd 드롭인으로 환경변수만 추가, 기존 설정은 건드리지 않음)
#
#  - OLLAMA_FLASH_ATTENTION=1     : Flash Attention (긴 컨텍스트에서 빠르고 VRAM 절약)
#  - OLLAMA_KV_CACHE_TYPE=q8_0    : KV 캐시를 8비트로 (VRAM 절반, 속도 비슷, 품질 영향 거의 없음)
#  - OLLAMA_KEEP_ALIVE=24h        : 모델을 VRAM 에 계속 상주 (다시 로드하는 시간 제거)
#  - OLLAMA_NUM_PARALLEL=1        : 동시 처리 1개 (이 봇은 한 번에 하나만 생성하므로 VRAM 낭비 방지)
#  - OLLAMA_MAX_LOADED_MODELS=1   : 모델 하나만 상주
#
# 사용법: bash scripts/tune_ollama.sh
set -euo pipefail

if ! systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
  echo "systemd 의 ollama 서비스가 없습니다. (ollama serve 를 직접 실행하는 환경)"
  echo "대신 아래 스크립트로 튜닝 환경변수를 적용해서 시작하세요:"
  echo "  bash scripts/run_ollama.sh"
  exit 0
fi

[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"

DROPIN_DIR="/etc/systemd/system/ollama.service.d"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/tuning.conf" <<'EOF'
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF

systemctl daemon-reload
systemctl restart ollama

for _ in $(seq 1 20); do
  curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done

echo "== 적용된 환경변수 =="
systemctl show ollama -p Environment | tr ' ' '\n' | grep OLLAMA_ || true
echo
echo "봇을 재시작하면 시작할 때 모델을 미리 올립니다(웜업)."
echo "속도는 디스코드에서 /상태 의 '응답 속도' 줄로 확인하세요."
