#!/usr/bin/env bash
# Ollama 모델 저장 경로를 다른 디스크(예: /mnt/hdd)로 옮긴다.
# 모델이 용량의 대부분(수 GB~수십 GB)을 차지하므로 모델 폴더만 옮기면 충분하다.
#
# 사용법:
#   bash scripts/move_ollama_to_hdd.sh               # 기본: /mnt/hdd/ollama
#   bash scripts/move_ollama_to_hdd.sh /mnt/hdd/foo  # 경로 지정
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo bash "$0" "$@"

TARGET="${1:-/mnt/hdd/ollama}"
MODELS="$TARGET/models"
SRC="/usr/share/ollama/.ollama/models"      # ollama 기본 설치 시 모델 위치
DROPIN_DIR="/etc/systemd/system/ollama.service.d"

# 0) 대상 디스크 파일시스템 확인 (ntfs/exfat/vfat 은 리눅스 권한이 안 먹어서 ollama 서비스가 쓰기 실패함)
PARENT="$(dirname "$TARGET")"
[ -d "$PARENT" ] || { echo "상위 경로가 없습니다: $PARENT (디스크가 마운트되어 있나요?)"; exit 1; }
FS="$(findmnt -no FSTYPE -T "$PARENT" || true)"
case "$FS" in
  ntfs*|fuseblk|exfat|vfat)
    echo "대상이 '$FS' 파일시스템입니다. ext4 같은 리눅스 파일시스템이 필요합니다."; exit 1 ;;
esac
echo "대상: $MODELS  (파일시스템: ${FS:-알 수 없음})"

# 1) 서비스 중지
if systemctl list-unit-files 2>/dev/null | grep -q '^ollama.service'; then
  systemctl stop ollama || true
else
  pkill -x ollama || true
fi

# 2) 실패한 다운로드 찌꺼기 삭제 (루트 용량 확보)
if [ -d "$SRC/blobs" ]; then
  find "$SRC/blobs" -name '*-partial*' -print -delete
fi

# 3) 기존 모델 이동
mkdir -p "$MODELS"
if [ -d "$SRC" ] && [ ! -L "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
  command -v rsync >/dev/null 2>&1 || apt-get install -y rsync
  echo "기존 모델을 이동합니다..."
  rsync -a --remove-source-files "$SRC/" "$MODELS/"
  find "$SRC" -type d -empty -delete 2>/dev/null || true
fi

# 4) 권한 (ollama 서비스 계정이 읽고 쓸 수 있어야 함)
if id ollama >/dev/null 2>&1; then
  chown -R ollama:ollama "$TARGET"
  if ! sudo -u ollama test -w "$MODELS"; then
    echo "ollama 계정이 $MODELS 에 쓸 수 없습니다. 상위 폴더 권한을 확인하세요:"
    echo "  sudo chmod o+x $PARENT"
    exit 1
  fi
fi

# 5) systemd 에 경로 지정
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_DIR/models.conf" <<EOF
[Service]
Environment="OLLAMA_MODELS=$MODELS"
EOF
systemctl daemon-reload
systemctl start ollama

# 6) 확인
for _ in $(seq 1 20); do
  curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done
echo
echo "== 설정 완료 =="
systemctl show ollama -p Environment | tr ' ' '\n' | grep OLLAMA_MODELS || true
ollama list || true
df -h / "$PARENT" | sed -n '1,3p'
echo
echo "이제 모델 받기:  bash scripts/ensure_model.sh"
