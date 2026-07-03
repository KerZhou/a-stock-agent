"""
板块扫描器 — 搜索概念/行业板块，批量估值筛选低估标的。

数据源策略 (东财被封时的替代方案):
  搜索板块:   同花顺 HTML (q.10jqka.com.cn)
  成分股列表: 同花顺 HTML + 百度概念板块 API
  批量估值:   腾讯财经 (不封IP，支持批量)
  深度筛选:   valuation_compare.py (仅 Top N)

用法:
  python scripts/sector_scan.py search HBM              # 搜索板块
  python scripts/sector_scan.py scan 307940             # 扫描指定板块代码
  python scripts/sector_scan.py quick 商业航天           # 关键词直接扫描
  python scripts/sector_scan.py quick HBM -o report.md  # 输出到文件
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"


# ---------------------------------------------------------------------------
# 1. 搜索概念/行业板块 (同花顺 HTML)
# ---------------------------------------------------------------------------
def _fetch_ths_boards(board_type: str = "gn") -> list[dict]:
    """
    从同花顺 HTML 页面抓取概念/行业板块列表。
    board_type: "gn"(概念) / "thshy"(行业)
    返回: [{code, name}]
    """
    url = f"http://q.10jqka.com.cn/{board_type}/"
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
    except Exception as e:
        print(f"  [WARN] 同花顺板块列表请求失败: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    boards = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(rf"/{board_type}/detail/code/(\d+)/", a["href"])
        if m:
            code = m.group(1)
            name = a.get_text(strip=True)
            if name and code not in seen:
                seen.add(code)
                boards.append({"code": code, "name": name})

    return boards


def search_concept_boards(keyword: str) -> list[dict]:
    """
    搜索概念和行业板块，按关键词模糊匹配。
    返回: [{code, name, type, board_path}]
    """
    results = []

    # 搜概念板块
    for b in _fetch_ths_boards("gn"):
        if keyword.lower() in b["name"].lower() or keyword.lower() in b["code"]:
            results.append({**b, "type": "概念", "board_path": "gn"})

    # 搜行业板块
    for b in _fetch_ths_boards("thshy"):
        if keyword.lower() in b["name"].lower() or keyword.lower() in b["code"]:
            results.append({**b, "type": "行业", "board_path": "thshy"})

    return results


# ---------------------------------------------------------------------------
# 2. 获取板块成分股 (同花顺 HTML + 百度补充)
# ---------------------------------------------------------------------------
def get_board_stocks_ths(board_code: str, board_type: str = "gn") -> list[dict]:
    """
    从同花顺板块详情页抓取成分股。
    board_type: "gn"(概念) / "thshy"(行业)
    第一页约 10 只，含现价/涨跌幅/换手率/市盈率。
    返回: [{code, name, price, change_pct, turnover_pct, pe}]
    """
    url = f"http://q.10jqka.com.cn/{board_type}/detail/code/{board_code}/"
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
    except Exception as e:
        print(f"  [WARN] 同花顺板块详情失败: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    stocks = []
    for row in table.find_all("tr")[1:]:
        tds = row.find_all("td")
        if len(tds) < 3:
            continue
        code = tds[1].get_text(strip=True)
        name = tds[2].get_text(strip=True)
        if not code or not code.isdigit() or len(code) != 6:
            continue
        if "ST" in name.upper():
            continue

        def _safe(td, default=None):
            try:
                v = td.get_text(strip=True).replace(",", "")
                return float(v) if v and v != "--" else default
            except ValueError:
                return default

        stocks.append({
            "code": code,
            "name": name,
            "price": _safe(tds[3]) if len(tds) > 3 else None,
            "change_pct": _safe(tds[4]) if len(tds) > 4 else None,
            "turnover_pct": _safe(tds[7]) if len(tds) > 7 else None,
            "pe": _safe(tds[13]) if len(tds) > 13 else None,
        })

    return stocks


def get_board_stocks_baidu(code: str) -> list[str]:
    """
    百度股市通获取个股所属概念板块的所有成分股代码。
    注意: 这是反向的——先知道板块里的某只股票，通过百度拉该板块所有成分股。
    返回: [code, ...] 股票代码列表
    """
    # 百度不直接支持板块成分股列表，跳过
    return []


# ---------------------------------------------------------------------------
# 3. 批量估值 (腾讯财经 — 不封 IP)
# ---------------------------------------------------------------------------
def batch_valuation(codes: list[str], batch_size: int = 50) -> dict[str, dict]:
    """
    腾讯财经批量拉取 PE/PB/市值。
    codes: ["688017", "300476", ...]
    返回: {code: {name, price, pe_ttm, pb, mcap_yi, change_pct, turnover_pct, ...}}
    """
    result = {}

    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        prefixed = []
        for c in batch:
            if c.startswith(("6", "9")):
                prefixed.append(f"sh{c}")
            elif c.startswith("8"):
                prefixed.append(f"bj{c}")
            else:
                prefixed.append(f"sz{c}")

        url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            raw = resp.read().decode("gbk")
        except Exception as e:
            print(f"  [WARN] 腾讯批量请求失败: {e}")
            continue

        for line in raw.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line or '"' not in line:
                continue
            try:
                key = line.split("=")[0].split("_")[-1]
                vals = line.split('"')[1].split("~")
                if len(vals) < 53:
                    continue
                stock_code = key[2:]
                result[stock_code] = {
                    "name": vals[1],
                    "price": _sf(vals[3]),
                    "change_pct": _sf(vals[32]),
                    "pe_ttm": _sf(vals[39]),
                    "mcap_yi": _sf(vals[44]),
                    "float_mcap_yi": _sf(vals[45]),
                    "pb": _sf(vals[46]),
                    "turnover_pct": _sf(vals[38]),
                    "vol_ratio": _sf(vals[49]),
                }
            except (IndexError, ValueError):
                continue

        if i + batch_size < len(codes):
            time.sleep(0.3)

    return result


def _sf(val: str) -> float | None:
    """安全转浮点"""
    if not val or val in ("-", ""):
        return None
    try:
        f = float(val)
        return f if f != 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 4. 估值筛选
# ---------------------------------------------------------------------------
def filter_undervalued(
    quotes: dict[str, dict],
    pe_max: float = 50,
    pe_min: float = 0,
    pb_max: float = 100,
    top_n: int = 30,
) -> list[dict]:
    """
    筛选低估标的。
    默认: PE > 0 且 < 50, PB > 0, 按 PE 升序
    """
    filtered = []
    for code, q in quotes.items():
        pe = q.get("pe_ttm")
        pb = q.get("pb")
        if pe is None or pb is None:
            continue
        if pe_min < pe < pe_max and 0 < pb < pb_max:
            filtered.append({
                "code": code,
                "name": q.get("name", ""),
                "price": q.get("price"),
                "pe_ttm": pe,
                "pb": pb,
                "mcap_yi": q.get("mcap_yi"),
                "change_pct": q.get("change_pct"),
                "turnover_pct": q.get("turnover_pct"),
                "vol_ratio": q.get("vol_ratio"),
            })

    filtered.sort(key=lambda x: x.get("pe_ttm") or 999)
    return filtered[:top_n]


# ---------------------------------------------------------------------------
# 5. 报告输出
# ---------------------------------------------------------------------------
def fmt_sector_report(
    board_name: str,
    board_code: str,
    total_count: int,
    all_stocks: list[dict],
    filtered: list[dict],
    pe_max: float,
) -> str:
    """生成板块扫描 Markdown 报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 🔍 板块扫描: {board_name} ({board_code})",
        "",
        f"> 扫描时间: {now} | 共 {total_count} 只成分股 | "
        f"筛选条件: 0 < PE < {pe_max:.0f}",
        "",
    ]

    if not filtered:
        lines.append("**未找到符合条件的低估标的。**")
        lines.append("")
        lines.append("建议:")
        lines.append("- 放宽 PE 上限: `--pe-max 80`")
        lines.append("- 查看全量数据: `--pe-max 9999`")
        # 显示所有股票概览
        if all_stocks:
            lines.append("")
            lines.append("## 全部成分股概览")
            lines.append("")
            lines.append(
                f"| 代码 | 名称 | 现价 | PE(TTM) | PB | "
                f"市值(亿) | 涨跌幅% | 换手率% |"
            )
            lines.append(
                f"|------|------|------|---------|-----|"
                f"---------|---------|---------|"
            )
            for s in all_stocks:
                _row(lines, s)
        return "\n".join(lines)

    lines.append(f"## 低估候选（共 {len(filtered)} 只，按 PE 升序）")
    lines.append("")
    lines.append(
        f"| 排名 | 代码 | 名称 | 现价 | PE(TTM) | PB | "
        f"市值(亿) | 涨跌幅% | 换手率% |"
    )
    lines.append(
        f"|------|------|------|------|---------|-----|"
        f"---------|---------|---------|"
    )

    for i, s in enumerate(filtered, 1):
        price_str = f"{s['price']:.2f}" if s["price"] else "N/A"
        pe_str = f"{s['pe_ttm']:.1f}" if s["pe_ttm"] else "N/A"
        pb_str = f"{s['pb']:.2f}" if s["pb"] else "N/A"
        mcap_str = f"{s['mcap_yi']:.0f}" if s["mcap_yi"] else "N/A"
        chg_str = f"{s['change_pct']:+.2f}" if s["change_pct"] else "N/A"
        turn_str = f"{s['turnover_pct']:.2f}" if s["turnover_pct"] else "N/A"

        lines.append(
            f"| {i} | {s['code']} | {s['name']} | {price_str} | "
            f"{pe_str} | {pb_str} | {mcap_str} | {chg_str} | {turn_str} |"
        )

    lines.append("")

    # 关注建议
    lines.append("## 关注建议")
    lines.append("")
    for s in filtered[:5]:
        reasons = []
        if s["pe_ttm"] and s["pe_ttm"] < 20:
            reasons.append(f"PE 仅 {s['pe_ttm']:.1f}")
        if s["mcap_yi"] and s["mcap_yi"] < 200:
            reasons.append(f"小市值 {s['mcap_yi']:.0f} 亿")
        if s["turnover_pct"] and s["turnover_pct"] > 5:
            reasons.append(f"换手率 {s['turnover_pct']:.1f}%")
        if s["change_pct"] and s["change_pct"] < -3:
            reasons.append("当日回调")
        reason_str = "，".join(reasons) if reasons else "估值相对较低"
        lines.append(f"- **{s['name']}({s['code']})**: {reason_str}")

    lines.append("")
    lines.append(
        "> 💡 对感兴趣的标的，可用 `python scripts/portfolio.py` 做深度估值分析"
    )

    return "\n".join(lines)


