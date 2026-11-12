"""테마 대장주: 지금 강한 테마와 그 안에서 앞서가는 종목.

한국: 네이버 테마 분류 전체를 쓰고, 테마 안 종목들의 수익률 중앙값으로 테마의 힘을 잼.
미국: 정해 둔 테마 목록(US_THEMES)의 종목을 야후에서 받아 같은 방식으로 잼.
대장주 = 테마 안에서 시가총액·거래대금 기준을 넘는 종목 중 최근 20거래일 수익률이 높은 순.
결과는 state/themes.json 에 쓰고 시드 페이지의 '테마' 목록에 나와요.
"""
import statistics
from concurrent.futures import ThreadPoolExecutor

US_THEMES = {
    "AI 반도체": ["NVDA", "AVGO", "AMD", "TSM", "ARM", "MRVL"],
    "메모리·저장장치": ["MU", "SKHY", "SNDK", "WDC", "STX"],
    "반도체 장비": ["ASML", "AMAT", "LRCX", "KLAC"],
    "AI 데이터센터·네오클라우드": ["CRWV", "NBIS", "ORCL", "IREN", "CIFR"],
    "광통신·AI 네트워크": ["ANET", "CRDO", "ALAB", "COHR", "LITE", "CLS"],
    "전력·전력장비": ["VRT", "GEV", "ETN", "CEG", "VST"],
    "원전·SMR": ["OKLO", "SMR", "CCJ", "LEU", "NNE"],
    "양자컴퓨터": ["IONQ", "RGTI", "QBTS"],
    "우주": ["SPCX", "RKLB", "ASTS", "LUNR"],
    "방산·드론": ["LMT", "NOC", "KTOS", "AVAV", "PLTR"],
    "비트코인·디지털자산": ["MSTR", "COIN", "HOOD", "CRCL", "BMNR"],
    "사이버보안": ["CRWD", "PANW", "ZS", "NET"],
    "AI 소프트웨어": ["PLTR", "NOW", "APP", "SNOW", "DDOG"],
    "로봇·자율주행·UAM": ["TSLA", "ISRG", "ACHR", "JOBY"],
    "비만치료제·헬스케어": ["LLY", "NVO", "HIMS"],
}
US_NAMES = {
    "NVDA": "엔비디아", "AVGO": "브로드컴", "AMD": "AMD", "TSM": "TSMC", "ARM": "ARM", "MRVL": "마벨",
    "MU": "마이크론", "SKHY": "SK하이닉스ADR", "SNDK": "샌디스크", "WDC": "웨스턴디지털", "STX": "시게이트",
    "ASML": "ASML", "AMAT": "어플라이드머티리얼즈", "LRCX": "램리서치", "KLAC": "KLA",
    "CRWV": "코어위브", "NBIS": "네비우스", "ORCL": "오라클", "IREN": "아이렌", "CIFR": "사이퍼마이닝",
    "ANET": "아리스타", "CRDO": "크레도", "ALAB": "아스테라랩스", "COHR": "코히런트", "LITE": "루멘텀", "CLS": "셀레스티카",
    "VRT": "버티브", "GEV": "GE버노바", "ETN": "이튼", "CEG": "컨스텔레이션", "VST": "비스트라",
    "OKLO": "오클로", "SMR": "뉴스케일", "CCJ": "카메코", "LEU": "센트러스", "NNE": "나노뉴클리어",
    "IONQ": "아이온큐", "RGTI": "리게티", "QBTS": "디웨이브",
    "SPCX": "스페이스X", "RKLB": "로켓랩", "ASTS": "AST스페이스모바일", "LUNR": "인튜이티브머신스",
    "LMT": "록히드마틴", "NOC": "노스롭그러먼", "KTOS": "크라토스", "AVAV": "에어로바이런먼트", "PLTR": "팔란티어",
    "MSTR": "스트래티지", "COIN": "코인베이스", "HOOD": "로빈후드", "CRCL": "써클", "BMNR": "비트마인",
    "CRWD": "크라우드스트라이크", "PANW": "팔로알토", "ZS": "지스케일러", "NET": "클라우드플레어",
    "NOW": "서비스나우", "APP": "앱러빈", "SNOW": "스노우플레이크", "DDOG": "데이터독",
    "TSLA": "테슬라", "ISRG": "인튜이티브서지컬", "ACHR": "아처", "JOBY": "조비",
    "LLY": "일라이릴리", "NVO": "노보노디스크", "HIMS": "힘스앤허스",
}
KR_MIN_MCAP = 1000      # 억 원. 이보다 작은 종목은 테마 계산·대장주에서 뺌
KR_MIN_VALUE = 30       # 억 원. 20일 평균 거래대금이 이보다 작으면 대장주 후보에서 뺌
KR_TOP = 24             # 보여줄 한국 테마 수
LEADERS = 4             # 테마마다 대장주 수


