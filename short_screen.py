"""
ショート版スクリーニング（Stage 4 テンプレート）

ワインスタインのステージ分析と、オニール／モラレス流の空売りルールに基づく。
既存の screen.py が作る data/universe.json を銘柄リストとして使い、
結果を data/short.json に保存する（short.html で表示）。

環境変数（既存アプリと同じ）:
    UNIVERSE      prime（既定） / topix500 / all
    MIN_TURNOVER  20日平均売買代金の下限（円）。ショートは踏み上げ回避のため既定5億円
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JST = timezone(timedelta(hours=9))


@dataclass
class ShortParams:
    ma_short: int = 50            # 50日線
    ma_mid: int = 150             # 150日線 ≒ 30週線（ワインスタインの基準線）
    ma_long: int = 200            # 200日線
    slope_lookback: int = 20      # 移動平均の向きを判定する日数（約1か月）
    min_drawdown: float = 0.30    # 52週高値から何%以上下げているか
    leader_runup: float = 1.5     # 天井前に安値から何倍上がっていたか（旧主導株）
    max_rs_rating: float = 30.0   # RSレーティング（1〜99）がこれ以下
    volume_break_mult: float = 1.5  # 50日線割れの日の出来高が50日平均の何倍以上か
    volume_break_window: int = 40   # 直近何営業日以内に大商いの50日線割れがあったか
    rally_zone: float = 0.05      # 50日線の下何%以内なら「戻り売りゾーン」
    min_turnover_jpy: float = float(os.environ.get("MIN_TURNOVER_SHORT", 5e8))
    market_ticker: str = "1306.T"  # 地合い判定（TOPIX連動ETF）


CONDITIONS = {
    "c1": "株価が150日線（30週線）の下",
    "c2": "150日線が下向き",
    "c3": "50日線＜150日線＜200日線",
    "c4": "200日線が下向き",
    "c5": "株価が50日線の下",
    "c6": "52週高値から30%以上下落",
    "c7": "天井前に1.5倍以上上昇（旧主導株）",
    "c8": "直近40日に大商いで50日線割れ",
    "c9": "売買代金が十分（踏み上げ回避）",
    "c10": "RSが30以下",
}


# ---------------------------------------------------------------------------
# 銘柄リスト
# ---------------------------------------------------------------------------
def load_universe() -> dict:
    uni = json.loads((DATA / "universe.json").read_text(encoding="utf-8"))["stocks"]
    mode = os.environ.get("UNIVERSE", "prime").lower()
    if mode == "all":
        return uni
    if mode == "topix500":
        keep = ("Core30", "Large70", "Mid400")
        return {k: v for k, v in uni.items() if v.get("scale", "").startswith(keep)}
    return {k: v for k, v in uni.items() if v.get("market") == "プライム"}


# ---------------------------------------------------------------------------
# 株価取得
# ---------------------------------------------------------------------------
def fetch_prices(codes: list[str], batch: int = 100) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    tickers = [f"{c}.T" for c in codes]
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            df = yf.download(chunk, period="2y", auto_adjust=True, progress=False,
                             group_by="ticker", threads=True)
        except Exception as e:  # yfinance の一時的なエラーは飛ばす
            print("download error:", e)
            continue
        for t in chunk:
            try:
                d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                d = d.dropna(subset=["Close"])
                if not d.empty:
                    out[t[:-2]] = d
            except KeyError:
                pass
    return out


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def weighted_return(close: pd.Series) -> float:
    """IBD風の加重リターン（直近3か月を2倍に重み付け）。"""
    if len(close) < 253:
        return np.nan
    c = close.iloc[-1]
    return (0.4 * (c / close.iloc[-63] - 1) + 0.2 * (c / close.iloc[-126] - 1)
            + 0.2 * (c / close.iloc[-189] - 1) + 0.2 * (c / close.iloc[-252] - 1))


def market_state(df: pd.DataFrame, p: ShortParams) -> dict:
    close = df["Close"]
    ma50 = close.rolling(p.ma_short).mean()
    ma200 = close.rolling(p.ma_long).mean()
    below200 = bool(close.iloc[-1] < ma200.iloc[-1])
    falling200 = bool(ma200.iloc[-1] < ma200.iloc[-1 - p.slope_lookback])
    below50 = bool(close.iloc[-1] < ma50.iloc[-1])
    if below200 and falling200:
        label = "下降トレンド（ショート向き）"
    elif below50 or below200:
        label = "調整中（ショートは選別して）"
    else:
        label = "上昇トレンド（ショートは控える）"
    return {"label": label, "below_50ma": below50, "below_200ma": below200,
            "ma200_falling": falling200, "ok_for_short": below200 and falling200}


def evaluate(df: pd.DataFrame, p: ShortParams) -> dict | None:
    if len(df) < max(p.ma_long + p.slope_lookback, 253):
        return None
    close, vol = df["Close"], df["Volume"]
    ma50 = close.rolling(p.ma_short).mean()
    ma150 = close.rolling(p.ma_mid).mean()
    ma200 = close.rolling(p.ma_long).mean()
    vol50 = vol.rolling(50).mean()

    c = float(close.iloc[-1])
    last252 = close.iloc[-252:]
    high52 = float(last252.max())
    high_pos = len(close) - 252 + int(last252.values.argmax())
    low_before_high = float(close.iloc[max(0, high_pos - 252): high_pos + 1].min())

    cross_down = (close < ma50) & (close.shift(1) >= ma50.shift(1))
    heavy = vol >= vol50 * p.volume_break_mult
    recent_break = bool((cross_down & heavy).iloc[-p.volume_break_window:].any())

    turnover20 = float((close * vol).iloc[-20:].mean())
    m50 = float(ma50.iloc[-1])
    low20 = float(close.iloc[-20:].min())
    dist50 = c / m50 - 1

    lb = p.slope_lookback
    return {
        "close": round(c, 1),
        "c1": bool(c < ma150.iloc[-1]),
        "c2": bool(ma150.iloc[-1] < ma150.iloc[-1 - lb]),
        "c3": bool(ma50.iloc[-1] < ma150.iloc[-1] < ma200.iloc[-1]),
        "c4": bool(ma200.iloc[-1] < ma200.iloc[-1 - lb]),
        "c5": bool(c < ma50.iloc[-1]),
        "c6": bool(c <= high52 * (1 - p.min_drawdown)),
        "c7": bool(high52 >= low_before_high * p.leader_runup),
        "c8": recent_break,
        "c9": bool(turnover20 >= p.min_turnover_jpy),
        "drawdown_pct": round((c / high52 - 1) * 100, 1),
        "dist_50ma_pct": round(dist50 * 100, 1),
        "turnover_oku": round(turnover20 / 1e8, 1),
        "rally_zone": bool(-p.rally_zone <= dist50 < 0),
        # --- エントリー目安（オニール／モラレス流の戻り売り） ---
        # 戻り売り：50日線の3%下〜50日線まで戻ったところで売る
        "ma50": round(m50, 1),
        "entry_lo": round(m50 * 0.97, 1),
        "entry_hi": round(m50, 1),
        # 下放れ売り：直近20日の安値を割ったら売る（戻りを待たない場合）
        "breakdown": round(low20 * 0.995, 1),
        # 損切り：50日線の4%上（50日線を明確に回復したら撤退）
        "stop": round(m50 * 1.04, 1),
        # 利確目安：売値から-20%
        "target": round(m50 * 0.8, 1),
        "_wr": weighted_return(close),
    }


def main():
    p = ShortParams()
    uni = load_universe()
    codes = sorted(uni)
    print(f"universe: {len(codes)} 銘柄")

    mkt_df = fetch_prices(["1306"]).get("1306")
    market = market_state(mkt_df, p) if mkt_df is not None else {"label": "取得失敗"}

    prices = fetch_prices(codes)
    rows = []
    for code, df in prices.items():
        r = evaluate(df, p)
        if r is None:
            continue
        r["code"] = code
        r["name"] = uni[code].get("name", "")
        r["sector"] = uni[code].get("sector", "")
        rows.append(r)

    res = pd.DataFrame(rows)
    res["rs"] = (res["_wr"].rank(pct=True) * 98 + 1).round(0)
    res["c10"] = res["rs"] <= p.max_rs_rating
    cond = list(CONDITIONS)
    res["score"] = res[cond].sum(axis=1).astype(int)
    res["all_pass"] = res["score"] == len(cond)
    res = res.drop(columns="_wr")
    res = res[res["score"] >= 8].sort_values(
        ["all_pass", "rally_zone", "score", "rs"], ascending=[False, False, False, True])

    out = {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "conditions": CONDITIONS,
        "params": asdict(p),
        "universe_size": len(prices),
        "stocks": json.loads(res.to_json(orient="records", force_ascii=False)),
    }
    (DATA / "short.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"全条件クリア {int(res['all_pass'].sum())} / 8条件以上 {len(res)}")


if __name__ == "__main__":
    main()
