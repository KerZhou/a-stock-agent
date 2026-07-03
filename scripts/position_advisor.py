"""
建仓建议引擎 — 估值锚定 + 安全边际 + 分批建仓计划

对单只股票输出"买不买、买多少、什么价买"的具体建议。
支持 A股（同花顺一致预期 + 腾讯行情）和美股（yfinance）。

逻辑:
  1. PE历史分位数 + 一致预期EPS → 推算合理价格区间
  2. 合理价格 × 0.7~0.8 = 买入区间（格雷厄姆安全边际原则）
  3. 分批: 30%进入区间买 → 30%跌10%加 → 40%跌20%或催化剂加
  4. 目标价: 合理估值 × 1.1
  5. 止损价: 加权成本 × 0.85

用法:
  python scripts/position_advisor.py 603011                  # 单只A股
  python scripts/position_advisor.py INTC -m US              # 单只美股
  python scripts/position_advisor.py --watchlist              # 自选全部
  python scripts/position_advisor.py --portfolio              # 持仓全部
  python scripts/position_advisor.py 603011 -o advice.md     # 保存报告
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

# 把 scripts/ 加入 path 以便 import valuation_compare
sys.path.insert(0, str(Path(__file__).parent))
from valuation_compare import (
    ValuationData,
    _delay,
    _pct_rank_comment,
    get_cn_stock_valuation,
    get_us_stock_valuation,
)

DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class FairValueEstimate:
    """合理估值估算结果"""
    method: str              # "pe_percentile" | "graham" | "combined"
    fair_low: float          # 保守估值
    fair_mid: float          # 中性估值
    fair_high: float         # 乐观估值
    confidence: str          # "high" | "medium" | "low"
    details: dict = field(default_factory=dict)


@dataclass
class StagedPlan:
    """分批建仓计划"""
    buy_zone_high: float     # 买入区间上沿
    buy_zone_low: float      # 买入区间下沿
    stage1_price: float      # 第一批触发价
    stage1_pct: float        # 30%
    stage2_price: float      # 第二批触发价
    stage2_pct: float        # 30%
    stage3_price: float      # 第三批触发价
    stage3_pct: float        # 40%
    weighted_cost: float     # 加权平均成本
    target_price: float      # 目标价
    stop_loss: float         # 止损价
    risk_reward: float       # 风险回报比


@dataclass
class PositionAdvice:
    """单只股票的建仓建议"""
    symbol: str
    name: str
    market: str              # "US" | "CN"
    currency: str            # "$" | "¥"
    current_price: float | None
    source: str
    # 估值快照
    pe_ttm: float | None
    pe_pct_rank: float | None
    pe_comment: str
    pb: float | None
    pct_in_52w: float | None
    high_52w: float | None
    low_52w: float | None
    # EPS 预测
    eps_current: float | None
    eps_forecast: float | None
    growth_rate: float | None
    analyst_count: int
    # 估值 & 计划
    fair_value: FairValueEstimate | None
    staged_plan: StagedPlan | None
    # 已有持仓
    existing_shares: float | None
    existing_cost: float | None
    # 结论
    recommendation: str      # "强烈推荐" | "可以建仓" | "观望" | "不建议"
    signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EPS 预测获取
# ---------------------------------------------------------------------------
def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """
    同花顺机构一致预期EPS。
    直连 basic.10jqka.com.cn，解析HTML表格。
    返回 DataFrame: 年度, 预测机构数, 最小值, 均值, 最大值
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36",
        "Referer": "https://basic.10jqka.com.cn/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        dfs = pd.read_html(StringIO(r.text))
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                return df
        return dfs[0] if dfs else pd.DataFrame()
    except Exception as e:
        print(f"    ⚠ 同花顺一致预期获取失败: {e}")
        return pd.DataFrame()