def metrics(rows, shares=0):
    """rows: [[날짜, 시, 고, 저, 종, 거래량], ...] -> 수익률 등. 데이터가 모자라면 None"""
    if len(rows) < 22:
        return None
    c = [r[4] for r in rows]
    pct = lambda a, b: round((a - b) / b * 100, 2) if b else 0
    return {
        "date": rows[-1][0], "c": c[-1], "r1": pct(c[-1], c[-2]), "r5": pct(c[-1], c[-6]), "r20": pct(c[-1], c[-21]),
        "val": round(sum(r[4] * r[5] for r in rows[-20:]) / 20 / 1e8, 1),
        "mcap": round(c[-1] * shares / 1e8) if shares else 0,
        "sp": [round(x, 2) for x in c[-21:]],
    }


def _pack(name, members, label):
    """members: [(code, 이름, metrics)] -> 테마 한 개"""
    med = lambda k: round(statistics.median(m[k] for _, _, m in members), 2)
    lead = sorted(members, key=lambda t: t[2]["r20"], reverse=True)[:LEADERS]
    return {
        "theme": name, "n": len(members), "r1": med("r1"), "r5": med("r5"), "r20": med("r20"), "src": label,
        "leaders": [{"code": c, "name": n, **{k: m[k] for k in ("c", "r1", "r5", "r20", "val", "mcap", "sp")}}
                    for c, n, m in lead],
    }


def kr_themes(theme_map, met, names):
    """theme_map: 종목코드 -> [(테마명, 테마 종목 수)], met: 종목코드 -> metrics"""
    by = {}
    for code, lst in theme_map.items():
        m = met.get(code)
        if not m or m["mcap"] < KR_MIN_MCAP:
            continue
        for t, _ in lst:
            by.setdefault(t, []).append(code)
    out = []
    for t, codes in by.items():
        if len(codes) < 3:
            continue
        members = [(c, names.get(c, c), met[c]) for c in codes]
        liquid = [x for x in members if x[2]["val"] >= KR_MIN_VALUE] or members
        th = _pack(t, members, "네이버 테마")
        th["leaders"] = _pack(t, liquid, "")["leaders"]
        out.append(th)
    out.sort(key=lambda x: x["r20"], reverse=True)
    return out[:KR_TOP]


def us_themes(yh):
    """yh(ticker) -> (meta, rows)  (alert._yh 를 넘겨 받음)"""
    tickers = sorted({t for v in US_THEMES.values() for t in v})

    def load(t):
        try:
            return t, metrics(yh(t, "3mo", "1d")[1])
        except Exception:
            return t, None

    with ThreadPoolExecutor(8) as ex:
        met = {t: m for t, m in ex.map(load, tickers) if m}
    out = []
    for name, ts in US_THEMES.items():
        members = [(t, US_NAMES.get(t, t), met[t]) for t in ts if t in met]
        if len(members) >= 2:
            out.append(_pack(name, members, "직접 고른 테마"))
    out.sort(key=lambda x: x["r20"], reverse=True)
    return out
