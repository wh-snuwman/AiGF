"""이미지 인식: 첨부 이미지를 내려받아 비전 모델(gemma3 등)에 넘길 수 있게 줄여서 준비한다."""
from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger("vision")


def shrink(data: bytes, max_side: int) -> bytes:
    """긴 변을 max_side 로 줄이고 JPEG 로 변환한다. Pillow 가 없거나 실패하면 원본을 그대로 쓴다."""
    try:
        from PIL import Image  # 선택 의존성
        img = Image.open(io.BytesIO(data))
        img.seek(0)                       # GIF 는 첫 프레임
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


async def collect_images(message: Any, cfg: dict) -> list[bytes]:
    """메시지에 첨부된 이미지를 최대 max_images 장까지 가져온다."""
    if not cfg.get("enabled", True):
        return []
    out: list[bytes] = []
    for att in getattr(message, "attachments", []):
        ctype = (att.content_type or "").lower()
        if not ctype.startswith("image/") or att.size > cfg.get("max_bytes", 8_000_000):
            continue
        try:
            data = await att.read()
        except Exception:  # noqa: BLE001
            log.warning("이미지 다운로드 실패: %s", att.filename)
            continue
        out.append(shrink(data, cfg.get("max_side", 1024)))
        if len(out) >= cfg.get("max_images", 2):
            break
    return out


def has_image(message: Any) -> bool:
    return any((a.content_type or "").lower().startswith("image/") for a in getattr(message, "attachments", []))
