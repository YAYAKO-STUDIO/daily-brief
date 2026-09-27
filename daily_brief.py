"""
每日簡報 v4 — 拆成「個人 AI 簡報」＋「群組財經時事」兩份

v4 相對 v3 的改動（2026-09-27）：
- 商業 / 市場 → 改成群組通用的「財經時事」，發到 Discord #財經新聞（影響股市的時事，不報明牌）
- AI / 工具 → 留一份給 Yukina 的 Telegram，人設更新：拿掉求職（2026-09-07 起她自理）
- 新增台灣財經來源（Yahoo 股市台股／國際、中央社財經），拿掉跟股市無關的 HN / TechCrunch
- 恢復 GitHub 自己排程：v3 只靠外部 routine 叫醒，routine 停了簡報就無聲無息死掉（6/23 起停了 3 個月）
- 沒設 DISCORD_WEBHOOK_NEWS 時，財經時事改發 Telegram，不會漏

流程：
1. 抓兩組 RSS 近 N 小時條目（跨組去重）
2. 各自交 Gemini 整理（個人 = Telegram HTML、群組 = Discord Markdown）
3. 推送；任一邊失敗都發 Telegram 通知並讓 Actions 亮紅燈
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
    """把 API key / webhook 從錯誤訊息移除以免外洩到 Telegram / log。"""
    s = str(msg)
    s = re.sub(r'key=[A-Za-z0-9_\-]+', 'key=***REDACTED***', s)
    s = re.sub(r'AIzaSy[A-Za-z0-9_\-]{30,}', '***REDACTED***', s)
    s = re.sub(r'discord(app)?\.com/api/webhooks/\S+', 'discord webhook ***REDACTED***', s)
    return s


# === 配置（從 GitHub Secrets 讀） ===
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_NEWS", "")  # 選填；沒設就改發 Telegram
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

# === 兩組來源 ===
# 每個 feed 帶 (url, 來源簡稱, priority)。priority 越小越先被選進來，同優先度才比發佈時間。
AI_FEEDS = [
    ("https://www.anthropic.com/news/rss.xml", "Anthropic", 0),
    ("https://techcrunch.com/category/artificial-intelligence/feed/", "TechCrunch", 1),
    ("https://www.figma.com/blog/feed/", "Figma", 2),
    ("https://blog.adobe.com/en/topics/creativity.rss", "Adobe", 2),
]
# 台灣來源 2026-09-27 實測可抓（經濟日報和中央社內容大量重複，只留中央社）
MARKET_FEEDS = [
    ("https://tw.stock.yahoo.com/rss?category=tw-market", "Yahoo股市", 0),
    ("https://feeds.feedburner.com/rsscna/finance", "中央社", 0),
    ("https://tw.stock.yahoo.com/rss?category=intl-markets", "Yahoo國際", 1),
    ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "CNBC", 1),
    ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "WSJ", 1),
    ("https://www.cnbc.com/id/19854910/device/rss/rss.html", "CNBC Tech", 2),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk", 3),
]

DISCORD_LIMIT = 1900  # Discord 單則上限 2000 字，留點餘裕
DISCORD_FOOTER = "-# AI 依新聞摘要整理，可能有誤，重要消息請點原文確認。僅供了解時事，非投資建議。"


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
        return {"ok": False, "error": _sanitize_error(e)}


def split_for_discord(text, limit=DISCORD_LIMIT):
    """超過上限就在換行處切段，不切斷句子中間。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur.rstrip())
            cur = ""
        cur += line[:limit] + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


def send_discord(text):
    """發到 Discord webhook。allowed_mentions 清空：新聞內容不該 @ 到任何人（含 @everyone）。"""
    try:
        for chunk in split_for_discord(text):
            r = requests.post(
                DISCORD_WEBHOOK,
                json={"content": chunk, "allowed_mentions": {"parse": []}},
                timeout=15,
            )
            if r.status_code >= 300:
                return {"ok": False, "error": f"HTTP {r.status_code} {r.text[:200]}"}
            time.sleep(0.5)
        return {"ok": True, "error": ""}
    except Exception as e:
        return {"ok": False, "error": _sanitize_error(e)}


