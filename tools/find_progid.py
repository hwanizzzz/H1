"""
하나증권 1Q OpenAPI COM ProgID 찾기 도구 (Windows 전용)

사용:
  python tools/find_progid.py            # 하나 관련 후보만 나열
  python tools/find_progid.py --dispatch # 각 후보를 실제 Dispatch 로 검증
  python tools/find_progid.py --all      # 필터 없이 전체(디버그용)

주의:
  한국 증권사 OCX 대부분이 32비트 전용입니다.
  64비트 Python 에서는 로드에 실패합니다 (-2147221164 = Class not registered).
  이 도구는 시작 시 Python 비트를 표시합니다.
"""

import argparse
import os
import struct
import sys
import glob

try:
    import winreg
    import win32com.client
    import pythoncom
except ImportError:
    print("이 도구는 Windows + pywin32 환경에서만 동작합니다.")
    sys.exit(1)


# 하나증권 COM 클래스에서 실제로 관찰된/추정되는 접두어들
HANA_PREFIXES = ("HANA", "H1", "H1QOPEN", "HAOPEN", "HANAOP", "HANAAPI",
                 "HANAWTS", "1QOPEN", "1QAPI")


def is_hana_candidate(name: str, allow_all: bool = False) -> bool:
    if allow_all:
        return "." in name and not name.startswith("{")
    up = name.upper()
    return any(up.startswith(p) for p in HANA_PREFIXES) and "." in name


def scan_registry(hive_and_path, label: str, allow_all: bool):
    """레지스트리 하위 키 나열"""
    hive, path = hive_and_path
    found = []
    try:
        with winreg.OpenKey(hive, path) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                if is_hana_candidate(name, allow_all) and not name.startswith("{"):
                    found.append(name)
                i += 1
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  ({label} 조회 오류: {e})")
    return sorted(set(found))


def scan_ocx():
    """Program Files 아래 하나증권 관련 OCX/DLL 검색"""
    roots = [r"C:\Program Files", r"C:\Program Files (x86)",
             r"C:\hanaw", r"C:\HanaSecurities", r"C:\1Q", r"C:\하나증권"]
    hits = []
    for root in roots:
        if not os.path.exists(root):
            continue
        for pat in ("*.ocx", "*.dll"):
            for path in glob.iglob(os.path.join(root, "**", pat), recursive=True):
                low = path.lower()
                if any(k in low for k in ("hana", "1q", "openapi", "h1")):
                    hits.append(path)
                    if len(hits) >= 30:
                        return hits
    return hits


def dispatch_test(progids):
    """각 후보를 실제로 Dispatch 시도"""
    if not progids:
        return [], []
    pythoncom.CoInitialize()
    success, fail = [], []
    for p in progids:
        try:
            obj = win32com.client.Dispatch(p)
            success.append(p)
            del obj
            print(f"  \u2713 성공 : {p}")
        except Exception as e:
            code = getattr(e, "hresult", "?")
            fail.append((p, code))
            print(f"  \u2717 실패 : {p}   ({code})")
    return success, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dispatch", action="store_true",
                    help="후보를 실제 Dispatch 시도 (실제 로드 가능한지 검증)")
    ap.add_argument("--all", action="store_true",
                    help="필터 없이 모든 ProgID 나열 (디버그용, 매우 느림)")
    args = ap.parse_args()

    bits = struct.calcsize("P") * 8
    print("=" * 70)
    print(" 하나증권 1Q OpenAPI ProgID 검색")
    print(f" Python: {sys.version.split()[0]} ({bits}bit)")
    print("=" * 70)

    if bits == 64:
        print()
        print(" [주의] 64비트 Python 감지됨.")
        print(" 한국 증권사 OCX 대부분은 32비트 전용이라 로드가 실패합니다.")
        print(" (오류: -2147221164 Class not registered)")
        print(" 32비트 Python 을 별도 설치해 이 도구를 다시 실행하세요.")
        print()

    print("[1] 레지스트리 스캔")
    print("-" * 70)

    # 32비트 프로세스에서는 Wow6432Node 가 자동으로 매핑되므로 하나만 봐도 되지만,
    # 64비트 프로세스에서 실행됐을 경우를 위해 두 곳 모두 확인.
    targets = [
        ((winreg.HKEY_CLASSES_ROOT, ""), "HKCR"),
        ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes"), "HKLM\\Classes"),
        ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\Wow6432Node"),
         "HKLM\\Classes\\Wow6432Node (32비트)"),
        ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Classes"),
         "HKLM\\WOW6432Node\\Classes (32비트)"),
    ]

    all_candidates = []
    for tup, label in targets:
        found = scan_registry(tup, label, args.all)
        if found:
            print(f"  [{label}]")
            for p in found:
                print(f"     {p}")
            all_candidates.extend(found)
        else:
            print(f"  [{label}] (해당 없음)")

    unique = sorted(set(all_candidates))

    print("\n[2] 하나증권 관련 OCX/DLL 파일")
    print("-" * 70)
    ocx = scan_ocx()
    for h in ocx:
        print(f"  {h}")
    if not ocx:
        print("  (검색 결과 없음)")

    if args.dispatch and unique:
        print("\n[3] 후보별 Dispatch 테스트")
        print("-" * 70)
        success, fail = dispatch_test(unique)
    else:
        success = []
        if unique:
            print("\n※ --dispatch 옵션 없이는 실제 로드 테스트를 생략합니다.")
            print("  실제 사용 가능한 ProgID 확인: python tools\\find_progid.py --dispatch")

    print("\n" + "=" * 70)
    if success:
        print(" 사용 가능한 ProgID:")
        for p in success:
            print(f"   {p}")
        print("\n 다음 단계 — config/config.local.yaml 의 api 블록에 추가:")
        print(f"   api:")
        print(f"     progid: \"{success[0]}\"")
    elif unique:
        print(" 하나증권 관련 후보는 발견됐지만, Dispatch 테스트 결과는 아래를 확인:")
        if bits == 64:
            print("  → 64비트 Python 문제일 가능성이 큼. 32비트 Python 으로 재시도.")
        else:
            print("  → 각 후보 옆의 오류 코드로 원인 파악:")
            print("     -2147221164 (Class not reg)  : OCX 파일 실제로 없음/미등록")
            print("     -2147221005 (Invalid class)  : ProgID 자체가 존재하지 않음")
            print("     -2147024894 (File not found) : DLL 경로 문제")
    else:
        print(" 하나증권 관련 ProgID 를 찾지 못했습니다.")
        print(" 확인 사항:")
        print("  1) 하나증권 1Q OpenAPI 는 1QHTS 와 별개로 신청·설치 필요")
        print("     → 하나증권 홈페이지 [해외파생 API] 신청 후 설치 프로그램 다운로드")
        print("  2) 설치 후 OCX 를 관리자 권한으로 등록:")
        print("     regsvr32 \"C:\\경로\\HANAAPI.ocx\"  (예시)")
        print("  3) 하나증권 API 지원팀 문의: 정확한 ProgID 요청")
    print("=" * 70)


if __name__ == "__main__":
    main()