def get_cn_eps_forecast(symbol: str) -> dict:
    """
    A股 EPS 预测（同花顺一致预期）。

    Returns:
        {
            "eps_current": float | None,   # 当年/最近 EPS
            "eps_forecast": float | None,  # 明年一致预期 EPS
            "analyst_count": int,
            "growth_rate": float | None,   # (forecast/current - 1)
            "source": str,
        }
    """
    result = {
        "eps_current": None,
        "eps_forecast": None,
        "analyst_count": 0,
        "growth_rate": None,
        "source": "",
    }

    df = _ths_eps_forecast(symbol)
    if df.empty or len(df.columns) < 3:
        return result

    try:
        cols = [str(c) for c in df.columns]

        # 定位列名: 均值(一致预期EPS)、预测机构数
        mean_col = None
        analyst_col = None
        for c in cols:
            if "均值" in c:
                mean_col = c
            if "机构" in c:
                analyst_col = c

        rows = list(df.iterrows())

        # 第一行 = 最近年度 (当年EPS)
        if len(rows) >= 1:
            _, r0 = rows[0]
            # 取均值列
            if mean_col and mean_col in r0.index:
                val = pd.to_numeric(r0[mean_col], errors="coerce")
                if pd.notna(val):
                    result["eps_current"] = float(val)
            # 取机构数
            if analyst_col and analyst_col in r0.index:
                val = pd.to_numeric(r0[analyst_col], errors="coerce")
                if pd.notna(val):
                    result["analyst_count"] = int(val)

        # 第二行 = 下一年度 (预期EPS)
        if len(rows) >= 2:
            _, r1 = rows[1]
            if mean_col and mean_col in r1.index:
                val = pd.to_numeric(r1[mean_col], errors="coerce")
                if pd.notna(val):
                    result["eps_forecast"] = float(val)

        # 计算增长率
        cur = result["eps_current"]
        nxt = result["eps_forecast"]
        if cur and nxt and cur > 0:
            result["growth_rate"] = (nxt / cur - 1)

        result["source"] = "同花顺一致预期"
    except Exception as e:
        print(f"    ⚠ 解析同花顺EPS表格失败: {e}")

    return result


def get_us_eps_forecast(symbol: str) -> dict:
    """
    美股 EPS 预测（yfinance）。

    Returns: 同 get_cn_eps_forecast() 格式
    """
    import yfinance as yf

    result = {
        "eps_current": None,
        "eps_forecast": None,
        "analyst_count": 0,
        "growth_rate": None,
        "source": "",
    }

    try:
        _delay()
        ticker = yf.Ticker(symbol)

        # 尝试获取 forwardEps
        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            pass

        result["eps_forecast"] = info.get("forwardEps")
        result["eps_current"] = info.get("trailingEps")

        # 从 earnings_history 计算增长率
        try:
            _delay()
            eps_hist = ticker.earnings_history
            if eps_hist is not None and not eps_hist.empty:
                # 按年度排序取最近2年
                eps_sorted = eps_hist.sort_index(ascending=False)
                if len(eps_sorted) >= 2:
                    recent = eps_sorted.iloc[0].get("epsActual", eps_sorted.iloc[0].iloc[0])
                    prev = eps_sorted.iloc[1].get("epsActual", eps_sorted.iloc[1].iloc[0])
                    try:
                        recent = float(recent)
                        prev = float(prev)
                        if prev > 0:
                            # 年化增长率（可能只是季度数据）
                            hist_growth = recent / prev - 1
                            if result["growth_rate"] is None:
                                result["growth_rate"] = hist_growth
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

        # 如果没有 forwardEps 但有历史增长率，估算
        if result["eps_forecast"] is None and result["eps_current"] and result["growth_rate"]:
            result["eps_forecast"] = result["eps_current"] * (1 + result["growth_rate"])

        # 从 growth_rate 反推（如果前面没算到）
        cur = result["eps_current"]
        nxt = result["eps_forecast"]
        if cur and nxt and cur > 0 and result["growth_rate"] is None:
            result["growth_rate"] = (nxt / cur - 1)

        result["source"] = "yfinance"
    except Exception as e:
        print(f"    ⚠ yfinance EPS获取失败: {e}")

    return result


