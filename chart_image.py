"""알림에 붙일 일봉 차트 이미지 (캔들 + 이동평균 5/10/20/30/60/120 + 볼린저밴드 + 거래량)."""

import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

UP, DOWN = "#d23c3c", "#2f63c8"
MA_COLORS = {5: "#e0912b", 10: "#2c9ad1", 20: "#d1437f", 30: "#7a5fd6", 60: "#2a9a66", 120: "#8a7660"}
SHOW = 100  # 이미지에 보여줄 최근 봉 수


def _ma(closes, n):
    out, s = [], 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        out.append(s / n if i >= n - 1 else math.nan)
    return out


def _bb(closes, n=20, k=2):
    up, lo = [], []
    for i in range(len(closes)):
        if i < n - 1:
            up.append(math.nan)
            lo.append(math.nan)
            continue
        w = closes[i - n + 1 : i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        up.append(m + k * sd)
        lo.append(m - k * sd)
    return up, lo


def daily_chart_png(code, rows, subtitle=""):
    """rows: [[YYYYMMDD, 시가, 고가, 저가, 종가, 거래량], ...] 오래된 순."""
    closes = [r[4] for r in rows]
    mas = {n: _ma(closes, n) for n in MA_COLORS}
    bb_up, bb_lo = _bb(closes)

    start = max(0, len(rows) - SHOW)
    view = rows[start:]
    xs = range(len(view))

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(8, 5), dpi=110, sharex=True,
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.04},
    )
    fig.patch.set_facecolor("white")

    ax.fill_between(xs, bb_lo[start:], bb_up[start:], color="#8b9893", alpha=0.12, linewidth=0)
    ax.plot(xs, bb_up[start:], color="#8b9893", lw=0.8, ls="--")
    ax.plot(xs, bb_lo[start:], color="#8b9893", lw=0.8, ls="--")

    for i, (_, o, h, l, c, v) in enumerate(view):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=col, lw=0.8)
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), (h - l) * 0.002 or 1),
                               facecolor=col, edgecolor=col, lw=0.5))
        prev = view[i - 1][4] if i else o
        axv.bar(i, v, width=0.7, color=UP if c >= prev else DOWN, alpha=0.55)

    for n, colr in MA_COLORS.items():
        ax.plot(xs, mas[n][start:], color=colr, lw=1.6 if n == 20 else 1.0, label=f"MA{n}")

    last = view[-1][4]
    ax.axhline(last, color="#18211d", lw=0.6, ls=":")
    ax.annotate(f"{last:,.0f}", xy=(len(view) - 1, last), xytext=(6, 0), textcoords="offset points",
                va="center", fontsize=9, fontweight="bold")

    ax.set_title(f"{code}  Daily  {view[-1][0][:4]}-{view[-1][0][4:6]}-{view[-1][0][6:]}  {subtitle}",
                 fontsize=10, loc="left")
    ax.legend(ncol=7, fontsize=7, loc="upper left", frameon=False)
    ax.grid(alpha=0.15)
    axv.grid(alpha=0.15)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K"))
    ticks = list(range(0, len(view), 20))
    axv.set_xticks(ticks)
    axv.set_xticklabels([f"{view[t][0][4:6]}/{view[t][0][6:]}" for t in ticks], fontsize=8)
    ax.set_xlim(-1, len(view) + 3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
