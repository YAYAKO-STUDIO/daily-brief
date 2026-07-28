"""
Yukina 每日簡報 v3 — repository_dispatch 觸發 + Gemini LLM 整理版

v3 相對 v2 的改動（2026-07-28）：
- 9 則 → 3 則（砍心跳、砍完成訊息；只有失敗才響）
- 6 分類 → 2 分類（AI/工具、商業/市場），Anthropic 官方消息置頂
- 人設 prompt 更新成 2026-07 的現況（求職中、派工 App、加密降權）
- 跨分類去重（TechCrunch 本來在兩類重複出現）
- Gemini JSON 壞掉不再整份放棄，退回純標題列表（v2 失敗率約 15%）

流程：
1. 抓 2 大分類 RSS 近 48 小時條目（跨類去重）
2. 整批交 Gemini 2.5 Flash 整理成 3 條繁中 HTML 訊息
3. 推 Telegram（第 1 則有聲，2-3 則靜音）
"""

import os
import re
import sys
import time
import json
import html
import feedparser
import requests
from datetime import datetime, timezone, timedelta


def _sanitize_error(msg):
    """把 API key 從錯誤訊息移除以免外洩到 Telegram / log。"""
    s = str(msg)
    s = re.sub(r'key=[A-Za-z0-9_\-]+', 'key=***REDACTED***', s)
    s = re.sub(r'AIzaSy[A-Za-z0-9_\-]{30,}', '***REDACTED***', s)
    return s


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