# ---------------------------------------------------------------------------
# 估值获取 (复用 valuation_compare)
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
# 合理估值计算
# ---------------------------------------------------------------------------
def calc_fair_value_pe_percentile(
    val: ValuationData,
    eps_forecast: float | None,
) -> FairValueEstimate | None:
    """PE百分位法: 历史PE中位数 × 预期EPS = 合理价格"""
    if not val.pe_percentiles or not eps_forecast or eps_forecast <= 0:
        return None

    p50 = val.pe_percentiles.get("P50")
    p25 = val.pe_percentiles.get("P25")
    p75 = val.pe_percentiles.get("P75")

    if not p50 or p50 <= 0:
        return None

    fair_mid = p50 * eps_forecast
    fair_low = (p25 or p50 * 0.7) * eps_forecast
    fair_high = (p75 or p50 * 1.3) * eps_forecast

    # 如果当前 PE 分位 > 75，用更保守的估计
    if val.pe_pct_rank and val.pe_pct_rank > 75:
        fair_mid = (p25 + p50) / 2 * eps_forecast
        fair_high = p50 * eps_forecast

    return FairValueEstimate(
        method="pe_percentile",
        fair_low=round(fair_low, 2),
        fair_mid=round(fair_mid, 2),
        fair_high=round(fair_high, 2),
        confidence="high" if val.pe_pct_rank is not None else "low",
        details={
            "历史PE中位数": round(p50, 2),
            "历史PE_25%": round(p25, 2) if p25 else None,
            "历史PE_75%": round(p75, 2) if p75 else None,
            "预期EPS": round(eps_forecast, 4),
            "PE分位": round(val.pe_pct_rank, 1) if val.pe_pct_rank else None,
        },
    )


def calc_fair_value_graham(
    eps: float | None,
    growth_rate: float | None,
) -> FairValueEstimate | None:
    """格雷厄姆公式: V = EPS × (8.5 + 2g)"""
    if not eps or eps <= 0:
        return None

    g = growth_rate if growth_rate else 0
    g = max(0, min(g, 0.25))  # 截断到 [0%, 25%]
    g_pct = g * 100  # 转为百分比用于公式

    value = eps * (8.5 + 2 * g_pct)

    return FairValueEstimate(
        method="graham",
        fair_low=round(value * 0.85, 2),
        fair_mid=round(value, 2),
        fair_high=round(value * 1.15, 2),
        confidence="medium",
        details={
            "EPS": round(eps, 4),
            "增长率": f"{g_pct:.1f}%",
            "格雷厄姆PE": round(8.5 + 2 * g_pct, 1),
        },
    )


def reconcile_fair_value(
    estimates: list[FairValueEstimate],
) -> FairValueEstimate | None:
    """综合多种估值方法，取最终合理价值"""
    valid = [e for e in estimates if e is not None]
    if not valid:
        return None

    if len(valid) == 1:
        return valid[0]

    # 多种方法: 取均值
    mids = [e.fair_mid for e in valid]
    fair_mid = sum(mids) / len(mids)
    fair_low = min(e.fair_low for e in valid)
    fair_high = max(e.fair_high for e in valid)

    # 置信度取最高
    conf_order = {"high": 3, "medium": 2, "low": 1}
    best_conf = max(valid, key=lambda e: conf_order.get(e.confidence, 0)).confidence

    # 合并 details
    details = {}
    for e in valid:
        details[f"{e.method}"] = e.details

    return FairValueEstimate(
        method="combined",
        fair_low=round(fair_low, 2),
        fair_mid=round(fair_mid, 2),
        fair_high=round(fair_high, 2),
        confidence=best_conf,
        details=details,
    )


# ---------------------------------------------------------------------------
# 分批建仓计划
# ---------------------------------------------------------------------------
SAFETY_HIGH = 0.80  # 安全边际上沿
SAFETY_LOW = 0.70   # 安全边际下沿


