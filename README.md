# LINE 秘書ボット

LINE 公式アカウント上で動く AI 秘書ボット。Claude API を使い、クライアントへの返信案生成・会話の要約を自動で行います。

## 機能

- **返信案生成** — 受け取ったメッセージに対し、登録済みクライアント情報を踏まえた返信案を Claude が生成。修正指示にも会話履歴を踏まえて対応
- **まとめモード** — 「まとめ」と送信すると、直近の LINE 会話ログとメール一覧を AI が要約してレポート
- **会話メモリ** — ユーザーごとに直近の会話を保持し、TTL による自動失効を実装
- **GAS 連携** — Google Apps Script 経由で LINE 会話ログと Gmail 一覧を取得し、コンテキストとして Claude に渡す

## アーキテクチャ

```
LINE → Webhook → FastAPI → Claude API → push_message → LINE
                    ↑
               GAS Web App
          (LINE ログ・Gmail 取得)
```

## 技術スタック

| カテゴリ | 採用技術 |
|---------|---------|
| バックエンド | Python / FastAPI / uvicorn |
| AI | Anthropic API（claude-sonnet） |
| メッセージング | LINE Messaging API（line-bot-sdk） |
| 外部連携 | GAS Web App（LINE ログ・Gmail 取得） |
| 非同期処理 | asyncio / BackgroundTasks |

## セットアップ

```bash
python -m venv venv
venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

`.env` に以下を設定：

```
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
ANTHROPIC_API_KEY=...
GAS_LINE_LOG_WEB_APP_URL=...   # 任意：LINE ログ取得用 GAS URL
GAS_GMAIL_WEB_APP_URL=...      # 任意：Gmail 一覧取得用 GAS URL
```

## 起動

```bash
uvicorn main:app --reload
```
