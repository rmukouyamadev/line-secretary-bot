import asyncio
import os
import sys
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen

from dotenv import load_dotenv

import anthropic
from anthropic import APIError
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from linebot import LineBotApi
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    LocationMessage,
    MessageEvent,
    StickerMessage,
    TextMessage,
    TextSendMessage,
)
from linebot.models.sources import SourceGroup, SourceRoom, SourceUser
from linebot.webhook import WebhookParser

load_dotenv()
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GAS_LINE_LOG_WEB_APP_URL = (os.environ.get("GAS_LINE_LOG_WEB_APP_URL") or "").strip()
GAS_LINE_LOG_DAYS = int(os.environ.get("GAS_LINE_LOG_DAYS", "3"))
GAS_GMAIL_WEB_APP_URL = (os.environ.get("GAS_GMAIL_WEB_APP_URL") or "").strip()
GAS_GMAIL_DAYS = int(os.environ.get("GAS_GMAIL_DAYS", "3"))
GAS_GMAIL_MAX = int(os.environ.get("GAS_GMAIL_MAX", "50"))
GAS_FETCH_TIMEOUT = float(
    os.environ.get("GAS_FETCH_TIMEOUT")
    or os.environ.get("GAS_LINE_LOG_FETCH_TIMEOUT", "30")
)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    sys.stderr.write(
        "環境変数 LINE_CHANNEL_SECRET と LINE_CHANNEL_ACCESS_TOKEN を設定してください。\n"
    )
    sys.exit(1)

if not ANTHROPIC_API_KEY:
    sys.stderr.write("環境変数 ANTHROPIC_API_KEY を設定してください。\n")
    sys.exit(1)

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
webhook_parser = WebhookParser(CHANNEL_SECRET)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# LINE テキストメッセージの上限に合わせる
LINE_TEXT_MAX_LEN = 5000
# push_message 1 リクエストあたりのメッセージ数上限
LINE_PUSH_MESSAGES_MAX = 5
# Claude 入力が肥大化しないよう GAS 取得テキストの上限（超過分は切り捨て）
GAS_TEXT_MAX_CHARS = 120_000

CONTEXT_HEADER = "以下は返信を検討するためのコンテキストです。"
SUMMARY_CONTEXT_HEADER = "以下はまとめ対象のコンテキストです。"
LINE_CONTEXT_SECTION = "### LINEの会話履歴"
GMAIL_CONTEXT_SECTION = "### メール一覧"

SUMMARY_USER_INSTRUCTION = """上記のLINEの会話履歴とメール一覧を踏まえ、依頼主向けに以下を日本語で簡潔にまとめてください。
- 直近のやり取りの要点
- 未対応・要確認の事項
- 推奨する次のアクション
データが欠けている場合は、その旨も記載してください。"""

SECRETARY_SYSTEM_PROMPT = """あなたはクライアント対応の秘書です。
依頼主・取引先・問い合わせの相手に対し、丁寧で分かりやすい文体で返答してください。
事実でない内容は断定せず、不明な点は確認する旨を添えてください。
ユーザーが日本語以外で書いた場合は、その言語に合わせて返答して構いません。

【クライアント情報】
- Aさん（株式会社XX）：Webサイトリニューアルの案件。丁寧な敬語で対応。意思決定が慎重なので、選択肢を提示するスタイルが効果的。
- Bさん（個人事業主）：SNS運用の相談。フランクな口調OK。レスポンスを重視する方なので、返信は短めに。

上記の情報を踏まえて、返信案を作成してください。

依頼主（Botの利用者）との会話履歴が渡される場合があります。
直前に提示した返信案への修正指示（例:「もう少しカジュアルにして」）は、
履歴を踏まえて反映した新しい返信案を提示してください。"""

SUMMARY_SYSTEM_PROMPT = """あなたは依頼主の業務秘書です。
提供されたLINEの会話履歴とメール一覧を読み、依頼主が状況を把握できるよう要点を整理して報告してください。
事実でない内容は断定せず、情報が不足している場合はその旨を明記してください。
箇条書きを活用し、読みやすく簡潔にまとめてください。

レポートの形式：
- クライアントごとに分けて報告する
- 各クライアントについて、以下を含める：
  - 直近のやりとりの要約
  - 返信が必要かどうか（「返信が必要」「返信不要」などと明記）
  - 返信が必要な場合は、返信案（丁寧なビジネス文体）
- 返信が必要なものは、緊急度が高い順に並べる
"""

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

app = FastAPI(title="LINE 秘書 Bot")

SUMMARY_KEYWORD = "まとめ"
THINKING_REPLY = "考え中..."
THINKING_REPLY_SUMMARY = "まとめを作成しています..."

# 会話メモリ（メモリ内・ユーザーIDごと）
MEMORY_MAX_TURNS = int(os.environ.get("MEMORY_MAX_TURNS", "5"))
MEMORY_TTL_SECONDS = float(os.environ.get("MEMORY_TTL_SECONDS", "3600"))


