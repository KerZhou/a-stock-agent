"""
股票估值分析工具 — 美股 & A股，多数据源自动降级

数据源优先级:
  美股: yfinance (主) → fast_info (备用)
  A股:  akshare/东方财富 (主) → efinance (备用) → baostock (历史数据补充)

提供统一的 ValuationData 数据结构:
  - get_us_stock_valuation("INTC")
  - get_cn_stock_valuation("603011")
  - print_valuation(data)
  - compare_valuations(data_a, data_b)
"""

import os
import random
import sys
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")


@contextmanager
def _suppress_stdout():
    """临时抑制 stdout（用于静默 baostock login/logout 的打印输出）。"""
    devnull = open(os.devnull, "w")
    old = sys.stdout
    sys.stdout = devnull
    try:
        yield
    finally:
        sys.stdout = old
        devnull.close()

# 请求间隔配置 (秒) — 防止被限频
REQUEST_DELAY = (0.5, 2.0)  # 随机间隔范围
MAX_RETRIES = 3
RETRY_DELAY = 5  # 限频重试等待


def _delay():
    """随机等待，避免高频请求触发风控"""
    time.sleep(random.uniform(*REQUEST_DELAY))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ValuationData:
    """统一估值数据结构，美股 / A股 共用"""
    symbol: str
    name: str = ""
    market: str = ""  # "US" / "CN"
    currency: str = ""  # "$" / "¥"
    source: str = ""  # 实际使用的数据源

    # 行情
    price: Optional[float] = None
    market_cap: Optional[float] = None
    market_cap_display: str = ""

    # 估值指标
    pe_ttm: Optional[float] = None
    pe_forward: Optional[float] = None
    pb: Optional[float] = None
    ps: Optional[float] = None

    # 52 周区间
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    pct_in_52w: Optional[float] = None

    # 估值分位数 (近 N 年)
    hist_years: int = 5
    pe_pct_rank: Optional[float] = None
    pe_percentiles: Optional[dict] = None
    pe_comment: str = ""
    pb_pct_rank: Optional[float] = None
    pb_percentiles: Optional[dict] = None
    pb_comment: str = ""

    # 原始补充信息
    extra: dict = field(default_factory=dict)


def _pct_rank_comment(pct: float, pe_value: float = None) -> str:
    # PE 为负 = 亏损，不能算"低估"
    if pe_value is not None and pe_value < 0:
        return "⚠️ 亏损 (PE为负)"
    if pct <= 25:
        return "低估 (≤25%分位)"
    elif pct <= 50:
        return "合理偏低 (25-50%分位)"
    elif pct <= 75:
        return "合理偏高 (50-75%分位)"
    else:
        return "高估 (>75%分位)"


# ---------------------------------------------------------------------------
# 美股 — yfinance
# ---------------------------------------------------------------------------
def _safe_yf_info(ticker) -> dict:
    """
    带重试地获取 ticker.info；被限频则退回 fast_info。
    即使全部失败也返回空 dict，不抛异常。
    """
    import yfinance as yf

    info = {}
    # 1) 标准 info (带重试)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _delay()
            info = ticker.info
            if info is None:
                info = {}
            if info.get("currentPrice") or info.get("regularMarketPrice"):
                return info
        except yf.exceptions.YFRateLimitError:
            print(f"  [yfinance] 限频，第 {attempt}/{MAX_RETRIES} 次重试 "
                  f"(等待 {RETRY_DELAY}s)...")
            time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"  [yfinance] info 请求异常: {e}")
            break

    # 2) fast_info 兜底 (不走 quote API，不容易被限频)
    print("  [yfinance] info 不可用，尝试 fast_info...")
    try:
        fi = ticker.fast_info
        if fi.last_price is not None:
            info["currentPrice"] = fi.last_price
        if fi.fifty_two_week_high is not None:
            info["fiftyTwoWeekHigh"] = fi.fifty_two_week_high
        if fi.fifty_two_week_low is not None:
            info["fiftyTwoWeekLow"] = fi.fifty_two_week_low
        if fi.market_cap is not None:
            info["marketCap"] = fi.market_cap
        info.setdefault("trailingPE", getattr(fi, "trailing_pe", None))
    except Exception as e:
        print(f"  [yfinance] fast_info 也不可用: {e}")

    return info