def get_recent_entries(feeds, seen, limit=8, hours=48, per_source=None):
    """抓多個 RSS 近 N 小時的條目。seen 是跨組共用的已見集合，避免同一篇出現兩次。
    per_source：每個來源最多幾則 —— 不設的話優先度高的來源會把名額吃光（實測財經 14 則全是台灣來源）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for feed_url, source, priority in feeds:
        try:
            f = feedparser.parse(feed_url, agent="Mozilla/5.0 (daily-brief)")
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
                    "summary": re.sub(r"<[^>]+>", "", entry.get("summary", "") or entry.get("description", ""))[:500].strip(),
                    "source": source,
                    "priority": priority,
                    "pub_dt": pub_dt,
                })
        except Exception as e:
            print(f"  feed {feed_url} parse failed: {e}", flush=True)

    unique = []
    per = {}
    for it in sorted(items, key=lambda x: (x["priority"], -x["pub_dt"].timestamp())):
        key = it["url"] or it["title"]
        if key in seen:
            continue
        if per_source and per.get(it["source"], 0) >= per_source:
            continue
        seen.add(key)
        per[it["source"]] = per.get(it["source"], 0) + 1
        unique.append(it)
    return unique[:limit]


def to_prompt_entries(entries):
    return [
        {"title": e["title"], "summary": e["summary"][:400], "url": e["url"], "source": e["source"]}
        for e in entries
    ]


# ---------- 個人版：AI / 工具（Telegram） ----------
# 人設：Gemini 靠這段判斷「對你影響」寫什麼，過時了整份簡報就會失準。
# 刻意不寫健康狀況和求職 —— 這段會送到 Google 的 API。
PERSONA = """- 一人公司創業者，YAYAKO Studio 主理人（互動工具、網站、品牌內容）
- Claude Code / AI 自動化重度使用者 —— Anthropic 的產品動態直接改變她每天的工作流，優先度最高
- 正在開發給工程業小型團隊用的「派工 App」
- 內容創作者：社群媒體、網站、影音
- 主要語言障礙是英文 —— 全部翻成通順繁中，不要留英文長句"""


def build_ai_prompt(entries):
    return f"""你是 Yukina 的每日 AI 工具簡報編輯。

Yukina 的背景（2026-09 更新）：
{PERSONA}

今天日期：{DATE_STR}（{WEEKDAY_ZH}）

以下是 RSS 抓到的新聞條目（JSON，含 title / summary / url / source）：

```json
{json.dumps(to_prompt_entries(entries), ensure_ascii=False, indent=2)}
```

請輸出 JSON：`{{"message": "HTML 字串"}}`，格式：
```
🤖 <b>AI / 工具 | {DATE_STR}（{WEEKDAY_ZH}）</b>

• <b>{{標題（翻成繁中）}}</b>：{{2-3 句繁中摘要}}
→ <a href="{{url}}">{{source}}</a>

(3-5 則)

<b>👉 對你影響：</b>{{1-2 句具體觀點，從一人公司 / Claude Code 使用者 / 創作者角度切入}}
```

**強制規則：**
- 全部繁體中文（禁簡體字），翻譯要符合台灣閱讀習慣
- Anthropic / Claude 相關的重大更新幾乎一定要放
- **只根據提供的 summary 寫，不要補上 summary 裡沒有的數字、日期、人名或細節**
- HTML escape：內文的 `<` `>` `&` 要轉成 `&lt;` `&gt;` `&amp;`（但 `<b>`、`<a href="">` 標籤保留）
- ≤ 3500 字元
- 「對你影響」必須具體，禁寫空泛廢話
- 今天沒有值得講的 → 寫「• 今日無重大更新」，不要硬湊
- 不要提求職、找工作相關的角度

**只輸出 JSON，禁用 markdown code fence 包覆。**
"""


# ---------- 群組版：財經時事（Discord） ----------
def build_market_prompt(entries):
    return f"""你是 Discord 朋友群組「財經新聞」頻道的每日時事編輯。

讀者：一群台灣朋友，台股為主、也看美股，少數人碰加密貨幣。每個人的持股和財務狀況都不同，**不要預設任何人的部位**。
目的：讓大家花 1 分鐘大致了解「現在有哪些事在影響股市」。

今天日期：{DATE_STR}（{WEEKDAY_ZH}）

以下是 RSS 抓到的新聞條目（JSON，含 title / summary / url / source，中英文混合）：

```json
{json.dumps(to_prompt_entries(entries), ensure_ascii=False, indent=2)}
```

請輸出 JSON：`{{"message": "Discord Markdown 字串"}}`，格式：
```
## 📰 財經時事 {DATE_STR}（{WEEKDAY_ZH}）

**1. {{標題（繁中）}}**
{{1-2 句摘要}}
📈 {{為什麼會影響股市，一句話}}
🔗 [{{source}}](<{{url}}>)

(3-5 則，依「對大盤影響多大」排序)
```

**挑選原則**：優先挑會牽動大盤的事 —— 央行利率（Fed、台灣央行）、通膨與就業數據、關稅與貿易、
匯率、台積電 / 輝達等權值股的重大消息或財報、地緣政治。同一件事多家報導就合併成一則。
單一小型股的漲跌、個人理財文、業配文不要選。

**強制規則：**
- 全部繁體中文（禁簡體字），翻譯要符合台灣閱讀習慣
- **只根據提供的 summary 寫，不要補上 summary 裡沒有的數字、日期、人名或細節**
- **絕對不要給買賣建議、目標價、推薦個股或「可以布局」「逢低進場」這類字眼**；只說明事件和它可能影響的方向
- 連結一定要用 `[來源](<網址>)` 的格式（網址外面要有角括號）
- 整則 ≤ 1700 字元
- 今天沒有值得講的 → 寫「今日無重大財經消息」，不要硬湊

