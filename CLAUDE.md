# Stock Analysis Project

炒股分析项目，基于 conda `stock` 虚拟环境（Python 3.13）。

## 环境

- **Conda 环境**: `stock`
- **Python**: 3.13
- **激活**: `conda activate stock`
- **环境文件**: `environment.yml`（可用 `conda env create -f environment.yml` 重建）

## 📌 数据源优先级准则（重要，选源必读）

**遵循「稳定源优先，东财兜底带降级」**：

1. **优先稳定源**（不封 IP、免鉴权）：腾讯 `qt.gtimg.cn`、同花顺 `10jqka.com.cn`、百度股市通、baostock、新浪三表
2. **东财（eastmoney）最后选**：风控动态，本地此刻可用 ≠ CI 可用 ≠ 持续可用。任何东财接口**必须带 try/except 降级**（失败跳过、不影响主流程），并用 `daily_review._em_get` 节流（≥1s 间隔）
3. **每个数据需求都设计降级链**：`稳定源 → 东财兜底`，东财挂了有 fallback

## ⚠️ 数据源现状（2026-07-03 复核）

东财风控动态：`push2`/`datacenter` 仍常被封；`search-api-web`(个股新闻)/`np-weblist`(7×24快讯) 本地实测可用，但 CI 表现待验证。统一按「可用就抓、挂了降级」处理。

