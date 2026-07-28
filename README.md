# Yukina 每日簡報

每天台北時間 12:27 自動抓 RSS，交 Gemini 整理成 3 則繁中簡報，推到 Yukina 的 Telegram。

## 架構

```
Anthropic cloud routine（每天台北 12:27）
    ↓ repository_dispatch webhook（type: daily_brief）
GitHub Actions「Daily Brief」
    ↓
python daily_brief.py
    ├── 抓 2 大分類 RSS（feedparser，近 48 小時、跨分類去重）
    ├── 整批交 Gemini 2.5 Flash → 3 條 HTML 訊息
    └── 推 Telegram Bot API
```

> ⚠️ **這個 workflow 沒有自己的 cron，只吃 `repository_dispatch`。**
> 外部 routine 一停，簡報就會**無聲無息地死掉**（GitHub 這邊不會亮紅燈，因為根本沒被觸發）。
> 2026-06-23 就是這樣停了一個多月才被發現。
> 當初拿掉 schedule 的原因：GitHub 免費版排程常延遲數小時，會半夜推送。

## 2 大分類

| Emoji | 分類 | RSS 來源 | 備註 |
|-------|------|----------|------|
| 🤖 | AI / 工具 | Anthropic News、TechCrunch AI、Figma Blog、Adobe Creativity | Anthropic 官方消息**優先度最高**，直接影響日常工作流 |
| 💹 | 商業 / 市場 | CNBC Markets、WSJ Markets、Hacker News、TechCrunch、CNBC Tech、CoinDesk | CoinDesk 優先度最低（已停止主動交易，只在重大事件時出現） |

每則訊息安排：

1. **今日必看 3 條** — 有通知音，這是唯一會響的一則
2. 🤖 AI / 工具 — 靜音
3. 💹 商業 / 市場 — 靜音

只有出錯時才會另外發一則有聲警告。

## 需要的 Secrets

在 repo Settings → Secrets and variables → Actions 設定：

- `TELEGRAM_BOT_TOKEN` — Telegram Bot token（從 @BotFather 取得）
- `TELEGRAM_CHAT_ID` — 接收訊息的 chat ID
- `GEMINI_API_KEY` — Google Gemini API key（從 https://aistudio.google.com/apikey 取得，免費）

## 手動觸發

在 Actions tab 找到 "Daily Brief" workflow，點 "Run workflow" 即可立即測試。

## 人設 prompt 要定期更新

`daily_brief.py` 裡的 `PERSONA` 決定「對你影響」那幾句寫得準不準。Yukina 的重心一變（換專案、找到工作、開始/停止某條投資線），**這段沒跟著改，整份簡報就會慢慢失準**。

刻意不寫健康狀況——這段會送到 Google 的 API，只寫成工作型態偏好。

## 架構升級紀錄

- **v1（純 RSS）**：抓 RSS → 模板化內容，英文標題列表。已淘汰。
- **v2（Gemini LLM 整理）**：抓 RSS → 整批交 Gemini 2.5 Flash → 繁中摘要 + 「對你影響」段 + 「今日 3 個必看」。6 分類、9 則訊息。
- **v3（當前，2026-07-28）**：
  - 9 則 → 3 則（砍每日心跳與「✅ 完成」訊息，只有失敗才響）
  - 6 分類 → 2 分類，Anthropic 置頂、加密降權、Figma/Adobe 併入 AI 類（官方 blog 更新太稀疏，原本常整則「今日無重大更新」）
  - 跨分類去重（TechCrunch 主 feed 與 AI feed 會出同一篇，v2 會重複兩次）
  - 人設更新成 2026-07 現況（求職中、派工 App、加密停止主動交易）
  - **Gemini 壞掉不再整份放棄**：多重試一次，仍失敗就降級推純標題列表（v2 失敗率約 15%，都是 JSON 格式問題，一壞就整天沒簡報）

免費 tier 完全夠用（每日 1 次呼叫、~12K tokens）。