class ConversationMemory:
    """ユーザーIDごとに直近の会話を保持し、一定時間で失効する。"""

    def __init__(self, max_turns: int, ttl_seconds: float) -> None:
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds
        self._lock = Lock()
        # user_id -> (最終アクセス時刻, messages)
        self._sessions: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def _is_expired(self, last_access: float, now: float) -> bool:
        return now - last_access > self._ttl_seconds

    def get_messages(self, user_id: str) -> list[dict[str, str]]:
        with self._lock:
            now = time.monotonic()
            entry = self._sessions.get(user_id)
            if entry is None:
                return []
            last_access, messages = entry
            if self._is_expired(last_access, now):
                del self._sessions[user_id]
                return []
            self._sessions[user_id] = (now, messages)
            return list(messages)

    def append_turn(
        self, user_id: str, user_content: str, assistant_content: str
    ) -> None:
        with self._lock:
            now = time.monotonic()
            entry = self._sessions.get(user_id)
            messages: list[dict[str, str]] = []
            if entry is not None:
                last_access, messages = entry
                if self._is_expired(last_access, now):
                    messages = []
            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": assistant_content})
            max_messages = self._max_turns * 2
            if len(messages) > max_messages:
                messages = messages[-max_messages:]
            self._sessions[user_id] = (now, messages)


conversation_memory = ConversationMemory(MEMORY_MAX_TURNS, MEMORY_TTL_SECONDS)


def _push_recipient_id(source) -> str | None:
    """プッシュメッセージの送信先 ID（ユーザー / グループ / ルーム）。"""
    if source is None:
        return None
    if isinstance(source, SourceUser) and source.user_id:
        return source.user_id
    if isinstance(source, SourceGroup) and source.group_id:
        return source.group_id
    if isinstance(source, SourceRoom) and source.room_id:
        return source.room_id
    return None


def _gas_web_app_url(base_url: str, query_params: dict[str, int | str]) -> str | None:
    """GAS WebApp URL にクエリを付与する（既存クエリがあれば & で連結）。"""
    if not base_url:
        return None
    parts = urlsplit(base_url)
    extra = "&".join(f"{k}={v}" for k, v in query_params.items())
    new_query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _fetch_gas_text(url: str | None, *, label: str) -> str | None:
    """GAS WebApp からテキストを取得する。未設定・失敗時は None。"""
    if not url:
        return None
    try:
        req = UrlRequest(
            url,
            headers={"Accept": "text/plain, application/json;q=0.9, */*;q=0.8"},
            method="GET",
        )
        with urlopen(req, timeout=GAS_FETCH_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as e:
        print(f"GAS {label} の取得に失敗しました:", e)
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) > GAS_TEXT_MAX_CHARS:
        text = text[-GAS_TEXT_MAX_CHARS:]
        text = f"（{label}が長いため末尾のみ表示）\n" + text
    return text


def _fetch_gas_line_logs(days: int) -> str | None:
    url = _gas_web_app_url(GAS_LINE_LOG_WEB_APP_URL, {"days": int(days)})
    return _fetch_gas_text(url, label="LINE ログ")


def _fetch_gas_gmail_list(days: int, max_count: int) -> str | None:
    url = _gas_web_app_url(
        GAS_GMAIL_WEB_APP_URL,
        {"days": int(days), "max": int(max_count)},
    )
    return _fetch_gas_text(url, label="メール一覧")


def _fetch_all_context() -> tuple[str | None, str | None]:
    """LINE ログとメール一覧を GAS から取得する。"""
    return (
        _fetch_gas_line_logs(GAS_LINE_LOG_DAYS),
        _fetch_gas_gmail_list(GAS_GMAIL_DAYS, GAS_GMAIL_MAX),
    )


def _is_summary_request(text: str) -> bool:
    return text.strip() == SUMMARY_KEYWORD


def _context_sections(
    line_logs: str | None, gmail_list: str | None
) -> list[str]:
    sections: list[str] = []
    if line_logs:
        sections.extend([LINE_CONTEXT_SECTION, line_logs, ""])
    if gmail_list:
        sections.extend([GMAIL_CONTEXT_SECTION, gmail_list, ""])
    return sections


def _build_claude_user_message(
    user_text: str,
    *,
    line_logs: str | None = None,
    gmail_list: str | None = None,
) -> str:
    """コンテキスト（LINE・メール）と今回のメッセージを Claude 用ユーザー文にまとめる。"""
    sections = _context_sections(line_logs, gmail_list)
    blocks: list[str] = []
    if sections:
        blocks.extend([CONTEXT_HEADER, ""])
        blocks.extend(sections)
        blocks.extend(["---", ""])
    blocks.append(user_text)
    return "\n".join(blocks).strip()