# === 2 大分類 ===
# 每個 feed 帶 (url, 來源簡稱, priority)。priority 越小越先被選進來，
# 同優先度才比發佈時間 —— Anthropic 官方消息對 Yukina 的工作流影響最直接，
# 加密降到最低（她已停止主動交易，不需要每天硬湊）。
SECTIONS = [
    {
        "emoji": "🤖",
        "name": "AI / 工具",
        "limit": 8,
        "feeds": [
            ("https://www.anthropic.com/news/rss.xml", "Anthropic", 0),
            ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch", 1),
            ("https://www.figma.com/blog/feed/", "Figma", 2),
            ("https://blog.adobe.com/en/topics/creativity.rss", "Adobe", 2),
        ],
    },
    {
        "emoji": "💹",
        "name": "商業 / 市場",
        "limit": 10,
        "feeds": [
            ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "CNBC", 0),
            ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "WSJ", 0),
            ("https://news.ycombinator.com/rss", "HN", 1),
            ("https://techcrunch.com/feed/", "TechCrunch", 1),
            ("https://www.cnbc.com/id/19854910/device/rss/rss.html", "CNBC Tech", 1),
            ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk", 3),
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


def get_recent_entries(feeds, seen, limit=8, hours=48):
    """抓多個 RSS 近 N 小時的條目。

    seen 是跨分類共用的已見集合 —— TechCrunch 主 feed 和 AI feed 會出同一篇，
    v2 沒處理，同一則新聞在兩類各出現一次。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for feed_url, source, priority in feeds:
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
                    "source": source,
                    "priority": priority,
                    "pub_dt": pub_dt,
                })
        except Exception as e:
            print(f"  feed {feed_url} parse failed: {e}", flush=True)

    unique = []
    for it in sorted(items, key=lambda x: (x["priority"], -x["pub_dt"].timestamp())):
        key = it["url"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:limit]


# 人設：Gemini 靠這段判斷「對你影響」寫什麼，過時了整份簡報就會失準。
# 刻意不寫健康狀況 —— 這段會送到 Google 的 API，只寫成工作型態偏好。
PERSONA = """- 一人公司創業者，YAYAKO Studio 主理人
- **正在找正職**：偏好遠端 / 彈性工時 / 非高重複性的工作，人才市場與遠端工作動態對她有實質意義
- Claude Code / AI 自動化重度使用者 —— Anthropic 的產品動態直接改變她每天的工作流，優先度最高
- 正在開發「派工 App」給工程業小型團隊 —— 工程業數位化、小型 ERP、SaaS 訂價與獲客的消息有用
- 內容創作者：社群媒體、網站、影音 / 語音
- 台股有部位；加密貨幣有少量部位但**已停止主動交易**，只有重大事件才需要提，不要每天硬湊
- 主要語言障礙是英文 —— 全部翻成通順繁中，不要留英文長句
- 色弱（顏色相關新聞不必特別強調顏色細節）"""


def build_prompt(all_entries):
    return f"""你是 Yukina 的每日簡報編輯。

Yukina 的背景（2026-07 更新）：
{PERSONA}

今天日期：{DATE_STR}（{WEEKDAY_ZH}）

以下是從 RSS 抓的英文新聞條目（JSON，含 section / title / summary / url / source）：

```json
{json.dumps(all_entries, ensure_ascii=False, indent=2)}
```

請輸出 JSON：`{{"messages": [3 條 HTML 字串]}}`

**第 1 條（今日必看）**：
```
📊 <b>每日簡報 {DATE_STR}（{WEEKDAY_ZH}）</b>

1️⃣ <b>{{標題}}</b>：{{2-3 句摘要}}
👉 {{對 Yukina 的具體影響，一句話}}

2️⃣ <b>{{標題}}</b>：{{2-3 句摘要}}
👉 {{對 Yukina 的具體影響，一句話}}

3️⃣ <b>{{標題}}</b>：{{2-3 句摘要}}
👉 {{對 Yukina 的具體影響，一句話}}
```
從全部條目挑「對 Yukina 最有感」的 3 則。判準是**能不能改變她這週的決定或做法**，不是新聞本身多大。
Anthropic / Claude 相關的重大更新幾乎一定該進必看。

**第 2 條**：🤖 AI / 工具
**第 3 條**：💹 商業 / 市場

第 2-3 條格式：
```
{{emoji}} <b>{{分類名}} | {DATE_STR}</b>

• <b>{{標題（翻成繁中）}}</b>：{{2-3 句繁中摘要}}
→ <a href="{{url}}">{{source}}</a>

(3-5 則)

<b>👉 對你影響：</b>{{1-2 句具體觀點，從一人公司 / Claude Code 使用者 / 求職中 / 創作者角度切入}}
```

**強制規則：**
- 全部繁體中文（禁簡體字）
- 第 1 條選過的新聞，第 2-3 條不要再重複一次
- HTML escape：內文的 `<` `>` `&` 要轉成 `&lt;` `&gt;` `&amp;`（但 `<b>`、`<a href="">`、`<i>` 標籤保留 raw）
- 每條訊息 ≤ 3800 字元
- 「對你影響」必須具體，禁寫「市場有風險請投資人留意」這類空泛廢話
- 某分類今天沒有值得講的 → 寫「• 今日無重大更新（過去 48 小時無新進度）」+「<b>👉 對你影響：</b>無」
- 不要硬湊數量，沒有就說沒有
- 翻譯不要直譯，要符合台灣中文閱讀習慣

**只輸出 JSON，禁用 markdown code fence 包覆。**
"""


def call_gemini(all_entries):
    """整理成 3 條 HTML 訊息。回傳 list[str]。"""
    payload = {
        "contents": [{"parts": [{"text": build_prompt(all_entries)}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    }

    # Retry on transient errors (5xx, timeout) — Gemini 偶爾 503 Service Unavailable
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(GEMINI_URL, json=payload, timeout=180)
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code} on attempt {attempt + 1}/3"
                print(f"Gemini transient error: {last_err}, retrying in 30s...", flush=True)
                if attempt < 2:
                    time.sleep(30)
                    continue
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            messages = json.loads(text)["messages"]
            if not isinstance(messages, list) or len(messages) != 3:
                got = len(messages) if hasattr(messages, "__len__") else "N/A"
                raise ValueError(f"expected list of 3, got {type(messages).__name__} len={got}")
            return messages
        except requests.exceptions.Timeout:
            last_err = f"Timeout on attempt {attempt + 1}/3"
            print(f"Gemini {last_err}, retrying in 30s...", flush=True)
            if attempt < 2:
                time.sleep(30)
                continue
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # v2 在這裡直接放棄整份簡報，佔了約 15% 的失敗。改成重試，仍失敗才降級。
            last_err = f"Bad response on attempt {attempt + 1}/3: {e}"
            print(f"Gemini {last_err}, retrying in 10s...", flush=True)
            if attempt < 2:
                time.sleep(10)
                continue
        except requests.exceptions.HTTPError as e:
            # Non-transient HTTP error (4xx other than 429) — don't retry
            raise RuntimeError(_sanitize_error(e))
    raise RuntimeError(f"Gemini failed after 3 attempts. Last error: {last_err}")


def build_fallback(sections_entries):
    """Gemini 掛掉時的降級版：純標題列表，英文原文但至少有東西可看。"""
    lines = [
        f"📊 <b>每日簡報 {DATE_STR}（{WEEKDAY_ZH}）</b>",
        "",
        "⚠️ LLM 整理失敗，以下是未經整理的原始標題（英文）：",
        "",
    ]
    for section, entries in sections_entries:
        lines.append(f"{section['emoji']} <b>{html.escape(section['name'])}</b>")
        if not entries:
            lines.append("• 今日無新條目")
        for e in entries[:6]:
            title = html.escape(e["title"])
            url = html.escape(e["url"], quote=True)
            lines.append(f'• <a href="{url}">{title}</a>（{html.escape(e["source"])}）')
        lines.append("")
    return "\n".join(lines)[:3800]


def main():
    print(f"=== Daily Brief v3 {DATE_STR} {TIME_STR} ===", flush=True)

    # Step 1：抓 RSS（跨分類去重）
    seen = set()
    sections_entries = []
    all_entries = []
    for section in SECTIONS:
        entries = get_recent_entries(section["feeds"], seen, limit=section["limit"], hours=48)
        sections_entries.append((section, entries))
        for e in entries:
            all_entries.append({
                "section": section["name"],
                "title": e["title"],
                "summary": e["summary"][:400],
                "url": e["url"],
                "source": e["source"],
            })
        print(f"  {section['name']}: {len(entries)} entries", flush=True)
    print(f"Total entries collected: {len(all_entries)}", flush=True)

    if not all_entries:
        send_telegram(
            f"⚠️ 每日簡報 {DATE_STR}：所有 RSS 都沒抓到條目，可能是來源全掛或網路問題。",
            silent=False,
        )
        sys.exit(1)

    # Step 2：Gemini 整理（失敗降級為純標題列表，不再整份放棄）
    degraded = False
    try:
        messages = call_gemini(all_entries)
        print("Gemini integration OK: 3 messages received", flush=True)
    except Exception as e:
        safe_err = _sanitize_error(e)[:200]
        print(f"Gemini failed, falling back to raw titles: {safe_err}", flush=True)
        messages = [build_fallback(sections_entries)]
        degraded = True

    # Step 3：推送（第 1 則有聲，其餘靜音）
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

    # v2 每天推一則「✅ 完成」，對使用者是雜訊 —— 只有出事才響。
    if failed:
        send_telegram(
            f"⚠️ 簡報有 {len(failed)} 條推送失敗 (indices: {failed})，請看 GitHub Actions log。",
            silent=False,
        )
        sys.exit(1)
    if degraded:
        sys.exit(1)  # 降級版已送出，但仍讓 Actions 亮紅燈，方便事後發現


if __name__ == "__main__":
    main()
