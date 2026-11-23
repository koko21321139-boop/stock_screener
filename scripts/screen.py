#!/usr/bin/env python3
"""ミネルヴィニのトレンドテンプレート＋CAN-SLIM で日本株をスクリーニングする。

データ（すべて無料）
  銘柄一覧 … JPX「東証上場銘柄一覧」(Excel、月 1 回更新)
  株価・決算 … Yahoo Finance（yfinance。非公式のため仕様変更で止まることがある）

環境変数
  UNIVERSE      prime（既定：プライム全体）/ topix500 / all（プライム・スタンダード・グロース）
  MIN_TURNOVER  20 日平均売買代金の下限（円。既定 1 億円）
  FUND_DAYS     決算データを取り直す間隔（日。既定 7）
"""
import datetime as dt, io, json, math, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JST = dt.timezone(dt.timedelta(hours=9))
UNIVERSE = os.environ.get("UNIVERSE") or "prime"
MIN_TURNOVER = float(os.environ.get("MIN_TURNOVER") or 1e8)
FUND_DAYS = int(os.environ.get("FUND_DAYS") or 7)
MARKET_ETF = "1306.T"   # TOPIX 連動 ETF（地合いの判定に使う）

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_XLS = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

TT_LABELS = [
    "株価 > 150日線・200日線", "150日線 > 200日線", "200日線が1か月以上上向き", "50日線 > 150日線・200日線",
    "株価 > 50日線", "52週安値から +30% 以上", "52週高値から -25% 以内", "RS 70 以上",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", "utf-8")


def r(x, n=1):
    """JSON 用に丸める（NaN は None）。"""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), n)


# ---------- 銘柄一覧 ----------
def fetch_listing():
    """JPX の Excel を読み、コード・銘柄名・市場・業種・規模区分の表を返す。"""
    url = JPX_XLS
    try:   # 掲載ページから最新の Excel の場所を探す（ファイル名が変わっても動くように）
        html = urllib.request.urlopen(JPX_PAGE, timeout=30).read().decode("utf-8", "replace")
        m = re.search(r'href="([^"]+data_j\.xlsx?)"', html)
        if m:
            url = urllib.parse.urljoin(JPX_PAGE, m.group(1))
    except Exception as e:
        log("  掲載ページを読めませんでした", e)
    raw = urllib.request.urlopen(url, timeout=60).read()
    df = pd.read_excel(io.BytesIO(raw), dtype=str)
    col = lambda key: next(c for c in df.columns if key in c)
    out = pd.DataFrame({
        "code": df[col("コード")].str.strip(),
        "name": df[col("銘柄名")].str.strip(),
        "market": df[col("市場")].str.strip(),
        "sector": df[col("33業種区分")].str.strip(),
        "scale": df[col("規模区分")].fillna("").str.strip(),
    })
    return out


def universe():
    path = DATA / "universe.json"
    u = load(path, {})
    today = dt.datetime.now(JST).date()
    if u.get("updated", "") >= (today - dt.timedelta(days=6)).isoformat() and u.get("stocks"):
        return u
    log("銘柄一覧を更新します")
    try:
        df = fetch_listing()
    except Exception as e:
        log("  銘柄一覧を取得できませんでした（前回分を使います）", e)
        if u.get("stocks"):
            return u
        raise
    df = df[df["market"].str.contains("内国株式", na=False)]
    stocks = {}
    for row in df.itertuples():
        mk = "プライム" if "プライム" in row.market else "スタンダード" if "スタンダード" in row.market else "グロース" if "グロース" in row.market else None
        if not mk:
            continue
        scale = row.scale.replace("TOPIX ", "") if row.scale.startswith("TOPIX") else ""
        stocks[row.code] = {"name": row.name, "sector": row.sector, "market": mk, "scale": scale}
    if len(stocks) < 1000:
        log(f"  {len(stocks)} 社しか取れなかったため前回分を使います")
        return u if u.get("stocks") else {"stocks": stocks}
    u = {"updated": today.isoformat(), "stocks": stocks}
    save(path, u)
    log(f"  {len(stocks)} 社")
    return u


