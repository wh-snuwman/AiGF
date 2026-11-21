"""서버 전용 진단:  python scripts/web_probe.py [검색어]
인터넷 정보가 왜 실패하는지 검색 순서(web.backends)의 엔진을 하나씩 시험해 표로 보여 준다.
설치된 ddgs 에서 켜진 엔진만 시험하고, 차단(HTTP 403/429/202, 봇 확인 페이지)과 진짜 '결과 없음'을 구분한다.
.env 에 네이버 키가 있으면 네이버 검색 API 도 시험한다.
"""
import importlib.util
import socket
import sys
import time
import urllib.request

QUERY = " ".join(sys.argv[1:]) or "요즘 유행어 신조어"


def classify(e: BaseException) -> str:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from bot.web import classify_error
    return classify_error(e)


def net_checks() -> None:
    print("== 네트워크 점검 ==")
    for fam, name in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            infos = socket.getaddrinfo("search.yahoo.com", 443, fam, socket.SOCK_STREAM)
            addr = infos[0][4][0]
        except OSError as e:
            print(f"  {name}: 주소 해석 실패 ({e})")
            continue
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(4)
        t0 = time.time()
        try:
            s.connect(infos[0][4])
            print(f"  {name}: 접속 성공 ({addr}, {time.time() - t0:.1f}s)")
        except OSError as e:
            print(f"  {name}: 접속 실패 ({addr}) → {e}  {'← IPv6 경로가 없는 서버일 수 있어요' if fam == socket.AF_INET6 else ''}")
        finally:
            s.close()
    try:
        req = urllib.request.Request("https://ko.wikipedia.org/api/rest_v1/page/summary/%EC%9C%84%ED%82%A4%EB%B0%B1%EA%B3%BC",
                                     headers={"User-Agent": "friend-bot/1.0 (probe)"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5) as r:   # noqa: S310
            print(f"  위키백과 API: {r.status} ({time.time() - t0:.1f}s) → 폴백으로 사용 가능")
    except Exception as e:  # noqa: BLE001
        print(f"  위키백과 API: 실패 ({type(e).__name__}: {e})")


def main() -> None:
    net_checks()
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from bot.config import load_settings
    from bot.web import WebInfo, classify_error, ddgs_text_engines, ddgs_version
    avail = ddgs_text_engines()
    print(f"\n== ddgs {ddgs_version() or '(없음)'}: 켜진 엔진 → 업스트림 ==")
    print("  " + (", ".join(f"{n}({u})" for n, u in sorted(avail.items())) if avail else "ddgs 가 없어요 (pip install ddgs)"))
    w = WebInfo(load_settings().get("web", {}), None)
    chain, skipped = w.chain()
    names = [n for n, _u in chain]
    for n in sorted(avail or {}):                       # 설정에 없지만 켜진 엔진도 참고로 시험
        if n not in names and n not in ("wikipedia", "grokipedia"):
            names.append(n)
    up = {**(avail or {}), **dict(chain)}
    if skipped:
        print(f"  설정에서 제외: {', '.join(skipped)}")
    print(f"\n== 엔진별 시험 (검색어: {QUERY!r}) ==")
    print(f"{'엔진':<12}{'업스트림':<10}{'결과':<8}{'건수':<6}{'시간':<7}비고")
    good = []
    for b in names:
        t0 = time.time()
        try:
            rows = w._search_one(QUERY, 5, b)
            dt = time.time() - t0
            if rows:
                good.append(b)
                print(f"{b:<12}{up.get(b, b):<10}{'성공':<8}{len(rows):<6}{dt:<7.1f}{rows[0][0][:30]}")
            else:
                print(f"{b:<12}{up.get(b, b):<10}{'빈 결과':<8}{0:<6}{dt:<7.1f}(200 이지만 비어 있음: 몰래 막힌 경우가 많음)")
        except Exception as e:  # noqa: BLE001
            print(f"{b:<12}{up.get(b, b):<10}{'실패':<8}{'-':<6}{time.time() - t0:<7.1f}{classify_error(e)}")
        time.sleep(3)                                   # 엔진에 부담을 주지 않는다
    print("\n== 결론 ==")
    if any(n in ("naver", "searxng") for n in good):
        print("공식 API/자가호스팅 검색이 동작해요. 그대로 두면 됩니다.")
    elif good:
        print(f"지금 되는 무료 엔진: {good}. 무료 엔진은 시간에 따라 되다 말다 해요 → 안정적으로 쓰려면 네이버 검색 API 키를 .env 에 넣으세요.")
    else:
        print("성공한 엔진이 없어요. 폴백(위키백과 / config/slang.txt)이 쓰입니다. 네이버 검색 API 키를 넣는 게 가장 확실해요 (README '인터넷 정보').")
    print("IPv6 접속 실패가 보이면 IPv4 우선 설정(README '문제 해결')도 해 두세요.")


if __name__ == "__main__":
    main()