def calc_staged_plan(fair: FairValueEstimate) -> StagedPlan:
    """根据合理估值生成分批建仓计划"""
    buy_zone_high = round(fair.fair_mid * SAFETY_HIGH, 2)
    buy_zone_low = round(fair.fair_mid * SAFETY_LOW, 2)

    stage1_price = buy_zone_high
    stage2_price = round(stage1_price * 0.90, 2)
    stage3_price = round(stage1_price * 0.80, 2)

    weighted_cost = round(
        stage1_price * 0.30 + stage2_price * 0.30 + stage3_price * 0.40, 2
    )
    target_price = round(fair.fair_mid * 1.1, 2)
    stop_loss = round(weighted_cost * 0.85, 2)

    risk = weighted_cost - stop_loss
    reward = target_price - weighted_cost
    risk_reward = round(reward / risk, 1) if risk > 0 else 0

    return StagedPlan(
        buy_zone_high=buy_zone_high,
        buy_zone_low=buy_zone_low,
        stage1_price=stage1_price,
        stage1_pct=30,
        stage2_price=stage2_price,
        stage2_pct=30,
        stage3_price=stage3_price,
        stage3_pct=40,
        weighted_cost=weighted_cost,
        target_price=target_price,
        stop_loss=stop_loss,
        risk_reward=risk_reward,
    )


# ---------------------------------------------------------------------------
# 推荐引擎
# ---------------------------------------------------------------------------
def generate_recommendation(
    price: float | None,
    plan: StagedPlan | None,
    fair: FairValueEstimate | None,
    growth_rate: float | None,
) -> tuple[str, list[str]]:
    """生成推荐等级和信号列表"""
    if not price:
        return "无法判断", ["❌ 无实时行情，无法生成建议"]

    signals = []

    if not plan or not fair:
        return "无法估值", ["⚠️ 数据不足，无法计算合理估值和建仓计划"]

    # ---- 价格位置信号 ----
    if price <= plan.buy_zone_low:
        signals.append(f"💎 当前价处于买入区间下方，极具安全边际")
    elif price <= plan.buy_zone_high:
        signals.append(f"📉 当前价处于买入区间内，可以开始建仓")
    elif price <= fair.fair_mid:
        pct_above = (price / plan.buy_zone_high - 1) * 100
        signals.append(f"⏳ 当前价高于买入区间 {pct_above:.1f}%，建议等待回调")
    else:
        pct_above = (price / fair.fair_mid - 1) * 100
        signals.append(f"⚠️ 当前价高于合理估值 {pct_above:.1f}%，不建议追高")

    # ---- 目标价信号 ----
    upside = (plan.target_price / price - 1) * 100
    signals.append(f"🎯 目标价 {plan.target_price:.2f} (上涨空间 {upside:+.1f}%)")

    # ---- 止损信号 ----
    downside = (plan.stop_loss / price - 1) * 100
    signals.append(f"🛑 止损价 {plan.stop_loss:.2f} (最大亏损 {downside:+.1f}%)")

    # ---- 风险回报比 ----
    if plan.risk_reward >= 3:
        signals.append(f"📊 风险回报比 1:{plan.risk_reward:.1f} ✅ 优秀")
    elif plan.risk_reward >= 2:
        signals.append(f"📊 风险回报比 1:{plan.risk_reward:.1f} — 良好")
    else:
        signals.append(f"📊 风险回报比 1:{plan.risk_reward:.1f} ⚠️ 偏低")

    # ---- 增长信号 ----
    if growth_rate and growth_rate > 0.2:
        signals.append(f"📈 盈利高增长 {growth_rate*100:+.1f}%，成长型标的")
    elif growth_rate is not None and growth_rate < 0:
        signals.append(f"⚠️ 盈利负增长 {growth_rate*100:+.1f}%，估值模型可靠性降低")

    # ---- 推荐等级 ----
    if price < plan.buy_zone_low and (growth_rate is None or growth_rate > 0):
        rec = "强烈推荐"
    elif price <= plan.buy_zone_high:
        rec = "可以建仓"
    elif price <= fair.fair_mid:
        rec = "观望"
    else:
        rec = "不建议"

    return rec, signals


