"""
안전 검증 모음
  - validate_python_bits: 32비트 Python 아닌 경우 경고
  - validate_account_safety: 계좌 안전 검증(모의/실계좌 사고 방지)

시작 시 api.account_no 와 is_mock 의 일관성을 검사한다.

검사 규칙:
  1. account_no 가 mock_accounts 또는 live_accounts 에 반드시 등록돼 있어야 함
     (require_registration=true 인 경우)
  2. 모의계좌로 등록된 번호인데 is_mock=false → 거부
  3. 실계좌로 등록된 번호인데 is_mock=true  → 거부
  4. 양쪽 목록에 중복 등록되어 있으면 거부

이로써:
  - 실계좌번호를 config 에 두고 is_mock 만 잘못 false 로 바꾸는 사고 방지
  - 모의계좌번호로 실거래 모드 실행 시도 방지
  - 등록되지 않은 알 수 없는 계좌번호 사용 차단
"""

import struct
import sys

from utils.logger import setup_logger

logger = setup_logger("safety")


class AccountSafetyError(RuntimeError):
    pass


def validate_python_bits() -> None:
    """
    32비트 Python 아닐 경우 경고를 로그로 남긴다(치명적이지 않아 실행은 계속).
    한국 증권사 OCX 는 대부분 32비트 전용이라 64비트 Python 에서는
    COM Dispatch 가 실패한다(-2147221164 Class not registered).
    """
    bits = struct.calcsize("P") * 8
    if bits != 32 and sys.platform == "win32":
        logger.warning(
            f"현재 Python: {sys.version.split()[0]} ({bits}bit).\n"
            f"한국 증권사 OCX 는 대부분 32비트 전용입니다. "
            f"COM 접속이 실패할 가능성이 높습니다.\n"
            f"32비트 Python 설치 권장: https://www.python.org/downloads/windows/"
        )


def validate_account_safety(config: dict) -> None:
    """
    검증 실패 시 AccountSafetyError 를 발생시켜 봇 시작을 차단한다.
    성공하면 사용 모드를 로그로 강조 표시한다.
    """
    safety = config.get("account_safety") or {}
    mock = set(safety.get("mock_accounts") or [])
    live = set(safety.get("live_accounts") or [])
    require = safety.get("require_registration", True)

    api_cfg = config["api"]
    acct = api_cfg.get("account_no", "")
    is_mock = bool(api_cfg.get("is_mock", True))

    if not acct or acct.startswith("YOUR_"):
        raise AccountSafetyError(
            "api.account_no 가 설정되지 않았습니다. config/config.yaml 을 확인하세요."
        )

    in_mock = acct in mock
    in_live = acct in live

    if in_mock and in_live:
        raise AccountSafetyError(
            f"계좌 '{acct}' 가 mock_accounts 와 live_accounts 양쪽에 등록되어 있습니다. "
            f"한쪽에서 제거하세요."
        )

    if require and not in_mock and not in_live:
        raise AccountSafetyError(
            f"계좌 '{acct}' 가 account_safety.mock_accounts / live_accounts 어디에도 "
            f"등록돼 있지 않습니다.\n"
            f"실거래 사고 방지를 위해 사용할 계좌번호를 명시적으로 등록해야 합니다.\n"
            f"config.yaml 예:\n"
            f"  account_safety:\n"
            f"    mock_accounts:  [\"{acct}\"]    # 모의계좌일 때\n"
            f"    live_accounts:  [\"{acct}\"]    # 실계좌일 때"
        )

    if in_mock and not is_mock:
        raise AccountSafetyError(
            f"계좌 '{acct}' 는 모의계좌로 등록되어 있는데 is_mock=false 로 설정됐습니다.\n"
            f"실계좌 모드로 모의계좌번호를 사용하려는 시도입니다. 봇 시작 거부."
        )

    if in_live and is_mock:
        raise AccountSafetyError(
            f"계좌 '{acct}' 는 실계좌로 등록되어 있는데 is_mock=true 로 설정됐습니다.\n"
            f"모의 서버에 실계좌번호로 접속하려는 시도입니다. 봇 시작 거부."
        )

    # 성공 — 사용 모드 강조 표시
    if is_mock:
        banner = (
            "\n" + "*" * 60 +
            f"\n***  모의투자 모드  ***  계좌: {acct}\n" +
            "*" * 60
        )
    else:
        banner = (
            "\n" + "!" * 60 +
            f"\n!!!  실계좌 모드  !!!  계좌: {acct}\n" +
            "!!!  실제 주문이 체결됩니다  !!!\n" +
            "!" * 60
        )
    logger.warning(banner)
