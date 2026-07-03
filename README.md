# 📈 a-stock-agent

> AI 驱动的多源数据 A股 / 美股复盘系统 —— 每个交易日自动完成「数据采集 → 量化分析 → AI 解读 → 报告推送」，把 30+ 分钟的人工复盘压缩为零人工干预的自动化闭环，**零服务器成本**（GitHub Actions）。

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Platform](https://img.shields.io/badge/Platform-GitHub%20Actions-lightgrey)
![Status](https://img.shields.io/badge/状态-个人研究项目-orange)

---

## ✨ 核心特性

- **多源数据自动降级**：8+ 数据源（腾讯 / 同花顺 / 百度股市通 / baostock / 新浪 / mootdx / yfinance / akshare），主力源被封仍 100% 可用
- **AIGC 决策简报**：量化计算 → DeepSeek 推理 → 结构化输出，先算数再喂 AI，有效抑制数字幻觉；TF-IDF RAG 检索历史相似行情
- **8 类信号检测**：浮盈亏、目标价、止损、52 周高低位、估值分位、板块轮动、强势股、北向异动
- **Serverless 全自动**：GitHub Actions 每日两次（盘中 + 收盘），数据采集 → AI 分析 → 多通道推送，零服务器
- **多通道推送降级**：GitHub Issue → PushPlus → Server 酱 → Telegram

---

## 🚀 快速开始

### 1. 环境准备

```bash
# 用 conda 重建环境（Python 3.13）
conda env create -f environment.yml
conda activate stock
```

### 2. 配置密钥

```bash
cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY 和一个推送通道 Token
```

> `.env` 已在 `.gitignore` 中，不会被提交。推送 Token 全部可选 —— 不配则只生成报告、不推送。

### 3. 准备你的持仓 / 自选数据

```bash
cp data/portfolio.csv.example  data/portfolio.csv   # 填入你的真实持仓
cp data/watchlist.csv.example  data/watchlist.csv   # 填入你的自选股
```

### 4. 运行

```bash
python scripts/daily_review.py            # 每日复盘（全量）
python scripts/portfolio.py               # 持仓分析
python scripts/portfolio.py --watchlist   # 自选分析
python scripts/valuation_compare.py       # 估值对比（A股多源降级）
python scripts/sector_scan.py search 芯片 # 板块扫描
python scripts/sector_rotation.py --report# 板块轮动监控
python scripts/position_advisor.py 603011 # 建仓建议
```

---

## 📊 功能模块

| 脚本 | 功能 | 示例 |
|------|------|------|
| `daily_review.py` | 每日复盘（8 章节报告 + AI 解读 + 推送） | `python scripts/daily_review.py` |
| `portfolio.py` | 持仓 / 自选分析与信号 | `python scripts/portfolio.py --all` |
| `valuation_compare.py` | A股 / 美股估值对比（多源降级核心） | `python scripts/valuation_compare.py` |
| `sector_scan.py` | 板块搜索 + 批量估值筛选 | `python scripts/sector_scan.py quick 白酒` |
| `sector_rotation.py` | 板块轮动趋势监控 | `python scripts/sector_rotation.py --report` |
| `position_advisor.py` | 基于安全边际的分批建仓计划 | `python scripts/position_advisor.py --watchlist` |
| `notify.py` | 统一推送通知库 | `python scripts/notify.py "标题" "内容"` |

> 详见 [`CLAUDE.md`](CLAUDE.md) 的完整命令文档。

---

## 🔌 数据源

| 数据源 | 用途 | 稳定性 |
|--------|------|--------|
| 腾讯财经 `qt.gtimg.cn` | 实时行情 / PE / PB / 市值（不封 IP） | ✅ 稳定首选 |
| mootdx（通达信） | TCP 行情 / K线 / 盘口 / 财务 | ✅ 稳定 |
| 同花顺 `10jqka.com.cn` | 板块 / 热点 / 北向 / 一致预期 | ✅ 稳定 |
| 百度股市通 | 概念板块 / K线带 MA / 历史估值分位 | ✅ 稳定 |
| baostock | 历史 K线 / 估值（独立 TCP 源） | ✅ 稳定 |
| 新浪财经 | 财报三表 | ✅ 稳定 |
| yfinance | 美股行情 | ⚠️ 易 429 |
| 东方财富 | 龙虎榜 / 资金流 / 新闻（**带降级**） | ⚠️ 风控动态 |

**降级链设计**：每个数据需求都有 `稳定源 → 东财兜底` 的降级链，东财失败自动跳过、不影响主流程。

---

## ⏰ 自动化（GitHub Actions）

- **工作流**：[`.github/workflows/daily_review.yml`](.github/workflows/daily_review.yml)
- **触发**：交易日盘中 + 收盘两次自动运行
- **Secrets 配置**：在仓库 `Settings → Secrets and variables → Actions` 中填入 `DEEPSEEK_API_KEY` 及推送 Token（`GITHUB_TOKEN` / `GITHUB_REPOSITORY` 由 Actions 自动注入）
- **CI 环境**：`environment-ci.yml`（移除本地依赖）
- 支持手动 `workflow_dispatch` 触发

---

## 📁 项目结构

```
stock/
├── scripts/               # 10 个核心模块（~5400 行）
├── data/                  # 持仓/自选（CSV，用户数据，不入库）
├── reports/               # 自动生成的报告（.gitignore）
├── .github/workflows/     # GitHub Actions 定时任务
├── .claude/skills/        # 第三方 Claude Skill（见致谢）
├── environment.yml        # conda 环境
├── environment-ci.yml     # CI 环境
└── CLAUDE.md              # 详细开发文档
```

---

## 🙏 致谢

本项目站在以下开源项目 / 公开数据源的肩膀上：

- **[a-stock-data](https://github.com/simonlin1212/a-stock-data)** —— A股全栈数据 Claude Skill（作者 **Simon 林**，[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)）。本项目集成使用其数据获取能力，相关代码与作者声明保留于 `.claude/skills/a-stock-data/`。
- 各金融数据源：腾讯财经、同花顺、百度股市通、baostock、新浪财经、通达信（mootdx）等。
- [DeepSeek](https://www.deepseek.com/) 提供 LLM 推理能力。

> 📌 本项目使用的 `a-stock-data` 为 V3.2.1（7 层 27 端点）版本，上游已迭代至 10 层 40 端点，可按需升级。

---

## ⚠️ 免责声明

**股市有风险，投资需谨慎。**

本项目仅为个人学习与研究用途的自动化分析工具，**不构成任何投资建议**。所有 AI 生成内容、量化信号、估值判断均可能存在错误或偏差，据此交易风险自负。请结合自身判断与独立研究做出决策。

---

## 📄 License

- 本项目自身代码：[MIT License](LICENSE) © 2026 KerZhou
- 集成的第三方 `a-stock-data` Skill：保留其原始 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)，版权所有 Simon 林，详见 `.claude/skills/a-stock-data/LICENSE`
