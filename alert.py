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
UA = {"User-Agent": "Mozilla/5.0 (seed-alert)"}


def load_watchlist():
    with open(WATCHLIST, encoding="utf-8-sig") as f:
        rows = []
        for r in csv.DictReader(f):
            code = (r.get("종목코드") or "").strip().zfill(6)
            name = (r.get("종목명") or "").strip()
            lines = []
            for x in (r.get("알림선") or "").replace(",", "/").split("/"):
                try:
                    if x.strip():
                        lines.append(float(x.strip()))
                except ValueError:
                    pass
            if code.isdigit() and len(code) == 6:
                rows.append({"code": code, "name": name or code, "lines": lines})
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


def fetch_quote(code):
    """네이버 실시간 현재가. 프리·애프터마켓(넥스트레이드) 시간에는 그 가격을 돌려줌.
    {"price", "open", "high", "low", "session"} 또는 None"""
    try:
        r = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}", headers=UA, timeout=10)
        d = r.json()["datas"][0]
    except Exception:
        return None
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


def send_ntfy(topic, title, message, code=None, high=False):
    body = {
        "topic": topic,
        "title": title,
        "message": message,
        "tags": ["chart_with_downwards_trend"],
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
    try:
        r = requests.put(
            f"https://ntfy.sh/{topic}",
            data=png,
            headers={
                "Title": _h(title),
                "Message": _h(message),
                "Filename": f"{code}.png",
                "Tags": "chart_with_downwards_trend",
                "Priority": "4" if high else "3",
                "Click": f"https://m.stock.naver.com/domestic/stock/{code}/total",
            },
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[이미지 알림 실패, 글자로 보냄] {e}")
        send_ntfy(topic, title, message, code=code, high=high)


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
            item["week"] = fetch_fchart(code, "week", CHART_WEEKS + 5)[-CHART_WEEKS:]
            m1, m10, m30 = minute_bars(code)
            item["m1"], item["m10"], item["m30"] = m1[-CHART_M1:], m10[-CHART_M10:], m30[-CHART_M30:]
        except Exception as e:
            print(f"[차트 실패] {item['name']}({code}): {e}")
        time.sleep(0.3)


def save_chart(chart, now):
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
    if not (test or chart_only):
        hm = now.hour * 100 + now.minute
        if now.weekday() >= 5 or not (800 <= hm <= 2000):
            print(f"장 시간이 아니라 건너뜀 ({now:%m-%d %H:%M} KST)")
            return

    # 기록: {"날짜": {"종목코드": "마지막 알림 시각"}} — 오늘 것만 남김
    old = load_state().get(today, {})
    last_sent = old if isinstance(old, dict) else {}
    state = {today: last_sent}
    # PC 실시간 감시가 켜져 있으면 알림은 PC에 맡기고 여기서는 차트 데이터만 준비 (중복 알림 방지)
    quiet = chart_only or (not test and pc_alive(topic))
    if quiet and not chart_only:
        print("[양보] PC 실시간 감시가 켜져 있어서 알림은 쉬어요")

    def due(code):
        t = last_sent.get(code)
        if not t:
            return True
        # 예약 실행이 몇 분 늦을 수 있어서 5분 여유를 둠
        return now - datetime.fromisoformat(t) >= timedelta(minutes=REPEAT_MIN - 5)

    report, hits = [], []
    chart = {}
    today_ymd = now.strftime("%Y%m%d")
    market_checked = False
    for s in load_watchlist():
        try:
            candles = merge_live(fetch_candles(s["code"]), fetch_quote(s["code"]), today_ymd)
            closes = [c[4] for c in candles]
            last_date = candles[-1][0] if candles else ""
            chart[s["code"]] = {"name": s["name"], "day": candles[-CHART_DAYS:]}
        except Exception as e:  # 한 종목 실패해도 나머지는 계속
            print(f"[실패] {s['name']}({s['code']}): {e}")
            report.append(f"{s['name']}: 시세를 못 가져옴")
            continue
        # 공휴일 등 장이 안 열린 날: 9:30이 지나도 오늘 시세가 없으면 전체 건너뜀
        if not market_checked and not (test or chart_only):
            market_checked = True
            if last_date != today_ymd and now.hour * 100 + now.minute >= 930:
                print(f"오늘({today_ymd}) 시세가 없어 휴장일로 보고 건너뜀 (마지막 {last_date})")
                save_state(state)
                return
        if len(closes) < MA_DAYS + 1:
            report.append(f"{s['name']}: 데이터 부족")
            continue

        price = closes[-1]
        ma = sma(closes, MA_DAYS)
        gap = (price - ma) / ma * 100
        r = rsi(closes, RSI_DAYS)
        near = abs(gap) <= NEAR_PCT
        low = r is not None and r <= RSI_MAX

        line = f"{s['name']} {price:,.0f}원 | 20일선 {gap:+.1f}% | RSI {r:.1f}"
        print(line, "← 조건 충족" if near and low else "")
        report.append(line)

        if near and low and due(s["code"]) and not quiet:
            hits.append((s, line, candles[-CHART_DAYS:], r))
        time.sleep(0.3)

    # 주봉·분봉 (차트용 + 알림선 확인용)
    chart_extras(chart)

    # ---------- 전일 대비 3% 단위 변동 ----------
    for s in load_watchlist():
        item = chart.get(s["code"])
        if not item or quiet or len(item.get("day", [])) < 2 or item["day"][-1][0] != today_ymd:
            continue
        prev_close, px = item["day"][-2][4], item["day"][-1][4]
        chg = (px - prev_close) / prev_close * 100
        step = int(chg / STEP_PCT)  # +7.2% → 2, -3.5% → -1
        key = f"step:{s['code']}"
        done = last_sent.get(key, 0)
        if step != 0 and abs(step) > abs(done) or (step != 0 and (step > 0) != (done > 0)):
            last_sent[key] = step
            title = f"{'급등' if step > 0 else '급락'} {step * STEP_PCT:+.0f}%: {s['name']}"
            msg = f"전일 {prev_close:,.0f}원 → 지금 {px:,.0f}원 ({chg:+.2f}%)"
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
        if not item or quiet or len(item.get("day", [])) < MA_DAYS:
            continue
        closes = [b[4] for b in item["day"]]
        px = closes[-1]
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
        msg = " / ".join(f"{n} {lv:,.0f}원까지 {'위로' if g >= 0 else '아래로'} {abs(g):.1f}%" for n, lv, g in near) + f" · 지금 {px:,.0f}원"
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
    since = last_sent.get("_checked", "")
    for s in load_watchlist():
        item = chart.get(s["code"])
        if not s["lines"] or not item or quiet:
            continue
        m1 = [b for b in item.get("m1", []) if b[0][:8] == today_ymd]
        prev = [b for b in m1 if b[0] <= since]
        new = [b for b in m1 if b[0] > since]
        live = item["day"][-1][4] if item.get("day") and item["day"][-1][0] == today_ymd else None
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
            title = f"알림선 도달: {s['name']} {L:,.0f}원"
            msg = f"{how} {kind} {L:,.0f}원에 닿았어요 · 지금 {now_px:,.0f}원"
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
        last_sent["_checked"] = max([b[0] for it in chart.values() for b in it.get("m1", [])] or [since])

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