# ---------------------------------------------------------------------------
# 持仓检测
# ---------------------------------------------------------------------------
def check_existing_position(symbol: str, market: str) -> dict:
    """检查 portfolio.csv 中是否已有该股票持仓"""
    path = DATA_DIR / "portfolio.csv"
    if not path.exists():
        return {"shares": None, "cost": None}

    try:
        df = pd.read_csv(path, dtype=str)
        match = df[
            (df["symbol"] == symbol) &
            (df["market"].str.upper() == market.upper())
        ]
        if match.empty:
            return {"shares": None, "cost": None}

        row = match.iloc[0]
        shares = pd.to_numeric(row.get("shares"), errors="coerce")
        cost = pd.to_numeric(row.get("cost_basis"), errors="coerce")
        return {"shares": shares, "cost": cost}
    except Exception:
        return {"shares": None, "cost": None}


# ---------------------------------------------------------------------------
# 主编排函数
# ---------------------------------------------------------------------------
def advise_single_stock(symbol: str, market: str) -> PositionAdvice:
    """对单只股票生成完整建仓建议"""
    tag = "美股" if market.upper() == "US" else "A股"
    print(f"   → {symbol} [{tag}] ...", end=" ", flush=True)

    # 1. 获取估值数据
    val = get_valuation(symbol, market)

    # 默认值
    name = symbol
    price = None
    source = ""
    pe_ttm = pe_pct_rank = pb = pct_in_52w = None
    high_52w = low_52w = None
    pe_comment = ""

    if val:
        name = val.name or name
        price = val.price
        source = val.source
        pe_ttm = val.pe_ttm
        pe_pct_rank = val.pe_pct_rank
        pe_comment = val.pe_comment
        pb = val.pb
        pct_in_52w = val.pct_in_52w
        high_52w = val.high_52w
        low_52w = val.low_52w

    # 2. 获取 EPS 预测
    if market.upper() == "US":
        eps_data = get_us_eps_forecast(symbol)
    else:
        eps_data = get_cn_eps_forecast(symbol)

    eps_current = eps_data.get("eps_current")
    eps_forecast = eps_data.get("eps_forecast")
    growth_rate = eps_data.get("growth_rate")
    analyst_count = eps_data.get("analyst_count", 0)

    # 3. 合理估值计算
    estimates = []

    # 方法一: PE 百分位法
    fv_pe = calc_fair_value_pe_percentile(val, eps_forecast)
    if fv_pe:
        estimates.append(fv_pe)

    # 方法二: 格雷厄姆公式
    graham_eps = eps_forecast or eps_current
    fv_graham = calc_fair_value_graham(graham_eps, growth_rate)
    if fv_graham:
        estimates.append(fv_graham)

    # 方法三 (备选): 如果没有 EPS 但有 PE 和价格，反推
    if not estimates and val and val.pe_ttm and val.pe_ttm > 0 and price:
        # 用当前价格和PE反推EPS，再用历史PE中位数重估
        implied_eps = price / val.pe_ttm
        fv_pe_fallback = calc_fair_value_pe_percentile(val, implied_eps)
        if fv_pe_fallback:
            fv_pe_fallback.confidence = "low"
            estimates.append(fv_pe_fallback)

    fair = reconcile_fair_value(estimates)

    # 4. 分批建仓计划
    plan = calc_staged_plan(fair) if fair else None

    # 5. 推荐信号
    rec, signals = generate_recommendation(price, plan, fair, growth_rate)

    # 6. 检查已有持仓
    existing = check_existing_position(symbol, market)
    existing_shares = existing["shares"]
    existing_cost = existing["cost"]

    # 已有持仓信号
    if existing_shares and existing_cost:
        cur = "$" if market.upper() == "US" else "¥"
        signals.append(
            f"⏳ 已持有 {existing_shares:.0f} 股，成本 {cur}{existing_cost:.2f}"
        )

    # 数据缺失警告
    if not eps_forecast and not eps_current:
        signals.append("⚠️ 无EPS预测数据，估值仅供参考")
    if analyst_count < 3 and analyst_count > 0:
        signals.append(f"⚠️ 仅 {analyst_count} 家机构覆盖，预期可靠性较低")
    if fair and fair.confidence == "low":
        signals.append("⚠️ 估值置信度低，建议结合其他分析方法")

    currency = "$" if market.upper() == "US" else "¥"

    print("✓" if price else "✗")

    return PositionAdvice(
        symbol=symbol,
        name=name,
        market=market,
        currency=currency,
        current_price=price,
        source=source,
        pe_ttm=pe_ttm,
        pe_pct_rank=pe_pct_rank,
        pe_comment=pe_comment,
        pb=pb,
        pct_in_52w=pct_in_52w,
        high_52w=high_52w,
        low_52w=low_52w,
        eps_current=eps_current,
        eps_forecast=eps_forecast,
        growth_rate=growth_rate,
        analyst_count=analyst_count,
        fair_value=fair,
        staged_plan=plan,
        existing_shares=existing_shares,
        existing_cost=existing_cost,
        recommendation=rec,
        signals=signals,
    )


