"""
LLM 深度分析模块 — 调用 DeepSeek API 生成 AI 解读报告。

数据源: DeepSeek API（OpenAI 兼容接口）
模型: deepseek-chat（快速便宜）/ deepseek-reasoner（深度思考）
配置: 环境变量 DEEPSEEK_API_KEY

用法（作为库）:
  from llm_analysis import generate_ai_analysis
  ai_section = generate_ai_analysis(report_markdown)

用法（CLI 测试）:
  python scripts/llm_analysis.py reports/2026-06-04.md
"""

import os
import sys
import warnings
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent))
from rag_engine import retrieve_similar, format_rag_context

# ---------------------------------------------------------------------------
# DeepSeek API 配置
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"  # 便宜快速；深度思考用 "deepseek-reasoner"

SYSTEM_PROMPT = """你是一位资深A股/美股投资顾问，风格简洁直接，不说废话。

规则：
1. 用通俗语言，避免专业术语（如果用了要解释）
2. 给明确建议，不要模棱两可的"建议关注"
3. 数字要具体，不要"涨幅较大"这种模糊说法
4. 每个板块控制在 3-5 句话
5. 如果数据缺失或异常，直接指出，不要编造
6. 给买卖建议前先看「持仓股数」和「持仓市值」：小仓位（单只不足 200 股或市值低于 1 万元）不得建议"分批减仓/减半/分批加仓"，必须一次性买卖；仓位大小直接决定建议是否可执行
7. 严格区分「今日涨跌」与「累计浮亏%」：「今日涨跌」是当天涨跌幅，判断个股是否跟涨/跟跌板块时看它；「累计浮亏%」是成本价相对现价，判断止盈止损时看它。绝不能混淆——例如不得因"累计浮亏10%"就断言"今天没跟涨"。
8. 结合「📰 新闻与事件上下文」分析持仓：若个股或其板块有相关利好/利空新闻（政策、公告、产业链事件如某公司上市/中标/复产/解禁），建议必须体现这些事件的影响；新闻里未提及的不要编造。"""


def generate_ai_analysis(
    report_md: str,
    model: str = MODEL,
    api_key: str = "",
) -> str:
    """
    调用 DeepSeek API 分析每日报告，返回 Markdown 格式的 AI 解读。

    Args:
        report_md: 每日复盘报告的 Markdown 文本
        model: DeepSeek 模型名 (deepseek-chat / deepseek-reasoner)
        api_key: API Key，为空则读 DEEPSEEK_API_KEY 环境变量

    Returns:
        AI 分析的 Markdown 文本
    """
    key = api_key or API_KEY
    if not key:
        warnings.warn("DEEPSEEK_API_KEY 未配置，跳过 AI 分析")
        return ""

    # 截断过长报告（控制 token 消耗）
    input_text = report_md[:12000]

    # RAG: 检索历史相似行情
    rag_context = ""
    try:
        similar = retrieve_similar(input_text, top_k=2)
        rag_context = format_rag_context(similar)
    except Exception:
        pass  # RAG 失败不影响主流程

    rag_section = ""
    if rag_context:
        rag_section = f"""
## 历史参考数据（RAG 检索）

{rag_context}

请对比今日数据与历史参考数据，如果行情有相似之处，指出后续可能的走势参考。
"""

    user_prompt = f"""请根据以下今日市场数据，给出简明分析报告。

## 今日市场数据

{input_text}
{rag_section}
## 请按以下格式输出

### 📊 市场解读
（2-3句话总结今天大盘整体情况，用通俗语言）

### 🎯 持仓建议
（逐只分析：持有/减仓/加仓，给出理由和具体价位建议）

### 🔥 板块机会
（哪些板块在走强？背后的逻辑是什么？能不能追？）

### ⚠️ 风险提示
（有什么需要注意的风险？北向资金/估值/事件风险等）

### 📋 操作清单
（如果今天只能做一件事，应该做什么？）"""

    try:
        client = OpenAI(api_key=key, base_url=BASE_URL)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,  # 低温度 = 更稳定理性的分析
            max_tokens=2000,
        )
        content = response.choices[0].message.content
        return content.strip() if content else ""

    except Exception as e:
        warnings.warn(f"DeepSeek API 调用失败: {e}")
        return ""


# ---------------------------------------------------------------------------
# CLI 测试
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/llm_analysis.py <报告文件.md>")
        print("示例: python scripts/llm_analysis.py reports/2026-06-04.md")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    if not report_path.exists():
        print(f"文件不存在: {report_path}")
        sys.exit(1)

    report_md = report_path.read_text(encoding="utf-8")
    print("🤖 正在调用 DeepSeek 生成 AI 分析...")

    result = generate_ai_analysis(report_md)
    if result:
        print()
        print(result)
        print()
        print(f"✓ AI 分析完成 ({len(result)} 字)")
    else:
        print("✗ AI 分析失败（检查 DEEPSEEK_API_KEY 配置）")


if __name__ == "__main__":
    main()
