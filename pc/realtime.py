"""시드 실시간 감시 (기숙사 Windows PC용)

- 정규장(09:00~15:30): 한국투자증권 웹소켓으로 체결될 때마다 가격을 받음 (몇 초 지연)
- 그 밖의 시간(08:00~09:00, 15:30~20:00)이나 웹소켓이 끊겼을 때: 1분마다 네이버 시세로 확인
- 알림 규칙은 GitHub 감시와 같음
    1) 20일선 ±2% + RSI 30 이하
    2) 볼린저 상단·하단, 20일선에 2% 안으로 근접
    3) 내가 그린 알림선에 닿음
  같은 조건은 1시간이 지나 다시 해당되면 또 알림
- 2분마다 '켜져 있음' 신호를 보내서, 그동안 GitHub 감시는 알림을 쉬어 중복을 막음

설정은 같은 폴더의 config.ini (config.example.ini 를 복사해서 채움)
"""

import configparser
import csv
import io
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # 저장소 루트의 alert.py, chart_image.py 재사용
import alert  # noqa: E402

KST = alert.KST
log = logging.getLogger("seed")

# ---------- 설정 읽기 ----------
cfg = configparser.ConfigParser()
if not cfg.read(HERE / "config.ini", encoding="utf-8"):
    sys.exit("config.ini 가 없어요. config.example.ini 를 복사해서 config.ini 로 이름을 바꾸고 내용을 채워 주세요.")

APPKEY = cfg.get("kis", "appkey", fallback="").strip()
SECRET = cfg.get("kis", "secretkey", fallback="").strip()
TR_ID = cfg.get("kis", "tr_id", fallback="H0STCNT0").strip()
WS_URL = cfg.get("kis", "ws_url", fallback="ws://ops.koreainvestment.com:21000").strip()
REST_URL = cfg.get("kis", "rest_url", fallback="https://openapi.koreainvestment.com:9443").strip()
TOPIC = cfg.get("ntfy", "topic", fallback="").strip()
GH_TOKEN = cfg.get("github", "token", fallback="").strip()
GH_REPO = cfg.get("github", "repo", fallback="thedivano/seed").strip()

if not TOPIC:
    sys.exit("config.ini 의 [ntfy] topic 을 채워 주세요.")

STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "seed.log"

# ---------- 공유 데이터 ----------
lock = threading.RLock()
watch = []            # [{code, name, lines}]
history = {}          # code -> 지난 거래일까지의 일봉 [날짜,시,고,저,종,량] (오늘 제외)
today_bar = {}        # code -> 오늘 일봉 (실시간 가격으로 고가·저가·종가를 갱신)
prices = {}           # code -> (가격, 받은 시각)
last_tick = {}        # code -> 웹소켓으로 마지막 받은 시각
state = {}            # {"날짜": {key: "마지막 알림 시각"}}


def now():
    return datetime.now(KST)


def in_hours(t=None):
    t = t or now()
    hm = t.hour * 100 + t.minute
    return t.weekday() < 5 and 800 <= hm <= 2000


def regular_hours(t=None):
    t = t or now()
    hm = t.hour * 100 + t.minute
    return t.weekday() < 5 and 900 <= hm <= 1530


# ---------- 관심종목 (GitHub 의 watchlist.csv) ----------
def refresh_watchlist():
    global watch
    if GH_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GH_REPO}/contents/watchlist.csv",
                headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.raw+json"},
                timeout=15,
            )
            r.raise_for_status()
            alert.WATCHLIST.write_text(r.text, encoding="utf-8")
        except Exception as e:
            log.warning("GitHub 에서 관심종목을 못 받아서 갖고 있는 파일을 써요: %s", e)
    new = [w for w in alert.load_watchlist() if w.get("market", "KR") == "KR"]  # PC는 한국 종목만
    with lock:
        changed = {w["code"] for w in new} != {w["code"] for w in watch}
        watch = new
    return changed


def refresh_history():
    """지난 거래일까지의 일봉 종가 (오늘 것은 빼고 저장, 오늘은 실시간 가격으로 채움)."""
    today = now().strftime("%Y%m%d")
    for w in list(watch):
        try:
            c = alert.fetch_candles(w["code"])
            past = [x for x in c if x[0] != today]
            tb = next((x for x in c if x[0] == today), None)
            with lock:
                history[w["code"]] = past[-300:]
                if tb and w["code"] not in today_bar:
                    today_bar[w["code"]] = list(tb)
                if c and w["code"] not in prices:
                    prices[w["code"]] = (c[-1][4], now())
        except Exception as e:
            log.warning("%s 일봉을 못 받았어요: %s", w["name"], e)
        time.sleep(0.2)


# ---------- 알림 규칙 ----------
def load_state():
    global state
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}


def save_state():
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("기록 저장 실패: %s", e)


def due(key):
    day = now().strftime("%Y-%m-%d")
    sent = state.setdefault(day, {})
    for d in list(state):
        if d != day:
            del state[d]
    t = sent.get(key)
    return not t or now() - datetime.fromisoformat(t) >= timedelta(minutes=alert.REPEAT_MIN - 1)


