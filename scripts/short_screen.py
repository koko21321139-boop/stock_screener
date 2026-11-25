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
import time
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
    sector_weak_ratio: float = 0.4  # 業種内でステージ4の銘柄がこの割合以上なら「弱い業種」
    sector_min_count: int = 5       # 業種判定に必要な最低銘柄数
    ud_window: int = 50             # 上げ日／下げ日の出来高を比べる日数
    earnings_warn_days: int = 14    # 決算発表まで何日以内なら警告
    exdiv_warn_days: int = 30       # 権利落ち日まで何日以内なら警告
    ftd_min_gain: float = 0.012     # フォロースルーデーとみなす指数の上昇率（+1.2%以上）
    # --- ステージ3→4移行（早めの売り場） ---
    tr_dd_min: float = 0.10         # 高値からの下落がこの範囲（10〜25%）
    tr_dd_max: float = 0.25
    tr_high_within: int = 189       # 52週高値がこの営業日以内（約9か月）＝天井が最近
    tr_cross_window: int = 15       # 150日線割れが直近何営業日以内か
    tr_cross_vol: float = 1.4       # 150日線割れ前後の出来高が50日平均の何倍以上か
    tr_rs_drop: float = 20          # RSが20営業日前から何ポイント以上低下したか


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
    "c11": "業種の40%以上がステージ4（弱い業種）",
    "c12": "下げ日の出来高が上げ日より多い（売り抜け）",
}