# ---------------------------------------------------------------------------
# 报告格式化
# ---------------------------------------------------------------------------
def _f(v: float | None, fmt: str = ".2f") -> str:
    """安全格式化浮点数"""
    return f"{v:{fmt}}" if v is not None else "N/A"


def fmt_position_advice(advice: PositionAdvice) -> str:
    """单只股票的建仓建议 Markdown"""
    cur = advice.currency
    tag = "美股" if advice.market == "US" else "A股"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 🎯 建仓建议: {advice.name} ({advice.symbol}) [{tag}]",
        "",
        f"> 生成时间: {now} | 数据源: {advice.source or 'N/A'}",
        "",
    ]

    # ---- 基本面速览 ----
    lines.append("## 基本面速览")
    lines.append("")
    if advice.current_price:
        lines.append(f"- 当前价格: **{cur}{advice.current_price:.2f}**")
    else:
        lines.append("- 当前价格: N/A")

    pe_parts = []
    if advice.pe_ttm is not None:
        pe_parts.append(f"PE(TTM) {advice.pe_ttm:.1f}")
    if advice.pe_comment:
        pe_parts.append(advice.pe_comment)
    if pe_parts:
        lines.append(f"- 估值: {' | '.join(pe_parts)}")
    if advice.pb is not None:
        lines.append(f"- PB: {advice.pb:.2f}")
    if advice.pe_pct_rank is not None:
        lines.append(f"- PE历史分位: {advice.pe_pct_rank:.0f}%")

    if advice.pct_in_52w is not None:
        range_str = ""
        if advice.high_52w and advice.low_52w:
            range_str = f" (高{cur}{advice.high_52w:.2f} / 低{cur}{advice.low_52w:.2f})"
        lines.append(f"- 52周区间: {advice.pct_in_52w:.0f}%{range_str}")

    if advice.existing_shares and advice.existing_cost:
        lines.append(
            f"- ⏳ 已持有 {advice.existing_shares:.0f} 股，"
            f"成本 {cur}{advice.existing_cost:.2f}"
        )
    lines.append("")

    # ---- 一致预期 EPS ----
    if advice.eps_current or advice.eps_forecast:
        lines.append("## 一致预期EPS")
        lines.append("")
        if advice.eps_current:
            lines.append(f"- 当年/最新EPS: {cur}{advice.eps_current:.4f}")
        if advice.eps_forecast:
            lines.append(
                f"- 明年一致预期EPS: {cur}{advice.eps_forecast:.4f}"
                f" ({advice.analyst_count}家机构覆盖)"
            )
        if advice.growth_rate is not None:
            lines.append(f"- 隐含增速: {advice.growth_rate*100:+.1f}%")
        lines.append("")

    # ---- 合理估值 ----
    if advice.fair_value:
        fv = advice.fair_value
        lines.append("## 合理估值")
        lines.append("")
        lines.append(f"- 估值方法: {fv.method}")
        lines.append(
            f"- 合理价格区间: **{cur}{fv.fair_low:.2f} ~ "
            f"{cur}{fv.fair_high:.2f}** (中值 {cur}{fv.fair_mid:.2f})"
        )
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
            fv.confidence, "⚪"
        )
        lines.append(f"- 置信度: {conf_emoji} {fv.confidence}")

        # 显示各方法详情
        if fv.details:
            for key, val in fv.details.items():
                if isinstance(val, dict):
                    lines.append(f"  - {key}:")
                    for k2, v2 in val.items():
                        if v2 is not None:
                            lines.append(f"    - {k2}: {v2}")
                elif val is not None:
                    lines.append(f"  - {key}: {val}")
        lines.append("")

    # ---- 分批建仓计划 ----
    if advice.staged_plan:
        sp = advice.staged_plan
        lines.append("## 分批建仓计划")
        lines.append("")
        lines.append(
            f"买入区间: {cur}{sp.buy_zone_low:.2f} ~ {cur}{sp.buy_zone_high:.2f}"
        )
        lines.append("")
        lines.append("| 阶段 | 触发价 | 配置比例 | 说明 |")
        lines.append("|------|--------|----------|------|")
        lines.append(
            f"| 第一批 | {cur}{sp.stage1_price:.2f} | 30% | "
            f"价格进入买入区间 |"
        )
        lines.append(
            f"| 第二批 | {cur}{sp.stage2_price:.2f} | 30% | "
            f"回调10%加仓 |"
        )
        lines.append(
            f"| 第三批 | {cur}{sp.stage3_price:.2f} | 40% | "
            f"回调20%或催化剂出现 |"
        )
        lines.append("")

        lines.append(f"- 加权平均成本: {cur}{sp.weighted_cost:.2f}")
        lines.append(
            f"- 🎯 目标价: {cur}{sp.target_price:.2f}"
            f" (上涨空间 {(sp.target_price / advice.current_price - 1) * 100:+.1f}%)"
            if advice.current_price
            else f"- 🎯 目标价: {cur}{sp.target_price:.2f}"
        )
        lines.append(
            f"- 🛑 止损价: {cur}{sp.stop_loss:.2f}"
            f" (最大亏损 {(sp.stop_loss / advice.current_price - 1) * 100:+.1f}%)"
            if advice.current_price
            else f"- 🛑 止损价: {cur}{sp.stop_loss:.2f}"
        )
        rr_emoji = "✅" if sp.risk_reward >= 2 else "⚠️"
        lines.append(f"- 📊 风险回报比: 1:{sp.risk_reward:.1f} {rr_emoji}")
        lines.append("")

    # ---- 综合建议 ----
    lines.append("## 综合建议")
    lines.append("")

    rec_emoji = {
        "强烈推荐": "🟢🟢🟢",
        "可以建仓": "🟢",
        "观望": "🟡",
        "不建议": "🔴",
        "无法估值": "⚪",
        "无法判断": "⚪",
    }.get(advice.recommendation, "⚪")

    lines.append(f"**{rec_emoji} {advice.recommendation}**")
    lines.append("")

    for sig in advice.signals:
        lines.append(f"- {sig}")
    lines.append("")

    return "\n".join(lines)


