"""
Yukina 每日簡報 v2 — GitHub Actions cron + Gemini LLM 整理版

每天台北時間 12:27 ± 延遲：
1. 推「💚 已啟動」心跳到 Telegram
2. 抓 6 大分類 RSS 近 48 小時的條目（各 5-6 則，讓 LLM 選材）
3. 把全部英文條目交給 Gemini 2.5 Flash 整理成 7 條繁中 HTML 訊息
   （總覽 + 6 分類，含「對你影響」段）
4. 推送 7 條到 Telegram
5. 結尾推「✅ 完成」總結
"""

import os
import sys
import time
import json
import feedparser
import requests
from datetime import datetime, timezone, timedelta

# === 配置（從 GitHub Secrets 讀） ===
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-flash-latest:generateContent?key={GEMINI_KEY}"
)

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
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    try:
        r = requests.post(TG_URL, json=payload, timeout=15)
        data = r.json()
        return {"ok": data.get("ok", False), "error": data.get("description", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_recent_entries(feeds, limit=6, hours=48):
    """從多個 RSS feed 抓近 N 小時的條目，去重 + 按發佈時間排序，取 limit 則。"""
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
                    "title": entry.get("title", "(no title)").strip(),
                    "url": entry.get("link", ""),
                    "summary": (entry.get("summary", "") or entry.get("description", ""))[:500].strip(),
                    "pub_dt": pub_dt,
                })
        except Exception as e:
            print(f"  feed {feed_url} parse failed: {e}", flush=True)
    seen, unique = set(), []
    for it in sorted(items, key=lambda x: x["pub_dt"], reverse=True):
        key = it["url"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:limit]


def call_gemini(all_entries):
    """把所有 RSS 條目交給 Gemini 整理成 7 條 HTML 訊息。回傳 list[str]。"""
    prompt = f"""你是 Yukina 的每日簡報編輯。

Yukina 的背景：
- 一人公司創業者、主管
- Claude Code / AI 自動化重度使用者（Anthropic 產品動態直接影響她日常）
- 內容創作者（社群媒體、之後架網站、影音 / 語音）
- 對台美股、加密貨幣有觀察但不重押
- 目標：用 AI 分身放大個人生產力
- 色弱（顏色相關新聞不必特別強調顏色細節）

今天日期：{DATE_STR}（{WEEKDAY_ZH}）

以下是從 6 大分類 RSS 抓的英文新聞條目（JSON 格式，含 category / title / summary / url）：

```json
{json.dumps(all_entries, ensure_ascii=False, indent=2)}
```

請輸出 JSON：`{{"messages": [7 條 HTML 字串]}}`

7 條訊息規格：

**第 1 條（總覽）**：
```
📊 <b>每日簡報 {DATE_STR}（{WEEKDAY_ZH}）</b>

<b>今日 3 個必看：</b>

1️⃣ <b>{{標題}}</b>：{{摘要}}。{{對 Yukina 一句話影響}}

2️⃣ <b>{{標題}}</b>：{{摘要}}。{{對 Yukina 一句話影響}}

3️⃣ <b>{{標題}}</b>：{{摘要}}。{{對 Yukina 一句話影響}}

往下滑看 6 大分類詳情 ↓
```
從所有英文新聞中挑「對 Yukina 最有感」的 3 則作為今日 3 個必看。

**第 2-7 條（分類）**，固定順序與 emoji：
- 第 2: 💹 台美股 macro
- 第 3: 🤖 AI 界更新
- 第 4: 📱 科技新聞
- 第 5: 🚀 創業 / SaaS 趨勢
- 第 6: 🎨 創作者工具更新
- 第 7: ₿ 加密貨幣 / 宏觀經濟

每條格式：
```
{{emoji}} <b>{{分類名}} | {DATE_STR}</b>

• <b>{{標題（翻成繁中）}}</b>：{{2-3 句繁中摘要}}
→ <a href="{{url}}">{{來源簡稱，如 CNBC、TechCrunch、CoinDesk、HN}}</a>

• <b>{{標題（翻成繁中）}}</b>：{{2-3 句繁中摘要}}
→ <a href="{{url}}">{{來源簡稱}}</a>

(2-3 則)

<b>👉 對你影響：</b>{{1-2 句具體觀點，從一人公司 / Claude Code 使用者 / 創作者角度切入}}
```

**強制規則：**
- 全部繁體中文（禁簡體字）
- HTML escape：內文裡的 `<` `>` `&` 字元要轉成 `&lt;` `&gt;` `&amp;`（但 `<b>`、`<a href="">`、`<i>` 標籤保留 raw）
- 每條訊息 ≤ 3800 字元
- 「對你影響」必須具體，禁寫「市場有風險請投資人留意」這類空泛廢話
- 某分類沒有 RSS 條目或新聞都不重要 → 寫「• 今日無重大更新（過去 48 小時無新進度）」+ 「<b>👉 對你影響：</b>無」
- 不要硬湊新聞數量，沒有就說沒有
- 翻譯不要直譯，要符合台灣中文閱讀習慣

**只輸出 JSON，禁用 markdown code fence 包覆。**
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    }
    r = requests.post(GEMINI_URL, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)
    return result["messages"]


def main():
    print(f"=== Daily Brief v2 {DATE_STR} {TIME_STR} ===", flush=True)

    # Step 0：心跳（最先做）
    heartbeat = f"💚 Yukina 每日簡報 routine 已啟動 ({DATE_STR} {TIME_STR})，正在抓 RSS + Gemini 整理..."
    r = send_telegram(heartbeat, silent=True)
    print(f"Step 0 heartbeat: ok={r['ok']} err={r['error']}", flush=True)
    if not r["ok"]:
        print("FATAL: heartbeat failed. Stopping.", flush=True)
        sys.exit(1)

    # Step 1-2：抓 6 大分類 RSS
    all_entries = []
    for cat in CATEGORIES:
        entries = get_recent_entries(cat["feeds"], limit=6, hours=48)
        for e in entries:
            all_entries.append({
                "category": cat["name"],
                "title": e["title"],
                "summary": e["summary"][:400],
                "url": e["url"],
            })
        print(f"  {cat['name']}: {len(entries)} entries", flush=True)
    print(f"Total entries collected: {len(all_entries)}", flush=True)

    # Step 3：Gemini 整理
    try:
        messages = call_gemini(all_entries)
        if not isinstance(messages, list) or len(messages) != 7:
            raise ValueError(f"Gemini returned {type(messages).__name__} with len={len(messages) if hasattr(messages,'__len__') else 'N/A'}, expected list of 7")
        print(f"Gemini integration OK: 7 messages received", flush=True)
    except Exception as e:
        print(f"FATAL Gemini failed: {e}", flush=True)
        send_telegram(f"⚠️ Gemini LLM 整理失敗：{str(e)[:200]}。請看 GitHub Actions log。", silent=False)
        sys.exit(1)

    # Step 4：推送 7 條
    failed = []
    for i, text in enumerate(messages):
        if not isinstance(text, str):
            text = str(text)
        if len(text) > 4000:
            text = text[:4000] + "\n\n(訊息過長已截斷)"
        r = send_telegram(text, silent=(i > 0))
        print(f"  #{i+1}: ok={r['ok']} err={r['error']}", flush=True)
        if not r["ok"]:
            failed.append(i)
        if i < len(messages) - 1:
            time.sleep(0.4)

    # Step 5：總結
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
