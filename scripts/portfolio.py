"""
持仓分析工具 — 读取 portfolio.csv / watchlist.csv，调用估值引擎，输出分析报告。

用法:
  python scripts/portfolio.py                  # 分析持仓
  python scripts/portfolio.py --watchlist      # 分析自选
  python scripts/portfolio.py --all            # 全部分析
  python scripts/portfolio.py --output reports/2026-06-03.md   # 输出到文件
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# 与 daily_review.py 一致，统一用北京时区（CI runner 默认 UTC）
BEIJING_TZ = timezone(timedelta(hours=8))

# 把 scripts/ 加入 path 以便 import valuation_compare
sys.path.insert(0, str(Path(__file__).parent))
from valuation_compare import (
    ValuationData,
    get_cn_stock_valuation,
    get_us_stock_valuation,
)

DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def load_portfolio() -> pd.DataFrame:
    """读取持仓 CSV"""
    path = DATA_DIR / "portfolio.csv"
    if not path.exists():
        raise FileNotFoundError(f"持仓文件不存在: {path}")
    df = pd.read_csv(path, dtype=str, index_col=False)
    df["cost_basis"] = pd.to_numeric(df["cost_basis"], errors="coerce")
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    if "target_price" in df.columns:
        df["target_price"] = pd.to_numeric(df["target_price"], errors="coerce")
    else:
        df["target_price"] = None
    if "stop_loss" in df.columns:
        df["stop_loss"] = pd.to_numeric(df["stop_loss"], errors="coerce")
    else:
        df["stop_loss"] = None
    df["target_price"] = df["target_price"].where(df["target_price"].notna(), None)
    df["stop_loss"] = df["stop_loss"].where(df["stop_loss"].notna(), None)
    return df


def load_watchlist() -> pd.DataFrame:
    """读取自选 CSV"""
    path = DATA_DIR / "watchlist.csv"
    if not path.exists():
        raise FileNotFoundError(f"自选文件不存在: {path}")
    df = pd.read_csv(path, dtype=str, index_col=False)
    if "alert_price" in df.columns:
        df["alert_price"] = pd.to_numeric(df["alert_price"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# 估值获取
# ---------------------------------------------------------------------------
def get_valuation(symbol: str, market: str) -> ValuationData | None:
    """根据市场获取估值数据，失败返回 None"""
    try:
        if market.upper() == "US":
            return get_us_stock_valuation(symbol)
        else:
            return get_cn_stock_valuation(symbol)
    except Exception as e:
        print(f"  ⚠ {symbol} 估值获取失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 持仓分析
# ---------------------------------------------------------------------------
@dataclass
class PositionAnalysis:
    """单只持仓的分析结果"""
    symbol: str
    name: str
    market: str
    cost_basis: float
    shares: float
    current_price: float | None
    market_value: float | None     # 持仓市值
    pnl: float | None              # 盈亏金额
    pnl_pct: float | None          # 累计盈亏比例 %（成本价 vs 现价）
    change_pct: float | None       # 今日涨跌幅 %
    pe_ttm: float | None
    pb: float | None
    pe_comment: str
    pct_in_52w: float | None       # 52 周区间位置 %
    target_price: float | None
    stop_loss: float | None
    strategy: str
    notes: str
    source: str
    # 信号
    signals: list[str]             # 买卖信号


def analyze_position(row: pd.Series) -> PositionAnalysis:
    """分析单只持仓"""
    symbol = row["symbol"]
    market = row.get("market", "CN")
    cost = row.get("cost_basis", 0) or 0
    shares = row.get("shares", 0) or 0
    target = row.get("target_price") or None
    stop = row.get("stop_loss") or None
    strategy = row.get("strategy", "")
    notes = row.get("notes", "")
    name = row.get("name", symbol)

    val = get_valuation(symbol, market)

    # 默认值
    price = market_value = pnl = pnl_pct = change_pct = None
    pe_ttm = pb = pct_in_52w = None
    pe_comment = ""
    source = ""
    signals = []

    if val and val.price:
        price = val.price
        market_value = price * shares
        if cost > 0:
            pnl = market_value - cost * shares
            pnl_pct = (price / cost - 1) * 100
        pe_ttm = val.pe_ttm
        pb = val.pb
        pct_in_52w = val.pct_in_52w
        pe_comment = val.pe_comment
        source = val.source
        # 今日涨跌（腾讯 vals[32]，区别于累计浮亏 pnl_pct）
        change_pct = val.extra.get("change_pct")

        # ---- 信号生成 ----
        # 1. 盈亏信号
        if pnl_pct is not None:
            if pnl_pct > 20:
                signals.append(f"📈 浮盈 {pnl_pct:.1f}%，考虑是否部分止盈")
            elif pnl_pct < -10:
                signals.append(f"📉 浮亏 {pnl_pct:.1f}%，关注止损线")

        # 2. 目标价信号
        if target and price >= target:
            signals.append(f"🎯 已达目标价 {target}，考虑减仓/止盈")

        # 3. 止损信号
        if stop and price <= stop:
            signals.append(f"🛑 已触及止损价 {stop}，建议立即止损")

        # 4. 52 周位置信号
        if pct_in_52w is not None:
            if pct_in_52w > 85:
                signals.append(f"⚠️ 52周高位 ({pct_in_52w:.0f}%)，注意回调风险")
            elif pct_in_52w < 15:
                signals.append(f"💎 52周低位 ({pct_in_52w:.0f}%)，可能是加仓机会")

        # 5. 估值分位数信号
        if val.pe_pct_rank is not None:
            if val.pe_pct_rank > 80:
                signals.append(f"估值偏高 (PE {val.pe_pct_rank:.0f}%分位)")
            elif val.pe_pct_rank < 25:
                signals.append(f"估值偏低 (PE {val.pe_pct_rank:.0f}%分位)")

    else:
        signals.append("❌ 无法获取实时行情")

    return PositionAnalysis(
        symbol=symbol, name=name or (val.name if val else symbol),
        market=market, cost_basis=cost, shares=shares,
        current_price=price, market_value=market_value,
        pnl=pnl, pnl_pct=pnl_pct, change_pct=change_pct,
        pe_ttm=pe_ttm, pb=pb, pe_comment=pe_comment,
        pct_in_52w=pct_in_52w, target_price=target, stop_loss=stop,
        strategy=strategy, notes=notes, source=source, signals=signals,
    )


# ---------------------------------------------------------------------------
# 自选分析
# ---------------------------------------------------------------------------
@dataclass
class WatchlistAnalysis:
    """单只自选的分析结果"""
    symbol: str
    name: str
    market: str
    sector: str
    reason: str
    alert_price: float | None
    current_price: float | None
    pe_ttm: float | None
    pb: float | None
    pe_comment: str
    pct_in_52w: float | None
    source: str
    notes: str
    signals: list[str]


def analyze_watchlist_item(row: pd.Series) -> WatchlistAnalysis:
    """分析单只自选股"""
    symbol = row["symbol"]
    market = row.get("market", "CN")
    sector = row.get("sector", "")
    reason = row.get("reason", "")
    alert = row.get("alert_price") or None
    notes = row.get("notes", "")
    name = row.get("name", symbol)

    val = get_valuation(symbol, market)

    price = pe_ttm = pb = pct_in_52w = None
    pe_comment = ""
    source = ""
    signals = []

    if val and val.price:
        price = val.price
        pe_ttm = val.pe_ttm
        pb = val.pb
        pct_in_52w = val.pct_in_52w
        pe_comment = val.pe_comment
        source = val.source
        name = val.name or name

        # ---- 信号 ----
        # 1. 到价提醒
        if alert and price <= alert:
            signals.append(f"🔔 已到达提醒价 {alert}，可以关注建仓")

        # 2. 52 周低位
        if pct_in_52w is not None and pct_in_52w < 20:
            signals.append(f"💎 52周低位 ({pct_in_52w:.0f}%)，处于底部区间")

        # 3. 估值偏低
        if val.pe_pct_rank is not None and val.pe_pct_rank < 30:
            signals.append(f"估值偏低 (PE {val.pe_pct_rank:.0f}%分位)，值得关注")

        # 4. 无信号
        if not signals:
            signals.append("暂无触发信号")
    else:
        signals.append("❌ 无法获取实时行情")

    return WatchlistAnalysis(
        symbol=symbol, name=name, market=market,
        sector=sector, reason=reason, alert_price=alert,
        current_price=price, pe_ttm=pe_ttm, pb=pb,
        pe_comment=pe_comment, pct_in_52w=pct_in_52w,
        source=source, notes=notes, signals=signals,
    )


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------
def _currency(market: str) -> str:
    return "$" if market.upper() == "US" else "¥"


def fmt_portfolio_report(positions: list[PositionAnalysis]) -> str:
    """生成持仓分析 Markdown 报告"""
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 📊 持仓分析报告",
        f"",
        f"> 生成时间: {today}",
        f"",
    ]

    # 汇总表
    total_cost = 0
    total_value = 0
    total_pnl = 0

    lines.append("## 汇总")
    lines.append("")
    lines.append(f"{'代码':<8} {'名称':<10} {'成本价':>8} {'股数':>6} {'现价':>8} {'今日%':>7} "
                 f"{'盈亏%':>8} {'市值':>12} {'PE':>8} {'52周位':>8} {'信号'}")
    lines.append("-" * 103)

    for p in positions:
        cur = _currency(p.market)
        cost_str = f"{cur}{p.cost_basis:.2f}" if p.cost_basis else "N/A"
        price_str = f"{cur}{p.current_price:.2f}" if p.current_price else "N/A"
        pnl_str = f"{p.pnl_pct:+.1f}%" if p.pnl_pct is not None else "N/A"
        mv_str = f"{cur}{p.market_value:,.0f}" if p.market_value else "N/A"
        pe_str = f"{p.pe_ttm:.1f}" if p.pe_ttm else "N/A"
        w52_str = f"{p.pct_in_52w:.0f}%" if p.pct_in_52w is not None else "N/A"
        sig_str = p.signals[0] if p.signals else ""
        shares_str = f"{int(p.shares)}" if p.shares else ""
        today_str = f"{p.change_pct:+.1f}%" if p.change_pct is not None else ""

        lines.append(
            f"{p.symbol:<8} {p.name:<10} {cost_str:>8} {shares_str:>6} {price_str:>8} {today_str:>7} "
            f"{pnl_str:>8} {mv_str:>12} {pe_str:>8} {w52_str:>8} {sig_str}"
        )

        if p.cost_basis and p.shares:
            total_cost += p.cost_basis * p.shares
        if p.market_value:
            total_value += p.market_value
        if p.pnl is not None:
            total_pnl += p.pnl

    lines.append("-" * 90)
    if total_cost > 0:
        total_pnl_pct = (total_value / total_cost - 1) * 100
        lines.append(
            f"{'合计':<8} {'':<10} {'':>8} {'':>8} "
            f"{total_pnl_pct:+.1f}% {total_value:,.0f} {'':>8} {'':>8}"
        )
    lines.append("")

    # 每只股票详情
    lines.append("## 逐只分析")
    lines.append("")

    for p in positions:
        cur = _currency(p.market)
        tag = "美股" if p.market == "US" else "A股"
        lines.append(f"### {p.name} ({p.symbol}) [{tag}]  `[数据源: {p.source}]`")
        lines.append("")
        lines.append(f"- 成本价: {cur}{p.cost_basis:.2f}  |  现价: "
                     f"{cur}{p.current_price:.2f}" if p.current_price else f"- 成本价: {cur}{p.cost_basis:.2f}  |  现价: N/A")
        if p.pnl is not None:
            emoji = "🟢" if p.pnl >= 0 else "🔴"
            lines.append(f"- {emoji} 盈亏: {cur}{p.pnl:+,.2f} ({p.pnl_pct:+.1f}%)")
        if p.shares:
            lines.append(f"- 持仓股数: {int(p.shares)} 股")
        if p.market_value:
            lines.append(f"- 持仓市值: {cur}{p.market_value:,.0f}")
        if p.change_pct is not None:
            lines.append(f"- 今日涨跌: {p.change_pct:+.2f}%")
        if p.pe_ttm:
            lines.append(f"- PE(TTM): {p.pe_ttm:.1f}  {p.pe_comment}")
        if p.pb:
            lines.append(f"- PB: {p.pb:.2f}")
        if p.pct_in_52w is not None:
            lines.append(f"- 52周区间: {p.pct_in_52w:.0f}%")
        if p.target_price and not pd.isna(p.target_price):
            lines.append(f"- 🎯 目标价: {cur}{p.target_price:.2f}")
        if p.stop_loss and not pd.isna(p.stop_loss):
            lines.append(f"- 🛑 止损价: {cur}{p.stop_loss:.2f}")
        if p.strategy:
            lines.append(f"- 策略: {p.strategy}")
        if p.notes:
            lines.append(f"- 备注: {p.notes}")
        if p.signals:
            lines.append(f"- **信号**: {' | '.join(p.signals)}")
        lines.append("")

    return "\n".join(lines)


def fmt_watchlist_report(items: list[WatchlistAnalysis]) -> str:
    """生成自选分析 Markdown 报告"""
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 👀 自选分析报告",
        f"",
        f"> 生成时间: {today}",
        f"",
    ]

    lines.append("## 汇总")
    lines.append("")
    lines.append(f"{'代码':<8} {'名称':<10} {'板块':<14} {'现价':>8} "
                 f"{'PE':>8} {'52周位':>8} {'估值':>14} {'信号'}")
    lines.append("-" * 95)

    for w in items:
        cur = _currency(w.market)
        price_str = f"{cur}{w.current_price:.2f}" if w.current_price else "N/A"
        pe_str = f"{w.pe_ttm:.1f}" if w.pe_ttm else "N/A"
        w52_str = f"{w.pct_in_52w:.0f}%" if w.pct_in_52w is not None else "N/A"
        val_str = w.pe_comment or "N/A"
        sig_str = w.signals[0] if w.signals else ""

        lines.append(
            f"{w.symbol:<8} {w.name:<10} {w.sector:<14} {price_str:>8} "
            f"{pe_str:>8} {w52_str:>8} {val_str:>14} {sig_str}"
        )

    lines.append("")

    # 有信号的股票详情
    alerted = [w for w in items if any("🔔" in s or "💎" in s for s in w.signals)]
    if alerted:
        lines.append("## ⚡ 触发信号的股票")
        lines.append("")
        for w in alerted:
            cur = _currency(w.market)
            lines.append(f"### {w.name} ({w.symbol})")
            lines.append("")
            lines.append(f"- 现价: {cur}{w.current_price:.2f}" if w.current_price else "- 现价: N/A")
            if w.pe_ttm:
                lines.append(f"- PE(TTM): {w.pe_ttm:.1f}  {w.pe_comment}")
            if w.alert_price:
                lines.append(f"- 提醒价: {cur}{w.alert_price:.2f}")
            if w.reason:
                lines.append(f"- 关注理由: {w.reason}")
            lines.append(f"- **信号**: {' | '.join(w.signals)}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="持仓/自选分析工具")
    parser.add_argument("--watchlist", action="store_true", help="分析自选列表")
    parser.add_argument("--all", action="store_true", help="分析持仓+自选")
    parser.add_argument("--output", "-o", type=str, help="输出到文件路径")
    args = parser.parse_args()

    do_portfolio = not args.watchlist or args.all
    do_watchlist = args.watchlist or args.all
    output_parts = []

    if do_portfolio:
        print("📋 正在分析持仓...")
        df = load_portfolio()
        print(f"   共 {len(df)} 只持仓")
        positions = []
        for _, row in df.iterrows():
            print(f"   → {row['symbol']} ({row.get('market', 'CN')}) ...", end=" ", flush=True)
            analysis = analyze_position(row)
            positions.append(analysis)
            print("✓" if analysis.current_price else "✗")

        report = fmt_portfolio_report(positions)
        output_parts.append(report)
        print(report)

    if do_watchlist:
        print("\n👀 正在分析自选...")
        df = load_watchlist()
        print(f"   共 {len(df)} 只自选")
        items = []
        for _, row in df.iterrows():
            print(f"   → {row['symbol']} ({row.get('market', 'CN')}) ...", end=" ", flush=True)
            analysis = analyze_watchlist_item(row)
            items.append(analysis)
            print("✓" if analysis.current_price else "✗")

        report = fmt_watchlist_report(items)
        output_parts.append(report)
        print(report)

    # 输出到文件
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n\n---\n\n".join(output_parts), encoding="utf-8")
        print(f"\n📝 报告已保存到: {out_path}")


if __name__ == "__main__":
    main()
