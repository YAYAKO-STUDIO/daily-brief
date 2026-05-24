# Yukina 每日簡報

每天台北時間 12:30 自動從 6 大分類 RSS 抓最新新聞，推送到 Yukina 的 Telegram。

## 架構

```
GitHub Actions cron (30 4 * * * UTC = 台北 12:30)
    ↓
ubuntu-latest runner
    ↓
python daily_brief.py
    ├── 抓 6 大分類 RSS（feedparser）
    ├── 組 7 條 HTML 訊息（總覽 + 6 分類）
    └── 推 Telegram Bot API
```

## 6 大分類

| Emoji | 分類 | RSS 來源 |
|-------|------|----------|
| 💹 | 台美股 macro | CNBC Markets、WSJ Markets |
| 🤖 | AI 界更新 | TechCrunch AI、Anthropic News |
| 📱 | 科技新聞 | TechCrunch、CNBC Tech |
| 🚀 | 創業 / SaaS 趨勢 | Hacker News |
| 🎨 | 創作者工具更新 | Figma Blog、Adobe Creativity |
| ₿ | 加密貨幣 / 宏觀經濟 | CoinDesk、Cointelegraph |

## 需要的 Secrets

在 repo Settings → Secrets and variables → Actions 設定：

- `TELEGRAM_BOT_TOKEN` — Telegram Bot token（從 @BotFather 取得）
- `TELEGRAM_CHAT_ID` — 接收訊息的 chat ID
- `GEMINI_API_KEY` — Google Gemini API key（從 https://aistudio.google.com/apikey 取得，免費）

## 手動觸發

在 Actions tab 找到 "Daily Brief" workflow，點 "Run workflow" 即可立即測試。

## 架構升級紀錄

- **v1（純 RSS）**：抓 RSS → 模板化內容，英文標題列表。已淘汰。
- **v2（當前，Gemini LLM 整理）**：抓 RSS → 整批交 Gemini 2.5 Flash → 繁中摘要 + 「對你影響」段 + 「今日 3 個必看」。免費 tier 完全夠用（每日 1 次呼叫、~12K tokens）。