def get_us_stock_valuation(symbol: str, years: int = 5) -> ValuationData:
    """
    获取美股估值数据 (yfinance)。

    Args:
        symbol: 美股代码，如 "INTC"、"AAPL"
        years:  历史分位数回看年数，默认 5
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    info = _safe_yf_info(ticker)

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    pe_ttm = info.get("trailingPE")
    pe_forward = info.get("forwardPE")
    pb = info.get("priceToBook")
    ps = info.get("priceToSalesTrailing12Months")
    market_cap = info.get("marketCap")
    high_52w = info.get("fiftyTwoWeekHigh")
    low_52w = info.get("fiftyTwoWeekLow")
    name = info.get("shortName", symbol)

    # 52 周区间位置
    pct_in_52w = None
    if price and high_52w and low_52w and high_52w != low_52w:
        pct_in_52w = (price - low_52w) / (high_52w - low_52w) * 100

    # 市值可读
    market_cap_display = ""
    if market_cap:
        market_cap_display = f"${market_cap / 1e9:.1f}B"

    # 如果 52 周数据缺失，从 history 补
    if (high_52w is None or low_52w is None) and price:
        try:
            _delay()
            h52 = ticker.history(period="1y")
            if not h52.empty:
                high_52w = high_52w or float(h52["High"].max())
                low_52w = low_52w or float(h52["Low"].min())
                if high_52w != low_52w:
                    pct_in_52w = (price - low_52w) / (high_52w - low_52w) * 100
        except Exception:
            pass

    # 历史价格百分位 (作为估值分位数参考)
    pe_pct_rank = None
    pe_percentiles = None
    pe_comment = ""
    if price:
        try:
            _delay()
            hist = ticker.history(period=f"{years}y")
            if not hist.empty:
                pe_pct_rank = float((hist["Close"] <= price).sum() / len(hist) * 100)
                p = np.percentile(hist["Close"], [10, 25, 50, 75, 90])
                pe_percentiles = {
                    "P10": p[0], "P25": p[1], "P50": p[2], "P75": p[3], "P90": p[4],
                    "min": float(hist["Close"].min()), "max": float(hist["Close"].max()),
                }
                pe_comment = _pct_rank_comment(pe_pct_rank, pe_value=pe_ttm)
        except Exception:
            pass

    # 历史 EPS
    extra = {}
    try:
        eps_df = ticker.earnings_history
        if eps_df is not None and not eps_df.empty:
            extra["earnings_history"] = eps_df.to_dict("records")
    except Exception:
        pass

    return ValuationData(
        symbol=symbol, name=name, market="US", currency="$",
        source="yfinance",
        price=price, market_cap=market_cap, market_cap_display=market_cap_display,
        pe_ttm=pe_ttm, pe_forward=pe_forward, pb=pb, ps=ps,
        high_52w=high_52w, low_52w=low_52w, pct_in_52w=pct_in_52w,
        hist_years=years,
        pe_pct_rank=pe_pct_rank, pe_percentiles=pe_percentiles, pe_comment=pe_comment,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# A股 — 多数据源降级
# ---------------------------------------------------------------------------
def _get_cn_realtime_tencent(symbol: str) -> Optional[dict]:
    """腾讯财经 qt.gtimg.cn 获取 A 股/ETF/指数实时行情（不封 IP，最稳定）。

    个股返回 PE/PB/总市值；ETF/指数本就无 PE（ vals[39] 为空），价格/市值完整。
    作为实时行情首选源，避开 akshare/efinance 被封 + baostock「只能取前一交易日
    收盘价」的双重问题（baostock 当日日线要到 17:00 后才更新）。
    """
    import urllib.request

    # 交易所前缀：6/9→沪市个股, 5→沪市 ETF/基金(51/56/58), 8/4→北交所, 其余(0/3/1)→深市
    c0 = symbol[0] if symbol else ""
    if c0 in ("6", "9", "5"):
        prefix = "sh"
    elif c0 in ("8", "4"):
        prefix = "bj"
    else:
        prefix = "sz"

    try:
        url = f"https://qt.gtimg.cn/q={prefix}{symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception:
        return None

    line = data.strip().split(";")[0].strip()
    if "=" not in line or '"' not in line:
        return None
    vals = line.split('"')[1].split("~")
    if len(vals) < 53:
        return None

    def _f(idx):
        try:
            v = vals[idx]
            return float(v) if v else None  # 空串 → None
        except (IndexError, ValueError):
            return None

    price = _f(3)
    if not price:        # 停牌/无效代码
        return None

    pe = _f(39)
    pb = _f(46)
    mcap_yi = _f(44)     # 总市值，单位「亿」
    turnover = _f(38)    # 换手率 %

    return {
        "price": price,
        "name": vals[1] if len(vals) > 1 and vals[1] else symbol,
        # 0/空视作缺失（腾讯对部分股票或 ETF 的 PE/PB 返回 0）→ 触发历史估值补全；
        # 保留负数（亏损股 PE 为负，需标注"⚠️ 亏损"）
        "pe_ttm": pe if pe else None,
        "pb": pb if pb else None,
        # 转成「元」与 akshare 对齐（market_cap_display = total_mv/1e8 亿）
        "total_mv": mcap_yi * 1e8 if mcap_yi else None,
        "circ_mv": None,
        "change_pct": _f(32),
        "turnover_pct": turnover if turnover else None,
        "volume": _f(36),
        "amount": _f(37),
        "source": "tencent",
    }


def _get_cn_realtime_akshare(symbol: str) -> Optional[dict]:
    """akshare 获取 A 股实时行情"""
    import akshare as ak

    _delay()
    realtime = ak.stock_zh_a_spot_em()
    rt = realtime[realtime["代码"] == symbol]
    if rt.empty:
        return None

    row = rt.iloc[0]
    return {
        "price": float(row["最新价"]) if pd.notna(row["最新价"]) else None,
        "name": str(row.get("名称", symbol)),
        "pe_ttm": float(row["市盈率-动态"]) if pd.notna(row.get("市盈率-动态")) else None,
        "pb": float(row["市净率"]) if pd.notna(row.get("市净率")) else None,
        "total_mv": float(row["总市值"]) if pd.notna(row.get("总市值")) else None,
        "circ_mv": float(row["流通市值"]) if pd.notna(row.get("流通市值")) else None,
        "change_pct": float(row["涨跌幅"]) if pd.notna(row.get("涨跌幅")) else None,
        "volume": row.get("成交量"),
        "amount": row.get("成交额"),
        "source": "akshare",
    }


def _get_cn_realtime_efinance(symbol: str) -> Optional[dict]:
    """efinance 获取 A 股实时行情 (备用)"""
    import efinance as ef

    _delay()
    market_map = ef.stock.get_realtime_quotes()
    if market_map is None or market_map.empty:
        return None

    rt = market_map[market_map["股票代码"] == symbol]
    if rt.empty:
        return None

    row = rt.iloc[0]
    return {
        "price": float(row.get("最新价", 0)) if pd.notna(row.get("最新价")) else None,
        "name": str(row.get("股票名称", symbol)),
        "pe_ttm": float(row.get("PE(动)", 0)) if pd.notna(row.get("PE(动)")) else None,
        "pb": float(row.get("PB", 0)) if pd.notna(row.get("PB")) else None,
        "total_mv": float(row.get("总市值", 0)) if pd.notna(row.get("总市值")) else None,
        "circ_mv": None,
        "change_pct": float(row.get("涨跌幅", 0)) if pd.notna(row.get("涨跌幅")) else None,
        "volume": row.get("成交量"),
        "amount": row.get("成交额"),
        "source": "efinance",
    }


def _get_cn_realtime_baostock(symbol: str) -> Optional[dict]:
    """
    baostock 获取 A 股最新行情 (最终兜底)。
    baostock 不提供实时行情和 PE/PB，取最近一个交易日收盘价。
    """
    import baostock as bs

    _delay()
    bs_prefix = _to_bs_symbol(symbol)
    with _suppress_stdout():
        lg = bs.login()
    try:
        # 取最近 5 个交易日的日线，取最后一条
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        start_date = (pd.Timestamp.now() - pd.DateOffset(days=14)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_prefix,
            "date,open,high,low,close,volume,amount",
            start_date=start_date, end_date=end_date, frequency="d",
        )
        if rs.error_code != "0":
            return None

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=rs.fields)
        last = df.iloc[-1]

        # 获取股票名称（baostock 不提供，用代码代替）
        name_result = bs.query_stock_basic(code=bs_prefix)
        stock_name = symbol
        while name_result.next():
            row_data = name_result.get_row_data()
            if row_data and len(row_data) > 1:
                stock_name = row_data[1]
                break

        return {
            "price": float(last["close"]),
            "name": stock_name,
            "pe_ttm": None,  # baostock 不提供
            "pb": None,
            "total_mv": None,
            "circ_mv": None,
            "change_pct": None,
            "volume": last.get("volume"),
            "amount": last.get("amount"),
            "source": "baostock",
        }
    finally:
        with _suppress_stdout():
            bs.logout()


def _to_bs_symbol(symbol: str) -> str:
    """6 位代码转 baostock 格式: 603011 → sh.603011, 000001 → sz.000001。

    沪市: 6/9 开头个股, 5 开头 ETF/基金(51/56/58)；
    深市: 0/3 开头个股, 1 开头 ETF(15/16)、可转债(12)。
    （曾把沪市 ETF 515880 错判成深市导致 baostock 查不到。）
    """
    if symbol and symbol[0] in ("6", "9", "5"):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _get_cn_realtime(symbol: str) -> dict:
    """
    获取 A 股实时行情，腾讯 → akshare → efinance → baostock 自动降级。

    腾讯不封 IP 且给当日实时价，置首；akshare/efinance 走东财(常被封)；
    baostock 为历史兜底（仅前一交易日收盘价，当日未更新）。
    """
    sources = [
        ("tencent", _get_cn_realtime_tencent),
        ("akshare", _get_cn_realtime_akshare),
        ("efinance", _get_cn_realtime_efinance),
        ("baostock", _get_cn_realtime_baostock),
    ]
    for _src_name, src_fn in sources:
        try:
            result = src_fn(symbol)
            if result and result.get("price"):
                return result
        except Exception:
            pass  # 降级：静默尝试下一个源（东财常被封，属预期）

    raise RuntimeError(f"所有 A 股数据源均无法获取 {symbol} 的实时行情")


def _get_cn_52w_akshare(symbol: str) -> Optional[tuple]:
    """akshare 获取 52 周最高/最低"""
    import akshare as ak

    _delay()
    end_date = pd.Timestamp.now().strftime("%Y%m%d")
    start_date = (pd.Timestamp.now() - pd.DateOffset(weeks=52)).strftime("%Y%m%d")
    hist = ak.stock_zh_a_hist(
        symbol=symbol, period="daily",
        start_date=start_date, end_date=end_date, adjust="qfq",
    )
    if hist.empty:
        return None
    return float(hist["最高"].max()), float(hist["最低"].min())


def _get_cn_52w_efinance(symbol: str) -> Optional[tuple]:
    """efinance 获取 52 周最高/最低 (备用)"""
    import efinance as ef

    _delay()
    end_date = pd.Timestamp.now().strftime("%Y%m%d")
    start_date = (pd.Timestamp.now() - pd.DateOffset(weeks=52)).strftime("%Y%m%d")
    df = ef.stock.get_quote_history(symbol, beg=start_date, end=end_date, klt=101, fqt=1)
    if df is None or df.empty:
        return None
    return float(df["最高"].max()), float(df["最低"].min())


def _get_cn_52w_baostock(symbol: str) -> Optional[tuple]:
    """baostock 获取 52 周最高/最低 (最终兜底)"""
    import baostock as bs

    _delay()
    bs_prefix = _to_bs_symbol(symbol)
    with _suppress_stdout():
        lg = bs.login()
    try:
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        start_date = (pd.Timestamp.now() - pd.DateOffset(weeks=52)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_prefix,
            "date,high,low",
            start_date=start_date, end_date=end_date, frequency="d",
            adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            return None
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df["high"] = pd.to_numeric(df["high"], errors="coerce")
        df["low"] = pd.to_numeric(df["low"], errors="coerce")
        return float(df["high"].max()), float(df["low"].min())
    except Exception:
        return None
    finally:
        with _suppress_stdout():
            bs.logout()


def _get_cn_52w(symbol: str) -> tuple:
    """获取 52 周最高/最低，akshare → efinance 降级"""
    for src_name, src_fn in [
        ("akshare", _get_cn_52w_akshare),
        ("efinance", _get_cn_52w_efinance),
        ("baostock", _get_cn_52w_baostock),
    ]:
        try:
            result = src_fn(symbol)
            if result:
                return result
        except Exception:
            pass  # 降级：静默
    return None, None


def _get_cn_valuation_history_akshare(symbol: str, years: int = 5) -> Optional[pd.DataFrame]:
    """
    akshare + 百度股市通 获取历史 PE/PB (最完整)。
    走百度 API，不受东方财富风控影响。
    """
    import akshare as ak

    period_map = {1: "近一年", 3: "近三年", 5: "近五年", 10: "近十年"}
    period = period_map.get(years, "近五年")

    pe_df = None
    pb_df = None

    # 获取历史 PE
    _delay()
    try:
        pe_df = ak.stock_zh_valuation_baidu(
            symbol=symbol, indicator="市盈率(TTM)", period=period
        )
    except Exception:
        pass  # 百度估值接口失败静默（ETF 等无历史估值数据）

    # 获取历史 PB
    _delay()
    try:
        pb_df = ak.stock_zh_valuation_baidu(
            symbol=symbol, indicator="市净率", period=period
        )
    except Exception:
        pass

    if pe_df is None and pb_df is None:
        return None

    # 合并 PE 和 PB
    result = pd.DataFrame()
    if pe_df is not None and not pe_df.empty:
        result = pe_df.rename(columns={"date": "trade_date", "value": "pe"})
    if pb_df is not None and not pb_df.empty:
        pb_renamed = pb_df.rename(columns={"date": "trade_date", "value": "pb"})
        if result.empty:
            result = pb_renamed
        else:
            result = result.merge(pb_renamed, on="trade_date", how="outer")

    for col in ["pe", "pb"]:
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors="coerce")

    return result


def _get_cn_valuation_history_baostock(symbol: str, years: int = 5) -> Optional[pd.DataFrame]:
    """
    baostock 获取历史估值数据 (兜底)。
    baostock 提供 peTTM/pbMRQ。
    """
    import baostock as bs

    _delay()
    bs_prefix = _to_bs_symbol(symbol)
    with _suppress_stdout():
        lg = bs.login()
    try:
        end_date = pd.Timestamp.now().strftime("%Y-%m-%d")
        start_date = (pd.Timestamp.now() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")

        rs = bs.query_history_k_data_plus(
            bs_prefix,
            "date,peTTM,pbMRQ",
            start_date=start_date, end_date=end_date, frequency="d",
        )
        if rs.error_code != "0":
            return None
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df = df.rename(columns={"date": "trade_date", "peTTM": "pe", "pbMRQ": "pb"})
        for col in ["pe", "pb"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["trade_date", "pe", "pb"]]
    except Exception:
        return None
    finally:
        with _suppress_stdout():
            bs.logout()


def _get_cn_valuation_history(symbol: str, years: int = 5) -> Optional[pd.DataFrame]:
    """获取 A 股历史 PE/PB 数据，akshare/百度 → baostock 降级"""
    for src_name, src_fn in [
        ("akshare/百度", _get_cn_valuation_history_akshare),
        ("baostock", _get_cn_valuation_history_baostock),
    ]:
        try:
            result = src_fn(symbol, years)
            if result is not None and not result.empty:
                return result
        except Exception:
            pass  # 降级：静默
    return None


def get_cn_stock_valuation(symbol: str, years: int = 5) -> ValuationData:
    """
    获取 A 股估值数据，多数据源自动降级。

    Args:
        symbol: 6 位股票代码，如 "603011"、"000001"
        years:  历史分位数回看年数，默认 5
    """
    # 1) 实时行情 (自动降级)
    rt = _get_cn_realtime(symbol)
    price = rt["price"]
    name = rt["name"]
    pe_ttm = rt.get("pe_ttm")
    pb = rt.get("pb")
    total_mv = rt.get("total_mv")
    circ_mv = rt.get("circ_mv")
    source = rt.get("source", "unknown")

    market_cap_display = ""
    if total_mv:
        market_cap_display = f"¥{total_mv / 1e8:.1f}亿"

    # 2) 52 周最高/最低 (自动降级)
    high_52w, low_52w = _get_cn_52w(symbol)
    pct_in_52w = None
    if price and high_52w and low_52w and high_52w != low_52w:
        pct_in_52w = (price - low_52w) / (high_52w - low_52w) * 100

    # 3) 历史估值分位数 (PE / PB) — akshare/百度 → baostock 降级
    pe_pct_rank = pb_pct_rank = None
    pe_percentiles = pb_percentiles = None
    pe_comment = pb_comment = ""

    try:
        val_period = _get_cn_valuation_history(symbol, years)
        if val_period is not None and not val_period.empty:
            # 如果实时行情没拿到 PE/PB，从历史数据末尾补上
            if pe_ttm is None and "pe" in val_period.columns:
                last_pe = val_period["pe"].dropna()
                if not last_pe.empty:
                    pe_ttm = float(last_pe.iloc[-1])
            if pb is None and "pb" in val_period.columns:
                last_pb = val_period["pb"].dropna()
                if not last_pb.empty:
                    pb = float(last_pb.iloc[-1])

            # PE 分位数
            if pe_ttm is not None and "pe" in val_period.columns:
                pe_hist = val_period["pe"].dropna()
                pe_hist = pe_hist[pe_hist > 0]
                if len(pe_hist) > 0:
                    pe_pct_rank = float((pe_hist <= pe_ttm).sum() / len(pe_hist) * 100)
                    p = np.percentile(pe_hist, [10, 25, 50, 75, 90])
                    pe_percentiles = {
                        "P10": p[0], "P25": p[1], "P50": p[2],
                        "P75": p[3], "P90": p[4],
                        "min": float(pe_hist.min()), "max": float(pe_hist.max()),
                    }
                    pe_comment = _pct_rank_comment(pe_pct_rank, pe_value=pe_ttm)

            # PB 分位数
            if pb is not None and "pb" in val_period.columns:
                pb_hist = val_period["pb"].dropna()
                pb_hist = pb_hist[pb_hist > 0]
                if len(pb_hist) > 0:
                    pb_pct_rank = float((pb_hist <= pb).sum() / len(pb_hist) * 100)
                    p = np.percentile(pb_hist, [10, 25, 50, 75, 90])
                    pb_percentiles = {
                        "P10": p[0], "P25": p[1], "P50": p[2],
                        "P75": p[3], "P90": p[4],
                        "min": float(pb_hist.min()), "max": float(pb_hist.max()),
                    }
                    pb_comment = _pct_rank_comment(pb_pct_rank)
    except Exception as e:
        print(f"  历史估值数据获取失败: {e}")

    return ValuationData(
        symbol=symbol, name=name, market="CN", currency="¥",
        source=source,
        price=price, market_cap=total_mv, market_cap_display=market_cap_display,
        pe_ttm=pe_ttm, pb=pb,
        high_52w=high_52w, low_52w=low_52w, pct_in_52w=pct_in_52w,
        hist_years=years,
        pe_pct_rank=pe_pct_rank, pe_percentiles=pe_percentiles, pe_comment=pe_comment,
        pb_pct_rank=pb_pct_rank, pb_percentiles=pb_percentiles, pb_comment=pb_comment,
        extra={
            "change_pct": rt.get("change_pct"),
            "turnover_pct": rt.get("turnover_pct"),
            "volume": rt.get("volume"),
            "amount": rt.get("amount"),
            "circ_market_cap": circ_mv,
        },
    )


# ---------------------------------------------------------------------------
# 打印 & 对比
# ---------------------------------------------------------------------------
def _fmt(val, unit="", precision=2):
    if val is None:
        return "N/A"
    return f"{unit}{val:.{precision}f}"


def print_valuation(data: ValuationData) -> None:
    """打印单只股票的估值报告"""
    tag = "美股" if data.market == "US" else "A股"
    print("=" * 60)
    print(f"【{tag}】{data.name} ({data.symbol})  [数据源: {data.source}]")
    print("=" * 60)
    print(f"  当前股价:     {_fmt(data.price, data.currency)}")
    print(f"  市值:         {data.market_cap_display or 'N/A'}")
    if data.pe_ttm is not None:
        print(f"  PE (TTM):     {data.pe_ttm:.2f}")
    if data.pe_forward is not None:
        print(f"  PE (Forward): {data.pe_forward:.2f}")
    if data.pb is not None:
        print(f"  PB:           {data.pb:.2f}")
    if data.ps is not None:
        print(f"  PS:           {data.ps:.2f}")
    if data.high_52w is not None:
        print(f"  52周最高:     {_fmt(data.high_52w, data.currency)}")
    if data.low_52w is not None:
        print(f"  52周最低:     {_fmt(data.low_52w, data.currency)}")
    if data.pct_in_52w is not None:
        print(f"  52周区间位置: {data.pct_in_52w:.1f}%")

    if data.pe_pct_rank is not None:
        p = data.pe_percentiles
        print(f"\n  --- 估值分位数 (PE, 近{data.hist_years}年) ---")
        print(f"  当前 PE:       {data.pe_ttm:.2f}")
        print(f"  PE 百分位:     {data.pe_pct_rank:.1f}%  → {data.pe_comment}")
        if p:
            print(f"  分布: P10={p['P10']:.2f}, P25={p['P25']:.2f}, "
                  f"P50={p['P50']:.2f}, P75={p['P75']:.2f}, P90={p['P90']:.2f}")
            print(f"        最低={p['min']:.2f}, 最高={p['max']:.2f}")

    if data.pb_pct_rank is not None:
        p = data.pb_percentiles
        print(f"\n  --- 估值分位数 (PB, 近{data.hist_years}年) ---")
        print(f"  当前 PB:       {data.pb:.2f}")
        print(f"  PB 百分位:     {data.pb_pct_rank:.1f}%  → {data.pb_comment}")
        if p:
            print(f"  分布: P10={p['P10']:.2f}, P25={p['P25']:.2f}, "
                  f"P50={p['P50']:.2f}, P75={p['P75']:.2f}, P90={p['P90']:.2f}")

    if data.market == "CN" and data.extra:
        ex = data.extra
        if ex.get("change_pct") is not None:
            print(f"\n  涨跌幅:       {ex['change_pct']:.2f}%")
        if ex.get("circ_market_cap"):
            print(f"  流通市值:     ¥{ex['circ_market_cap'] / 1e8:.1f}亿")


def compare_valuations(data_a: ValuationData, data_b: ValuationData) -> None:
    """打印两只股票的对比表"""
    print("\n" + "=" * 65)
    print("【对比汇总】")
    print("=" * 65)

    col_a = f"{data_a.symbol} ({data_a.market})"
    col_b = f"{data_b.symbol} ({data_b.market})"
    print(f"{'指标':<18} {col_a:<22} {col_b:<22}")
    print("-" * 62)

    rows = [
        ("当前股价",
         _fmt(data_a.price, data_a.currency),
         _fmt(data_b.price, data_b.currency)),
        ("市值",
         data_a.market_cap_display or "N/A",
         data_b.market_cap_display or "N/A"),
        ("PE",
         _fmt(data_a.pe_ttm, precision=2),
         _fmt(data_b.pe_ttm, precision=2)),
        ("PB",
         _fmt(data_a.pb, precision=2),
         _fmt(data_b.pb, precision=2)),
        ("52周最高",
         _fmt(data_a.high_52w, data_a.currency),
         _fmt(data_b.high_52w, data_b.currency)),
        ("52周最低",
         _fmt(data_a.low_52w, data_a.currency),
         _fmt(data_b.low_52w, data_b.currency)),
        ("52周位置%",
         _fmt(data_a.pct_in_52w, precision=1),
         _fmt(data_b.pct_in_52w, precision=1)),
        ("PE百分位%",
         _fmt(data_a.pe_pct_rank, precision=1),
         _fmt(data_b.pe_pct_rank, precision=1)),
        ("PE估值",
         data_a.pe_comment or "N/A",
         data_b.pe_comment or "N/A"),
        ("PB百分位%",
         _fmt(data_a.pb_pct_rank, precision=1),
         _fmt(data_b.pb_pct_rank, precision=1)),
        ("PB估值",
         data_a.pb_comment or "N/A",
         data_b.pb_comment or "N/A"),
    ]
    for label, va, vb in rows:
        print(f"{label:<18} {va:<22} {vb:<22}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    us = None
    try:
        us = get_us_stock_valuation("INTC")
        print_valuation(us)
    except Exception as e:
        print(f"美股 INTC 获取失败: {e}")

    print()

    cn = None
    try:
        cn = get_cn_stock_valuation("603011")
        print_valuation(cn)
    except Exception as e:
        print(f"A股 603011 获取失败: {e}")

    if us and cn:
        compare_valuations(us, cn)
    elif us or cn:
        print("\n(部分数据缺失，跳过对比)")
