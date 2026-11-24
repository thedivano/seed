"""관심종목 감시 → 조건 충족 시 ntfy 알림.

조건 (config 값은 아래에서 바꿀 수 있음)
  1) 20일선 근접: 현재가가 20일 이동평균의 ±NEAR_PCT % 안에 있음
  2) RSI(14) 가 RSI_MAX 이하
두 조건을 모두 만족하면 알림. 조건이 이어지는 동안은 1시간마다 다시 알림.
평일 08:00~20:00 (프리마켓·정규장·애프터마켓) 동안만 확인.

환경변수
  NTFY_TOPIC  (필수) 폰 ntfy 앱에서 구독한 토픽 이름
  TEST_MODE   "1" 이면 조건과 상관없이 현재 상태를 알림으로 보냄 (설정 확인용)
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import base64

import requests

# ---------- 설정 ----------
MA_DAYS = 20
RSI_DAYS = 14
NEAR_PCT = 2.0   # 20일선 ±2% 이내면 "근접"
RSI_MAX = 30.0
REPEAT_MIN = 60  # 조건이 이어지는 동안 이 간격(분)마다 다시 알림
LEVEL_PCT = 2.0  # 볼린저 상단·하단, 20일선에 이 % 안으로 다가오면 알림 (같은 선은 1시간 지나 다시 오면 또 알림)
STEP_PCT = 3.0   # 전일 종가 대비 이 % 단위(±3, ±6, ±9…)를 새로 넘을 때마다 알림
# --------------------------

ROOT = Path(__file__).parent
WATCHLIST = ROOT / "watchlist.csv"
STATE = ROOT / "state" / "sent.json"
CHART = ROOT / "data" / "chart.json"   # 시드 페이지 차트용 일봉 데이터
CHART_DAYS = 260                       # 일봉: 저장할 거래일 수 (120일선을 그리려면 넉넉히)
CHART_WEEKS = 260                      # 주봉: 약 5년
CHART_M1 = 1500                        # 1분봉: 최근 2거래일 정도
CHART_M10 = 600                        # 10분봉
CHART_M30 = 300                        # 30분봉
KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (seed-alert)"}


def load_watchlist():
    with open(WATCHLIST, encoding="utf-8-sig") as f:
        rows = []
        for r in csv.DictReader(f):
            market = (r.get("시장") or "KR").strip().upper() or "KR"
            raw = (r.get("종목코드") or "").strip()
            code = raw.upper() if market == "US" else raw.zfill(6)
            name = (r.get("종목명") or "").strip()
            # 알림 칸이 없으면(예전 파일) 켜진 것으로, N/0/끔 이면 꺼진 것으로
            alert_on = (r.get("알림") or "Y").strip().upper() not in ("N", "0", "OFF", "끔", "X")
            lines = []
            for x in (r.get("알림선") or "").replace(",", "/").split("/"):
                try:
                    if x.strip():
                        lines.append(float(x.strip()))
                except ValueError:
                    pass
            ok = (code.isdigit() and len(code) == 6) if market == "KR" else (code and all(ch.isalnum() or ch in ".-" for ch in code))
            if ok:
                rows.append({"code": code, "name": name or code, "lines": lines, "market": market, "alert": alert_on})
        return rows


def fetch_candles(code, days=420):
    """네이버 금융 일봉 [날짜, 시가, 고가, 저가, 종가, 거래량] 목록 (오래된 순).
    장중에는 마지막 줄이 지금까지의 오늘 일봉(종가 = 현재가)."""
    end = datetime.now(KST)
    start = end - timedelta(days=days)
    url = "https://api.finance.naver.com/siseJson.naver"
    params = {
        "symbol": code,
        "requestType": 1,
        "startTime": start.strftime("%Y%m%d"),
        "endTime": end.strftime("%Y%m%d"),
        "timeframe": "day",
    }
    res = requests.get(url, params=params, headers=UA, timeout=15)
    res.raise_for_status()
    # 응답이 작은따옴표를 쓰는 유사 JSON이라 바꿔서 읽음
    data = json.loads(res.text.strip().replace("'", '"'))
    candles = []
    for row in data[1:]:  # 첫 줄은 헤더
        try:
            date = str(row[0]).strip()
            o, h, l, c = (float(x) for x in row[1:5])
            v = int(float(row[5])) if len(row) > 5 else 0
            candles.append([date, o, h, l, c, v])
        except (IndexError, TypeError, ValueError):
            continue
    return candles


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


KRX = {}  # code -> [정규장 종가(장중엔 현재가), 정규장 등락률, 장 상태]
INFO = {}  # code -> 시드 카드에 보여줄 시세 정보 (네이버가 주는 글자 그대로)


def fetch_info(code, d):
    """정규장 시가·고가·저가·거래량·거래대금 + 52주 최고·최저, PER, PBR."""
    info = {k: d.get(k, "") for k in ("closePrice", "compareToPreviousClosePrice", "fluctuationsRatio",
                                        "openPrice", "highPrice", "lowPrice",
                                        "accumulatedTradingVolume", "accumulatedTradingValue")}
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration", headers=UA, timeout=10)
        for t in r.json().get("totalInfos", []):
            if t.get("code") in ("highPriceOf52Weeks", "lowPriceOf52Weeks", "per", "pbr", "marketValue"):
                info[t["code"]] = t.get("value", "")
    except Exception:
        pass
    # 최근 5거래일 외국인·기관·개인 순매수(주) + 그날 종가: [[날짜, 외국인, 기관, 개인, 종가], ...] 최근 날짜가 앞
    try:
        rows = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend", params={"pageSize": 5},
                            headers=UA, timeout=10).json()
        flow = []
        for r in (rows or [])[:5]:
            q = [_int(r.get(k)) for k in ("foreignerPureBuyQuant", "organPureBuyQuant", "individualPureBuyQuant", "closePrice")]
            if r.get("bizdate") and q[0] is not None:
                flow.append([r["bizdate"], *q])
        if flow:
            info["flow"] = flow
    except Exception:
        pass
    INFO[code] = info


def _int(v):
    try:
        return int(float(str(v).replace(",", "").replace("+", "")))
    except (TypeError, ValueError):
        return None


def fetch_quote(code):
    """네이버 실시간 현재가. 프리·애프터마켓(넥스트레이드) 시간에는 그 가격을 돌려줌.
    {"price", "open", "high", "low", "session"} 또는 None"""
    try:
        r = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}", headers=UA, timeout=10)
        d = r.json()["datas"][0]
    except Exception:
        return None
    try:
        o = d.get("overMarketPriceInfo") or {}
        close, cmp = _num(d.get("closePrice")), _num(d.get("compareToPreviousClosePrice")) or 0
        # [정규장 종가, 정규장 등락률, 장 상태, 전일 정규장 종가, 시간외 가격, 시간외 등락률]
        KRX[code] = [close, _num(d.get("fluctuationsRatio")), d.get("marketStatus", ""), close - cmp,
                     _num(o.get("overPrice")) or 0, _num(o.get("fluctuationsRatio")) or 0]
        fetch_info(code, d)
    except Exception:
        pass
    over = d.get("overMarketPriceInfo") or {}
    if over.get("overMarketStatus") == "OPEN" and _num(over.get("overPrice")):
        src, session = over, over.get("tradingSessionType", "OVER")
        price = _num(over.get("overPrice"))
    elif d.get("marketStatus") == "OPEN":
        src, session, price = d, "REGULAR", _num(d.get("closePrice"))
    else:
        return None  # 장이 닫혀 있음
    return {"price": price, "open": _num(src.get("openPrice")) or price,
            "high": _num(src.get("highPrice")) or price, "low": _num(src.get("lowPrice")) or price,
            "session": session}


def merge_live(candles, q, today_ymd):
    """일봉 끝에 오늘 일봉을 실시간 현재가로 맞춰 넣음 (프리마켓엔 일봉이 아직 없어서 새로 만듦)."""
    if not q:
        return candles
    if candles and candles[-1][0] == today_ymd:
        c = list(candles[-1])
        c[2], c[3], c[4] = max(c[2], q["price"]), min(c[3], q["price"]), q["price"]
        return candles[:-1] + [c]
    return candles + [[today_ymd, q["open"], max(q["high"], q["price"]), min(q["low"], q["price"]), q["price"], 0]]


# ---------- 미국 주식 (야후 파이낸스) ----------
YH = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _yh(ticker, rng, interval, prepost=False):
    r = requests.get(YH + ticker, params={"range": rng, "interval": interval, "includePrePost": str(prepost).lower()},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        o, h, l = q["open"][i] or c, q["high"][i] or c, q["low"][i] or c
        stamp = datetime.fromtimestamp(t, NY)
        key = stamp.strftime("%Y%m%d") if interval in ("1d", "1wk") else stamp.strftime("%Y%m%d%H%M")
        rows.append([key, round(o, 4), round(h, 4), round(l, 4), round(c, 4), int(q["volume"][i] or 0)])
    return res["meta"], rows


def us_load(ticker):
    """미국 종목 일봉 + 실시간 가격(프리·애프터 포함). KRX/INFO 와 같은 모양으로 채움."""
    meta_d, day = _yh(ticker, "2y", "1d")
    meta, m1 = _yh(ticker, "2d", "1m", prepost=True)
    reg = meta.get("regularMarketPrice") or (day[-1][4] if day else None)
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    live = m1[-1][4] if m1 else reg
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now_ts = time.time()
    status = "OPEN" if period and period.get("start", 0) <= now_ts <= period.get("end", 0) else "CLOSE"
    if day and live and status != "OPEN":
        pass  # 장 밖: 일봉 종가는 정규장 종가 그대로 두고, 현재가는 시간외 가격
    if day:
        last = list(day[-1])
        if status == "OPEN" and live:
            last[2], last[3], last[4] = max(last[2], live), min(last[3], live), live
        day[-1] = last
    rc = (reg - prev) / prev * 100 if reg and prev else 0
    ext = live if live and status != "OPEN" and abs(live - (reg or live)) > 1e-9 else 0
    KRX[ticker] = [reg, round(rc, 2), status, prev, ext or 0, round((ext - prev) / prev * 100, 2) if ext and prev else 0]
    INFO[ticker] = {"openPrice": f"{day[-1][1]:,.2f}" if day else "", "highPrice": f"{meta.get('regularMarketDayHigh', 0):,.2f}",
                    "lowPrice": f"{meta.get('regularMarketDayLow', 0):,.2f}", "closePrice": f"{reg:,.2f}" if reg else "",
                    "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"), "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
                    "currency": meta.get("currency", "USD"), "name": meta.get("longName") or meta.get("shortName")}
    return day, live, m1


def us_extras(ticker, m1):
    _, week = _yh(ticker, "5y", "1wk")
    _, m5 = _yh(ticker, "10d", "5m", prepost=True)
    _, m30 = _yh(ticker, "1mo", "30m", prepost=True)
    m10 = []
    for t, o, h, l, c, v in m5:
        mins = int(t[8:10]) * 60 + int(t[10:12]); st = mins // 10 * 10
        key = f"{t[:8]}{st // 60:02d}{st % 60:02d}"
        if m10 and m10[-1][0] == key:
            b = m10[-1]; b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
        else:
            m10.append([key, o, h, l, c, v])
    return week, m1, m10, m30


def money(code, v):
    """종목 통화에 맞춘 가격 글자"""
    if v is None:
        return "-"
    if code and not code.isdigit():
        return f"${v:,.2f}"
    return f"{v:,.0f}원"


def fetch_fchart(code, timeframe, count):
    """네이버 차트 데이터. 분봉은 종가와 '그날 누적' 거래량만 옴."""
    res = requests.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": timeframe, "count": count, "requestType": 0},
        headers=UA, timeout=20,
    )
    res.raise_for_status()
    rows = []
    for part in res.text.split('data="')[1:]:
        f = part.split('"', 1)[0].split("|")
        if len(f) < 6 or f[4] in ("", "null"):
            continue
        c = float(f[4])
        num = lambda x: c if x in ("", "null") else float(x)
        rows.append([f[0], num(f[1]), num(f[2]), num(f[3]), c, int(float(f[5] or 0))])
    return rows


def minute_bars(code):
    """1분봉(거래량을 분당으로 바꿈)과 10분봉·30분봉."""
    raw = fetch_fchart(code, "minute", 8000)
    m1, prev_day, prev_cum = [], None, 0
    for t, o, h, l, c, cum in raw:
        day = t[:8]
        vol = cum if day != prev_day else max(cum - prev_cum, 0)
        prev_day, prev_cum = day, cum
        m1.append([t, c, c, c, c, vol])

    def group(n):
        out = []
        for t, o, h, l, c, v in m1:
            mins = int(t[8:10]) * 60 + int(t[10:12])
            start = mins // n * n
            key = f"{t[:8]}{start // 60:02d}{start % 60:02d}"
            if out and out[-1][0] == key:
                b = out[-1]
                b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
            else:
                out.append([key, o, h, l, c, v])
        return out

    return m1, group(10), group(30)


def sma(values, n):
    return sum(values[-n:]) / n


def rsi(values, n=14):
    """Wilder 방식 RSI (증권사 HTS와 같은 계산)."""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


PRICE_INFO = {}  # code -> (현재가, 전일 대비 %) : 모든 알림 맨 위에 붙임


NAMES = {}       # code -> 종목명
ALERT_ON = []    # 알림 켠 종목 코드 (순서대로) : 모든 알림 끝에 지금 가격을 함께 붙임


def board(skip=None):
    """알림 켠 종목들의 지금 가격·등락률 한 줄씩"""
    out = []
    for c in ALERT_ON:
        if c == skip or c not in PRICE_INFO:
            continue
        px, chg = PRICE_INFO[c]
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "-"
        out.append(f"{NAMES.get(c, c)} {money(c, px)} {arrow}{abs(chg):.2f}%")
    return ("\n\n[지금 가격]\n" + "\n".join(out)) if out else ""


def layout(title, message, code):
    """폰 알림 한 줄에 다 보이게: 제목 = 종목명 현재가 등락률, 본문 첫 줄 = 알림 종류.
    (제목, 본문, 태그) 를 돌려줌"""
    info, name = PRICE_INFO.get(code), NAMES.get(code)
    if not info or not name:
        return title, message, "chart_with_downwards_trend"
    px, chg = info
    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "-"
    kind = " ".join(title.replace(name, " ").replace(":", " ").split())
    tag = "red_circle" if chg > 0 else "large_blue_circle" if chg < 0 else "white_circle"
    return f"{name} {money(code, px)} {arrow}{abs(chg):.2f}%", f"[{kind}] {message}{board(code)}", tag


def send_ntfy(topic, title, message, code=None, high=False, _laid=False):
    tag = "chart_with_downwards_trend"
    if not _laid:
        title, message, tag = layout(title, message, code)
    body = {
        "topic": topic,
        "title": title,
        "message": message,
        "tags": [tag],
        "priority": 4 if high else 3,
    }
    if code:
        body["click"] = f"https://m.stock.naver.com/domestic/stock/{code}/total"
    r = requests.post("https://ntfy.sh", json=body, timeout=15)
    r.raise_for_status()


def _h(text):
    """ntfy 헤더에 한글을 넣으려면 RFC 2047 방식으로 감싸야 함."""
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def send_ntfy_image(topic, title, message, png, code, high=False):
    """차트 이미지를 첨부해서 알림. 실패하면 글자만 보냄."""
    title, message, tag = layout(title, message, code)
    try:
        r = requests.put(
            f"https://ntfy.sh/{topic}",
            data=png,
            headers={
                "Title": _h(title),
                "Message": _h(message),
                "Filename": f"{code}.png",
                "Tags": tag,
                "Priority": "4" if high else "3",
                "Click": f"https://m.stock.naver.com/domestic/stock/{code}/total",
            },
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[이미지 알림 실패, 글자로 보냄] {e}")
        send_ntfy(topic, title, message, code=code, high=high, _laid=True)


def pc_alive(topic):
    """기숙사 PC의 실시간 감시가 최근 10분 안에 '켜져 있음' 신호를 보냈는지."""
    try:
        r = requests.get(f"https://ntfy.sh/{topic}-hb/json", params={"poll": "1", "since": "10m"}, timeout=10)
        return any('"event":"message"' in line for line in r.text.splitlines())
    except Exception:
        return False


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def chart_extras(chart):
    """일봉 외에 주봉·분봉을 붙임. 한 종목이 실패해도 일봉은 남김."""
    for code, item in chart.items():
        try:
            if item.get("market") == "US":
                week, m1, m10, m30 = us_extras(code, item.pop("_m1", []))
                item["week"], item["m1"], item["m10"], item["m30"] = week[-CHART_WEEKS:], m1[-CHART_M1:], m10[-CHART_M10:], m30[-CHART_M30:]
                time.sleep(0.3)
                continue
            item["week"] = fetch_fchart(code, "week", CHART_WEEKS + 5)[-CHART_WEEKS:]
            m1, m10, m30 = minute_bars(code)
            item["m1"], item["m10"], item["m30"] = m1[-CHART_M1:], m10[-CHART_M10:], m30[-CHART_M30:]
        except Exception as e:
            print(f"[차트 실패] {item['name']}({code}): {e}")
        time.sleep(0.3)


MARKETS = [("KOSPI", "^KS11", "코스피"), ("KOSDAQ", "^KQ11", "코스닥"), ("USDKRW", "KRW=X", "원/달러"),
           ("SPX", "^GSPC", "S&P500"), ("NDX", "^IXIC", "나스닥"), ("DJI", "^DJI", "다우")]


def _bars15(rows):
    """5분봉 -> 15분봉 [[시,고,저,종], ...] (지수 띠의 작은 봉 차트용)"""
    out, key = [], None
    for t, o, h, l, c, v in rows:
        k = t[:10] + f"{int(t[10:12]) // 15:02d}"
        if k != key:
            out.append([o, h, l, c]); key = k
        else:
            b = out[-1]; b[1], b[2], b[3] = max(b[1], h), min(b[2], l), c
    return [[round(x, 2) for x in b] for b in out][-32:]


def save_markets(now):
    """종합주가지수·환율: 지금 값, 전일 대비, 오늘 흐름(5분) → data/markets.json"""
    out = []
    for key, sym, name in MARKETS:
        try:
            meta, rows = _yh(sym, "1d", "5m")
            px = meta.get("regularMarketPrice") or (rows[-1][4] if rows else None)
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if key in ("KOSPI", "KOSDAQ"):  # 한국 지수 값·전일 대비는 네이버가 정확 (야후는 전일값이 어긋날 때가 있음)
                b = requests.get(f"https://m.stock.naver.com/api/index/{key}/basic", headers=UA, timeout=10).json()
                px = float(str(b["closePrice"]).replace(",", ""))
                prev = px - float(str(b["compareToPreviousClosePrice"]).replace(",", ""))
            if not px or not prev:
                continue
            out.append({"key": key, "name": name, "px": round(px, 2), "prev": round(prev, 2),
                        "chg": round((px - prev) / prev * 100, 2), "t": rows[-1][0] if rows else "",
                        "sp": [round(r[4], 2) for r in rows][-80:], "k": _bars15(rows)})
        except Exception as e:
            print(f"[지수 실패] {name}: {e}")
    if out:
        (CHART.parent / "markets.json").write_text(
            json.dumps({"updatedAt": now.isoformat(timespec="minutes"), "items": out}, ensure_ascii=False), encoding="utf-8")


def save_chart(chart, now):
    try:
        CHART.parent.mkdir(exist_ok=True)
        save_markets(now)
    except Exception as e:
        print("[지수 실패]", e)
    if not chart:
        return
    CHART.parent.mkdir(exist_ok=True)
    out = {"updatedAt": now.isoformat(timespec="minutes"), "stocks": chart}
    CHART.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC 이 비어 있어요. GitHub 저장소 Settings → Secrets 에 넣어주세요.")
    test = os.environ.get("TEST_MODE") == "1"
    chart_only = os.environ.get("CHART_ONLY") == "1"  # 알림 없이 차트 데이터만 갱신
    image_test = os.environ.get("IMAGE_TEST_TOPIC", "").strip()  # 이미지 알림 형식 점검용 임시 토픽

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    ny = datetime.now(NY)
    kr_hours = now.weekday() < 5 and 800 <= now.hour * 100 + now.minute <= 2000
    us_hours = ny.weekday() < 5 and 400 <= ny.hour * 100 + ny.minute <= 2000  # 미국 프리~애프터 (뉴욕 시간)
    if not (test or chart_only) and not (kr_hours or us_hours):
        print(f"장 시간이 아니라 건너뜀 ({now:%m-%d %H:%M} KST)")
        return

    # 기록: {"날짜": {"종목코드": "마지막 알림 시각"}} — 오늘 것만 남김
    old = load_state().get(today, {})
    last_sent = old if isinstance(old, dict) else {}
    state = {today: last_sent}
    # PC 실시간 감시가 켜져 있으면 알림은 PC에 맡기고 여기서는 차트 데이터만 준비 (중복 알림 방지)
    pc_on = not test and not chart_only and pc_alive(topic)
    if pc_on:
        print("[양보] PC 실시간 감시가 켜져 있어서 한국 종목 알림은 쉬어요")
    closed = set()  # 오늘 휴장으로 보이는 시장

    def mute(s):
        """이 종목은 지금 알림을 보내지 않음: 알림 꺼짐 / 장 시간 아님 / 휴장 / PC가 맡음"""
        if chart_only or not s.get("alert", True) or s["market"] in closed:
            return True
        if test:
            return False
        if s["market"] == "US":
            return not us_hours
        return pc_on or not kr_hours

    def tday(s):
        return ny.strftime("%Y%m%d") if s["market"] == "US" else now.strftime("%Y%m%d")

    def due(code):
        t = last_sent.get(code)
        if not t:
            return True
        # 예약 실행이 몇 분 늦을 수 있어서 5분 여유를 둠
        return now - datetime.fromisoformat(t) >= timedelta(minutes=REPEAT_MIN - 5)

    report, hits = [], []
    chart = {}
    today_ymd = now.strftime("%Y%m%d")
    checked = set()
    for s in load_watchlist():
        try:
            if s["market"] == "US":
                candles, live, m1 = us_load(s["code"])
                if live and candles and KRX[s["code"]][2] != "OPEN" and candles[-1][0] == ny.strftime("%Y%m%d"):
                    pass
                closes = [c[4] for c in candles]
                if live:
                    closes = closes[:-1] + [live] if candles and candles[-1][0] == tday(s) else closes + [live]
            else:
                candles = merge_live(fetch_candles(s["code"]), fetch_quote(s["code"]), today_ymd)
                closes = [c[4] for c in candles]
                live, m1 = None, []
            last_date = candles[-1][0] if candles else ""
            chart[s["code"]] = {"name": s["name"], "market": s["market"], "day": candles[-CHART_DAYS:]}
            if s["market"] == "US":
                chart[s["code"]]["_m1"] = m1
                chart[s["code"]]["live"] = live
            if s["code"] in KRX:
                chart[s["code"]]["krx"] = KRX[s["code"]]
            if s["code"] in INFO:
                chart[s["code"]]["info"] = INFO[s["code"]]
            # 등락 기준 = 전일 정규장 종가 (증권사 앱과 같음). 모르면 전날 일봉 종가
            base = (KRX.get(s["code"]) or [0, 0, 0, 0])[3] or (candles[-2][4] if len(candles) >= 2 else None)
            if base:
                chart[s["code"]]["base"] = base
                NAMES[s["code"]] = s["name"]
                if s.get("alert", True) and s["code"] not in ALERT_ON:
                    ALERT_ON.append(s["code"])
                cur = closes[-1] if closes else candles[-1][4]
                PRICE_INFO[s["code"]] = (cur, (cur - base) / base * 100)
        except Exception as e:  # 한 종목 실패해도 나머지는 계속
            print(f"[실패] {s['name']}({s['code']}): {e}")
            report.append(f"{s['name']}: 시세를 못 가져옴")
            continue
        # 공휴일 등 장이 안 열린 날: 장 시작 30분이 지나도 오늘 시세가 없으면 그 시장은 알림 쉼
        if s["market"] not in checked and not (test or chart_only):
            checked.add(s["market"])
            t_now = (ny if s["market"] == "US" else now)
            if last_date != tday(s) and t_now.hour * 100 + t_now.minute >= (1000 if s["market"] == "US" else 930):
                print(f"[{s['market']}] 오늘({tday(s)}) 시세가 없어 휴장일로 보고 알림을 쉬어요 (마지막 {last_date})")
                closed.add(s["market"])
        if len(closes) < MA_DAYS + 1:
            report.append(f"{s['name']}: 데이터 부족")
            continue

        price = closes[-1]
        ma = sma(closes, MA_DAYS)
        gap = (price - ma) / ma * 100
        r = rsi(closes, RSI_DAYS)
        near = abs(gap) <= NEAR_PCT
        low = r is not None and r <= RSI_MAX

        line = f"{s['name']} {money(s['code'], price)} | 20일선 {gap:+.1f}% | RSI {r:.1f}"
        print(line, "← 조건 충족" if near and low else "")
        report.append(line)

        if near and low and due(s["code"]) and not mute(s):
            hits.append((s, line, candles[-CHART_DAYS:], r))
        time.sleep(0.3)

    # 주봉·분봉 (차트용 + 알림선 확인용)
    chart_extras(chart)

    # ---------- 전일 대비 3% 단위 변동 ----------
    for s in load_watchlist():
        item = chart.get(s["code"])
        if not item or mute(s) or len(item.get("day", [])) < 2:
            continue
        if item["day"][-1][0] != tday(s) and not item.get("live"):
            continue
        prev_close, px = item.get("base") or item["day"][-2][4], item.get("live") or item["day"][-1][4]
        chg = (px - prev_close) / prev_close * 100
        step = int(chg / STEP_PCT)  # +7.2% → 2, -3.5% → -1
        key = f"step:{s['code']}"
        done = last_sent.get(key, 0)
        if step != 0 and abs(step) > abs(done) or (step != 0 and (step > 0) != (done > 0)):
            last_sent[key] = step
            title = f"{'급등' if step > 0 else '급락'} {step * STEP_PCT:+.0f}%: {s['name']}"
            msg = f"전일 {money(s['code'], prev_close)} → 지금 {money(s['code'], px)} ({chg:+.2f}%)"
            try:
                from chart_image import daily_chart_png
                png = daily_chart_png(s["code"], item["day"], f"{chg:+.1f}%", hlines=s["lines"])
                send_ntfy_image(topic, title, msg, png, s["code"], high=True)
            except Exception as e:
                print(f"[차트 이미지 실패] {e}")
                send_ntfy(topic, title, msg, code=s["code"], high=True)
            print(f"[변동] {title}")

    # ---------- 볼린저 상단·하단, 20일선에 2% 안으로 다가왔는지 ----------
    for s in load_watchlist():
        item = chart.get(s["code"])
        if not item or mute(s) or len(item.get("day", [])) < MA_DAYS:
            continue
        closes = [b[4] for b in item["day"]]
        px = item.get("live") or closes[-1]
        w = closes[-MA_DAYS:]
        mid = sum(w) / MA_DAYS
        sd = (sum((x - mid) ** 2 for x in w) / MA_DAYS) ** 0.5
        levels = [("밴드 상단", mid + 2 * sd), ("20일선", mid), ("밴드 하단", mid - 2 * sd)]
        near = []
        for lname, lv in levels:
            gap = (lv - px) / px * 100
            key = f"lvl:{s['code']}:{lname}"
            if abs(gap) <= LEVEL_PCT and due(key):
                near.append((lname, lv, gap))
                last_sent[key] = now.isoformat(timespec="minutes")
        if not near:
            continue
        title = f"근접: {s['name']} " + "·".join(n for n, _, _ in near)
        msg = " / ".join(f"{n} {money(s['code'], lv)}까지 {'위로' if g >= 0 else '아래로'} {abs(g):.1f}%" for n, lv, g in near) + f" · 지금 {money(s['code'], px)}"
        try:
            from chart_image import daily_chart_png
            en = {"밴드 상단": "BB upper", "20일선": "MA20", "밴드 하단": "BB lower"}
            png = daily_chart_png(s["code"], item["day"], "near " + " / ".join(en[n] for n, _, _ in near), hlines=s["lines"])
            send_ntfy_image(topic, title, msg, png, s["code"])
        except Exception as e:
            print(f"[차트 이미지 실패] {e}")
            send_ntfy(topic, title, msg, code=s["code"])
        print(f"[근접] {title}")

    # ---------- 내가 그린 알림선에 닿았는지 확인 ----------
    for s in load_watchlist():
        since = last_sent.get(f"_checked:{s['market']}", "")
        item = chart.get(s["code"])
        if not s["lines"] or not item or mute(s):
            continue
        m1 = [b for b in item.get("m1", []) if b[0][:8] == tday(s)]
        prev = [b for b in m1 if b[0] <= since]
        new = [b for b in m1 if b[0] > since]
        live = item.get("live") or (item["day"][-1][4] if item.get("day") and item["day"][-1][0] == tday(s) else None)
        last_px = last_sent.get(f"_px:{s['code']}")
        if new:
            start = prev[-1][4] if prev else (last_px or new[0][4])
            path = [start] + [b[4] for b in new] + ([live] if live else [])
        elif live and last_px:
            path = [last_px, live]  # 분봉이 없는 프리마켓: 지난 확인 가격 → 지금 가격
        else:
            if live:
                last_sent[f"_px:{s['code']}"] = live
            continue
        lo, hi, now_px = min(path), max(path), path[-1]
        for L in s["lines"]:
            key = f"line:{s['code']}:{L:g}"
            if not due(key) or not (lo <= L <= hi):
                continue
            from_below = start < L
            how = "아래에서 올라와" if from_below else "위에서 내려와"
            kind = "저항선" if from_below else "지지선"
            title = f"알림선 도달: {s['name']} {money(s['code'], L)}"
            msg = f"{how} {kind} {money(s['code'], L)}에 닿았어요 · 지금 {money(s['code'], now_px)}"
            try:
                from chart_image import daily_chart_png
                png = daily_chart_png(s["code"], item["day"], f"line {L:,.0f}", hlines=s["lines"])
                send_ntfy_image(topic, title, msg, png, s["code"], high=True)
            except Exception as e:
                print(f"[차트 이미지 실패] {e}")
                send_ntfy(topic, title, msg, code=s["code"], high=True)
            last_sent[key] = now.isoformat(timespec="minutes")
            print(f"[알림선] {title}")
        last_sent[f"_px:{s['code']}"] = now_px
    if not chart_only:
        for mk in ("KR", "US"):
            ts = [b[0] for it in chart.values() if it.get("market") == mk for b in it.get("m1", [])]
            if ts:
                last_sent[f"_checked:{mk}"] = max(ts)

    for s, line, rows, r in hits:
        again = s["code"] in last_sent
        title = f"{'계속 ' if again else ''}매수 조건: {s['name']}"
        try:
            from chart_image import daily_chart_png
            png = daily_chart_png(s["code"], rows, f"RSI {r:.1f}")
            send_ntfy_image(topic, title, line, png, s["code"], high=not again)
        except Exception as e:
            print(f"[차트 이미지 실패] {e}")
            send_ntfy(topic, title, line, code=s["code"], high=not again)
        last_sent[s["code"]] = now.isoformat(timespec="minutes")
        print(f"[알림] {title}")

    # 차트 데이터는 매번 저장 (chart-data 브랜치에 덮어써서 저장소가 커지지 않음)
    save_chart(chart, now)
    if image_test and chart:
        from chart_image import daily_chart_png
        code, item = next(iter(chart.items()))
        send_ntfy_image(image_test, f"이미지 점검: {item['name']}", "차트 첨부 확인용",
                        daily_chart_png(code, item["day"], "test"), code)
        print("[점검] 이미지 알림 보냄")
        try:
            time.sleep(2)
            poll = requests.get(f"https://ntfy.sh/{image_test}/json?poll=1", timeout=15).text
            (ROOT / "data" / "probe.json").write_text(poll, encoding="utf-8")
        except Exception as e:
            (ROOT / "data" / "probe.json").write_text(f"poll 실패: {e}", encoding="utf-8")
    if chart_only:
        print("[차트] 차트 데이터만 갱신")
        return

    if test:
        send_ntfy(topic, "시드 알림 테스트", "\n".join(report) or "관심종목이 비어 있어요")
        print("[테스트] 현재 상태 알림 보냄")

    save_state(state)


if __name__ == "__main__":
    main()
