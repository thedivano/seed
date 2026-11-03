"""종목 발굴: 코스피·코스닥 전체에서
  1) 종가가 볼린저밴드 하단과 20일선 사이에 있고
  2) 거래량이 직전 20일 평균의 500% 이상으로 튄
종목을 찾아 state/found.json 에 날짜와 함께 쌓아요. 시드 페이지의 '발굴' 목록에 나와요.
"""
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

from alert import KST, UA, fetch_candles

VOL_X = 5.0          # 거래량이 직전 20일 평균의 이 배수 이상
MIN_AVG_VOL = 50_000  # 직전 20일 평균 거래량이 이 주식 수 이상
MIN_MCAP = 1000       # 시가총액(억 원)이 이 이상인 종목만
MIN_VALUE = 10e8     # 그날 거래대금이 이 금액(원) 이상인 종목만 (너무 작은 종목 거르기)
LOOKBACK = 5         # 최근 며칠치 신호를 확인할지 (빠진 날 메우기)
KEEP_DAYS = 30       # 이 기간이 지난 발굴 기록은 지움
FOUND = Path(__file__).parent / "state" / "found.json"


def market_list(mkt):
    """네이버 모바일 시가총액 순 목록에서 일반 주식만 [(코드, 이름, 상장주식수)]."""
    out, page = [], 1
    while page < 40:
        r = requests.get(f"https://m.stock.naver.com/api/stocks/marketValue/{mkt}",
                         params={"page": page, "pageSize": 100}, headers=UA, timeout=15)
        r.raise_for_status()
        d = r.json()
        rows = d.get("stocks") or []
        if not rows:
            break
        for s in rows:
            code, name = str(s.get("itemCode", "")), str(s.get("stockName", ""))
            kind = str(s.get("stockEndType", "stock")).lower()
            if not re.fullmatch(r"\d{6}", code) or kind not in ("stock", ""):
                continue
            if "스팩" in name or "ETN" in name:
                continue
            try:  # 상장주식수 = 시가총액 / 종가 (그날 종가로 시가총액을 다시 계산하려고)
                shares = float(s.get("marketValueRaw") or 0) / float(s.get("closePriceRaw") or 0)
            except (TypeError, ValueError, ZeroDivisionError):
                shares = 0
            out.append((code, name, shares))
        total = int(d.get("totalCount") or 0)
        if total and page * 100 >= total:
            break
        page += 1
    return out


def check(code, name, mkt, shares=0):
    try:
        c = fetch_candles(code, days=200)
    except Exception:
        return [], None
    hits = []
    for back in range(LOOKBACK):
        i = len(c) - 1 - back
        if i < 40:
            break
        closes = [r[4] for r in c[i - 19:i + 1]]
        vols = [r[5] for r in c[i - 20:i]]
        date, o, h, l, close, vol = c[i]
        ma20 = sum(closes) / 20
        lower = ma20 - 2 * statistics.pstdev(closes)
        avg = sum(vols) / 20
        if not (lower <= close <= ma20) or avg < MIN_AVG_VOL or vol < VOL_X * avg or close * vol < MIN_VALUE or close * shares / 1e8 < MIN_MCAP:
            continue
        prev = c[i - 1][4]
        hits.append({
            "id": f"{code}_{date}", "prev": c[i - 1][0], "code": code, "name": name, "market": mkt, "date": date,
            "close": int(close), "chg": round((close - prev) / prev * 100, 2) if prev else 0,
            "volx": round(vol / avg, 2), "value": round(close * vol / 1e8), "vol": int(vol),
            "mcap": round(close * shares / 1e8),
            "ma20": int(round(ma20)), "lower": int(round(lower)),
        })
    # 미니 차트용 일봉 (최근 130일: 60봉 + 이동평균 60일 계산분) "날짜,시가,고가,저가,종가;..."
    day = ";".join(f"{r[0]},{int(r[1])},{int(r[2])},{int(r[3])},{int(r[4])}" for r in c[-130:]) if c else ""
    return hits, ((c[-1][0], c[-1][4], day) if c else None)


