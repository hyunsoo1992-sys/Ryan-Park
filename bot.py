
"""Korean equity news digest: Google News RSS -> OpenAI -> Telegram.

Required secrets: OPENAI_API_KEY, TELEGRAM_BOT_TOKEN.
Optional: TELEGRAM_CHAT_ID, OPENAI_MODEL, DIGEST_DATE (YYYY-MM-DD), DRY_RUN (1/0).
"""
import datetime as dt
import email.utils
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1004343274047")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")

TOPICS = [
    "site:reuters.com South Korea stocks semiconductor",
    "site:yonhapnews.co.kr 증시 기업 공시",
    "site:hankyung.com 증시 반도체 기업",
    "site:mk.co.kr/news 주식 기업 투자",
    "site:biz.chosun.com 기업 주식 반도체",
    "site:ft.com South Korea stocks",
    "site:bloomberg.com South Korea stocks",
    "site:dart.fss.or.kr 공시",
    "한국 증시 반도체 조선 방산 은행 배터리",
]


def request(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 KoreaMarketNewsBot/1.1",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:700]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def get_feed(query, date):
    # Search with extra calendar-day buffer.
    # The final 15:00-21:00 KST filter is applied in collect().
    after = date - dt.timedelta(days=2)
    before = date + dt.timedelta(days=2)

    q = (
        f"{query} "
        f"after:{after.isoformat()} "
        f"before:{before.isoformat()}"
    )

    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {
            "q": q,
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko",
        }
    )

    root = ET.fromstring(request(url))
    entries = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = item.findtext("pubDate") or ""
        source = item.find("source")

        try:
            timestamp = (
                email.utils.parsedate_to_datetime(published)
                .astimezone(KST)
            )
        except (ValueError, TypeError):
            continue

        if title and link:
            entries.append(
                {
                    "title": title,
                    "url": link,
                    "published_kst": timestamp.isoformat(),
                    "source": (
                        source.text
                        if source is not None and source.text
                        else ""
                    ),
                }
            )

    return entries


def collect(date):
    start = dt.datetime.combine(date, dt.time(15, 0), KST)
    end = dt.datetime.combine(date, dt.time(21, 0), KST)

    found = {}

    for topic in TOPICS:
        try:
            feed = get_feed(topic, date)
            logging.info(
                "RSS %s: %d results",
                topic[:45],
                len(feed),
            )

            for item in feed:
                stamp = dt.datetime.fromisoformat(
                    item["published_kst"]
                )

                if not start <= stamp <= end:
                    continue

                normalized = re.sub(
                    r"\W+", "", item["title"].lower()
                )
                key = hashlib.sha256(
                    normalized.encode()
                ).hexdigest()

                found.setdefault(key, item)

        except Exception as exc:
            logging.warning(
                "RSS query failed (%s): %s",
                topic,
                exc,
            )

        time.sleep(0.2)

    return sorted(
        found.values(),
        key=lambda x: x["published_kst"],
        reverse=True,
    )


def openai_digest(items, date):
    api_key = os.environ["OPENAI_API_KEY"]

    instructions = (
        "You are a meticulous Korea institutional equity news editor. "
        "Use ONLY the supplied RSS entries. "
        "RSS titles are not full articles: "
        "do not invent or infer numbers, quotes, specifics or article details. "
        "Select up to 15 genuinely important, unique Korean equity stories, "
        "prioritizing Reuters, Bloomberg, FT, DART, Yonhap, Hankyung, "
        "Maeil and Chosun Biz. "
        "Return plain English text, with no numbering or bullets. "
        "Each entry must be formatted exactly as:\n"
        "# TOPIC: One or two short factual sentences supported by the supplied headline.\n"
        "Source: URL\n\n"
        "Use short TOPIC labels such as SEMICONDUCTORS, BANKS, DEFENSE, SAMSUNG. "
        "No investment implications, opinions, questions, positioning or speculation. "
        "Use the exact provided URL for each entry, even if it is a Google News link. "
        "Never claim to have read or verified full articles. "
        "If fewer than 10 relevant stories exist, output fewer; never fabricate."
    )

    payload = {
        "model": MODEL,
        "instructions": instructions,
        "input": json.dumps(
            {
                "date_kst": date.isoformat(),
                "items": items[:120],
            },
            ensure_ascii=False,
        ),
        "max_output_tokens": 4500,
    }

    raw = request(
        "https://api.openai.com/v1/responses",
        json.dumps(payload).encode("utf-8"),
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )

    result = json.loads(raw)

    chunks = [
        content.get("text", "")
        for block in result.get("output", [])
        for content in block.get("content", [])
        if content.get("type") == "output_text"
    ]

    output = "\n".join(chunks).strip()

    if not output or "# " not in output or "Source:" not in output:
        raise RuntimeError(
            "OpenAI did not return a valid digest"
        )

    return output


def split_telegram(text, limit=3900):
    if len(text) <= limit:
        return [text]

    blocks = re.split(r"\n\s*\n", text)
    parts = []
    current = ""

    for block in blocks:
        candidate = (
            current + ("\n\n" if current else "") + block
        )

        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate

        if len(current) > limit:
            raise RuntimeError(
                "One news item exceeds Telegram's message length limit"
            )

    if current:
        parts.append(current)

    return parts


def telegram_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for chunk in split_telegram(text):
        body = urllib.parse.urlencode(
            {
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
        ).encode()

        response = json.loads(
            request(
                url,
                body,
                {
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                timeout=45,
            )
        )

        if not response.get("ok"):
            raise RuntimeError(
                "Telegram rejected the message: " + str(response)
            )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    today = dt.datetime.now(KST).date()

    date = (
        dt.date.fromisoformat(os.environ["DIGEST_DATE"])
        if os.getenv("DIGEST_DATE")
        else today
    )

    if date > today:
        raise ValueError(
            "DIGEST_DATE cannot be in the future in KST"
        )

    if (
        date == today
        and dt.datetime.now(KST).time() < dt.time(21, 0)
    ):
        raise RuntimeError(
            "Today's 15:00-21:00 KST window has not ended. "
            "Use a past date for testing."
        )

    articles = collect(date)

    logging.info(
        "Found %d eligible RSS entries for %s, 15:00-21:00 KST",
        len(articles),
        date,
    )

    if not articles:
        raise RuntimeError(
            "No in-window news found. "
            "Refusing to fabricate a digest. "
            "Try a recent trading day."
        )

    digest = openai_digest(articles, date)

    message = (
        f"KOREA MARKET NEWS | {date.isoformat()} "
        f"| 15:00-21:00 KST\n\n{digest}"
    )

    if DRY_RUN:
        print(message)
        logging.info(
            "DRY_RUN enabled: no Telegram message sent"
        )
    else:
        telegram_send(message)
        logging.info(
            "Telegram digest sent to channel %s",
            CHAT_ID,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Digest failed")
        sys.exit(1)
