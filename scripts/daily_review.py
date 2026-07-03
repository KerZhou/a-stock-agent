"""
每日复盘脚本 — 一键生成持仓+自选+市场信号的每日复盘报告。

数据源:
  大盘指数:     腾讯财经 qt.gtimg.cn（稳定，不封IP）
  北向资金:     同花顺 data.hexin.cn（稳定，零鉴权）
  热点题材:     同花顺 zx.10jqka.com.cn（稳定，零鉴权）
  行业涨跌:     东财 push2（可能被封，降级跳过）
  龙虎榜:       东财 datacenter（可能被封，降级跳过）
  持仓/自选:    复用 portfolio.py

用法:
  python scripts/daily_review.py                       # 全量复盘（默认）
  python scripts/daily_review.py --market CN           # 仅A股
  python scripts/daily_review.py --market US           # 仅美股
  python scripts/daily_review.py -o report.md          # 指定输出路径
  python scripts/daily_review.py --no-market           # 跳过市场信号，仅持仓+自选
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))
from pathlib import Path

import pandas as pd
import requests

# 把 scripts/ 加入 path 以便 import
sys.path.insert(0, str(Path(__file__).parent))
from portfolio import (
    analyze_position,
    analyze_watchlist_item,
    fmt_portfolio_report,
    fmt_watchlist_report,
    load_portfolio,
    load_watchlist,
)
from sector_rotation import run_rotation_check
from notify import send_notification
from llm_analysis import generate_ai_analysis

DATA_DIR = Path(__file__).parent.parent / "data"
REPORTS_DIR = Path(__file__).parent.parent / "reports"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"

# 报告时段：盘中(中午) vs 收盘(晚上)，用于区分文件名与标题
PERIOD_LABELS = {"noon": "盘中快报", "eod": "收盘复盘"}


def _infer_period() -> str:
    """按北京时间自动判断时段：<15 点为盘中(noon)，否则收盘(eod)。

    对应 cron：UTC04:00=北京12:00(盘中)，UTC07:30=北京15:30(收盘)。
    """
    return "noon" if datetime.now(BEIJING_TZ).hour < 15 else "eod"


# ---------------------------------------------------------------------------
# 东财防封基础设施
# ---------------------------------------------------------------------------
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers,
                              timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()


def _em_datacenter(report_name, columns="ALL", filter_str="",
                   page_size=50, sort_columns="", sort_types="-1"):
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = _em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ---------------------------------------------------------------------------
# 新闻与事件上下文 (东财 search-api-web + np-weblist，失败降级)
# ---------------------------------------------------------------------------
def _em_stock_news(code: str, page_size: int = 5) -> list[dict]:
    """东财个股新闻（JSONP）。返回 [{title, time, source}]"""
    cb = "jQuery_news"
    inner_params = json.dumps({
        "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                  "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}},
    }, separators=(',', ':'))
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    r = _em_get(url, params={"cb": cb, "param": inner_params},
                headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"}, timeout=12)
    text = r.text
    json_str = text[text.index("(") + 1: text.rindex(")")]
    d = json.loads(json_str)
    arts = d.get("result", {}).get("cmsArticleWebOld", []) or []
    return [{
        "title": re.sub(r'<[^>]+>', '', a.get("title", "")).strip(),
        "time": a.get("date", ""),
        "source": a.get("mediaName", ""),
    } for a in arts]


def _em_global_news(page_size: int = 15) -> list[dict]:
    """东财全球资讯 7x24 快讯。返回 [{title, summary, time}]"""
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    r = _em_get(url, params={
        "client": "web", "biz": "web_724", "fastColumn": "102",
        "sortEnd": "", "pageSize": str(page_size), "req_trace": str(uuid.uuid4()),
    }, headers={"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"}, timeout=12)
    d = r.json()
    items = d.get("data", {}).get("fastNewsList", []) or []
    return [{
        "title": item.get("title", ""),
        "summary": (item.get("summary", "") or "")[:120],
        "time": item.get("showTime", ""),
    } for item in items]


def get_news_context(per_stock: int = 3, market_n: int = 10) -> str:
    """抓持仓/自选个股新闻 + 市场快讯，返回给 AI 的新闻摘要。

    仅对 A 股 6 位代码抓个股新闻（美股/加密货币东财无）。任一源失败降级跳过，
    不影响主流程。
    """
    lines = []
    # 1. 持仓/自选个股新闻
    for csv_name in ["portfolio.csv", "watchlist.csv"]:
        path = DATA_DIR / csv_name
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, dtype=str, index_col=False)
        except Exception:
            continue
        for _, row in df.iterrows():
            code = str(row.get("symbol", ""))
            name = row.get("name", code)
            market = str(row.get("market", "CN")).upper()
            if market != "CN" or not re.match(r"^\d{6}$", code):
                continue
            try:
                news = _em_stock_news(code, per_stock)
            except Exception:
                news = []
            if news:
                lines.append(f"- **{name} ({code})**:")
                for n in news:
                    lines.append(f"  - {n['time']} {n['title']}")

    # 2. 市场快讯
    try:
        mkt = _em_global_news(market_n)
    except Exception:
        mkt = []
    if mkt:
        lines.append("")
        lines.append("**市场快讯**:")
        for n in mkt:
            lines.append(f"- {n['time']} {n['title']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. 大盘指数 (腾讯财经 — 稳定，不封IP)
# ---------------------------------------------------------------------------
def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """批量拉取腾讯财经实时行情（个股/指数/ETF 均可）"""
    prefixed = []
    for c in codes:
        # 已带交易所前缀（如 sh000001 指数）则原样使用，避免与个股混淆
        if c.startswith(("sh", "sz", "bj")):
            prefixed.append(c)
        elif c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":         vals[1],
            "price":        float(vals[3]) if vals[3] else 0,
            "last_close":   float(vals[4]) if vals[4] else 0,
            "change_amt":   float(vals[31]) if vals[31] else 0,
            "change_pct":   float(vals[32]) if vals[32] else 0,
            "high":         float(vals[33]) if vals[33] else 0,
            "low":          float(vals[34]) if vals[34] else 0,
            "amount_wan":   float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm":       float(vals[39]) if vals[39] else 0,
            "mcap_yi":      float(vals[44]) if vals[44] else 0,
            "pb":           float(vals[46]) if vals[46] else 0,
        }
    return result


def get_market_indices() -> dict[str, dict]:
    """获取主要A股指数行情"""
    # 指数代码必须显式指定交易所前缀：000001 既可能是「上证指数」也可能是
    # 「平安银行(深市个股)」，按首位数字猜前缀会抓错（曾把上证指数抓成平安银行）。
    codes = ["sh000001", "sz399001", "sz399006", "sh000300"]
    names = {
        "sh000001": "上证指数", "sz399001": "深证成指",
        "sz399006": "创业板指", "sh000300": "沪深300",
    }
    quotes = _tencent_quote(codes)
    result = {}
    for full in codes:
        code = full[2:]  # 去掉交易所前缀，与 _tencent_quote 返回的 key 一致
        if code in quotes:
            q = quotes[code]
            result[names.get(full, code)] = {
                "code": code,
                "price": q["price"],
                "change_pct": q["change_pct"],
                "pe_ttm": q["pe_ttm"],
                "pb": q["pb"],
                "mcap_wanyi": round(q["mcap_yi"] / 10000, 2) if q["mcap_yi"] else 0,
            }
    return result


# ---------------------------------------------------------------------------
# 2. 北向资金 (同花顺 — 稳定，零鉴权)
# ---------------------------------------------------------------------------
def get_northbound_flow() -> dict:
    """获取当日沪深股通累计净买入"""
    headers = {
        "User-Agent": UA,
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])

    if not times:
        return {"hgt_yi": 0, "sgt_yi": 0, "total_yi": 0, "signal": ""}

    n = len(times)
    hgt_vals = hgt[:n] + [None] * (n - len(hgt))
    sgt_vals = sgt[:n] + [None] * (n - len(sgt))

    # 取最后一个非空值
    hgt_last = 0
    sgt_last = 0
    for v in reversed(hgt_vals):
        if v is not None:
            hgt_last = float(v)
            break
    for v in reversed(sgt_vals):
        if v is not None:
            sgt_last = float(v)
            break

    total = hgt_last + sgt_last
    signal = ""
    if total > 50:
        signal = "🔥 北向大幅净流入"
    elif total > 20:
        signal = "📈 北向净流入"
    elif total < -30:
        signal = "⚠️ 北向大幅净流出"
    elif total < -10:
        signal = "📉 北向净流出"

    return {
        "hgt_yi": round(hgt_last, 2),
        "sgt_yi": round(sgt_last, 2),
        "total_yi": round(total, 2),
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# 3. 行业涨跌 TOP (同花顺热点 → 东财降级)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 3. 行业涨跌 TOP (东财优先 → 同花顺热点降级)
# ---------------------------------------------------------------------------
def _fetch_harden_raw(date: str) -> list[dict]:
    """抓取同花顺某日 getharden 强势股原始行，失败/空返回 []"""
    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
    except Exception:
        return []
    if data.get("errocode", 0) != 0:
        return []
    return data.get("data") or []


def _best_harden_rows(base_date: str = None, lookback: int = 5
                      ) -> tuple[list[dict], str]:
    """从 base_date 起向前回溯，返回第一个 zhangfu 非全 0 的交易日数据。

    解决当天 getharden 未刷新（非交易日/盘前，zhangfu 全 0）导致涨幅全 0 的问题。
    返回 (rows, used_date)；全部为空时返回 ([], base_date)。
    """
    if base_date is None:
        base_date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    for days_back in range(lookback):
        date = (datetime.now(BEIJING_TZ) - timedelta(days=days_back)
                ).strftime("%Y-%m-%d")
        rows = _fetch_harden_raw(date)
        if not rows:
            continue
        has_valid = any((r.get("zhangfu") or 0) != 0 for r in rows[:10])
        if has_valid or days_back == lookback - 1:
            return rows, date
    return [], base_date


def _aggregate_harden_to_sectors(rows: list[dict]) -> list[dict]:
    """同花顺强势股题材归因 → 板块热度排名（按强势股数量降序）"""
    tag_stocks: dict[str, list[dict]] = {}
    for row in rows:
        reason = row.get("reason", "")
        if not reason:
            continue
        for tag in (t.strip() for t in str(reason).split("+") if t.strip()):
            tag_stocks.setdefault(tag, []).append({
                "name": row.get("name", ""),
                "code": row.get("code", ""),
                "change_pct": row.get("zhangfu", 0),
            })

    result = []
    for i, (tag, stocks) in enumerate(
        sorted(tag_stocks.items(), key=lambda x: -len(x[1]))
    ):
        leader = stocks[0] if stocks else {}
        valid_chg = [s["change_pct"] for s in stocks if s.get("change_pct")]
        result.append({
            "rank": i + 1,
            "name": tag,
            "change_pct": round(sum(valid_chg) / len(valid_chg), 2) if valid_chg else 0,
            "code": "",
            "up_count": len(stocks),
            "down_count": 0,
            "leader": leader.get("name", ""),
            "leader_change": leader.get("change_pct", 0),
        })
    return result


def get_industry_ranking(top_n: int = 5) -> dict:
    """获取全行业涨跌幅排名。东财优先（有真实涨跌幅+跌幅榜），同花顺热点备用。"""
    # ---- 优先：东财行业排名（有真实涨跌幅 + 跌幅榜） ----
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "100", "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
        }
        r = _em_get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        items = r.json().get("data", {}).get("diff", [])
        if items:
            rows = [{
                "name": it.get("f14", ""),
                "change_pct": it.get("f3", 0) or 0,
                "code": it.get("f12", ""),
                "up_count": it.get("f104", 0) or 0,
                "down_count": it.get("f105", 0) or 0,
                "leader": it.get("f140", ""),
                "leader_change": it.get("f136", 0) or 0,
            } for it in items]
            # 显式按涨跌幅降序，确保 top=涨幅榜、bottom=跌幅榜
            # （东财默认排序不保证按涨跌幅，曾导致跌幅榜全是正数）
            rows.sort(key=lambda r: r["change_pct"], reverse=True)
            for i, r in enumerate(rows):
                r["rank"] = i + 1
            return {
                "top": rows[:top_n],
                "bottom": rows[-top_n:] if len(rows) > top_n * 2 else [],
                "total": len(rows),
                "source": "eastmoney",
                "note": "",
            }
    except Exception:
        pass

    # ---- 备用：同花顺强势股题材归因（带日期回溯） ----
    rows, used_date = _best_harden_rows()
    sectors = _aggregate_harden_to_sectors(rows)
    if sectors:
        return {
            "top": sectors[:top_n],
            # 同花顺由强势股反推，本质是「热度榜」无真实跌幅 → 不渲染假跌幅榜
            "bottom": [],
            "total": len(sectors),
            "source": "ths",
            "note": f"东财被封，按同花顺强势股反推（数据日期 {used_date}），仅涨幅榜",
        }

    return {"top": [], "bottom": [], "total": 0, "source": "", "note": ""}



# ---------------------------------------------------------------------------
# 4. 当日热点题材 (同花顺 — 稳定，零鉴权)
# ---------------------------------------------------------------------------
def get_hot_themes(date: str = None) -> tuple[pd.DataFrame, str]:
    """获取当日强势股及题材归因。返回 (df, used_date)。

    带日期回溯：当天 getharden 未刷新（zhangfu 全 0）时自动取最近有效交易日，
    避免强势股涨幅全 0。
    """
    rows, used_date = _best_harden_rows(date)
    df = pd.DataFrame(rows)
    if df.empty:
        return df, used_date

    rename_map = {
        "name": "名称", "code": "代码", "reason": "题材归因",
        "close": "收盘价", "zhangfu": "涨幅%",
        "huanshou": "换手率%", "chengjiaoe": "成交额",
    }
    for old, new in rename_map.items():
        if old in df.columns:
            df = df.rename(columns={old: new})
    # 涨幅% 统一转数值，便于 nlargest 排序
    if "涨幅%" in df.columns:
        df["涨幅%"] = pd.to_numeric(df["涨幅%"], errors="coerce").fillna(0)
    return df, used_date


def _theme_wordcount(df: pd.DataFrame, top_n: int = 10) -> list[tuple[str, int]]:
    """题材标签词频统计"""
    if df.empty or "题材归因" not in df.columns:
        return []
    all_tags = []
    for r in df["题材归因"].dropna():
        tags = [t.strip() for t in str(r).split("+") if t.strip()]
        all_tags.extend(tags)
    return Counter(all_tags).most_common(top_n)


# ---------------------------------------------------------------------------
# 5. 龙虎榜 (东财 — 可能被封)
# ---------------------------------------------------------------------------
def get_dragon_tiger(trade_date: str = None) -> dict:
    """获取全市场龙虎榜"""
    if trade_date is None:
        trade_date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    data = _em_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
        page_size=500,
        sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
    )
    if not data:
        return {"date": trade_date, "total_records": 0, "stocks": []}

    stocks = []
    for row in data:
        net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
        stocks.append({
            "code": row.get("SECURITY_CODE", ""),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "reason": row.get("EXPLANATION", ""),
            "close": row.get("CLOSE_PRICE") or 0,
            "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
            "net_buy_wan": round(net_buy, 1),
            "turnover_pct": round(float(row.get("TURNOVERRATE") or 0), 2),
        })

    return {
        "date": str(data[0].get("TRADE_DATE", ""))[:10] if data else trade_date,
        "total_records": len(stocks),
        "stocks": stocks,
    }


# ---------------------------------------------------------------------------
# 报告格式化
# ---------------------------------------------------------------------------
def fmt_market_indices(indices: dict) -> str:
    """格式化大盘指数"""
    lines = ["## 大盘概览", ""]
    if not indices:
        lines.append("*获取失败*")
        lines.append("")
        return "\n".join(lines)

    lines.append("| 指数 | 点位 | 涨跌幅 | PE(TTM) | PB | 市值(万亿) |")
    lines.append("|------|------|--------|---------|-----|-----------|")
    for name, d in indices.items():
        chg = f"{d['change_pct']:+.2f}%"
        pe = f"{d['pe_ttm']:.1f}" if d.get("pe_ttm") else "N/A"
        pb = f"{d['pb']:.2f}" if d.get("pb") else "N/A"
        mcap = f"{d['mcap_wanyi']:.2f}" if d.get("mcap_wanyi") else "N/A"
        lines.append(f"| {name} | {d['price']:.2f} | {chg} | {pe} | {pb} | {mcap} |")
    lines.append("")
    return "\n".join(lines)


def fmt_northbound(nb: dict) -> str:
    """格式化北向资金"""
    lines = ["## 北向资金", ""]
    if not nb:
        lines.append("*获取失败*")
        lines.append("")
        return "\n".join(lines)

    total_emoji = "🟢" if nb["total_yi"] > 0 else "🔴"
    lines.append(f"- 沪股通净买入: **{nb['hgt_yi']:+.2f} 亿**")
    lines.append(f"- 深股通净买入: **{nb['sgt_yi']:+.2f} 亿**")
    lines.append(f"- 北向合计: {total_emoji} **{nb['total_yi']:+.2f} 亿**")
    if nb.get("signal"):
        lines.append(f"- {nb['signal']}")
    lines.append("")
    return "\n".join(lines)


def fmt_industry_ranking(data: dict) -> str:
    """格式化行业涨跌"""
    lines = ["## 行业涨跌 TOP 5", ""]
    if not data or not data.get("top"):
        lines.append("*获取失败（东财接口可能被封）*")
        lines.append("")
        return "\n".join(lines)

    if data.get("note"):
        lines.append(f"> ℹ️ {data['note']}")
        lines.append("")

    lines.append("### 涨幅榜")
    lines.append("")
    lines.append("| 排名 | 行业 | 涨跌幅 | 涨/跌 | 领涨股 |")
    lines.append("|------|------|--------|-------|--------|")
    for r in data["top"]:
        lines.append(
            f"| {r['rank']} | {r['name']} | {r['change_pct']:+.2f}% "
            f"| {r['up_count']}/{r['down_count']} "
            f"| {r['leader']} ({r.get('leader_change', 0):+.2f}%) |"
        )
    lines.append("")

    if data.get("bottom"):
        lines.append("### 跌幅榜")
        lines.append("")
        lines.append("| 排名 | 行业 | 涨跌幅 | 涨/跌 | 领跌股 |")
        lines.append("|------|------|--------|-------|--------|")
        for r in data["bottom"]:
            lines.append(
                f"| {r['rank']} | {r['name']} | {r['change_pct']:+.2f}% "
                f"| {r['up_count']}/{r['down_count']} "
                f"| {r['leader']} ({r.get('leader_change', 0):+.2f}%) |"
            )
        lines.append("")

    return "\n".join(lines)


def fmt_hot_themes(df: pd.DataFrame, theme_count: list, held_codes: set,
                   used_date: str = None) -> str:
    """格式化热点题材"""
    lines = ["## 当日热点题材", ""]
    if df.empty:
        lines.append("*获取失败*")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"当日强势股: {len(df)} 只")
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    if used_date and used_date != today:
        lines.append(f"> ℹ️ 当天 getharden 未刷新，已回退到 {used_date} 的强势股数据")
    lines.append("")

    # 题材词频 TOP 10
    if theme_count:
        lines.append("### 热门题材 TOP 10")
        lines.append("")
        lines.append("| 排名 | 题材 | 出现次数 |")
        lines.append("|------|------|----------|")
        for i, (tag, cnt) in enumerate(theme_count, 1):
            lines.append(f"| {i} | {tag} | {cnt} |")
        lines.append("")

    # 涨幅 TOP 10 个股
    lines.append("### 强势股 TOP 10")
    lines.append("")
    lines.append("| 代码 | 名称 | 涨幅% | 题材归因 |")
    lines.append("|------|------|-------|----------|")
    top10 = df.nlargest(10, "涨幅%") if "涨幅%" in df.columns else df.head(10)
    for _, row in top10.iterrows():
        code = str(row.get("代码", ""))
        name = str(row.get("名称", ""))
        zhangfu = row.get("涨幅%", 0)
        reason = str(row.get("题材归因", ""))
        marker = " ⭐**持仓/自选**" if code in held_codes else ""
        lines.append(f"| {code} | {name} | {zhangfu:+.2f} | {reason} |{marker}")
    lines.append("")

    # 持仓/自选股上榜
    if held_codes:
        held_hot = df[df["代码"].astype(str).isin(held_codes)]
        if not held_hot.empty:
            lines.append("### ⭐ 你的持仓/自选上榜")
            lines.append("")
            for _, row in held_hot.iterrows():
                code = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                zhangfu = row.get("涨幅%", 0)
                reason = str(row.get("题材归因", ""))
                lines.append(
                    f"- **{name}** ({code}) {zhangfu:+.2f}% — {reason}"
                )
            lines.append("")

    return "\n".join(lines)


def fmt_dragon_tiger(data: dict, held_codes: set) -> str:
    """格式化龙虎榜"""
    lines = ["## 龙虎榜", ""]
    if not data or not data.get("stocks"):
        lines.append(
            f"*{data.get('date', '')} 无数据"
            f"（非交易日、盘后未更新、或接口被封）*"
        )
        lines.append("")
        return "\n".join(lines)

    lines.append(f"日期: {data['date']} | 共 {data['total_records']} 条记录")
    lines.append("")

    # 净买入 TOP 10
    stocks = data["stocks"]
    top10 = sorted(stocks, key=lambda s: s["net_buy_wan"], reverse=True)[:10]

    lines.append("### 净买入 TOP 10")
    lines.append("")
    lines.append("| 代码 | 名称 | 涨跌幅 | 净买入(万) | 换手率 | 上榜原因 |")
    lines.append("|------|------|--------|-----------|--------|----------|")
    for s in top10:
        marker = " ⭐" if s["code"] in held_codes else ""
        lines.append(
            f"| {s['code']} | {s['name']}{marker} | "
            f"{s['change_pct']:+.2f}% | {s['net_buy_wan']:.0f} | "
            f"{s['turnover_pct']:.1f}% | {s['reason']} |"
        )
    lines.append("")

    # 持仓/自选上榜
    if held_codes:
        held_lhb = [s for s in stocks if s["code"] in held_codes]
        if held_lhb:
            lines.append("### ⭐ 你的持仓/自选上榜")
            lines.append("")
            for s in held_lhb:
                lines.append(
                    f"- **{s['name']}** ({s['code']}) "
                    f"{s['change_pct']:+.2f}% "
                    f"净买入{s['net_buy_wan']:.0f}万 — {s['reason']}"
                )
            lines.append("")

    return "\n".join(lines)


def fmt_signal_summary(signals: list[str]) -> str:
    """格式化关注信号汇总"""
    lines = ["## 📌 关注信号汇总", ""]
    if not signals:
        lines.append("今日无重要信号")
    else:
        for sig in signals:
            lines.append(f"- {sig}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------
def _get_held_codes() -> set[str]:
    """获取持仓+自选的所有代码集合（用于交叉分析）"""
    codes = set()
    for csv_name in ["portfolio.csv", "watchlist.csv"]:
        path = DATA_DIR / csv_name
        if path.exists():
            try:
                df = pd.read_csv(path, dtype=str, index_col=False)
                codes.update(df["symbol"].tolist())
            except Exception:
                pass
    return codes


def _wrap_details(summary: str, content: str) -> str:
    """包成 <details> 折叠块。

    GitHub Issue 网页可点击折叠；QQ 邮箱等不支持 <details> 的客户端会展开显示
    （内容不丢失）。简版报告不调用此函数，直接省略市场行情章节。
    """
    return (f"<details>\n<summary><b>{summary}</b></summary>\n\n"
            f"{content.strip()}\n\n</details>")


def _demote_h1(md: str) -> str:
    """把首个 # 一级标题降为 ##（持仓/自选子报告嵌入每日复盘时统一层级）。"""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines[i] = "#" + line  # "# 标题" → "## 标题"
            break
    return "\n".join(lines)