def mark(key):
    state.setdefault(now().strftime("%Y-%m-%d"), {})[key] = now().isoformat(timespec="minutes")
    save_state()


def send(title, msg, code, rows, hlines, high=False, subtitle=""):
    log.info("[알림] %s | %s", title, msg)
    try:
        from chart_image import daily_chart_png
        png = daily_chart_png(code, rows, subtitle, hlines=hlines)
        alert.send_ntfy_image(TOPIC, title, msg, png, code, high=high)
    except Exception as e:
        log.warning("사진 없이 보냄: %s", e)
        alert.send_ntfy(TOPIC, title, msg, code=code, high=high)


def evaluate(code, price, prev):
    """새 가격이 들어올 때마다 세 가지 규칙 확인."""
    with lock:
        w = next((x for x in watch if x["code"] == code), None)
        past = history.get(code)
        tb = today_bar.get(code)
        if tb is None or tb[0] != now().strftime("%Y%m%d"):
            tb = today_bar[code] = [now().strftime("%Y%m%d"), price, price, price, price, 0]
        tb[2], tb[3], tb[4] = max(tb[2], price), min(tb[3], price), price
        rows = (past or []) + [list(tb)]
    if not w or not past or len(past) < alert.MA_DAYS or not w.get("alert", True):
        return
    closes = [x[4] for x in rows]
    if len(rows) >= 2:
        alert.NAMES[code] = w["name"]
        alert.PRICE_INFO[code] = (price, (price - rows[-2][4]) / rows[-2][4] * 100)
    w20 = closes[-alert.MA_DAYS:]
    ma20 = sum(w20) / alert.MA_DAYS
    sd = (sum((x - ma20) ** 2 for x in w20) / alert.MA_DAYS) ** 0.5
    r = alert.rsi(closes, alert.RSI_DAYS)
    gap20 = (price - ma20) / ma20 * 100
    name = w["name"]

    # 1) 20일선 근접 + RSI 30 이하
    key = code
    if abs(gap20) <= alert.NEAR_PCT and r is not None and r <= alert.RSI_MAX and due(key):
        again = any(key in v for v in state.values())
        line = f"{name} {price:,.0f}원 | 20일선 {gap20:+.1f}% | RSI {r:.1f}"
        send(f"{'계속 ' if again else ''}매수 조건: {name}", line, code, rows, w["lines"], high=not again, subtitle=f"RSI {r:.1f}")
        mark(key)

    # 2) 볼린저 상단·하단, 20일선 2% 근접
    near = []
    for lname, lv, en in (("밴드 상단", ma20 + 2 * sd, "BB upper"), ("20일선", ma20, "MA20"), ("밴드 하단", ma20 - 2 * sd, "BB lower")):
        g = (lv - price) / price * 100
        k = f"lvl:{code}:{lname}"
        if abs(g) <= alert.LEVEL_PCT and due(k):
            near.append((lname, lv, g, en))
            mark(k)
    if near:
        msg = " / ".join(f"{n} {lv:,.0f}원까지 {'위로' if g >= 0 else '아래로'} {abs(g):.1f}%" for n, lv, g, _ in near) + f" · 지금 {price:,.0f}원"
        send(f"근접: {name} " + "·".join(n for n, *_ in near), msg, code, rows, w["lines"], subtitle="near " + " / ".join(e for *_, e in near))

    # 4) 전일 종가 대비 3% 단위 변동
    if len(rows) >= 2:
        pc = rows[-2][4]
        chg = (price - pc) / pc * 100
        step = int(chg / alert.STEP_PCT)
        day = now().strftime("%Y-%m-%d")
        sk = f"step:{code}"
        done = state.setdefault(day, {}).get(sk, 0)
        if step != 0 and (abs(step) > abs(done) or (step > 0) != (done > 0)):
            state[day][sk] = step
            save_state()
            send(f"{'급등' if step > 0 else '급락'} {step * alert.STEP_PCT:+.0f}%: {name}",
                 f"전일 {pc:,.0f}원 → 지금 {price:,.0f}원 ({chg:+.2f}%)", code, rows, w["lines"], high=True, subtitle=f"{chg:+.1f}%")

    # 3) 내가 그린 알림선: 이전 가격과 새 가격 사이에 선이 있으면 닿은 것
    if prev is not None:
        lo, hi = min(prev, price), max(prev, price)
        for L in w["lines"]:
            k = f"line:{code}:{L:g}"
            if lo <= L <= hi and due(k):
                up = prev < L
                msg = f"{'아래에서 올라와' if up else '위에서 내려와'} {'저항선' if up else '지지선'} {L:,.0f}원에 닿았어요 · 지금 {price:,.0f}원"
                send(f"알림선 도달: {name} {L:,.0f}원", msg, code, rows, w["lines"], high=True, subtitle=f"line {L:,.0f}")
                mark(k)