| 数据源 | 用途 | 稳定性 |
|--------|------|--------|
| **腾讯财经** `qt.gtimg.cn` | 实时行情(价/PE/PB/市值/涨跌停)，**不封IP** | ✅ 稳定（行情首选）|
| **同花顺** `10jqka.com.cn` | 板块/热点/北向/强势股，零鉴权 | ✅ 稳定 |
| **百度股市通** `finance.pae.baidu.com` | 概念板块/K线带MA | ✅ 稳定 |
| **baostock** (TCP) | 历史K线/PE/PB估值，独立源 | ✅ 稳定 |
| **新浪财经** `quotes.sina.cn` | 财报三表 | ✅ 稳定 |
| **yfinance** | 美股行情 | ⚠️ 容易429 |
| **akshare/百度** | 历史PE/PB估值分位数(走百度API) | ✅ 稳定 |
| **东财** `eastmoney.com` | 龙虎榜/资金流/研报/**新闻/快讯** | ⚠️ 风控动态，必须降级 |

**各数据层落地**：
- 行情/估值/指数/今日涨跌：✅ 腾讯优先（`valuation_compare.py` 降级链：腾讯→akshare→efinance→baostock）
- 历史K线/估值分位：✅ baostock（独立源）
- 板块/热点/北向：✅ 同花顺
- **新闻/快讯**：⚠️ **仅东财有**（个股新闻 search-api-web + 7×24 np-weblist）。腾讯/百度/新浪/同花顺均无可用新闻接口（2026-07-03 实测）。按「尽力而为」抓取，CI 若被封则跳过——`daily_review.get_news_context()` 已带降级
- 龙虎榜/资金流/解禁：仅东财，带降级

## 已安装的核心依赖

### A股数据（多数据源降级）

| 数据源 | 用途 | 底层 | 稳定性 |
|--------|------|------|--------|
| akshare | 主力：实时行情、历史K线、财务数据 | 东方财富 API | ⚠️ 当前被封 |
| efinance | 备用：实时行情、历史K线 | 东方财富 API（同上） | ⚠️ 当前被封 |
| baostock | 兜底：历史K线、估值指标(peTTM/pbMRQ) | 独立数据源 | **稳定** |
| akshare/百度股市通 | 历史PE/PB估值分位数 | 百度 API | **稳定** |

**降级链**: akshare(东方财富) → efinance(东方财富) → **baostock**(独立源)。当东方财富被风控时 baostock 仍可用。

### cn-financial-mcp（A股 MCP Server）
- 基于 AKShare 的 A股金融数据 MCP Server，editable install 自 `~/repos/cn-financial-mcp/`
- 共 42 个工具，详见 `~/repos/cn-financial-mcp/README.md`
- ⚠️ 底层走 akshare/东财，东财被封时不可用

### a-stock-data skill（项目级）
- 位置: `.claude/skills/a-stock-data/SKILL.md`
- 覆盖 7 层 27 端点（行情/研报/信号/资金/新闻/基础数据/公告）
- 内嵌全部调用代码，自包含零依赖外部文件
- 优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防封
- ⚠️ 东财相关端点当前被封，mootdx/腾讯/百度/同花顺端点正常

### 美股数据
- `yfinance` — Yahoo Finance API，获取美股行情
- yfinance 容易被限频 (HTTP 429)，脚本中已做重试 + fast_info 备用降级
- 每次请求间加了随机延迟 (0.5~2s) 防触发风控

### 数据分析 & 可视化
- `numpy`, `pandas`, `scipy`, `scikit-learn`, `statsmodels`
- `matplotlib`, `mplfinance`, `plotly`, `seaborn`, `dash`

### 量化回测
- `backtrader`, `vectorbt`, `pandas-ta`

### 交互式开发
- `jupyterlab`, `notebook`

## 股票代码格式

- A股：6位纯数字，如 `603011`（合锻智能）、`600519`（贵州茅台）、`000001`（平安银行）
- 美股：Ticker symbol，如 `INTC`（Intel）、`AAPL`（Apple）
- baostock 格式：`sh.603011`（沪市6开头）、`sz.000001`（深市0/3开头）

## 项目结构

```
/home/ker/project/stock/
├── CLAUDE.md              # 本文件 — 项目说明 & AI 协作上下文
├── aim.md                 # 原始需求文档
├── plan.md                # 开发路线图 & 进度跟踪
├── environment.yml        # conda 环境定义
├── environment-ci.yml     # CI 专用环境（去掉本地 cn-financial-mcp 引用）
├── scripts/               # 分析脚本
│   ├── valuation_compare.py  # ✅ 美股/A股估值对比工具（多数据源降级）
│   ├── portfolio.py          # ✅ 持仓分析（读CSV→估值→信号→报告）
│   ├── sector_scan.py        # ✅ 板块扫描（同花顺+腾讯，不依赖东财）
│   ├── position_advisor.py   # ✅ 建仓建议（安全边际→分批建仓计划）
│   ├── daily_review.py       # ✅ 每日复盘（8节报告+板块轮动+推送通知）
│   ├── sector_rotation.py    # ✅ 板块轮动监控（快照/历史/趋势信号）
│   └── notify.py             # ✅ 统一推送通知库（PushPlus/Server酱/Telegram）
├── data/                  # 用户数据
│   ├── portfolio.csv         # ✅ 持仓（symbol,market,name,cost_basis,shares,...）
│   ├── watchlist.csv         # ✅ 自选（symbol,market,name,sector,reason,...）
│   ├── knowledge_gaps.json   # ✅ 知识盲区
│   └── sector_history.json   # ✅ 板块轮动历史（自动生成，保留30天）
├── reports/               # 输出报告目录
├── .github/workflows/
│   └── daily_review.yml      # ✅ GitHub Actions 定时任务（周一~五15:30北京时间）
├── notebooks/             # Jupyter notebooks
└── .claude/skills/
    └── a-stock-data/SKILL.md  # ✅ 项目级 A股数据 skill
```

## 核心脚本

### valuation_compare.py — 估值核心

统一估值分析工具，支持美股 (yfinance) 和 A股 (多源降级)。

```python
from valuation_compare import (
    get_us_stock_valuation,    # 美股估值
    get_cn_stock_valuation,    # A股估值（自动降级）
    print_valuation,           # 打印估值报告
    compare_valuations,        # 对比两只股票
    ValuationData,             # 统一数据结构 (dataclass)
)

us = get_us_stock_valuation("INTC")     # → ValuationData
cn = get_cn_stock_valuation("603011")   # → ValuationData
print_valuation(cn)
compare_valuations(us, cn)
```

### portfolio.py — 持仓分析

读取 `data/portfolio.csv` 和 `data/watchlist.csv`，调用估值引擎生成信号和报告。

```bash
python scripts/portfolio.py                    # 分析持仓
python scripts/portfolio.py --watchlist        # 分析自选
python scripts/portfolio.py --all              # 全部分析
python scripts/portfolio.py -o reports/xxx.md  # 保存到文件
```

**信号**: 浮盈/亏提醒、目标价到达、止损触发、52周高低位、估值分位数高估/低估

### sector_scan.py — 板块扫描

搜索概念/行业板块，批量估值筛选低估标的。数据源: 同花顺(板块列表/成分股) + 腾讯财经(批量估值)。

```bash
python scripts/sector_scan.py search 芯片              # 搜索板块
python scripts/sector_scan.py quick 白酒 --pe-max 80   # 关键词快速扫描
python scripts/sector_scan.py scan 881273 --type thshy  # 指定板块代码
python scripts/sector_scan.py quick 军工 -o report.md   # 保存报告
```

**限制**: 同花顺 AJAX 分页被反爬(401)，大板块只能拿到第一页 10-20 只成分股

### position_advisor.py — 建仓建议

基于安全边际模型，生成分批建仓计划。

```bash
python scripts/position_advisor.py 603011                  # 单只A股
python scripts/position_advisor.py INTC -m US              # 单只美股
python scripts/position_advisor.py --watchlist              # 自选全部
python scripts/position_advisor.py --portfolio              # 持仓全部
```

### daily_review.py — 每日复盘

一键生成完整复盘报告（大盘/北向/行业/热点/龙虎榜/持仓/自选/板块轮动/信号汇总），并推送通知。

```bash
python scripts/daily_review.py                       # 全量复盘
python scripts/daily_review.py --market CN           # 仅A股
python scripts/daily_review.py --no-market           # 跳过市场信号
python scripts/daily_review.py -o report.md          # 指定输出
```

### sector_rotation.py — 板块轮动监控

每日行业排名快照 + 30天历史趋势 + 轮动信号检测。

```bash
python scripts/sector_rotation.py              # 快照 + 信号检测
python scripts/sector_rotation.py --report     # 完整报告（含5日趋势表）
python scripts/sector_rotation.py -o reports/rotation.md
```

**信号**: 🔥资金流入（连续排名上升）、💧资金流出、⬆️弱势转强、⬇️强势转弱

### notify.py — 推送通知库

统一推送库，支持 GitHub Issue / PushPlus / Server酱 / Telegram 四通道，通过环境变量配置。

```python
from notify import send_notification
send_notification("标题", "Markdown 内容")
```

```bash
python scripts/notify.py "测试标题" "测试内容"   # CLI 快速测试
```

**环境变量**: `GITHUB_TOKEN`+`GITHUB_REPOSITORY`（CI自动提供）、`PUSHPLUS_TOKEN`、`SERVERCHAN_TOKEN`、`TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`

## 自动化（GitHub Actions）

- **工作流**: `.github/workflows/daily_review.yml`
- **触发**: 周一~周五 15:30 北京时间自动运行
- **步骤**: 运行 daily_review.py + sector_rotation.py → 推送通知 → 提交报告到仓库
- **Secrets**: 在 GitHub 仓库 Settings → Secrets 中设置推送 Token
- **CI 环境**: `environment-ci.yml`（去掉本地 cn-financial-mcp 引用）
- **手动触发**: GitHub Actions 页面点 "Run workflow"

## 数据文件格式

### portfolio.csv（持仓）

```csv
symbol,market,name,cost_basis,shares,buy_date,strategy,target_price,stop_loss,notes
INTC,US,Intel,22.50,100,2025-03-15,长线,50.00,18.00,反垄断+低估值+新一代CPU
```

必填: symbol, market, cost_basis, shares。其余可选。

### watchlist.csv（自选）

```csv
symbol,market,name,sector,reason,alert_price,notes
300308,CN,中际旭创,HBM/AI光模块,800G光模块龙头,,
```

必填: symbol, market。其余可选。

## 注意事项

- **东方财富风控**: akshare/efinance 底层都走东方财富 API，当前已被 IP 封锁。替代方案: 腾讯财经(批量行情)、同花顺(板块/热点)、baostock(历史数据)
- **Yahoo 限频**: yfinance 大量请求后会被限频 (429)，`ticker.info` 最容易触发。解决方案：用 `ticker.fast_info` 备用、请求间加随机延迟、等几分钟后重试
- cn-financial-mcp 的 pyproject.toml 中 build-backend 已修复为 `hatchling.build`（原为 `hatchling.backends`，是个 bug）
- akshare 接口名可能随版本变更，已知的：历史估值用 `stock_zh_valuation_baidu`（走百度股市通），而非旧版 `stock_a_indicator_lg`

## 知识盲区捕获

当用户提问关于股票、金融、经济学的问题时，主动识别其知识盲区：

1. 从提问内容中提取用户可能不掌握的底层概念（如：用户问"HBM 是什么"→ 可能也不懂 TSV、CoWoS、Interposer）
2. 用 Write/Edit 工具更新 `data/knowledge_gaps.json`，追加到 `gaps` 数组
3. 在回答末尾简要提一句："我注意到你可能还需要了解 XXX，已加入学习清单"

**触发条件**: 用户提出与金融/股票/经济学相关的问题时自动执行
**不要过度**: 每次对话最多记录 2-3 个核心概念，不要把基础常识也记录进去
**数据文件**: `data/knowledge_gaps.json`（含 `gaps` 待学数组和 `learned` 已学数组）
