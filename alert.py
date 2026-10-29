"""관심종목 감시 → 조건 충족 시 ntfy 알림.

조건 (config 값은 아래에서 바꿀 수 있음)
  1) 20일선 근접: 현재가가 20일 이동평균의 ±NEAR_PCT % 안에 있음
  2) RSI(14) 가 RSI_MAX 이하
두 조건을 모두 만족하면 알림. 같은 종목은 하루에 한 번만 보냄.
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

import requests

# ---------- 설정 ----------
MA_DAYS = 20
RSI_DAYS = 14
NEAR_PCT = 2.0   # 20일선 ±2% 이내면 "근접"
RSI_MAX = 30.0
# --------------------------

ROOT = Path(__file__).parent
WATCHLIST = ROOT / "watchlist.csv"
STATE = ROOT / "state" / "sent.json"
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (seed-alert)"}


def load_watchlist():
    with open(WATCHLIST, encoding="utf-8-sig") as f:
        rows = []
        for r in csv.DictReader(f):
            code = (r.get("종목코드") or "").strip().zfill(6)
            name = (r.get("종목명") or "").strip()
            if code.isdigit() and len(code) == 6:
                rows.append({"code": code, "name": name or code})
        return rows


def fetch_closes(code, days=120):
    """네이버 금융 일봉 (마지막 날짜, 종가 목록[오래된 순]). 장중에는 마지막 값이 현재가."""
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
    closes, last_date = [], ""
    for row in data[1:]:  # 첫 줄은 헤더
        try:
            closes.append(float(row[4]))  # 날짜, 시가, 고가, 저가, 종가, ...
            last_date = str(row[0]).strip()
        except (IndexError, TypeError, ValueError):
            continue
    return last_date, closes


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


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        sys.exit("NTFY_TOPIC 이 비어 있어요. GitHub 저장소 Settings → Secrets 에 넣어주세요.")
    test = os.environ.get("TEST_MODE") == "1"

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    if not test:
        hm = now.hour * 100 + now.minute
        if now.weekday() >= 5 or not (800 <= hm <= 2000):
            print(f"장 시간이 아니라 건너뜀 ({now:%m-%d %H:%M} KST)")
            return

    state = load_state()
    sent_today = set(state.get(today, []))
    # 오늘 기록만 남김
    state = {today: sorted(sent_today)}

    report, hits = [], []
    today_ymd = now.strftime("%Y%m%d")
    market_checked = False
    for s in load_watchlist():
        try:
            last_date, closes = fetch_closes(s["code"])
        except Exception as e:  # 한 종목 실패해도 나머지는 계속
            print(f"[실패] {s['name']}({s['code']}): {e}")
            report.append(f"{s['name']}: 시세를 못 가져옴")
            continue
        # 공휴일 등 장이 안 열린 날: 9:30이 지나도 오늘 시세가 없으면 전체 건너뜀
        if not market_checked and not test:
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

        if near and low and s["code"] not in sent_today:
            hits.append((s, line))
        time.sleep(0.3)

    for s, line in hits:
        send_ntfy(topic, f"매수 조건 도달: {s['name']}", line, code=s["code"], high=True)
        sent_today.add(s["code"])
        print(f"[알림] {s['name']}")

    if test:
        send_ntfy(topic, "시드 알림 테스트", "\n".join(report) or "관심종목이 비어 있어요")
        print("[테스트] 현재 상태 알림 보냄")

    state[today] = sorted(sent_today)
    save_state(state)


if __name__ == "__main__":
    main()
