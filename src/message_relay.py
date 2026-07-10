"""Shared text-formatting helpers for the Discord and Telegram bots.

Message relay between the chats of a union is a planned feature (together
with collecting poll votes through the bot's DMs into forum threads); the
helpers below are the platform-neutral pieces it will build on, and they are
already used by the current commands wherever text crosses a platform
boundary (HTML escaping for Telegram, message-length clipping, header-safe
display names).
"""
import html
import re

def _utf16_index_map(text: str):
    pos_map = {0: 0}
    utf16_pos = 0
    for i, ch in enumerate(text):
        utf16_pos += len(ch.encode("utf-16-le")) // 2
        pos_map[utf16_pos] = i + 1
    return pos_map

def _wrap_blockquote(segment: str) -> str:
    lines = segment.splitlines() or [segment]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)

def telegram_entities_to_discord(text: str, entities):
    """Render a Telegram message (text + entities) as Discord markdown, keeping
    links as [label](url). Party descriptions/ideologies are stored in this
    canonical markdown form."""
    if not text:
        return ""
    if not entities:
        return text

    pos_map = _utf16_index_map(text)
    opens = {}
    closes = {}

    def add_open(i, token):
        opens.setdefault(i, []).append(token)

    def add_close(i, token):
        closes.setdefault(i, []).append(token)

    for e in entities:
        start = pos_map.get(getattr(e, "offset", 0))
        end = pos_map.get(getattr(e, "offset", 0) + getattr(e, "length", 0))
        if start is None or end is None or start >= end:
            continue

        et = getattr(e, "type", "")
        if et == "bold":
            add_open(start, "**"); add_close(end, "**")
        elif et == "italic":
            add_open(start, "*"); add_close(end, "*")
        elif et == "underline":
            add_open(start, "__"); add_close(end, "__")
        elif et == "strikethrough":
            add_open(start, "~~"); add_close(end, "~~")
        elif et == "code":
            add_open(start, "`"); add_close(end, "`")
        elif et == "pre":
            lang = getattr(e, "language", "") or ""
            add_open(start, f"```{lang}\n"); add_close(end, "\n```")
        elif et == "spoiler":
            add_open(start, "||"); add_close(end, "||")
        elif et == "text_link":
            url = getattr(e, "url", "") or ""
            if url:
                add_open(start, "[")
                add_close(end, f"]({url})")
        elif et == "blockquote":
            seg = _wrap_blockquote(text[start:end])
            text = text[:start] + seg + text[end:]
            return telegram_entities_to_discord(text, [x for x in entities if x is not e])

    out = []
    for i, ch in enumerate(text):
        if i in closes:
            out.append("".join(reversed(closes[i])))
        if i in opens:
            out.append("".join(opens[i]))
        out.append(ch)
    end_idx = len(text)
    if end_idx in closes:
        out.append("".join(reversed(closes[end_idx])))
    return "".join(out)

def discord_to_telegram_html(text: str):
    """Render Discord markdown (the canonical stored form) as Telegram HTML,
    turning [label](url) links into <a> tags."""
    if not text:
        return ""

    text = re.sub(r"<(https?://[^\s>]+)>", r"\1", text)

    escaped = html.escape(text)

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )

    escaped = re.sub(
        r"```([a-zA-Z0-9_-]*)\n([\s\S]*?)```",
        lambda m: f"<pre><code>{m.group(2)}</code></pre>",
        escaped,
    )
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<u>\1</u>", escaped)
    escaped = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"\|\|([^|]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)

    lines = escaped.splitlines()
    converted = []
    for ln in lines:
        if ln.startswith("&gt; "):
            converted.append(f"<blockquote>{ln[5:]}</blockquote>")
        else:
            converted.append(ln)
    return "\n".join(converted)

def escape_html(text: str):
    return html.escape(text or "")

DISCORD_MSG_LIMIT = 2000
TELEGRAM_MSG_LIMIT = 4096

def clip_text(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

def _clip_escaped_html(escaped, limit):
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: max(limit - 1, 0)]
    cut = re.sub(r"&[#a-zA-Z0-9]{0,9}$", "", cut)
    return cut.rstrip() + "…"

def build_telegram_text(header, body_html, body_plain):
    """Собирает header+body для Telegram с учётом лимита 4096 символов.
    Если форматированный body слишком длинный, откатывается на экранированный
    plain-текст, чтобы обрезка не ломала HTML-теги."""
    header_html = escape_html(header)
    text = f"{header_html}\n{body_html}".strip()
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return text
    budget = max(TELEGRAM_MSG_LIMIT - len(header_html) - 1, 0)
    body = _clip_escaped_html(escape_html(body_plain or ""), budget)
    return f"{header_html}\n{body}".strip()

def clean_display_name(value, max_len=64):
    """Имена пользователей/чатов попадают в заголовки сообщений бота:
    убираем переводы строк (защита от подделки заголовка) и ограничиваем длину."""
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    return cleaned[:max_len] or "Unknown"