def fmt_report(advice_list: list[PositionAdvice]) -> str:
    """生成多只股票的汇总报告"""
    if not advice_list:
        return "# 🎯 建仓建议\n\n无数据。"

    parts = []
    # 汇总表
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 🎯 建仓建议汇总",
        "",
        f"> 生成时间: {now}",
        "",
        "## 快速总览",
        "",
    ]

    lines.append(
        f"{'代码':<8} {'名称':<10} {'现价':>8} "
        f"{'PE':>8} {'估值':>10} "
        f"{'合理价':>10} {'建议':>10}"
    )
    lines.append("-" * 80)

    for a in advice_list:
        cur = a.currency
        price_str = f"{cur}{a.current_price:.2f}" if a.current_price else "N/A"
        pe_str = f"{a.pe_ttm:.1f}" if a.pe_ttm else "N/A"
        val_str = a.pe_comment[:8] if a.pe_comment else "N/A"
        fair_str = (
            f"{cur}{a.fair_value.fair_mid:.2f}" if a.fair_value else "N/A"
        )
        rec_str = a.recommendation[:6]
        lines.append(
            f"{a.symbol:<8} {a.name:<10} {price_str:>8} "
            f"{pe_str:>8} {val_str:>10} "
            f"{fair_str:>10} {rec_str:>10}"
        )

    lines.append("")
    parts.append("\n".join(lines))

    # 每只股票详情
    for a in advice_list:
        parts.append(fmt_position_advice(a))

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# 自动检测市场
# ---------------------------------------------------------------------------
def detect_market(symbol: str, override: str | None = None) -> str:
    """检测股票市场: 6位数字→CN，字母→US。override 参数优先。"""
    if override:
        return override.upper()
    if symbol.isdigit() and len(symbol) == 6:
        return "CN"
    return "US"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="建仓建议引擎 — 估值锚定 + 安全边际 + 分批建仓计划"
    )
    parser.add_argument(
        "symbols", nargs="*",
        help="股票代码 (如 603011 INTC)，支持多个",
    )
    parser.add_argument(
        "--market", "-m", default=None,
        choices=["CN", "US"],
        help="市场 (自动检测: 6位数字→CN，字母→US；-m 可覆盖)",
    )
    parser.add_argument(
        "--watchlist", action="store_true",
        help="分析自选列表中所有股票",
    )
    parser.add_argument(
        "--portfolio", action="store_true",
        help="分析持仓列表中所有股票（给出加仓/减仓建议）",
    )
    parser.add_argument(
        "--output", "-o", type=str,
        help="输出到文件路径",
    )
    args = parser.parse_args()

    # 确定要分析的股票列表
    stock_list: list[tuple[str, str]] = []  # [(symbol, market), ...]

    # 从命令行参数
    for sym in args.symbols:
        mkt = detect_market(sym, args.market)
        stock_list.append((sym, mkt))

    # 从自选列表
    if args.watchlist:
        wl_path = DATA_DIR / "watchlist.csv"
        if wl_path.exists():
            df = pd.read_csv(wl_path, dtype=str)
            for _, row in df.iterrows():
                sym = row["symbol"]
                mkt = detect_market(sym, row.get("market") or args.market)
                if (sym, mkt) not in stock_list:
                    stock_list.append((sym, mkt))
        else:
            print(f"⚠ 自选文件不存在: {wl_path}")

    # 从持仓列表
    if args.portfolio:
        pf_path = DATA_DIR / "portfolio.csv"
        if pf_path.exists():
            df = pd.read_csv(pf_path, dtype=str)
            for _, row in df.iterrows():
                sym = row["symbol"]
                mkt = detect_market(sym, row.get("market") or args.market)
                if (sym, mkt) not in stock_list:
                    stock_list.append((sym, mkt))
        else:
            print(f"⚠ 持仓文件不存在: {pf_path}")

    if not stock_list:
        print("请指定股票代码，或使用 --watchlist / --portfolio")
        parser.print_help()
        return

    # 逐只分析
    print(f"🎯 正在生成建仓建议 ({len(stock_list)} 只)...")
    advice_list = []
    for sym, mkt in stock_list:
        advice = advise_single_stock(sym, mkt)
        advice_list.append(advice)

    # 生成报告
    report = fmt_report(advice_list)
    print()
    print(report)

    # 输出到文件
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n📝 报告已保存到: {out_path}")


if __name__ == "__main__":
    main()
