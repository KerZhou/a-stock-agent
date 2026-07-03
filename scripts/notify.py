"""
统一推送通知库 — 支持 GitHub Issue / PushPlus / Server酱 / Telegram 多通道。

所有脚本共用，通过环境变量配置（无需 config 文件）:
  GITHUB_TOKEN         GitHub Token（自动创建 Issue，CI 中由 GITHUB_TOKEN 提供）
  GITHUB_REPOSITORY    仓库地址（格式: owner/repo，CI 中自动提供）
  PUSHPLUS_TOKEN       PushPlus token（主力，200条/天，推送微信）
  SERVERCHAN_TOKEN     Server酱 token（备用，5条/天，推送微信）
  TELEGRAM_BOT_TOKEN   Telegram Bot API token（可选，无限制）
  TELEGRAM_CHAT_ID     Telegram 接收消息的 chat_id

用法（作为库导入）:
  from notify import send_notification
  send_notification("每日复盘 2026-06-04", report_markdown)

用法（CLI 单独测试）:
  python scripts/notify.py "测试标题" "测试内容"
"""

import os
import re
import sys
import warnings
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 内容预处理
# ---------------------------------------------------------------------------

def _truncate_markdown(content: str, max_bytes: int = 9000) -> str:
    """截断 Markdown 内容，在最后一个 ## 段落边界处切断，保留 10KB 以内。"""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content

    # 在字节限制内的最后一个 ## 边界处切断
    text = content[:max_bytes]  # 粗略切
    last_section = text.rfind("\n## ")
    if last_section > len(content) // 4:  # 至少保留 1/4 内容
        text = content[:last_section]

    return text.rstrip() + "\n\n> ... (内容过长已截断，完整报告请查看本地文件)"


def _md_to_html_simple(md: str) -> str:
    """将基础 Markdown 转换为 HTML（Telegram 需要）。"""
    text = md
    # 表格 → <pre>
    text = re.sub(
        r"(\|.+\|[\r\n]+\|[-| :]+\|[\r\n]+((\|.+\|[\r\n]*)+))",
        lambda m: "<pre>" + m.group(0).replace("\n", "\n") + "</pre>",
        text,
    )
    # 标题
    text = re.sub(r"^### (.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<b>【\1】</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<b>【\1】</b>", text, flags=re.MULTILINE)
    # 粗体
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 斜体
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # 行内代码
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    # 引用块去掉
    text = re.sub(r"^> .+$", "", text, flags=re.MULTILINE)
    return text


# ---------------------------------------------------------------------------
# PushPlus（主力 — 200条/天，推送微信）
# ---------------------------------------------------------------------------

