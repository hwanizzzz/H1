"""
매일 매매용 설정을 config/config.local.yaml 에 자동 병합.

동작:
  1) 기존 config.local.yaml 을 .backup 으로 백업
  2) api, account_safety 는 그대로 보존 (계좌·비번 등)
  3) symbols, risk 를 매일 매매용 세팅으로 교체
  4) execution 에 있는 폴링/시세소스/웜업 옵션은 유지 (신규 필드는 추가)

실행:
  C:\\Python311-32\\python.exe scripts\\apply_daily_trading_config.py

되돌리기:
  copy config\\config.local.yaml.backup config\\config.local.yaml
"""

import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("[오류] pyyaml 미설치. 설치: pip install pyyaml")
    sys.exit(1)


LOCAL_PATH = Path("config/config.local.yaml")
BACKUP_PATH = Path("config/config.local.yaml.backup")


# ── 매일 매매용 오버라이드 ──────────────────────────────────────────────────
TRADING_SYMBOLS = [
    {
        "code": "MGC",
        "name": "E-micro Gold",
        "exchange": "COMEX",
        "contract_code": "Z26",
        "quote_code": "",
        "tick_size": 0.10,
        "tick_value": 1.0,
        "timeframe": "1H",              # ← 1D → 1H (하루 24봉 마감)
        "strategy": {
            "name": "mean_reversion",   # ← 볼린저 반등 (횡보장 매매 잦음)
            "bb_period": 20,
            "bb_std": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 30.0,
            "rsi_overbought": 70.0,
            "atr_period": 14,
            "atr_multiplier": 1.5,      # 손절 좁게
        },
    },
    {
        "code": "MES",
        "name": "Micro E-mini S&P 500",
        "exchange": "CME",
        "contract_code": "U26",
        "quote_code": "",
        "tick_size": 0.25,
        "tick_value": 1.25,
        "timeframe": "1H",              # ← 1D → 1H
        "strategy": {
            "name": "donchian_breakout",
            "donchian_entry_period": 10,   # ← 20 → 10 (더 자주 돌파)
            "donchian_exit_period": 5,
            "atr_period": 14,
            "atr_multiplier": 1.5,
        },
    },
]

TRADING_RISK_OVERRIDE = {
    "risk_per_trade_pct": 2.0,       # ← 5 → 2 (매매 잦아진 만큼 축소)
    "daily_loss_limit_pct": 8.0,     # ← 6 → 8
    "max_positions": 5,
    "max_contracts_per_trade": 30,   # ← 50 → 30
    # account_equity, usd_krw_rate 는 기존 값 유지
}


def main():
    if not LOCAL_PATH.exists():
        print(f"[오류] {LOCAL_PATH} 파일이 없습니다.")
        print("먼저 config/config.local.example.yaml 을 복사해서 계좌 정보 채우세요.")
        sys.exit(2)

    # 1) 기존 파일 로드
    with open(LOCAL_PATH, "r", encoding="utf-8") as f:
        current = yaml.safe_load(f) or {}

    # 2) 백업
    shutil.copy2(LOCAL_PATH, BACKUP_PATH)
    print(f"[백업] {LOCAL_PATH} → {BACKUP_PATH}")

    # 3) api / account_safety 보존 확인 로그
    if "api" in current:
        print(f"[보존] api.user_id = {current['api'].get('user_id', '?')}")
        print(f"[보존] api.account_no = {current['api'].get('account_no', '?')}")
    if "account_safety" in current:
        print(f"[보존] account_safety.mock_accounts = "
              f"{current['account_safety'].get('mock_accounts', [])}")

    # 4) symbols 교체 (완전 대체)
    current["symbols"] = TRADING_SYMBOLS
    print(f"[교체] symbols: MGC(1H, mean_reversion) + MES(1H, donchian 10봉)")

    # 5) risk 병합 (account_equity 등 개인값은 기존 유지)
    risk = current.setdefault("risk", {})
    old_pct = risk.get("risk_per_trade_pct", "?")
    old_max = risk.get("max_contracts_per_trade", "?")
    risk.update(TRADING_RISK_OVERRIDE)
    print(f"[조정] risk_per_trade_pct: {old_pct}% → {risk['risk_per_trade_pct']}%")
    print(f"[조정] max_contracts_per_trade: {old_max} → {risk['max_contracts_per_trade']}")
    if "account_equity" in risk:
        print(f"[보존] risk.account_equity = {risk['account_equity']:,} 원")

    # 6) 저장 (사용자가 준 순서 최대한 유지)
    with open(LOCAL_PATH, "w", encoding="utf-8") as f:
        yaml.dump(current, f, allow_unicode=True, sort_keys=False,
                  indent=2, default_flow_style=False)

    print()
    print("=" * 60)
    print("완료. 이제 봇 재시작 하시면 매일 매매 세팅이 반영됩니다.")
    print("  1) 봇 창에서 Ctrl+C 로 종료")
    print("  2) 바탕화면 아이콘 재실행")
    print()
    print("되돌리기: copy config\\config.local.yaml.backup config\\config.local.yaml")
    print("=" * 60)


if __name__ == "__main__":
    main()
