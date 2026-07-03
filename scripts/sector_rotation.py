"""
板块轮动监控 — 每日行业排名快照 + 趋势检测 + 信号生成。

数据源:
  主力: 东财 push2 行业涨跌幅排名 (可能被封)
  降级: 同花顺热点题材归因 → 反推板块热度 (稳定)

历史存储: data/sector_history.json (滚动保留 30 天)

信号类型:
  🔥 资金流入 — 连续3天排名上升
  💧 资金流出 — 连续3天排名下降
  ⬆️ 弱势转强 — 非前10进入前10
  ⬇️ 强势转弱 — 前10跌出前10

用法:
  python scripts/sector_rotation.py              # 快照 + 信号检测
  python scripts/sector_rotation.py --report     # 完整报告
  python scripts/sector_rotation.py -o reports/rotation.md
"""

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

# 统一北京时区（CI runner 默认 UTC，避免日期/时间错位）
BEIJING_TZ = timezone(timedelta(hours=8))

DATA_DIR = Path(__file__).parent.parent / "data"
HISTORY_PATH = DATA_DIR / "sector_history.json"
MAX_HISTORY_DAYS = 30

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"


# ---------------------------------------------------------------------------
# 东财防封基础设施（与 daily_review.py 相同模式）
# ---------------------------------------------------------------------------
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.0
_em_last_call = [0.0]


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