def on_price(code, price, source):
    with lock:
        prev = prices.get(code, (None,))[0]
        prices[code] = (price, now())
        if source == "ws":
            last_tick[code] = time.time()
    if prev != price:
        try:
            evaluate(code, price, prev)
        except Exception as e:
            log.exception("확인 중 오류 (%s): %s", code, e)


# ---------- 한국투자증권 웹소켓 ----------
def approval_key():
    r = requests.post(
        f"{REST_URL}/oauth2/Approval",
        json={"grant_type": "client_credentials", "appkey": APPKEY, "secretkey": SECRET},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["approval_key"]


def ws_loop():
    import websocket  # websocket-client

    subscribed = set()

    def on_open(ws):
        log.info("증권사 실시간 시세에 연결됐어요")
        subscribed.clear()
        with lock:
            codes = [w["code"] for w in watch]
        for c in codes:
            ws.send(json.dumps({
                "header": {"approval_key": ws.key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": TR_ID, "tr_key": c}},
            }))
            subscribed.add(c)
            time.sleep(0.1)

    def on_message(ws, msg):
        if msg and msg[0] in "01":
            parts = msg.split("|", 3)
            if len(parts) < 4 or parts[0] == "1":
                return
            n = int(parts[2] or 1)
            f = parts[3].split("^")
            size = len(f) // n if n else len(f)
            for i in range(n):
                t = f[i * size:(i + 1) * size]
                try:
                    on_price(t[0], float(t[2]), "ws")
                except (IndexError, ValueError):
                    pass
            return
        try:
            data = json.loads(msg)
        except ValueError:
            return
        if data.get("header", {}).get("tr_id") == "PINGPONG":
            ws.send(msg)  # 살아 있다는 응답
        else:
            body = data.get("body", {})
            if body.get("rt_cd") not in (None, "0"):
                log.warning("구독 응답: %s", body.get("msg1"))

    while True:
        if not (APPKEY and SECRET):
            log.warning("증권사 앱키가 없어서 1분 시세 확인만 해요")
            return
        if not regular_hours():
            time.sleep(30)
            continue
        try:
            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                        on_error=lambda ws, e: log.warning("실시간 연결 오류: %s", e))
            ws.key = approval_key()
            stop = threading.Event()

            def watchdog():
                # 장이 끝나거나 관심종목이 바뀌면 연결을 닫고 다시 시작
                while not stop.is_set():
                    time.sleep(20)
                    with lock:
                        codes = {w["code"] for w in watch}
                    if not regular_hours() or codes != subscribed:
                        ws.close()
                        return

            threading.Thread(target=watchdog, daemon=True).start()
            ws.run_forever(ping_interval=None)
            stop.set()
        except Exception as e:
            log.warning("실시간 연결 실패, 30초 뒤 다시 시도: %s", e)
        time.sleep(30)


# ---------- 1분 시세 확인 (장외 시간, 웹소켓이 조용할 때) ----------
def latest_price(code):
    q = alert.fetch_quote(code)  # 프리·애프터마켓 가격까지 나옴
    return q["price"] if q else None


def poll_loop():
    while True:
        start = time.time()
        if in_hours():
            with lock:
                codes = [w["code"] for w in watch]
            for c in codes:
                # 정규장에 웹소켓으로 최근 90초 안에 받은 종목은 건너뜀
                if regular_hours() and time.time() - last_tick.get(c, 0) < 90:
                    continue
                try:
                    p = latest_price(c)
                    if p:
                        on_price(c, p, "poll")
                except Exception as e:
                    log.warning("%s 시세 확인 실패: %s", c, e)
                time.sleep(0.2)
        time.sleep(max(5, 60 - (time.time() - start)))


# ---------- 켜져 있음 신호 ----------
def heartbeat_loop():
    while True:
        if in_hours():
            try:
                requests.post(f"https://ntfy.sh/{TOPIC}-hb", data="alive", timeout=10)
            except Exception:
                pass
        time.sleep(120)


def refresh_loop():
    while True:
        time.sleep(600)
        try:
            if refresh_watchlist():
                log.info("관심종목이 바뀌어서 다시 불러와요")
            refresh_history()
        except Exception as e:
            log.warning("새로 불러오기 실패: %s", e)


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_state()
    refresh_watchlist()
    log.info("관심종목 %d개: %s", len(watch), ", ".join(w["name"] for w in watch))
    refresh_history()
    for target in (ws_loop, poll_loop, heartbeat_loop, refresh_loop):
        threading.Thread(target=target, daemon=True).start()
    if "--test" in sys.argv:
        with lock:
            lines = [f"{w['name']} {prices.get(w['code'], (0,))[0]:,.0f}원" for w in watch]
        alert.send_ntfy(TOPIC, "시드 PC 감시 시작", "\n".join(lines) or "관심종목이 비어 있어요")
        log.info("테스트 알림을 보냈어요")
    log.info("감시를 시작했어요. 이 창을 닫으면 멈춰요.")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