TR_CONDITIONS = {
    "t1": "天井前に1.5倍以上上昇（旧主導株）",
    "t2": "高値から10〜25%下落、高値は9か月以内",
    "t3": "上昇していた150日線が横ばい〜下向きに転換",
    "t4": "直近15日以内に大商いで150日線割れ、今も下",
    "t5": "50日線が下向きに転換",
    "t6": "RSが20日前から20ポイント以上低下",
    "t7": "売買代金が十分（踏み上げ回避）",
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
        df = None
        for attempt in range(3):  # 取得制限などの一時的なエラーは待ってやり直す
            try:
                df = yf.download(chunk, period="2y", auto_adjust=True, progress=False,
                                 group_by="ticker", threads=True)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                print("download error:", e)
            time.sleep(30 * (attempt + 1))
        if df is None or df.empty:
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
    alerts = cover_alerts(df, ma50, p)
    return {"label": label, "below_50ma": below50, "below_200ma": below200,
            "ma200_falling": falling200, "ok_for_short": below200 and falling200,
            "cover_alert": bool(alerts), "cover_reasons": alerts}


def cover_alerts(df: pd.DataFrame, ma50: pd.Series, p: ShortParams) -> list[str]:
    """ショートの買い戻しを検討すべき地合いの反転サイン。"""
    close, vol = df["Close"], df["Volume"]
    out = []

    # ① TOPIXが直近5営業日以内に50日線を下から上に回復
    above = close > ma50
    recovered = above & ~above.shift(1, fill_value=True)
    if recovered.iloc[-5:].any() and above.iloc[-1]:
        d = recovered.iloc[-5:][recovered.iloc[-5:]].index[-1]
        out.append(f"TOPIXが50日線を回復（{d:%m/%d}）")

    # ② フォロースルーデー（オニール）：直近25日の安値から4日目以降に、
    #    指数が+1.2%以上上昇し、出来高が前日を上回った日。安値を割り込んでいないこと。
    win = close.iloc[-25:]
    low_i = int(win.values.argmin())
    low_pos = len(close) - 25 + low_i
    low_v = float(close.iloc[low_pos])
    was_correction = bool(close.iloc[low_pos] < ma50.iloc[low_pos])
    if was_correction and float(close.iloc[low_pos:].min()) >= low_v:
        chg = close.pct_change()
        for k in range(low_pos + 4, len(close)):
            if chg.iloc[k] >= p.ftd_min_gain and vol.iloc[k] > vol.iloc[k - 1]:
                if len(close) - 1 - k <= 10:
                    out.append(f"フォロースルーデー（{close.index[k]:%m/%d} +{chg.iloc[k] * 100:.1f}%）")
                break
    return out


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

    chg = close.diff().iloc[-p.ud_window:]
    v = vol.iloc[-p.ud_window:]
    up_vol, down_vol = float(v[chg > 0].sum()), float(v[chg < 0].sum())
    ud = up_vol / down_vol if down_vol > 0 else np.nan

    lb = p.slope_lookback
    # --- ステージ3→4移行の判定 ---
    m150 = float(ma150.iloc[-1])
    dd = 1 - c / high52
    high_recent = (len(close) - 1 - high_pos) <= p.tr_high_within
    ma150_flat_down = m150 <= float(ma150.iloc[-1 - lb]) * 1.005
    ma150_was_rising = float(ma150.iloc[-60]) > float(ma150.iloc[-120])
    below150 = close < ma150
    cross150 = below150 & ~below150.shift(1, fill_value=True)
    heavy150 = vol >= vol50 * p.tr_cross_vol
    near_heavy = heavy150 | heavy150.shift(1, fill_value=False) | heavy150.shift(-1, fill_value=False)
    tr_cross = bool((cross150 & near_heavy).iloc[-p.tr_cross_window:].any()) and bool(below150.iloc[-1])
    ma50_down = m50 < float(ma50.iloc[-11])

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
        "c12": bool(ud < 1) if ud == ud else False,
        "ud_ratio": round(ud, 2) if ud == ud else None,
        "stage4": bool(c < ma150.iloc[-1] and ma150.iloc[-1] < ma150.iloc[-1 - lb]),
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
        # ステージ3→4移行
        "t1": bool(high52 >= low_before_high * p.leader_runup),
        "t2": bool(p.tr_dd_min <= dd <= p.tr_dd_max and high_recent),
        "t3": bool(ma150_flat_down and ma150_was_rising),
        "t4": tr_cross,
        "t5": bool(ma50_down),
        "t7": bool(turnover20 >= p.min_turnover_jpy),
        "ma150": round(m150, 1),
        "dist_150ma_pct": round((c / m150 - 1) * 100, 1),
        # 移行時の売り場：150日線への戻り（割れた線が上値抵抗になる）
        "tr_entry_lo": round(m150 * 0.97, 1),
        "tr_entry_hi": round(m150, 1),
        "tr_stop": round(max(m150 * 1.04, float(close.iloc[-20:].max()) * 1.01), 1),
        "tr_target": round(m150 * 0.8, 1),
        "_wr_prev": weighted_return(close.iloc[:-20]),
        "_wr": weighted_return(close),
    }


def fetch_events(code: str, p: ShortParams) -> dict:
    """yfinance から次回の決算発表日と権利落ち日を取る（日本株は欠けることがある）。"""
    today = datetime.now(JST).date()
    out = {"earnings_date": None, "earnings_soon": False, "exdiv_date": None, "exdiv_soon": False}
    try:
        cal = yf.Ticker(f"{code}.T").calendar or {}
    except Exception:
        return out
    ed = cal.get("Earnings Date")
    ed = ed[0] if isinstance(ed, (list, tuple)) and ed else ed
    if hasattr(ed, "year"):
        ed = pd.Timestamp(ed).date()
        if ed >= today:
            out["earnings_date"] = ed.isoformat()
            out["earnings_soon"] = (ed - today).days <= p.earnings_warn_days
    xd = cal.get("Ex-Dividend Date")
    if hasattr(xd, "year"):
        xd = pd.Timestamp(xd).date()
        if xd >= today:
            out["exdiv_date"] = xd.isoformat()
            out["exdiv_soon"] = (xd - today).days <= p.exdiv_warn_days
    time.sleep(0.2)
    return out