# ---------------------------------------------------------------------------
# 1. 数据获取 — 东财行业排名（主力）
# ---------------------------------------------------------------------------
def _fetch_em_industry() -> list[dict] | None:
    """
    东财 push2 全行业排名。
    返回 None 表示失败（被封/超时）。
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "100", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    headers = {"User-Agent": UA}
    try:
        r = _em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        items = d.get("data", {}).get("diff", [])
        if not items:
            return None

        rows = []
        for item in items:
            rows.append({
                "name": item.get("f14", ""),
                "code": item.get("f12", ""),
                "change_pct": item.get("f3", 0) or 0,
                "up_count": item.get("f104", 0) or 0,
                "down_count": item.get("f105", 0) or 0,
                "leader": item.get("f140", ""),
                "leader_change": item.get("f136", 0) or 0,
            })
        # 按涨跌幅降序排名（东财默认排序不保证按涨跌幅，否则「TOP10」名不副实）
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1
        return rows
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2. 数据获取 — 同花顺热点（降级方案）
# ---------------------------------------------------------------------------
def _fetch_ths_themes() -> list[dict] | None:
    """
    同花顺强势股题材归因 → 反推板块热度排名（带日期回溯）。
    当天 zhangfu 全 0（非交易日/未刷新）时自动取最近有效交易日，
    避免行业 TOP10 涨跌幅全 0。返回按题材出现频率排序的板块列表。
    """
    for days_back in range(5):
        date = (datetime.now(BEIJING_TZ) - timedelta(days=days_back)
                ).strftime("%Y-%m-%d")
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {"User-Agent": UA}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
        except Exception:
            continue
        if data.get("errocode", 0) != 0:
            continue

        rows = data.get("data") or []
        if not rows:
            continue
        # 跳过 zhangfu 全 0 的日期（非交易日/未刷新），除非已回溯到最后一天
        has_valid = any((row.get("zhangfu") or 0) != 0 for row in rows[:10])
        if not has_valid and days_back < 4:
            continue

        # 从强势股的题材归因字段提取板块标签
        tag_stocks = {}  # tag → [股票信息]
        for row in rows:
            reason = row.get("reason", "")
            if not reason:
                continue
            tags = [t.strip() for t in str(reason).split("+") if t.strip()]
            for tag in tags:
                if tag not in tag_stocks:
                    tag_stocks[tag] = []
                tag_stocks[tag].append({
                    "name": row.get("name", ""),
                    "code": row.get("code", ""),
                    "change_pct": row.get("zhangfu", 0),
                })

        # 按出现频率排序（频率=板块内强势股数量）
        sorted_tags = sorted(tag_stocks.items(), key=lambda x: -len(x[1]))

        result = []
        for i, (tag, stocks) in enumerate(sorted_tags[:50]):
            leader = stocks[0] if stocks else {}
            # 过滤掉 zhangfu=0 的数据再取平均（非交易日或未更新时为0）
            valid_chg = [s["change_pct"] for s in stocks if s.get("change_pct")]
            avg_chg = sum(valid_chg) / len(valid_chg) if valid_chg else 0
            result.append({
                "rank": i + 1,
                "name": tag,
                "code": "",
                "change_pct": round(avg_chg, 2),
                "up_count": len(stocks),
                "down_count": 0,
                "leader": leader.get("name", ""),
                "leader_change": leader.get("change_pct", 0),
            })
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# 3. 统一获取快照
# ---------------------------------------------------------------------------
def get_industry_snapshot() -> list[dict]:
    """
    获取当日行业板块排名快照，自动降级。
    返回 [{rank, name, code, change_pct, up_count, down_count, leader, leader_change}, ...]
    """
    # 主力: 东财
    data = _fetch_em_industry()
    if data:
        return data

    # 降级: 同花顺热点
    data = _fetch_ths_themes()
    if data:
        print("  (东财不可用，已降级到同花顺热点)")
        return data

    print("  [WARN] 所有板块数据源均不可用")
    return []


# ---------------------------------------------------------------------------
# 4. 历史数据管理
# ---------------------------------------------------------------------------
def load_history(history_path: Path = HISTORY_PATH) -> dict:
    """加载板块历史数据。返回 {"meta": {...}, "dates": {...}}。"""
    if not history_path.exists():
        return {"meta": {"last_updated": "", "max_days": MAX_HISTORY_DAYS}, "dates": {}}
    try:
        return json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"meta": {"last_updated": "", "max_days": MAX_HISTORY_DAYS}, "dates": {}}


def save_snapshot(
    date: str,
    data: list[dict],
    history_path: Path = HISTORY_PATH,
) -> None:
    """
    追加今日快照到历史文件，自动保留最近 N 天。
    如果同一天重复执行，覆盖该日数据。
    """
    history = load_history(history_path)

    # 写入/覆盖当日
    history["dates"][date] = data

    # 清理超过 MAX_HISTORY_DAYS 的旧数据
    all_dates = sorted(history["dates"].keys())
    if len(all_dates) > MAX_HISTORY_DAYS:
        cutoff = all_dates[-MAX_HISTORY_DAYS]
        for d in all_dates:
            if d < cutoff:
                del history["dates"][d]

    history["meta"]["last_updated"] = date
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                            encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. 趋势检测
# ---------------------------------------------------------------------------
def _get_recent_dates(history: dict, lookback: int = 5) -> list[str]:
    """获取最近 N 个有数据的日期（升序）。"""
    all_dates = sorted(history["dates"].keys())
    return all_dates[-lookback:]


def _build_rank_map(snapshot: list[dict]) -> dict[str, int]:
    """从快照构建 {板块名: 排名} 映射。"""
    return {row["name"]: row["rank"] for row in snapshot}


def detect_rotation_signals(
    history: dict,
    lookback: int = 5,
    consecutive_days: int = 3,
) -> list[dict]:
    """
    分析历史数据，检测板块轮动信号。

    Args:
        history: 历史数据 dict
        lookback: 回看天数
        consecutive_days: 连续变化天数阈值

    Returns:
        信号列表 [{type, sector, code, detail, current_rank, prev_rank, change_pct}]
    """
    dates = _get_recent_dates(history, lookback)
    if len(dates) < 2:
        return []  # 至少需要2天数据

    signals = []

    # 取今天和之前几天的排名映射
    today_map = _build_rank_map(history["dates"][dates[-1]])
    today_snapshot = history["dates"][dates[-1]]

    # --- 信号1: 连续排名变化 ---
    if len(dates) >= consecutive_days:
        check_dates = dates[-consecutive_days:]
        rank_maps = [_build_rank_map(history["dates"][d]) for d in check_dates]

        # 找出所有 check_dates 中都出现的板块
        all_sectors = set(rank_maps[0].keys())
        for rm in rank_maps[1:]:
            all_sectors &= set(rm.keys())

        for sector in all_sectors:
            ranks = [rm[sector] for rm in rank_maps]
            # 检查是否连续下降（排名数字变小 = 上升）
            rising = all(ranks[i] > ranks[i + 1] for i in range(len(ranks) - 1))
            # 检查是否连续上升（排名数字变大 = 下降）
            falling = all(ranks[i] < ranks[i + 1] for i in range(len(ranks) - 1))

            if rising:
                detail_ranks = "→".join(str(r) for r in ranks)
                current = today_snapshot[[r for r in today_snapshot if r["name"] == sector][0]["rank"] - 1] if sector in today_map else None
                # 从今日快照取 change_pct
                chg = 0
                for row in today_snapshot:
                    if row["name"] == sector:
                        chg = row.get("change_pct", 0)
                        break
                signals.append({
                    "type": "capital_inflow",
                    "sector": sector,
                    "code": "",
                    "detail": f"连续{consecutive_days}天排名上升 ({detail_ranks})",
                    "current_rank": ranks[-1],
                    "prev_rank": ranks[0],
                    "change_pct": chg,
                })

            elif falling:
                detail_ranks = "→".join(str(r) for r in ranks)
                chg = 0
                for row in today_snapshot:
                    if row["name"] == sector:
                        chg = row.get("change_pct", 0)
                        break
                signals.append({
                    "type": "capital_outflow",
                    "sector": sector,
                    "code": "",
                    "detail": f"连续{consecutive_days}天排名下降 ({detail_ranks})",
                    "current_rank": ranks[-1],
                    "prev_rank": ranks[0],
                    "change_pct": chg,
                })

    # --- 信号2: 前10进出 ---
    if len(dates) >= 2:
        prev_date = dates[-2]
        prev_map = _build_rank_map(history["dates"][prev_date])

        top10_today = {name for name, rank in today_map.items() if rank <= 10}
        top10_prev = {name for name, rank in prev_map.items() if rank <= 10}

        # 弱势转强: 之前不在前10，今天进前10
        for sector in top10_today - top10_prev:
            chg = 0
            for row in today_snapshot:
                if row["name"] == sector:
                    chg = row.get("change_pct", 0)
                    break
            prev_rank = prev_map.get(sector)
            prev_str = str(prev_rank) if prev_rank else "新上榜"
            signals.append({
                "type": "weak_to_strong",
                "sector": sector,
                "code": "",
                "detail": f"进入前10 ({prev_str}→{today_map[sector]})",
                "current_rank": today_map[sector],
                "prev_rank": prev_rank or 0,
                "change_pct": chg,
            })

        # 强势转弱: 之前在前10，今天跌出前10
        for sector in top10_prev - top10_today:
            prev_rank = prev_map.get(sector, "?")
            cur_rank = today_map.get(sector, "出榜")
            signals.append({
                "type": "strong_to_weak",
                "sector": sector,
                "code": "",
                "detail": f"跌出前10 ({prev_rank}→{cur_rank})",
                "current_rank": today_map.get(sector, 0),
                "prev_rank": prev_map.get(sector, 0),
                "change_pct": 0,
            })

    return signals


# ---------------------------------------------------------------------------
# 6. 报告格式化
# ---------------------------------------------------------------------------
def fmt_rotation_report(
    signals: list[dict],
    snapshot: list[dict],
    date: str,
    full_report: bool = False,
) -> str:
    """生成板块轮动 Markdown 报告。"""
    lines = [f"## 板块轮动 ({date})", ""]

    # 快照 TOP 10
    if snapshot:
        top10 = snapshot[:10]
        lines.append("### 今日行业 TOP 10")
        lines.append("")
        lines.append("| 排名 | 行业 | 涨跌幅 | 涨/跌 | 领涨股 |")
        lines.append("|------|------|--------|-------|--------|")
        for r in top10:
            chg = r.get("change_pct", 0)
            chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else str(chg)
            lines.append(
                f"| {r['rank']} | {r['name']} | {chg_str} "
                f"| {r.get('up_count', '?')}/{r.get('down_count', '?')} "
                f"| {r.get('leader', '')} ({r.get('leader_change', 0):+.2f}%) |"
            )
        lines.append("")

    # 轮动信号
    if not signals:
        lines.append("*暂无轮动信号（需要至少2天历史数据）*")
        lines.append("")
    else:
        # 按信号类型分组
        inflow = [s for s in signals if s["type"] == "capital_inflow"]
        outflow = [s for s in signals if s["type"] == "capital_outflow"]
        w2s = [s for s in signals if s["type"] == "weak_to_strong"]
        s2w = [s for s in signals if s["type"] == "strong_to_weak"]

        if inflow:
            lines.append("### 🔥 资金流入（连续排名上升）")
            lines.append("")
            for s in inflow:
                chg = f"{s['change_pct']:+.2f}%"
                lines.append(f"- **{s['sector']}** (现排名{s['current_rank']}) {chg} — {s['detail']}")
            lines.append("")

        if outflow:
            lines.append("### 💧 资金流出（连续排名下降）")
            lines.append("")
            for s in outflow:
                chg = f"{s['change_pct']:+.2f}%"
                lines.append(f"- **{s['sector']}** (现排名{s['current_rank']}) {chg} — {s['detail']}")
            lines.append("")

        if w2s:
            lines.append("### ⬆️ 弱势转强（进入前10）")
            lines.append("")
            for s in w2s:
                chg = f"{s['change_pct']:+.2f}%"
                lines.append(f"- **{s['sector']}** {chg} — {s['detail']}")
            lines.append("")

        if s2w:
            lines.append("### ⬇️ 强势转弱（跌出前10）")
            lines.append("")
            for s in s2w:
                lines.append(f"- **{s['sector']}** — {s['detail']}")
            lines.append("")

    # 5天趋势表（仅 --report 模式）
    if full_report and snapshot:
        history = load_history()
        dates = _get_recent_dates(history, 5)
        if len(dates) >= 2:
            lines.append("### 近5日 TOP 10 排名变化")
            lines.append("")
            header = "| 行业 |"
            sep = "|------|"
            for d in dates:
                short = d[5:]  # MM-DD
                header += f" {short} |"
                sep += "------|"
            lines.append(header)
            lines.append(sep)

            # 取今天的 TOP 10 行业名
            top10_names = [r["name"] for r in snapshot[:10]]
            for name in top10_names:
                row_str = f"| {name} |"
                for d in dates:
                    day_map = _build_rank_map(history["dates"].get(d, []))
                    rank = day_map.get(name, "-")
                    row_str += f" {rank} |"
                lines.append(row_str)
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. 主入口（供外部调用）
# ---------------------------------------------------------------------------
def run_rotation_check(date: str = None, full_report: bool = False) -> str:
    """
    板块轮动检查主入口。

    1. 获取今日快照
    2. 存入历史
    3. 检测信号
    4. 返回 Markdown 报告段

    供 daily_review.py 调用。
    """
    if date is None:
        date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    # 1. 获取快照
    print("  🔄 板块排名快照...", end=" ", flush=True)
    snapshot = get_industry_snapshot()
    print(f"✓ ({len(snapshot)} 个行业)" if snapshot else "✗")

    # 2. 存入历史
    if snapshot:
        save_snapshot(date, snapshot)

    # 3. 检测信号
    history = load_history()
    signals = detect_rotation_signals(history)
    if signals:
        print(f"  📡 轮动信号: {len(signals)} 条")

    # 4. 格式化报告
    return fmt_rotation_report(signals, snapshot, date, full_report=full_report)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="板块轮动监控")
    parser.add_argument(
        "--report", action="store_true",
        help="输出完整报告（含5日趋势表）",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="指定日期 (YYYY-MM-DD，默认今天)",
    )
    parser.add_argument(
        "--output", "-o", type=str,
        help="输出文件路径",
    )
    args = parser.parse_args()

    date = args.date or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    print(f"🔄 板块轮动监控 {date}...")

    report = run_rotation_check(date=date, full_report=args.report)
    print()
    print(report)

    if args.output:
        out_path = Path(args.output)
    else:
        reports_dir = Path(__file__).parent.parent / "reports"
        out_path = reports_dir / f"rotation-{date}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n📝 报告已保存到: {out_path}")

    # 如果有信号，尝试推送
    if args.report:
        try:
            from notify import send_notification
            signals = []
            history = load_history()
            signals = detect_rotation_signals(history)
            if signals:
                signal_summary = "板块轮动信号:\n"
                for s in signals:
                    icon = {"capital_inflow": "🔥", "capital_outflow": "💧",
                            "weak_to_strong": "⬆️", "strong_to_weak": "⬇️"}.get(s["type"], "")
                    signal_summary += f"- {icon} {s['sector']}: {s['detail']}\n"
                send_notification(f"板块轮动信号 {date}", signal_summary)
        except ImportError:
            pass


if __name__ == "__main__":
    main()