def _build_summary_user_message(
    line_logs: str | None, gmail_list: str | None
) -> str | None:
    """まとめ用: LINE ログとメールをコンテキストとして Claude に渡すユーザー文。"""
    sections = _context_sections(line_logs, gmail_list)
    if not sections:
        return None
    return "\n".join(
        [SUMMARY_CONTEXT_HEADER, ""] + sections + [SUMMARY_USER_INSTRUCTION]
    ).strip()


def _claude_generate_text(
    messages: list[dict[str, str]], *, system_prompt: str
) -> str:
    message = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=messages,
    )
    parts: list[str] = []
    for block in message.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts).strip() or "（返信を生成できませんでした）"


def _split_text_for_line(text: str, max_len: int = LINE_TEXT_MAX_LEN) -> list[str]:
    """LINE の文字数上限に合わせて分割する（改行・空白を優先して切る）。"""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        window = rest[:max_len]
        split_at = window.rfind("\n")
        if split_at < max_len // 2:
            split_at = window.rfind(" ")
        if split_at < max_len // 2:
            split_at = max_len
        chunk = rest[:split_at].rstrip()
        if not chunk:
            chunk = rest[:max_len]
            split_at = max_len
        chunks.append(chunk)
        rest = rest[split_at:].lstrip("\n")
    return chunks


def _push_claude_result(push_to: str, reply: str) -> None:
    chunks = _split_text_for_line(reply)
    try:
        for i in range(0, len(chunks), LINE_PUSH_MESSAGES_MAX):
            batch = [
                TextSendMessage(text=chunk)
                for chunk in chunks[i : i + LINE_PUSH_MESSAGES_MAX]
            ]
            line_bot_api.push_message(push_to, batch)
    except LineBotApiError as e:
        print(e)


def _claude_reply_and_push_sync(push_to: str, user_text: str) -> None:
    """通常メッセージ: 返信案を生成してプッシュする。"""
    line_logs, gmail_list = _fetch_all_context()
    current_user_message = _build_claude_user_message(
        user_text, line_logs=line_logs, gmail_list=gmail_list
    )
    history = conversation_memory.get_messages(push_to)
    messages = history + [{"role": "user", "content": current_user_message}]
    try:
        reply = _claude_generate_text(messages, system_prompt=SECRETARY_SYSTEM_PROMPT)
    except APIError as e:
        reply = (
            "申し訳ございません。ただいま返信を準備できませんでした。"
            "しばらくしてから再度お試しください。"
        )
        print(e)
    else:
        conversation_memory.append_turn(push_to, user_text, reply)
    _push_claude_result(push_to, reply)


def _claude_summary_and_push_sync(push_to: str) -> None:
    """「まとめ」: LINE ログとメールを取得し、要約をプッシュする。"""
    line_logs, gmail_list = _fetch_all_context()
    user_message = _build_summary_user_message(line_logs, gmail_list)
    if user_message is None:
        _push_claude_result(
            push_to,
            "まとめに必要なデータを取得できませんでした。"
            "GAS WebApp の URL 設定と、LINE ログ・メールの取得状況をご確認ください。",
        )
        return
    try:
        reply = _claude_generate_text(
            [{"role": "user", "content": user_message}],
            system_prompt=SUMMARY_SYSTEM_PROMPT,
        )
    except APIError as e:
        reply = (
            "申し訳ございません。ただいままとめを作成できませんでした。"
            "しばらくしてから再度お試しください。"
        )
        print(e)
    _push_claude_result(push_to, reply)


async def _claude_reply_and_push_task(push_to: str, user_text: str) -> None:
    await asyncio.to_thread(_claude_reply_and_push_sync, push_to, user_text)


async def _claude_summary_and_push_task(push_to: str) -> None:
    await asyncio.to_thread(_claude_summary_and_push_sync, push_to)


def _handle_message_event(event: MessageEvent, background_tasks: BackgroundTasks) -> None:
    msg = event.message
    if isinstance(msg, TextMessage):
        is_summary = _is_summary_request(msg.text)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=THINKING_REPLY_SUMMARY if is_summary else THINKING_REPLY
            ),
        )
        push_to = _push_recipient_id(event.source)
        if push_to:
            if is_summary:
                background_tasks.add_task(_claude_summary_and_push_task, push_to)
            else:
                background_tasks.add_task(
                    _claude_reply_and_push_task, push_to, msg.text
                )
        else:
            print("プッシュ送信先を特定できませんでした。source=", event.source)
    elif isinstance(msg, (StickerMessage, LocationMessage)):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="お手数ですが、ご用件はテキストでお送りください。"),
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="テキストでのメッセージにのみお返事できます。文章でお送りください。"
            ),
        )


@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks) -> str:
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = await request.body()
    body_str = body.decode("utf-8")
    try:
        events = webhook_parser.parse(body_str, signature)
    except InvalidSignatureError as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    for event in events:
        if isinstance(event, MessageEvent):
            _handle_message_event(event, background_tasks)

    return "OK"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)