def main():
    p = ShortParams()
    uni = load_universe()
    codes = sorted(uni)
    print(f"universe: {len(codes)} 銘柄")

    mkt_df = fetch_prices(["1306"]).get("1306")
    market = market_state(mkt_df, p) if mkt_df is not None else {"label": "取得失敗"}

    prices = fetch_prices(codes)
    if len(prices) < len(codes) * 0.5:
        # 半分以上取れなかったときは、前回の結果を残したまま止める
        raise SystemExit(f"株価の取得に失敗しました（{len(prices)}/{len(codes)}銘柄）。"
                         "Yahoo Finance の一時的な制限の可能性があります。時間をおいて再実行してください。")
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
    res["rs_prev"] = (res["_wr_prev"].rank(pct=True) * 98 + 1).round(0)
    res["t6"] = (res["rs_prev"] - res["rs"]) >= p.tr_rs_drop

    # --- ① 業種ごとの弱さ（全銘柄で集計） ---
    g = res.groupby("sector")["stage4"].agg(["count", "mean"])
    g = g[g["count"] >= p.sector_min_count]
    res["sector_stage4_pct"] = res["sector"].map((g["mean"] * 100).round(0))
    res["c11"] = res["sector_stage4_pct"].fillna(0) >= p.sector_weak_ratio * 100
    sectors = [{"sector": k, "count": int(r["count"]), "stage4_pct": round(float(r["mean"]) * 100)}
               for k, r in g.sort_values("mean", ascending=False).iterrows()]

    cond = list(CONDITIONS)
    res["score"] = res[cond].sum(axis=1).astype(int)
    res["all_pass"] = res["score"] == len(cond)
    res = res.drop(columns=["_wr", "_wr_prev", "stage4"])

    # --- ステージ3→4移行の候補（ステージ4とは別リスト） ---
    tcond = list(TR_CONDITIONS)
    res["tr_score"] = res[tcond].sum(axis=1).astype(int)
    tr = res[res["tr_score"] >= len(tcond) - 1].copy()
    tr["tr_pass"] = tr["tr_score"] == len(tcond)
    tr = tr.sort_values(["tr_pass", "tr_score", "rs"], ascending=[False, False, True])
    tr_cols = ["code", "name", "sector", "close", "rs", "rs_prev", "drawdown_pct", "dist_150ma_pct",
               "ma150", "tr_entry_lo", "tr_entry_hi", "tr_stop", "tr_target", "breakdown",
               "turnover_oku", "sector_stage4_pct", "c11", "c12", "ud_ratio", "tr_score", "tr_pass"] + tcond
    tr = tr[tr_cols]
    tev = tr["code"].map(lambda c: fetch_events(c, p))
    for k in ["earnings_date", "earnings_soon", "exdiv_date", "exdiv_soon"]:
        tr[k] = tev.map(lambda e: e.get(k))

    res = res[res["score"] >= len(cond) - 3].sort_values(
        ["all_pass", "c11", "rally_zone", "score", "rs"],
        ascending=[False, False, False, False, True])

    # --- ③ 決算・権利落ちの警告（候補銘柄だけ取得） ---
    ev = res["code"].map(lambda c: fetch_events(c, p))
    for k in ["earnings_date", "earnings_soon", "exdiv_date", "exdiv_soon"]:
        res[k] = ev.map(lambda e: e.get(k))

    out = {
        "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "conditions": CONDITIONS,
        "params": asdict(p),
        "universe_size": len(prices),
        "sectors": sectors,
        "tr_conditions": TR_CONDITIONS,
        "transition": json.loads(tr.to_json(orient="records", force_ascii=False)),
        "stocks": json.loads(res.to_json(orient="records", force_ascii=False)),
    }
    (DATA / "short.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"全条件クリア {int(res['all_pass'].sum())} / 候補 {len(res)} / 3→4移行 {len(tr)}")
    print("地合い:", market)


if __name__ == "__main__":
    main()