def pick(stocks):
    if UNIVERSE == "topix500":
        return [c for c, s in stocks.items() if s["scale"] in ("Core30", "Large70", "Mid400")]
    if UNIVERSE == "all":
        return list(stocks)
    return [c for c, s in stocks.items() if s["market"] == "プライム"]


# ---------- 株価（yfinance） ----------
def fetch_prices(tickers, period="2y"):
    """{ticker: DataFrame(Open, High, Low, Close, Volume)} を返す。取れなかった銘柄は入らない。"""
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1d", group_by="ticker", auto_adjust=True,
                                 threads=True, progress=False)
                break
            except Exception as e:
                log(f"  株価の取得に失敗（{attempt + 1}回目）", e)
                time.sleep(20)
        else:
            continue
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if len(sub) >= 60:
                out[t] = sub
        log(f"  株価 {min(i + 200, len(tickers))}/{len(tickers)}")
        time.sleep(2)
    return out


def fetch_fundamentals(ticker):
    """CAN-SLIM の C・A・I に使う決算データ。取れない項目は None。"""
    import yfinance as yf
    t = yf.Ticker(ticker)
    f = {"fetched": dt.date.today().isoformat()}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    f["qEpsYoY"] = pct_or_none(info.get("earningsQuarterlyGrowth"))
    f["qRevYoY"] = pct_or_none(info.get("revenueGrowth"))
    f["inst"] = pct_or_none(info.get("heldPercentInstitutions"))
    f["roe"] = pct_or_none(info.get("returnOnEquity"))
    f["epsAnnual"] = []
    try:
        inc = t.income_stmt
        row = next((k for k in ("Diluted EPS", "Basic EPS") if k in inc.index), None)
        if row:
            s = inc.loc[row].dropna().sort_index()
            f["epsAnnual"] = [[str(k)[:7], r(v, 2)] for k, v in s.items()][-4:]
    except Exception:
        pass
    try:   # 四半期 EPS：5 期あれば前年同期比を自前で計算（info の値より新しいことがある）
        q = t.quarterly_income_stmt
        row = next((k for k in ("Diluted EPS", "Basic EPS") if k in q.index), None)
        if row:
            s = q.loc[row].dropna().sort_index()
            f["epsQuarter"] = [[str(k)[:7], r(v, 2)] for k, v in s.items()][-6:]
            if len(s) >= 5 and s.iloc[-5] > 0:
                f["qEpsYoY"] = r((s.iloc[-1] / s.iloc[-5] - 1) * 100)
    except Exception:
        pass
    return f


def pct_or_none(v):
    try:
        return r(float(v) * 100) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------- 判定 ----------
def rs_raw(close):
    """IBD 流の相対力：直近 3 か月を 2 倍に重み付けした 1 年の騰落率。"""
    c = close.values
    if len(c) < 253:
        return None
    q = lambda n: c[-1] / c[-1 - n] - 1
    return 2 * q(63) + q(126) + q(189) + q(252)


def contractions(high, low, n=3, w=20):
    """直近 n×w 日を w 日ずつに区切った値幅（%）。VCP の「収縮」を見る。"""
    out = []
    for k in range(n, 0, -1):
        h = high.iloc[-k * w: len(high) - (k - 1) * w if k > 1 else None]
        l = low.iloc[-k * w: len(low) - (k - 1) * w if k > 1 else None]
        out.append((h.max() - l.min()) / h.max() * 100)
    return out


