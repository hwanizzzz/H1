"""
하나증권 1Q OpenAPI COM ProgID 찾기 도구 (Windows 전용)

사용:
  python tools/find_progid.py

동작:
  1) 레지스트리 HKCR 에서 "하나 / hana / 1Q / H1 / OPEN" 관련 ProgID 후보 스캔
  2) Program Files 아래 하나증권 관련 OCX 파일 검색
  3) 각 후보를 실제로 Dispatch 시도해 로딩 가능한지 표시
  4) 성공한 ProgID 를 config.local.yaml 의 api.progid 에 넣으면 됨
"""

import os
import sys
import glob

try:
    import winreg
    import win32com.client
    import pythoncom
except ImportError:
    print("이 도구는 Windows + pywin32 환경에서만 동작합니다.")
    sys.exit(1)


KEYWORDS = ("hana", "1q", "h1", "one", "openapi")
EXCLUDE = ("wow64", "clsid", "typelib", "interface", "component categories")


def scan_hkcr():
    """HKEY_CLASSES_ROOT 최상위에서 후보 ProgID 나열"""
    print("\n[1] 레지스트리 HKCR ProgID 후보")
    print("-" * 70)
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                lname = name.lower()
                if any(k in lname for k in KEYWORDS) and not any(x in lname for x in EXCLUDE):
                    if "." in name and not name.startswith("{"):
                        found.append(name)
                i += 1
    except Exception as e:
        print(f"레지스트리 조회 오류: {e}")

    for p in sorted(set(found)):
        print(f"  후보: {p}")
    if not found:
        print("  (후보 없음)")
    return sorted(set(found))


def scan_ocx():
    """Program Files 아래 관련 OCX 검색"""
    print("\n[2] 하나증권 관련 OCX/DLL 파일")
    print("-" * 70)
    roots = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\hanaw",
        r"C:\HanaSecurities",
        r"C:\1Q",
    ]
    patterns = ["*.ocx", "*.dll"]
    hits = []
    for root in roots:
        if not os.path.exists(root):
            continue
        for pat in patterns:
            for path in glob.iglob(os.path.join(root, "**", pat), recursive=True):
                low = path.lower()
                if any(k in low for k in ("hana", "1q", "openapi", "h1")):
                    hits.append(path)
    for h in hits[:30]:
        print(f"  {h}")
    if not hits:
        print("  (검색 결과 없음 — 설치 경로가 표준과 다를 수 있음)")
    return hits


def try_dispatch(progids):
    """각 후보를 실제로 Dispatch 시도"""
    print("\n[3] 후보별 Dispatch 테스트")
    print("-" * 70)
    if not progids:
        print("  (테스트할 후보 없음)")
        return None
    pythoncom.CoInitialize()
    success = []
    for p in progids:
        try:
            obj = win32com.client.Dispatch(p)
            print(f"  ✓ 성공 : {p}")
            success.append(p)
            del obj
        except Exception as e:
            code = getattr(e, "hresult", "?")
            print(f"  ✗ 실패 : {p}   ({code})")
    return success


def main():
    print("=" * 70)
    print(" 하나증권 1Q OpenAPI ProgID 검색")
    print("=" * 70)

    candidates = scan_hkcr()
    scan_ocx()
    success = try_dispatch(candidates)

    print("\n" + "=" * 70)
    if success:
        print(" 사용 가능한 ProgID:")
        for p in success:
            print(f"   {p}")
        print("\n 다음 단계 — config/config.local.yaml 에 아래 줄 추가:")
        print(f"   api:")
        print(f"     progid: \"{success[0]}\"")
    else:
        print(" 자동 감지 실패.")
        print(" 확인 사항:")
        print("  1) 1QHTS 가 정상 설치·로그인 상태인지")
        print("  2) 하나증권 1Q OpenAPI 를 별도 신청·설치했는지 (일반 HTS와 다름)")
        print("  3) 32비트 Python 이 필요할 수 있음 (많은 증권사 OCX 가 32비트 전용)")
        print("     python -c \"import struct; print(struct.calcsize('P')*8, 'bit')\"")
        print("  4) 그래도 안 되면 하나증권 API 지원팀에 정확한 ProgID 문의")
    print("=" * 70)


if __name__ == "__main__":
    main()
