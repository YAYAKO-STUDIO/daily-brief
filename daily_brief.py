"""
Yukina 每日簡報 — GitHub Actions cron 版

每天 UTC 04:30 (台北 12:30) 自動執行：
1. 推「💚 已啟動」心跳訊息到 Telegram
2. 抓 6 大分類 RSS feed 近 48 小時的條目
3. 組裝成 7 條 HTML 訊息（總覽 + 6 分類）推到 Telegram
4. 結尾推「✅ 完成」或「⚠️ N 條失敗」總結
"""

import os
import sys
import time
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from html import escape

# === 配置（從 GitHub Secrets 讀） ===
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

# === 時間（台北時區） ===
TZ_TAIPEI = timezone(timedelta(hours=8))
NOW = datetime.now(TZ_TAIPEI)
DATE_STR = NOW.strftime("%Y-%m-%d")
TIME_STR = NOW.strftime("%H:%M")
WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][NOW.weekday()]

# === 6 大分類 + RSS feed ===
CATEGORIES = [
    {
        "emoji": "💹",
        "name": "台美股 macro",
        "feeds": [
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Markets
            "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",  # WSJ Markets
        ],
    },
    {
        "emoji": "🤖",
        "name": "AI 界更新",
        "feeds": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://www.anthropic.com/news/rss.xml",
        ],
    },
    {
        "emoji": "📱",
        "name": "科技新聞",
        "feeds": [
            "https://techcrunch.com/feed/",
            "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # CNBC Tech
        ],
    },
    {
        "emoji": "🚀",
        "name": "創業 / SaaS 趨勢",
        "feeds": [
            "https://news.ycombinator.com/rss",
        ],
    },
    {
        "emoji": "🎨",
        "name": "創作者工具更新",
        "feeds": [
            "https://www.figma.com/blog/feed/",
            "https://blog.adobe.com/en/topics/creativity.rss",
        ],
    },
    {
        "emoji": "₿",
        "name": "加密貨幣 / 宏觀經濟",
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
        ],
    },
]


def send_telegram(text, silent=True):
    """推一條訊息到 Telegram。回傳 {ok: bool, error: str}"""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    try:
        r = requests.post(URL, json=payload, timeout=10)
        data = r.json()
        return {"ok": data.get("ok", False), "error": data.get("description", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_recent_entries(feeds, limit=3, hours=48):
    """從多個 RSS feed 抓近 N 小時的條目，按發佈時間排序取 limit 則。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for feed_url in feeds:
        try:
            f = feedparser.parse(feed_url)
            for entry in f.entries[:25]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                else:
                    pub_dt = datetime.now(timezone.utc)
                items.append({
                    "title": entry.get("title", "(無標題)").strip(),
                    "url": entry.get("link", ""),
                    "pub_dt": pub_dt,
                })
        except Exception as e:
            print(f"  feed {feed_url} parse failed: {e}", flush=True)
    # 去重（同樣標題或 URL 只留一個）
    seen = set()
    unique = []
    for it in sorted(items, key=lambda x: x["pub_dt"], reverse=True):
        key = it["url"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:limit]


def format_category(cat, entries):
    """組一條分類訊息"""
    lines = [f'{cat["emoji"]} <b>{cat["name"]} | {DATE_STR}</b>', ""]
    if not entries:
        lines.append("今日無重大更新（過去 48 小時無新進度）。")
    else:
        for e in entries:
            title = escape(e["title"])
            url = e["url"]
            lines.append(f'• <b>{title}</b>')
            lines.append(f'→ <a href="{url}">來源</a>')
            lines.append("")
    return "\n".join(lines)


def main():
    print(f"=== Daily Brief {DATE_STR} {TIME_STR} ===", flush=True)

    # Step 0：心跳訊息（最先做，確認推送通道通）
    heartbeat = f"💚 Yukina 每日簡報 routine 已啟動 ({DATE_STR} {TIME_STR})，開始抓資料..."
    r = send_telegram(heartbeat, silent=True)
    print(f"Step 0 heartbeat: ok={r['ok']} err={r['error']}", flush=True)
    if not r["ok"]:
        print(f"FATAL: heartbeat failed. Stopping.", flush=True)
        sys.exit(1)

    # Step 1+2：抓 6 大分類 RSS
    category_data = []
    for cat in CATEGORIES:
        entries = get_recent_entries(cat["feeds"], limit=3, hours=48)
        category_data.append({**cat, "entries": entries})
        print(f"  {cat['name']}: {len(entries)} entries", flush=True)

    total_entries = sum(len(c["entries"]) for c in category_data)

    # Step 3：組第 1 條總覽
    overview_lines = [
        f'📊 <b>每日簡報 {DATE_STR}（{WEEKDAY_ZH}）</b>',
        '',
        '<i>※ GitHub Actions 自動推送（純 RSS 版，未經 LLM 整理 — 之後可升級）</i>',
        '',
        f'<b>今日 6 大分類共 {total_entries} 則新聞：</b>',
        '',
    ]
    for c in category_data:
        overview_lines.append(f'{c["emoji"]} {c["name"]}：{len(c["entries"])} 則')
    overview_lines.append('')
    overview_lines.append('往下滑看各分類詳情 ↓')
    messages = ["\n".join(overview_lines)]

    # Step 3 續：6 條分類
    for c in category_data:
        messages.append(format_category(c, c["entries"]))

    # Step 4：推送 7 條
    failed = []
    for i, text in enumerate(messages):
        if len(text) > 4000:
            text = text[:4000] + "\n\n(訊息過長已截斷)"
        r = send_telegram(text, silent=(i > 0))
        print(f"  #{i+1}: ok={r['ok']} err={r['error']}", flush=True)
        if not r["ok"]:
            failed.append(i)
        if i < len(messages) - 1:
            time.sleep(0.4)  # 避免 Telegram rate limit

    # Step 5：總結訊息
    if not failed:
        summary = f"✅ 今日簡報推送完成 ({len(messages)}/{len(messages)})"
    else:
        summary = f"⚠️ 推送完成但有 {len(failed)} 條失敗 (indices: {failed})"
    r = send_telegram(summary, silent=True)
    print(f"Summary: {summary} (sent ok={r['ok']})", flush=True)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
