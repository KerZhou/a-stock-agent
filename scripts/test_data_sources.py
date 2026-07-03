"""
数据源诊断脚本 — 逐个测试每个数据源，定位问题。

用法:
  python scripts/test_data_sources.py
"""

import sys
import json
import re
import urllib.request
import requests
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.replace("/scripts/test_data_sources.py", "/scripts"))

BEIJING_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
now_bj = datetime.now(BEIJING_TZ)
today = now_bj.strftime("%Y-%m-%d")
print(f"当前北京时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"今天是: {now_bj.strftime('%A')}")
print("=" * 60)

# ---------------------------------------------------------------------------
# 1. 腾讯财经 — 大盘指数
# ---------------------------------------------------------------------------
print("\n### 1. 腾讯财经 — 大盘指数")
try:
    codes = ["sh000001", "sz399001", "sz399006", "sh000300"]
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        name = vals[1]
        price = float(vals[3]) if vals[3] else 0
        change_pct = float(vals[32]) if vals[32] else 0
        pe = float(vals[39]) if vals[39] else 0
        print(f"  {name}({code}): 价格={price}, 涨跌={change_pct:+.2f}%, PE={pe}")
        if price < 100 and code == "000001":
            print(f"  ⚠️ 上证指数异常！价格 {price} 远低于正常值(3000+)")
except Exception as e:
    print(f"  ✗ 失败: {e}")

# ---------------------------------------------------------------------------
# 2. 同花顺 — 强势股（行业涨跌数据源）
# ---------------------------------------------------------------------------
print(f"\n### 2. 同花顺 — 强势股 API")
for days_back in range(3):
    d = now_bj - timedelta(days=days_back)
    date_str = d.strftime("%Y-%m-%d")
    print(f"\n  尝试日期: {date_str} ({d.strftime('%A')})")
    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
        )
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
        errocode = data.get("errocode", -1)
        rows = data.get("data") or []
        print(f"  errocode={errocode}, 数据条数={len(rows)}")

        if rows:
            # 检查前5条的 zhangfu 字段
            has_valid = 0
            for row in rows[:10]:
                name = row.get("name", "")
                zhangfu = row.get("zhangfu", 0)
                reason = row.get("reason", "")
                print(f"    {name}: zhangfu={zhangfu}, reason={reason[:40]}")
                if zhangfu != 0:
                    has_valid += 1
            print(f"  有效数据(非零zhangfu): {has_valid}/10")
            if has_valid > 0:
                print(f"  ✅ 找到有效数据！应使用 {date_str}")
                break
    except Exception as e:
        print(f"  ✗ 失败: {e}")

# ---------------------------------------------------------------------------
# 3. 东财 — 行业排名
# ---------------------------------------------------------------------------
print(f"\n### 3. 东财 push2 — 行业排名")
try:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "5", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fs": "m:90+t:2",
        "fields": "f3,f12,f14",
    }
    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
    d = r.json()
    items = d.get("data", {}).get("diff", [])
    if items:
        for item in items[:5]:
            print(f"  {item.get('f14','')}: 涨跌幅={item.get('f3', 0):+.2f}%")
    else:
        print(f"  ✗ 无数据（可能被封）")
except Exception as e:
    print(f"  ✗ 失败: {e}")

# ---------------------------------------------------------------------------
# 4. 同花顺 — 北向资金
# ---------------------------------------------------------------------------
print(f"\n### 4. 同花顺 — 北向资金")
try:
    headers = {"User-Agent": UA, "Host": "data.hexin.cn", "Referer": "https://data.hexin.cn/"}
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    times = d.get("time", [])
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])
    print(f"  时间点数: {len(times)}")
    if times:
        print(f"  时间范围: {times[0]} ~ {times[-1]}")
        hgt_last = next((v for v in reversed(hgt) if v is not None), 0)
        sgt_last = next((v for v in reversed(sgt) if v is not None), 0)
        print(f"  沪股通: {hgt_last:+.2f}亿, 深股通: {sgt_last:+.2f}亿")
except Exception as e:
    print(f"  ✗ 失败: {e}")

# ---------------------------------------------------------------------------
# 5. baostock — A股估值
# ---------------------------------------------------------------------------
print(f"\n### 5. baostock — A股估值(603011)")
try:
    import baostock as bs
    bs.login()
    rs = bs.query_history_k_data_plus("sh.603011",
        "date,close,peTTM,pbMRQ",
        start_date=(now_bj - timedelta(days=7)).strftime("%Y-%m-%d"),
        end_date=today,
        frequency="d")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    if rows:
        for row in rows[-3:]:
            print(f"  {row[0]}: 收盘={row[1]}, PE={row[2]}, PB={row[3]}")
    else:
        print(f"  ✗ 无数据")
except Exception as e:
    print(f"  ✗ 失败: {e}")

# ---------------------------------------------------------------------------
# 6. yfinance — 美股
# ---------------------------------------------------------------------------
print(f"\n### 6. yfinance — 美股(GOOGL)")
try:
    import yfinance as yf
    t = yf.Ticker("GOOGL")
    info = t.fast_info
    print(f"  价格: ${info.last_price:.2f}")
    print(f"  52周高: ${info.year_high:.2f}, 低: ${info.year_low:.2f}")
except Exception as e:
    print(f"  ✗ 失败: {e}")

print("\n" + "=" * 60)
print("诊断完成")