def period_returns(c):
    """1日・1週・1か月・3か月・年初来の騰落率（%）。"""
    out = {}
    for k, n in (("d1", 1), ("w1", 5), ("m1", 21), ("m3", 63)):
        out[k] = (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n else None
    prev = c[c.index < pd.Timestamp(c.index[-1].year, 1, 1)]
    out["ytd"] = (c.iloc[-1] / prev.iloc[-1] - 1) * 100 if len(prev) else None
    return out


def sector_table(evals, stocks):
    """東証33業種ごとに、構成銘柄の騰落率の中央値・通過数・50日線より上の割合・RS中央値を出す。
    業種別の ETF が無い業種も多いので、銘柄の中央値で業種の強さを見る（売買代金の少ない銘柄は除外済み）。"""
    groups = {}
    for code, e in evals.items():
        groups.setdefault(stocks[code]["sector"] or "不明", []).append(e)
    med = lambda xs: (sorted(xs)[len(xs) // 2] if xs else None)
    out = []
    for name, es in groups.items():
        row = {"name": name, "n": len(es), "pass": sum(sum(bool(x) for x in e["tt"]) == 8 for e in es),
               "pct50": r(sum(bool(e["tt"][4]) for e in es) / len(es) * 100, 0),
               "rsMed": med([e["rs"] for e in es if e.get("rs") is not None])}
        for k in ("d1", "w1", "m1", "m3", "ytd"):
            row[k] = r(med([e["rets"][k] for e in es if e["rets"].get(k) is not None]), 2)
        out.append(row)
    ranked = sorted([x for x in out if x.get("m3") is not None and x["n"] >= 3], key=lambda x: -x["m3"])
    for k, x in enumerate(ranked, 1):
        x["rank"] = k
    return out


def evaluate(df):
    """1 銘柄の指標。RS の順位づけ前の値を返す。"""
    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    if len(c) < 200:
        return None
    ma50, ma150, ma200 = (c.rolling(n).mean() for n in (50, 150, 200))
    last = c.iloc[-1]
    hi52, lo52 = h.iloc[-252:].max(), l.iloc[-252:].min()
    m200_then = ma200.iloc[-22] if len(ma200.dropna()) > 22 else np.nan
    tt = [
        bool(last > ma150.iloc[-1] and last > ma200.iloc[-1]),
        bool(ma150.iloc[-1] > ma200.iloc[-1]),
        bool(ma200.iloc[-1] > m200_then),
        bool(ma50.iloc[-1] > ma150.iloc[-1] and ma50.iloc[-1] > ma200.iloc[-1]),
        bool(last > ma50.iloc[-1]),
        bool(last >= lo52 * 1.30),
        bool(last >= hi52 * 0.75),
        None,   # RS は全銘柄がそろってから
    ]
    vol50 = v.iloc[-50:].mean()
    vol10 = v.iloc[-10:].mean()
    turnover = (c.iloc[-20:] * v.iloc[-20:]).mean()
    con = contractions(h, l)
    pivot = h.iloc[-21:-1].max()     # 直近 20 日（当日を除く）の高値
    return {
        "close": last, "chg": (last / c.iloc[-2] - 1) * 100 if len(c) > 1 else None,
        "rets": period_returns(c),
        "tt": tt, "rsRaw": rs_raw(c),
        "fromHigh": (last / hi52 - 1) * 100, "fromLow": (last / lo52 - 1) * 100,
        "volRatio": v.iloc[-1] / vol50 if vol50 else None, "dryUp": vol10 / vol50 if vol50 else None,
        "turnover": turnover, "con": con,
        # VCP 候補：値幅が段階的に縮み、直近は 10% 以内、出来高も細っている
        "vcp": bool(con[0] > con[1] > con[2] and con[2] <= 10 and vol50 and vol10 / vol50 < 0.8),
        "breakout": bool(last > pivot and vol50 and v.iloc[-1] >= 1.5 * vol50),
        "pivot": pivot, "toPivot": (pivot / last - 1) * 100,
        "newHigh": bool(h.iloc[-1] >= hi52),
        "ma": (ma50.iloc[-1], ma150.iloc[-1], ma200.iloc[-1]),
        "_series": (c, ma50, ma150, ma200),
    }


def market_state(df):
    """地合い（CAN-SLIM の M）：TOPIX ETF の移動平均と売り抜け日。"""
    if df is None or len(df) < 200:
        return {"state": "不明", "note": "TOPIX ETF のデータが取れませんでした"}
    c, v = df["Close"], df["Volume"].fillna(0)
    ma50, ma200 = c.rolling(50).mean(), c.rolling(200).mean()
    ret = c.pct_change()
    dist = int(((ret.iloc[-25:] <= -0.002) & (v.iloc[-25:] > v.shift(1).iloc[-25:])).sum())
    above50, above200, up = c.iloc[-1] > ma50.iloc[-1], c.iloc[-1] > ma200.iloc[-1], ma50.iloc[-1] > ma200.iloc[-1]
    if above50 and above200 and up and dist <= 4:
        state = "上昇トレンド"
    elif above200 and dist <= 6:
        state = "注意（調整局面）"
    else:
        state = "下降・弱い地合い"
    return {"state": state, "close": r(c.iloc[-1], 0), "above50": bool(above50), "above200": bool(above200),
            "ma50over200": bool(up), "distDays": dist, "chg": r((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
            "spark": spark(c.iloc[-120:])}


def spark(series):
    s = series.dropna()
    if s.empty:
        return []
    base = s.iloc[0]
    return [r((x / base - 1) * 100, 1) for x in s.values]


def canslim(e, f, rs):
    """満たした文字を返す（判定できない文字は None）。基準は O'Neil の目安。"""
    out = {}
    out["C"] = None if f.get("qEpsYoY") is None else f["qEpsYoY"] >= 25
    eps = [v for _, v in f.get("epsAnnual") or [] if v is not None][-3:]
    if len(eps) < 3:
        out["A"] = None
    elif all(x > 0 for x in eps):   # 直近 3 期とも黒字のときだけ伸び率を計算（赤字をはさむと率が意味をなさない）
        g = [(eps[i] / eps[i - 1] - 1) * 100 for i in (1, 2)]
        out["A"] = all(x >= 20 for x in g) and (eps[2] / eps[0]) ** 0.5 - 1 >= 0.20
    else:
        out["A"] = False
    out["N"] = e["fromHigh"] >= -5
    out["S"] = bool(e["breakout"] or (e["volRatio"] or 0) >= 1.4)
    out["L"] = rs is not None and rs >= 80
    out["I"] = None if f.get("inst") is None else f["inst"] >= 10
    return {k: (None if v is None else bool(v)) for k, v in out.items()}


# ---------- メイン ----------
def run(prices=None, fundamentals_fn=None):
    u = universe()
    codes = pick(u["stocks"])
    tickers = [f"{c}.T" for c in codes]
    log(f"対象 {len(codes)} 銘柄（{UNIVERSE}）")
    if prices is None:
        prices = fetch_prices(tickers + [MARKET_ETF])
    fundamentals_fn = fundamentals_fn or fetch_fundamentals

    evals = {}
    for c in codes:
        df = prices.get(f"{c}.T")
        if df is None:
            continue
        try:
            e = evaluate(df)
        except Exception as ex:
            log(f"  {c} 計算できず", ex)
            continue
        if e and e["turnover"] >= MIN_TURNOVER:
            evals[c] = e
    if len(evals) < 50:
        sys.exit(f"有効な銘柄が {len(evals)} しかありません。株価の取得に失敗した可能性があります")
    asof = max(prices[f"{c}.T"].index[-1] for c in evals)
    asof = pd.Timestamp(asof).date().isoformat()

    # RS 順位（1〜99）
    raw = pd.Series({c: e["rsRaw"] for c, e in evals.items() if e["rsRaw"] is not None})
    rank = (raw.rank(pct=True) * 98 + 1).round().astype(int)
    for c, e in evals.items():
        e["rs"] = int(rank[c]) if c in rank else None
        e["tt"][7] = e["rs"] is not None and e["rs"] >= 70

    # 決算データ（トレンドテンプレートを 7 条件以上満たす銘柄だけ。キャッシュあり）
    fpath = DATA / "fundamentals.json"
    fund = load(fpath, {})
    stale = (dt.date.today() - dt.timedelta(days=FUND_DAYS)).isoformat()
    need = [c for c, e in evals.items() if sum(bool(x) for x in e["tt"]) >= 7
            and fund.get(c, {}).get("fetched", "") < stale]
    log(f"決算データを取得 {len(need)} 銘柄")
    for i, c in enumerate(need):
        try:
            fund[c] = fundamentals_fn(f"{c}.T")
        except Exception as ex:
            log(f"  {c} 決算データ取得失敗", ex)
        if i % 20 == 19:
            save(fpath, fund)
        time.sleep(0.6 if fundamentals_fn is fetch_fundamentals else 0)
    save(fpath, fund)

    prev = load(DATA / "screen.json", {})
    prev_pass = {i["code"] for i in prev.get("items", []) if i.get("ttPass") == 8} if prev.get("asof") != asof \
        else set(prev.get("prevPass", []))

    items = []
    for c, e in evals.items():
        s = u["stocks"][c]
        n = sum(bool(x) for x in e["tt"])
        f = fund.get(c, {}) if n >= 7 else {}
        it = {"code": c, "name": s["name"], "sector": s["sector"], "market": s["market"], "scale": s["scale"],
              "close": r(e["close"], 1), "chg": r(e["chg"], 2), "rs": e["rs"], "tt": e["tt"], "ttPass": n,
              "fromHigh": r(e["fromHigh"]), "fromLow": r(e["fromLow"]), "volRatio": r(e["volRatio"], 2),
              "dryUp": r(e["dryUp"], 2), "turnover": r(e["turnover"] / 1e8, 1),
              "vcp": e["vcp"], "breakout": e["breakout"], "newHigh": e["newHigh"],
              "pivot": r(e["pivot"], 1), "toPivot": r(e["toPivot"], 1), "con": [r(x) for x in e["con"]]}
        if n >= 7:
            cl, m50, m150, m200 = e["_series"]
            base = cl.iloc[-120]
            it["spark"] = {k: [r((x / base - 1) * 100) for x in sr.iloc[-120:].values]
                           for k, sr in (("c", cl), ("m50", m50), ("m150", m150), ("m200", m200))}
            it["fund"] = {k: f.get(k) for k in ("qEpsYoY", "qRevYoY", "inst", "roe", "epsAnnual", "epsQuarter")}
            try:
                it["cs"] = canslim(e, f, e["rs"])
            except Exception as ex:   # 決算データの形が想定外でも全体は止めない
                log(f"  {c} CAN-SLIM 判定できず", ex)
                it["cs"] = {}
            it["new"] = n == 8 and c not in prev_pass
        items.append(it)
    items.sort(key=lambda i: (-i["ttPass"], -(i["rs"] or 0)))

    out = {"asof": asof, "updated": dt.datetime.now(JST).isoformat(timespec="minutes"), "universe": UNIVERSE,
           "count": len(items), "minTurnover": MIN_TURNOVER, "ttLabels": TT_LABELS,
           "market": market_state(prices.get(MARKET_ETF)), "sectors": sector_table(evals, u["stocks"]), "prevPass": sorted(prev_pass), "items": items}
    save(DATA / "screen.json", out)

    # 通過銘柄の履歴（日付ごと。何日連続で通過しているかを見るため）
    hist = load(DATA / "history.json", {})
    hist[asof] = [i["code"] for i in items if i["ttPass"] == 8]
    hist = dict(sorted(hist.items())[-120:])
    save(DATA / "history.json", hist)
    log(f"{asof}：トレンドテンプレート通過 {len(hist[asof])} 銘柄 / {len(items)}（地合い：{out['market']['state']}）")


if __name__ == "__main__":
    run()
