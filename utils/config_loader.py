"""
Config 로더 — 기본 config 위에 로컬 오버라이드를 병합.

파일 구성:
  config/config.yaml        — 자리표시자와 공통 설정 (git에 커밋됨, 안전)
  config/config.local.yaml  — 계좌번호·비밀번호 등 민감 정보 오버라이드
                              (.gitignore 로 커밋 차단, 사용자 로컬에만 존재)

병합 규칙:
  로컬 값이 있으면 기본값을 재귀적으로 덮어씀. 리스트는 통째로 치환.
  로컬 파일이 없으면 기본 config 만 사용.
"""

import os
import yaml
from typing import Any


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(base_path: str = "config/config.yaml",
                local_path: str = "config/config.local.yaml") -> dict:
    with open(base_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            local = yaml.safe_load(f) or {}
        _deep_merge(cfg, local)
        cfg["_local_override_applied"] = True

    return cfg
