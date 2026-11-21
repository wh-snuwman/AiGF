"""서버 전용 진단:  python scripts/web_probe.py [검색어]
인터넷 정보(ddgs)가 왜 실패하는지 엔진별로 한 번씩 시험해 표로 보여 준다. 성공한 엔진을 config/settings.yaml 의
web.backends 에 위에서부터 적으면 된다. (ddgs 가 없으면 안내만 하고 끝난다.)
"""
import importlib.util
import socket
import sys
import time
import urllib.request

QUERY = " ".join(sys.argv[1:]) or "요즘 유행어 신조어"
# ddgs 9.x 에서 쓰이는 이름들 (버전에 따라 다를 수 있다 → 지원하지 않는 이름은 오류로 표시됨)
ENGINES = ["auto", "duckduckgo", "yahoo", "bing", "brave", "mojeek", "google", "yandex", "wikipedia"]


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
    spec = importlib.util.find_spec("ddgs")
    if spec is None:
        print("\nddgs 가 설치돼 있지 않아요 (pip install ddgs). 설치 전에는 위키백과/slang.txt 폴백만 쓰여요.")
        return
    from importlib.metadata import version
    print(f"\n== ddgs {version('ddgs')} 엔진별 시험 (검색어: {QUERY!r}) ==")
    print(f"{'엔진':<12}{'결과':<8}{'건수':<6}{'시간':<8}비고")
    good = []
    for b in ENGINES:
        t0 = time.time()
        try:
            sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
            from bot.web import _backend_ddgs
            rows = _backend_ddgs(QUERY, 5, b, "kr-kr", None, 6)
            dt = time.time() - t0
            if rows:
                good.append(b)
                print(f"{b:<12}{'성공':<8}{len(rows):<6}{dt:<8.1f}{rows[0][0][:30]}")
            else:
                print(f"{b:<12}{'빈 결과':<8}{0:<6}{dt:<8.1f}")
        except Exception as e:  # noqa: BLE001
            print(f"{b:<12}{'실패':<8}{'-':<6}{time.time() - t0:<8.1f}{classify(e)} | {str(e)[:60]}")
        time.sleep(2)                                   # 엔진에 부담을 주지 않는다
    print("\n== 결론 ==")
    if good:
        print("config/settings.yaml 의 web: 에 아래처럼 적으세요 (잘 되는 순서):")
        print(f"  backends: {good[:3]}")
    else:
        print("성공한 엔진이 없어요. 폴백(위키백과 / config/slang.txt)이 쓰입니다. IPv6 접속 실패가 보이면 IPv4 우선 설정(README '문제 해결')을 해 보세요.")


if __name__ == "__main__":
    main()