def generate_report(market: str = "ALL", no_market: bool = False,
                    period: str = None, brief: bool = None) -> str:
    """生成每日复盘报告。

    输出顺序：AI 深度分析 → 持仓 → 自选 → 关注信号汇总 → [市场行情章节]。
    brief=True（盘中简版）只输出前四块，便于邮箱快速阅读；brief=None 时按
    period 自动推断（noon=简版，eod=完整版）。完整版的市场行情章节用
    <details> 折叠。AI 分析始终基于全量数据（含市场行情），即使简版也不降质。
    """
    period = period or _infer_period()
    if brief is None:
        brief = (period == "noon")  # 盘中默认简版（QQ 邮箱友好）
    period_label = PERIOD_LABELS.get(period, "")
    brief_tag = "简版" if brief else ""
    title_suffix = f" · {period_label}{brief_tag}" if (period_label or brief_tag) else ""

    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    signals = []

    header = "\n".join([
        f"# 📊 每日复盘{title_suffix} {today}",
        "",
        f"> 生成时间: {now}",
    ])

    held_codes = _get_held_codes()
    do_cn = market in ("ALL", "CN")
    do_us = market in ("ALL", "US")

    # 各章节先收集到 secs（输出顺序最后再排）
    secs: dict[str, str] = {}

    # ---- 市场信号（仅A股） ----
    if not no_market and do_cn:
        # 1. 大盘指数
        print("  📈 大盘指数...", end=" ", flush=True)
        try:
            secs["indices"] = fmt_market_indices(get_market_indices())
            print("✓")
        except Exception as e:
            print(f"✗ {e}")
            secs["indices"] = fmt_market_indices({})

        # 2. 北向资金
        print("  🌊 北向资金...", end=" ", flush=True)
        try:
            nb = get_northbound_flow()
            secs["northbound"] = fmt_northbound(nb)
            if nb.get("signal"):
                signals.append(nb["signal"])
            print(f"✓ (合计 {nb.get('total_yi', 0):+.2f} 亿)")
        except Exception as e:
            print(f"✗ {e}")
            secs["northbound"] = fmt_northbound({})

        # 3. 行业涨跌
        print("  🏭 行业涨跌...", end=" ", flush=True)
        try:
            ind = get_industry_ranking(5)
            secs["industry"] = fmt_industry_ranking(ind)
            if ind.get("top"):
                top1 = ind["top"][0]
                signals.append(
                    f"🏭 今日最强行业: {top1['name']} ({top1['change_pct']:+.2f}%)"
                )
            print("✓")
        except Exception as e:
            print(f"✗ {e}")
            secs["industry"] = fmt_industry_ranking({})

        # 4. 热点题材
        print("  🔥 热点题材...", end=" ", flush=True)
        try:
            hot_df, hot_date = get_hot_themes()
            theme_count = _theme_wordcount(hot_df, 10)
            secs["hot"] = fmt_hot_themes(hot_df, theme_count, held_codes, hot_date)
            if theme_count:
                top_tags = ", ".join(t for t, _ in theme_count[:3])
                signals.append(f"🔥 热门题材: {top_tags}")
            # 检查持仓股是否上榜
            if not hot_df.empty and held_codes:
                held_hot = hot_df[
                    hot_df["代码"].astype(str).isin(held_codes)
                ]
                if not held_hot.empty:
                    for _, row in held_hot.iterrows():
                        signals.append(
                            f"⭐ {row.get('名称', '')} ({row.get('代码', '')}) "
                            f"上榜强势股 {row.get('涨幅%', 0):+.2f}%"
                        )
            print(f"✓ ({len(hot_df)} 只)")
        except Exception as e:
            print(f"✗ {e}")
            secs["hot"] = fmt_hot_themes(pd.DataFrame(), [], held_codes)

        # 5. 龙虎榜
        print("  🐯 龙虎榜...", end=" ", flush=True)
        try:
            lhb = get_dragon_tiger()
            secs["dragon"] = fmt_dragon_tiger(lhb, held_codes)
            if lhb.get("stocks") and held_codes:
                held_lhb = [
                    s for s in lhb["stocks"] if s["code"] in held_codes
                ]
                for s in held_lhb:
                    signals.append(
                        f"🐯 {s['name']} ({s['code']}) "
                        f"上榜龙虎榜 净买入{s['net_buy_wan']:.0f}万"
                    )
            print(f"✓ ({lhb.get('total_records', 0)} 条)")
        except Exception as e:
            print(f"✗ {e}")
            secs["dragon"] = fmt_dragon_tiger({}, held_codes)

    # ---- 持仓分析 ----
    if do_cn or do_us:
        pf_path = DATA_DIR / "portfolio.csv"
        if pf_path.exists():
            print("  📋 持仓分析...", end=" ", flush=True)
            try:
                df = load_portfolio()
                # 按 market 过滤
                if market != "ALL":
                    df = df[df["market"].str.upper() == market]
                positions = []
                for _, row in df.iterrows():
                    positions.append(analyze_position(row))
                secs["portfolio"] = fmt_portfolio_report(positions)
                # 汇总信号
                for p in positions:
                    for sig in p.signals:
                        if any(k in sig for k in ["⚠️", "🛑", "🎯", "💎"]):
                            signals.append(
                                f"📋 {p.name} ({p.symbol}): {sig}"
                            )
                print(f"✓ ({len(positions)} 只)")
            except Exception as e:
                print(f"✗ {e}")

        # ---- 自选分析 ----
        wl_path = DATA_DIR / "watchlist.csv"
        if wl_path.exists():
            print("  👀 自选分析...", end=" ", flush=True)
            try:
                df = load_watchlist()
                if market != "ALL":
                    df = df[df["market"].str.upper() == market]
                items = []
                for _, row in df.iterrows():
                    items.append(analyze_watchlist_item(row))
                secs["watchlist"] = fmt_watchlist_report(items)
                for w in items:
                    for sig in w.signals:
                        if any(k in sig for k in ["🔔", "💎"]):
                            signals.append(
                                f"👀 {w.name} ({w.symbol}): {sig}"
                            )
                print(f"✓ ({len(items)} 只)")
            except Exception as e:
                print(f"✗ {e}")

    # ---- 板块轮动（仅A股） ----
    if not no_market and do_cn:
        print("  🔄 板块轮动...", end=" ", flush=True)
        try:
            secs["rotation"] = run_rotation_check(date=today, full_report=False)
            # 从轮动信号中提取重要信号
            from sector_rotation import detect_rotation_signals, load_history
            rot_history = load_history()
            rot_signals = detect_rotation_signals(rot_history)
            for s in rot_signals:
                icon = {"capital_inflow": "🔥", "capital_outflow": "💧",
                        "weak_to_strong": "⬆️", "strong_to_weak": "⬇️"}.get(s["type"], "")
                signals.append(f"{icon} {s['sector']}: {s['detail']}")
            print(f"✓ ({len(rot_signals)} 条信号)" if rot_signals else "✓")
        except Exception as e:
            print(f"✗ {e}")

    # ---- 新闻与事件上下文（仅完整版；盘中新闻少且求快） ----
    if not brief:
        print("  📰 新闻上下文...", end=" ", flush=True)
        try:
            news_md = get_news_context()
            if news_md.strip():
                secs["news"] = "## 📰 新闻与事件上下文\n\n" + news_md
            print("✓")
        except Exception as e:
            print(f"✗ {e}")

    # ---- 信号汇总 ----
    secs["signals"] = fmt_signal_summary(signals)

    # ---- AI 深度分析（喂全量 draft，简版也基于完整数据） ----
    print("  🤖 AI 深度分析...", end=" ", flush=True)
    try:
        draft_parts = [header]
        if secs.get("news"):
            draft_parts.append(secs["news"])  # 新闻上下文优先，AI 据此分析事件影响
        for key in ["indices", "northbound", "industry", "hot", "dragon",
                    "rotation", "portfolio", "watchlist"]:
            if secs.get(key):
                draft_parts.append(secs[key])
        ai_md = generate_ai_analysis("\n\n".join(draft_parts))
        if ai_md:
            secs["ai"] = ("## 🤖 AI 深度分析\n\n"
                          "> 由 DeepSeek AI 生成，仅供参考\n\n" + ai_md)
            print("✓")
        else:
            print("✗ (未配置 DEEPSEEK_API_KEY)")
    except Exception as e:
        print(f"✗ {e}")

    # ---- 按目标顺序输出：AI → 持仓 → 自选 → 信号 → [市场行情] ----
    parts = [header]
    if secs.get("ai"):
        parts.append(secs["ai"])
    if secs.get("news"):
        parts.append(secs["news"])
    if secs.get("portfolio"):
        parts.append(_demote_h1(secs["portfolio"]))
    if secs.get("watchlist"):
        parts.append(_demote_h1(secs["watchlist"]))
    parts.append(secs["signals"])

    # 市场行情章节：简版跳过（邮箱短阅读）；完整版用 <details> 折叠
    if not brief:
        for key, summary in [
            ("indices", "📈 大盘概览（点击展开）"),
            ("northbound", "🌊 北向资金（点击展开）"),
            ("industry", "🏭 行业涨跌（点击展开）"),
            ("hot", "🔥 热点题材（点击展开）"),
            ("dragon", "🐯 龙虎榜（点击展开）"),
            ("rotation", "🔄 板块轮动（点击展开）"),
        ]:
            content = secs.get(key)
            if content and content.strip():
                parts.append(_wrap_details(summary, content))

    final_report = "\n\n".join(p for p in parts if p and p.strip())

    # ---- RAG: 将完整报告索引到向量库 ----
    try:
        from rag_engine import index_report
        index_report(today, final_report)
    except Exception:
        pass  # RAG 索引失败不影响主流程

    return final_report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="每日复盘 — 持仓+自选+市场信号一键报告"
    )
    parser.add_argument(
        "--market", "-m", default="ALL",
        choices=["CN", "US", "ALL"],
        help="市场过滤 (默认 ALL=全部)",
    )
    parser.add_argument(
        "--no-market", action="store_true",
        help="跳过市场信号，仅输出持仓+自选分析",
    )
    parser.add_argument(
        "--period", choices=["noon", "eod"], default=None,
        help="报告时段：noon=盘中快报(中午)，eod=收盘复盘(晚上)；"
             "默认按北京时间自动判断",
    )
    parser.add_argument(
        "--output", "-o", type=str,
        help="输出文件路径 (默认 reports/YYYY-MM-DD-{period}.md)",
    )
    args = parser.parse_args()

    period = args.period or _infer_period()
    period_label = PERIOD_LABELS.get(period, "")
    title_suffix = f" · {period_label}" if period_label else ""

    print(f"📊 正在生成每日复盘{title_suffix}...")
    report = generate_report(market=args.market, no_market=args.no_market,
                             period=period)
    print()
    print(report)

    # 确定输出路径
    if args.output:
        out_path = Path(args.output)
    else:
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        out_path = REPORTS_DIR / f"{today}-{period}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n📝 报告已保存到: {out_path}")

    # 推送通知
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    send_notification(
        title=f"📊 每日复盘{title_suffix} {today_str}",
        content=report,
    )


if __name__ == "__main__":
    main()
