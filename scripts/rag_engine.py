"""
RAG 检索引擎 — 历史报告相似行情检索。

存储: 本地 JSON 文件（data/rag_reports.json）
检索: 基于 TF-IDF 关键词相似度匹配，无需下载模型

用法（作为库）:
  from rag_engine import index_report, retrieve_similar

  # 索引一份报告
  index_report("2026-06-05", report_markdown)

  # 检索相似历史行情
  similar = retrieve_similar(today_report, top_k=3)

用法（CLI 建立索引）:
  python scripts/rag_engine.py --index
  python scripts/rag_engine.py --query "北向流出"
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
REPORTS_DIR = Path(__file__).parent.parent / "reports"
RAG_FILE = DATA_DIR / "rag_reports.json"

# 中文停用词
STOP_WORDS = set("的 是 在 了 和 与 有 不 也 这 那 就 都 而 或 但 如果 因为 所以".split())


def _tokenize(text: str) -> list[str]:
    """中文 bigram 分词 + 英文词级分词。"""
    # 去掉 Markdown 标记和数字
    text = re.sub(r"[#*|>\-]", " ", text)
    tokens = []

    # 提取中文段，做 bigram 滑动窗口
    for cn_segment in re.findall(r"[一-鿿]+", text):
        for i in range(len(cn_segment) - 1):
            bigram = cn_segment[i:i+2]
            if bigram not in STOP_WORDS:
                tokens.append(bigram)

    # 提取英文词
    for en_word in re.findall(r"[a-zA-Z]{2,}", text):
        tokens.append(en_word.lower())

    return tokens


def _compute_tfidf(text: str) -> Counter:
    """计算文本的词频向量。"""
    tokens = _tokenize(text)
    return Counter(tokens)


def _cosine_similarity(a: Counter, b: Counter) -> float:
    """计算两个 Counter 的余弦相似度。"""
    common_keys = set(a.keys()) & set(b.keys())
    if not common_keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in common_keys)
    mag_a = sum(v * v for v in a.values()) ** 0.5
    mag_b = sum(v * v for v in b.values()) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _load_store() -> dict:
    """加载 RAG 存储。"""
    if not RAG_FILE.exists():
        return {"reports": {}}
    try:
        return json.loads(RAG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"reports": {}}


def _save_store(store: dict) -> None:
    """保存 RAG 存储。"""
    RAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    RAG_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def index_report(date: str, report_md: str) -> None:
    """将一份报告索引到 RAG 存储。"""
    if not report_md.strip():
        return

    store = _load_store()

    # 按章节切分
    chunks = _split_report(report_md)

    store["reports"][date] = {
        "full_text": report_md[:6000],  # 保留完整文本（截断）
        "chunks": [{"text": c, "tfidf": dict(_compute_tfidf(c))} for c in chunks if len(c) >= 50],
    }

    # 只保留最近 60 天
    all_dates = sorted(store["reports"].keys())
    if len(all_dates) > 60:
        for d in all_dates[:-60]:
            del store["reports"][d]

    _save_store(store)
    n_chunks = len(store["reports"][date]["chunks"])
    print(f"  📇 RAG 索引: {date} → {n_chunks} 段")


def _split_report(report_md: str, max_chars: int = 1500) -> list[str]:
    """按章节切分报告。"""
    sections = report_md.split("\n## ")
    chunks = []
    for section in sections:
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            paragraphs = section.split("\n\n")
            current = ""
            for p in paragraphs:
                if len(current) + len(p) + 2 > max_chars and current:
                    chunks.append(current)
                    current = p
                else:
                    current = current + "\n\n" + p if current else p
            if current:
                chunks.append(current)
    return chunks


def index_all_reports() -> int:
    """索引 reports/ 目录下所有 .md 文件。"""
    count = 0
    if not REPORTS_DIR.exists():
        return 0

    for f in sorted(REPORTS_DIR.glob("*.md")):
        name = f.stem
        if name.startswith("rotation-"):
            continue
        date = name
        if len(date) != 10:
            continue
        report_md = f.read_text(encoding="utf-8")
        if report_md.strip():
            index_report(date, report_md)
            count += 1

    return count


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def retrieve_similar(
    query_text: str,
    top_k: int = 3,
    exclude_date: str = "",
) -> list[dict]:
    """检索与当前报告最相似的历史报告段落。"""
    store = _load_store()
    if not store["reports"]:
        return []

    query_tfidf = _compute_tfidf(query_text[:3000])

    scored = []
    for date, data in store["reports"].items():
        if date == exclude_date:
            continue
        for chunk_data in data.get("chunks", []):
            chunk_tfidf = Counter(chunk_data.get("tfidf", {}))
            sim = _cosine_similarity(query_tfidf, chunk_tfidf)
            if sim > 0.05:  # 过滤掉完全不相关的
                scored.append({
                    "date": date,
                    "content": chunk_data["text"],
                    "similarity": round(sim, 4),
                })

    # 按相似度排序，取 top_k
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:top_k]


def format_rag_context(similar_items: list[dict]) -> str:
    """将检索结果格式化为 Prompt 可用的上下文。"""
    if not similar_items:
        return ""

    lines = ["以下是历史上与今日行情相似的市场数据，供参考分析："]
    for item in similar_items:
        lines.append(f"\n### 📅 {item['date']} 的市场数据（相似度: {item['similarity']:.0%}）")
        lines.append(item["content"][:800])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="RAG 检索引擎")
    parser.add_argument("--index", action="store_true", help="索引所有历史报告")
    parser.add_argument("--query", type=str, help="测试检索")
    args = parser.parse_args()

    if args.index:
        print("📇 正在索引历史报告...")
        count = index_all_reports()
        print(f"✓ 共索引 {count} 份报告")

    elif args.query:
        results = retrieve_similar(args.query, top_k=3)
        if results:
            for r in results:
                print(f"\n📅 {r['date']} (相似度: {r['similarity']:.0%})")
                print(r["content"][:300])
                print("...")
        else:
            print("无检索结果（先运行 --index）")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