def reasons(code, date, prev):
    """거래량이 터진 이유를 짐작할 수 있게: 전날 장 마감(15:30) 뒤 ~ 그날 사이의 공시·뉴스 제목 최대 4개."""
    start, end = prev + "1530", date + "2359"
    out = []
    try:  # 공시
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/disclosure", params={"pageSize": 20, "page": 1},
                         headers=UA, timeout=10)
        for d in r.json() or []:
            t = str(d.get("datetime", "")).replace("-", "").replace("T", "").replace(":", "")[:12]
            if start <= t <= end:
                out.append({"k": "공시", "t": d.get("title", ""), "d": t,
                            "u": f"https://m.stock.naver.com/domestic/stock/{code}/disclosure"})
    except Exception:
        pass
    news = []
    try:  # 뉴스 (최신순으로 오니 기간보다 오래되면 멈춤)
        for page in range(1, 8):
            r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}", params={"pageSize": 20, "page": page},
                             headers=UA, timeout=10)
            groups = r.json() or []
            if not groups:
                break
            oldest = "9"
            for g in groups:
                for n in g.get("items", [])[:1]:  # 같은 내용 기사 묶음은 대표 1개만
                    t = str(n.get("datetime", ""))
                    oldest = min(oldest, t)
                    if start <= t <= end:
                        news.append({"k": n.get("officeName", "뉴스"), "t": n.get("titleFull") or n.get("title", ""),
                                     "d": t, "u": n.get("mobileNewsUrl", "")})
            if oldest < start:
                break
    except Exception:
        pass
    news.sort(key=lambda x: x["d"], reverse=True)
    return (out + news)[:4]


def main():
    universe = []
    for mkt in ("KOSPI", "KOSDAQ"):
        lst = market_list(mkt)
        print(f"{mkt}: {len(lst)}종목")
        universe += [(c, n, mkt, sh) for c, n, sh in lst]
    if not universe:
        print("종목 목록을 못 받았어요")
        sys.exit(1)

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda a: check(*a), universe))
    found = [h for hs, _ in res for h in hs]
    last = {a[0]: lp for a, (_, lp) in zip(universe, res) if lp}  # 종목별 가장 최근 종가

    old = {}
    if FOUND.exists():
        try:
            old = {x["id"]: x for x in json.loads(FOUND.read_text(encoding="utf-8")).get("items", [])}
        except Exception:
            old = {}
    for h in found:
        if old.get(h["id"], {}).get("news"):  # 이미 찾은 이유는 유지 (비어 있으면 다시 찾음)
            h["news"] = old[h["id"]]["news"]
        old[h["id"]] = h  # 최근 5일 신호는 새 값(거래량·시가총액 등)으로 덮어씀
    for x in old.values():  # 거래량이 터진 이유 (한 번 찾으면 그대로 둠)
        if "news" not in x and x.get("prev"):
            x["news"] = reasons(x["code"], x["date"], x["prev"])
    for x in old.values():  # 발굴 뒤 지금 얼마인지 보려고 오늘 종가를 붙임
        if x["code"] in last:
            x["nowDate"], now, x["day"] = last[x["code"]]
            x["now"] = int(now)
    cut = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    items = sorted((x for x in old.values() if x["date"] >= cut and x.get("volx", 0) >= VOL_X and x.get("mcap", 0) >= MIN_MCAP and x.get("vol", 0) / max(x.get("volx", 1), 1e-9) >= MIN_AVG_VOL), key=lambda x: (x["date"], x["volx"]), reverse=True)

    FOUND.parent.mkdir(exist_ok=True)
    FOUND.write_text(json.dumps({"updatedAt": datetime.now(KST).isoformat(timespec="minutes"), "items": items},
                                ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"이번에 찾은 신호 {len(found)}개 · 목록 {len(items)}개")
    for h in found[:30]:
        print(h["date"], h["name"], f"거래량 {h['volx']}배", h["close"])


if __name__ == "__main__":
    main()