def _row(lines: list, s: dict):
    """添加一行股票数据到报告"""
    price_str = f"{s.get('price', 'N/A') or 'N/A'}"
    if isinstance(s.get("price"), float):
        price_str = f"{s['price']:.2f}"
    pe_str = f"{s.get('pe_ttm') or s.get('pe', 'N/A')}"
    if isinstance(s.get("pe_ttm") or s.get("pe"), float):
        pe_str = f"{(s.get('pe_ttm') or s.get('pe')):.1f}"
    pb_str = f"{s.get('pb', 'N/A')}"
    if isinstance(s.get("pb"), float):
        pb_str = f"{s['pb']:.2f}"
    mcap_str = f"{s.get('mcap_yi', 'N/A')}"
    if isinstance(s.get("mcap_yi"), float):
        mcap_str = f"{s['mcap_yi']:.0f}"
    chg_str = f"{s.get('change_pct', 'N/A')}"
    if isinstance(s.get("change_pct"), float):
        chg_str = f"{s['change_pct']:+.2f}"
    turn_str = f"{s.get('turnover_pct', 'N/A')}"
    if isinstance(s.get("turnover_pct"), float):
        turn_str = f"{s['turnover_pct']:.2f}"

    lines.append(
        f"| {s.get('code', '')} | {s.get('name', '')} | {price_str} | "
        f"{pe_str} | {pb_str} | {mcap_str} | {chg_str} | {turn_str} |"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_search(args):
    """搜索板块"""
    keyword = args.keyword
    print(f"🔍 搜索板块: {keyword}")
    print()

    results = search_concept_boards(keyword)

    if not results:
        print(f"未找到包含「{keyword}」的板块")
        print("提示: 尝试更短的关键词，如 'HBM' → '先进封装' 或 '芯片'")
        return

    for b in results[:15]:
        print(f"  {b['code']}  {b['name']:<16} [{b['type']}]")


def cmd_scan(args):
    """扫描指定板块代码"""
    board_code = args.board
    board_type = args.type
    pe_max = args.pe_max
    top_n = args.top

    print(f"📊 扫描板块: {board_code} [{board_type}]")

    # 1. 获取成分股
    print("  1/3 获取成分股 (同花顺)...", end=" ", flush=True)
    stocks = get_board_stocks_ths(board_code, board_type=board_type)
    if not stocks:
        print("✗ 无数据")
        print("  提示: 板块代码格式为 6 位数字，用 `search` 命令查找")
        return
    print(f"✓ 共 {len(stocks)} 只")

    # 2. 腾讯批量估值（补充 PE/PB）
    print(f"  2/3 批量估值 (腾讯财经)...", end=" ", flush=True)
    codes = [s["code"] for s in stocks]
    quotes = batch_valuation(codes)
    print(f"✓ 获取 {len(quotes)} 只")

    # 合并数据：腾讯数据覆盖同花顺的
    merged = _merge_quotes(stocks, quotes)

    # 3. 筛选
    print(f"  3/3 筛选低估标的 (PE < {pe_max})...", end=" ", flush=True)
    filtered = filter_undervalued(merged, pe_max=pe_max, top_n=top_n)
    print(f"✓ {len(filtered)} 只符合条件")

    # 报告
    report = fmt_sector_report(
        board_code, board_code, len(stocks), list(merged.values()), filtered, pe_max
    )
    print()
    print(report)

    if args.output:
        _save(args.output, report)


def cmd_quick(args):
    """关键词快速扫描"""
    keyword = args.keyword
    pe_max = args.pe_max
    top_n = args.top

    print(f"⚡ 快速扫描: {keyword}")
    print()

    # 1. 搜索板块
    print("  1/4 搜索板块...", end=" ", flush=True)
    results = search_concept_boards(keyword)
    if not results:
        print("✗")
        print(f"  未找到包含「{keyword}」的板块")
        print("  提示: 用 `search` 命令查看所有匹配板块")
        return

    # 优先匹配名称完全包含关键词的
    board = results[0]
    for b in results:
        if keyword.lower() in b["name"].lower():
            board = b
            break

    print(f"✓ → {board['name']} ({board['code']}) [{board['type']}]")
    if len(results) > 1:
        print(f"  (共 {len(results)} 个匹配板块，使用第一个)")

    board_path = board.get("board_path", "gn")

    # 2. 获取成分股（如果第一个板块无数据，尝试其他匹配）
    print("  2/4 获取成分股 (同花顺)...", end=" ", flush=True)
    stocks = get_board_stocks_ths(board["code"], board_type=board_path)
    if not stocks:
        for alt in results[1:]:
            alt_path = alt.get("board_path", "gn")
            alt_stocks = get_board_stocks_ths(alt["code"], board_type=alt_path)
            if alt_stocks:
                print(f"✗ {board['name']}无数据 → ", end="")
                board = alt
                board_path = alt_path
                stocks = alt_stocks
                break
        if not stocks:
            print("✗ 所有匹配板块均无数据")
            return
    if not stocks:
        print("✗ 无数据")
        return
    print(f"✓ 共 {len(stocks)} 只")

    # 3. 腾讯批量估值
    print(f"  3/4 批量估值 (腾讯财经)...", end=" ", flush=True)
    codes = [s["code"] for s in stocks]
    quotes = batch_valuation(codes)
    print(f"✓ 获取 {len(quotes)} 只")

    # 合并
    merged = _merge_quotes(stocks, quotes)

    # 4. 筛选
    print(f"  4/4 筛选低估标的 (PE < {pe_max})...", end=" ", flush=True)
    filtered = filter_undervalued(merged, pe_max=pe_max, top_n=top_n)
    print(f"✓ {len(filtered)} 只符合条件")

    # 报告
    report = fmt_sector_report(
        board["name"], board["code"], len(stocks),
        list(merged.values()), filtered, pe_max,
    )
    print()
    print(report)

    if args.output:
        _save(args.output, report)


def _merge_quotes(
    stocks: list[dict], quotes: dict[str, dict]
) -> dict[str, dict]:
    """合并同花顺成分股和腾讯估值数据，以腾讯为准"""
    merged = {}
    for s in stocks:
        code = s["code"]
        q = quotes.get(code, {})
        merged[code] = {
            "code": code,
            "name": q.get("name") or s.get("name", ""),
            "price": q.get("price") or s.get("price"),
            "pe_ttm": q.get("pe_ttm") or s.get("pe"),
            "pb": q.get("pb"),
            "mcap_yi": q.get("mcap_yi"),
            "change_pct": q.get("change_pct") or s.get("change_pct"),
            "turnover_pct": q.get("turnover_pct") or s.get("turnover_pct"),
            "vol_ratio": q.get("vol_ratio"),
        }
    # 加上腾讯有但同花顺没有的
    for code, q in quotes.items():
        if code not in merged:
            merged[code] = {
                "code": code,
                "name": q.get("name", ""),
                "price": q.get("price"),
                "pe_ttm": q.get("pe_ttm"),
                "pb": q.get("pb"),
                "mcap_yi": q.get("mcap_yi"),
                "change_pct": q.get("change_pct"),
                "turnover_pct": q.get("turnover_pct"),
                "vol_ratio": q.get("vol_ratio"),
            }
    return merged


def _save(path_str: str, report: str):
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(f"\n📝 报告已保存到: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="板块扫描器 — 搜索板块，批量估值，筛选低估标的"
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # search
    p_search = sub.add_parser("search", help="搜索概念/行业板块")
    p_search.add_argument("keyword", help="搜索关键词")

    # scan
    p_scan = sub.add_parser("scan", help="扫描指定板块代码 (如 307940)")
    p_scan.add_argument("board", help="板块代码 (6位数字)")
    p_scan.add_argument("--type", default="gn", choices=["gn", "thshy"],
                        help="板块类型: gn=概念(默认), thshy=行业")
    p_scan.add_argument("--pe-max", type=float, default=50, help="PE上限 (默认 50)")
    p_scan.add_argument("--top", type=int, default=30, help="输出数量 (默认 30)")
    p_scan.add_argument("-o", "--output", help="输出到文件")

    # quick
    p_quick = sub.add_parser("quick", help="关键词快速扫描")
    p_quick.add_argument("keyword", help="板块关键词")
    p_quick.add_argument("--pe-max", type=float, default=50, help="PE上限 (默认 50)")
    p_quick.add_argument("--top", type=int, default=30, help="输出数量 (默认 30)")
    p_quick.add_argument("-o", "--output", help="输出到文件")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "quick":
        cmd_quick(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