def _send_pushplus(token: str, title: str, content: str) -> bool:
    """通过 PushPlus 推送 Markdown 消息到微信。"""
    url = "http://www.pushplus.plus/send"
    payload = {
        "token": token,
        "title": title,
        "content": _truncate_markdown(content),
        "template": "markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        d = r.json()
        if d.get("code") == 200:
            return True
        warnings.warn(f"PushPlus 失败: {d.get('msg', d)}")
        return False
    except Exception as e:
        warnings.warn(f"PushPlus 请求失败: {e}")
        return False


# ---------------------------------------------------------------------------
# Server酱（备用 — 5条/天，推送微信）
# ---------------------------------------------------------------------------

def _send_serverchan(token: str, title: str, content: str) -> bool:
    """通过 Server酱 推送 Markdown 消息到微信。"""
    url = f"https://sct.ftqq.com/{token}.send"
    payload = {
        "title": title[:100],  # Server酱标题限制
        "desp": _truncate_markdown(content),
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        d = r.json()
        if d.get("code") == 0:
            return True
        warnings.warn(f"Server酱 失败: {d.get('message', d)}")
        return False
    except Exception as e:
        warnings.warn(f"Server酱 请求失败: {e}")
        return False


# ---------------------------------------------------------------------------
# Telegram Bot（可选 — 无限制）
# ---------------------------------------------------------------------------

def _send_telegram(bot_token: str, chat_id: str, title: str, content: str) -> bool:
    """通过 Telegram Bot 推送消息。"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram HTML 模式，4096 字符限制
    html = f"<b>{title}</b>\n\n" + _md_to_html_simple(content)
    # Telegram 单条消息限 4096 字符
    if len(html) > 4000:
        html = html[:4000] + "\n\n... (截断)"

    payload = {
        "chat_id": chat_id,
        "text": html,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        d = r.json()
        if d.get("ok"):
            return True
        warnings.warn(f"Telegram 失败: {d.get('description', d)}")
        return False
    except Exception as e:
        warnings.warn(f"Telegram 请求失败: {e}")
        return False


# ---------------------------------------------------------------------------
# GitHub Issue（免费 — 零配置，CI 中 GITHUB_TOKEN 自动提供）
# ---------------------------------------------------------------------------

def _send_github_issue(token: str, repo: str, title: str, content: str) -> bool:
    """通过 GitHub API 创建 Issue。CI 中 GITHUB_TOKEN 自动可用。"""
    api_url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    body = content[:60000]  # GitHub Issue body 限制约 65536
    payload = {
        "title": title[:256],
        "body": body,
    }
    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            issue_url = r.json().get("html_url", "")
            print(f"  📋 Issue: {issue_url}")
            return True
        warnings.warn(f"GitHub Issue 失败: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        warnings.warn(f"GitHub Issue 请求失败: {e}")
        return False


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------

def _detect_channels() -> list[str]:
    """检测环境变量，返回已配置的通道列表。"""
    channels = []
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        channels.append("github_issue")
    if os.environ.get("PUSHPLUS_TOKEN"):
        channels.append("pushplus")
    if os.environ.get("SERVERCHAN_TOKEN"):
        channels.append("serverchan")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    return channels


def send_notification(
    title: str,
    content: str,
    channels: list[str] | None = None,
) -> dict[str, bool]:
    """
    发送 Markdown 通知到所有已配置的推送通道。

    Args:
        title: 通知标题
        content: Markdown 格式的通知内容
        channels: 指定通道列表，None=自动检测环境变量

    Returns:
        {通道名: 是否成功}，例如 {"pushplus": True, "serverchan": False}
    """
    if channels is None:
        channels = _detect_channels()

    if not channels:
        print("  📱 未配置任何推送通道 (GITHUB_TOKEN / PUSHPLUS_TOKEN / SERVERCHAN_TOKEN / TELEGRAM_BOT_TOKEN)")
        return {}

    results = {}
    for ch in channels:
        if ch == "github_issue":
            ok = _send_github_issue(
                os.environ["GITHUB_TOKEN"],
                os.environ["GITHUB_REPOSITORY"],
                title,
                content,
            )
            results["github_issue"] = ok
            status = "✓" if ok else "✗"
            print(f"  📱 GitHub Issue {status}")

        elif ch == "pushplus":
            ok = _send_pushplus(os.environ["PUSHPLUS_TOKEN"], title, content)
            results["pushplus"] = ok
            status = "✓" if ok else "✗"
            print(f"  📱 PushPlus {status}")

        elif ch == "serverchan":
            ok = _send_serverchan(os.environ["SERVERCHAN_TOKEN"], title, content)
            results["serverchan"] = ok
            status = "✓" if ok else "✗"
            print(f"  📱 Server酱 {status}")

        elif ch == "telegram":
            ok = _send_telegram(
                os.environ["TELEGRAM_BOT_TOKEN"],
                os.environ["TELEGRAM_CHAT_ID"],
                title,
                content,
            )
            results["telegram"] = ok
            status = "✓" if ok else "✗"
            print(f"  📱 Telegram {status}")

    return results


# ---------------------------------------------------------------------------
# CLI（用于快速测试）
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("用法: python scripts/notify.py <标题> <内容>")
        print("示例: python scripts/notify.py '测试' 'Hello **Markdown**'")
        sys.exit(1)

    title = sys.argv[1]
    content = sys.argv[2]

    channels = _detect_channels()
    print(f"已配置通道: {channels or '(无)'}")

    results = send_notification(title, content)
    if results:
        success = sum(1 for v in results.values() if v)
        print(f"推送结果: {success}/{len(results)} 成功")
    else:
        print("未推送（无可用通道）")


if __name__ == "__main__":
    main()
