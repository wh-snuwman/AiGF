"""설정 로더: config/settings.yaml, config/persona.yaml, .env"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


def load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict[str, Any]:
    return load_yaml("settings.yaml")


def discord_token() -> str:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token.startswith("여기에"):
        raise SystemExit(".env 파일에 DISCORD_TOKEN 을 설정해주세요.")
    return token