**只輸出 JSON，禁用 markdown code fence 包覆。**
"""


def call_gemini(prompt):
    """回傳 Gemini 產的 message 字串。"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.4},
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
            message = json.loads(text)["message"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("empty message")
            return message
        except requests.exceptions.Timeout:
            last_err = f"Timeout on attempt {attempt + 1}/3"
            print(f"Gemini {last_err}, retrying in 30s...", flush=True)
            if attempt < 2:
                time.sleep(30)
                continue
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_err = f"Bad response on attempt {attempt + 1}/3: {e}"
            print(f"Gemini {last_err}, retrying in 10s...", flush=True)
            if attempt < 2:
                time.sleep(10)
                continue
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(_sanitize_error(e))
    raise RuntimeError(f"Gemini failed after 3 attempts. Last error: {last_err}")


def fallback_telegram(title, entries):
    """Gemini 掛掉時的降級版：純標題列表。"""
    lines = [f"{title}", "", "⚠️ AI 整理失敗，以下是未經整理的原始標題：", ""]
    for e in entries[:8] or []:
        lines.append(f'• <a href="{html.escape(e["url"], quote=True)}">{html.escape(e["title"])}</a>（{html.escape(e["source"])}）')
    if not entries:
        lines.append("• 今日無新條目")
    return "\n".join(lines)[:3800]


def fallback_discord(entries):
    lines = [f"## 📰 財經時事 {DATE_STR}（{WEEKDAY_ZH}）", "", "⚠️ AI 整理失敗，以下是原始標題：", ""]
    for e in entries[:8]:
        lines.append(f"• [{e['title']}](<{e['url']}>)（{e['source']}）")
    return "\n".join(lines)


def main():
    print(f"=== Daily Brief v4 {DATE_STR} {TIME_STR} ===", flush=True)
    dry = "--dry-run" in sys.argv  # 只印出來不送出（本機測試用）

    seen = set()
    ai_entries = get_recent_entries(AI_FEEDS, seen, limit=8, hours=48)
    market_entries = get_recent_entries(MARKET_FEEDS, seen, limit=16, hours=36, per_source=3)
    print(f"  AI / 工具: {len(ai_entries)} entries", flush=True)
    print(f"  財經時事: {len(market_entries)} entries", flush=True)

    if not ai_entries and not market_entries:
        if not dry:
            send_telegram(f"⚠️ 每日簡報 {DATE_STR}：所有 RSS 都沒抓到條目，可能是來源全掛或網路問題。", silent=False)
        sys.exit(1)

    problems = []

    # --- 個人：AI / 工具 → Telegram ---
    ai_title = f"🤖 <b>AI / 工具 | {DATE_STR}（{WEEKDAY_ZH}）</b>"
    try:
        ai_msg = call_gemini(build_ai_prompt(ai_entries)) if ai_entries else f"{ai_title}\n\n• 今日無重大更新"
    except Exception as e:
        print(f"Gemini (AI) failed: {_sanitize_error(e)[:200]}", flush=True)
        ai_msg = fallback_telegram(ai_title, ai_entries)
        problems.append("AI 簡報降級為標題列表")

    # --- 群組：財經時事 → Discord（沒設 webhook 就發 Telegram） ---
    try:
        market_msg = call_gemini(build_market_prompt(market_entries)) if market_entries else f"## 📰 財經時事 {DATE_STR}\n今日無重大財經消息"
    except Exception as e:
        print(f"Gemini (market) failed: {_sanitize_error(e)[:200]}", flush=True)
        market_msg = fallback_discord(market_entries)
        problems.append("財經時事降級為標題列表")
    market_msg = market_msg.strip() + "\n" + DISCORD_FOOTER  # 免責聲明由程式加，不靠 AI

    if dry:
        print("\n───── Telegram（AI / 工具）─────\n" + ai_msg)
        print("\n───── Discord（財經時事）─────\n" + market_msg)
        print(f"\nDiscord 會切成 {len(split_for_discord(market_msg))} 則")
        return

    r = send_telegram(ai_msg[:4000], silent=False)
    print(f"  Telegram AI: ok={r['ok']} err={r['error']}", flush=True)
    if not r["ok"]:
        problems.append(f"Telegram 推送失敗：{r['error']}")

    if DISCORD_WEBHOOK:
        r = send_discord(market_msg)
        print(f"  Discord 財經時事: ok={r['ok']} err={r['error']}", flush=True)
        if not r["ok"]:
            problems.append(f"Discord 推送失敗：{r['error']}")
    else:
        # 沒設 webhook：Discord Markdown 轉純文字發 Telegram，至少不漏
        r = send_telegram(html.escape(market_msg)[:4000], silent=True)
        print(f"  Telegram 財經時事（無 Discord webhook）: ok={r['ok']}", flush=True)

    # 只有出事才響
    if problems:
        send_telegram(f"⚠️ 每日簡報 {DATE_STR}：" + "；".join(problems) + "。請看 GitHub Actions log。", silent=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
