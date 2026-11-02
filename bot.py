
import datetime as dt
import email.utils
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID", "").strip()
    or "-1004343274047"
)

DRY_RUN = os.getenv("DRY_RUN", "0").lower() in (
    "1", "true", "yes"
)

MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-2.5-flash"
)

QUERIES = [
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


def http(url, payload=None, headers=None, timeout=30):
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0",
            **(headers or {}),
        },
    )

    with urllib.request.urlopen(
        req, timeout=timeout
    ) as response:
        return response.read()


def gather(day):
    start = dt.datetime.combine(
        day, dt.time(15), KST
    )
    end = dt.datetime.combine(
        day, dt.time(21), KST
    )

    results = {}

    for query in QUERIES:
        url = (
            "https://news.google.com/rss/search?"
            + urllib.parse.urlencode({
                "q": query + " when:7d",
                "hl": "ko",
                "gl": "KR",
                "ceid": "KR:ko",
            })
        )

        try:
            root = ET.fromstring(http(url))

            for entry in root.findall(".//item"):
                title = (
                    entry.findtext("title") or ""
                ).strip()

                link = (
                    entry.findtext("link") or ""
                ).strip()

                stamp = entry.findtext("pubDate") or ""

                if not title or not link or not stamp:
                    continue

                when = (
                    email.utils
                    .parsedate_to_datetime(stamp)
                    .astimezone(KST)
                )

                if not start <= when <= end:
                    continue

                source = entry.find("source")

                source_name = (
                    source.text.strip()
                    if source is not None and source.text
                    else "News"
                )

                key = re.sub(
                    r"\W+", "", title.casefold()
                )

                results.setdefault(
                    key,
                    {
                        "title": title,
                        "url": link,
                        "source": source_name,
                        "time": when.isoformat(),
                    },
                )

        except Exception as exc:
            logging.warning(
                "RSS error for %s: %s",
                query,
                exc,
            )

    return sorted(
        results.values(),
        key=lambda x: x["time"],
        reverse=True,
    )[:25]


def ai_digest(items):
    key = os.getenv(
        "GEMINI_API_KEY", ""
    ).strip()

    if not key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY"
        )

    headlines = [
        {
            "id": i,
            "title": x["title"],
            "source": x["source"],
        }
        for i, x in enumerate(items)
    ]

    prompt = (
        "You are an institutional Korean equities "
        "news editor. Using ONLY the supplied RSS "
        "headlines, select 10-15 important distinct "
        "stories (or fewer if insufficient). "
        "Translate to English. Never invent details, "
        "figures, article contents or quotations. "
        "Do not provide opinions or investment "
        "implications. Keep each summary to ONE "
        "factual sentence strictly supported by "
        "its headline. Return ONLY JSON of the form "
        '{"articles":[{"id":0,'
        '"topic":"SEMICONDUCTORS",'
        '"headline":"English headline",'
        '"summary":"One factual English sentence"}]}. '
        "Use valid input IDs. Keep total text concise "
        "to fit one Telegram message. Input: "
        + json.dumps(
            headlines,
            ensure_ascii=False,
        )
    )

    body = json.dumps({
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 3500,
        },
    }).encode("utf-8")

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{MODEL}:generateContent"
    )

    last_error = None

    for attempt in range(3):
        try:
            raw = http(
                url,
                body,
                {
                    "Content-Type": "application/json",
                    "x-goog-api-key": key,
                },
                timeout=75,
            )

            data = json.loads(raw)

            response_text = "".join(
                p.get("text", "")
                for c in data.get("candidates", [])
                for p in c.get(
                    "content", {}
                ).get("parts", [])
            )

            generated = json.loads(
                response_text
            )["articles"]

            output = []
            used = set()

            for article in generated:
                idx = article.get("id")

                if (
                    type(idx) is not int
                    or not 0 <= idx < len(items)
                    or idx in used
                ):
                    continue

                topic = re.sub(
                    r"[^A-Z0-9 &/-]",
                    "",
                    str(
                        article.get("topic", "NEWS")
                    ).upper(),
                )[:35]

                headline = re.sub(
                    r"\s+",
                    " ",
                    str(
                        article.get("headline", "")
                    ).strip(),
                )[:160]

                summary = re.sub(
                    r"\s+",
                    " ",
                    str(
                        article.get("summary", "")
                    ).strip(),
                )[:220]

                if not headline or not summary:
                    continue

                output.append((
                    items[idx],
                    topic or "NEWS",
                    headline,
                    summary,
                ))

                used.add(idx)

            if output:
                logging.info(
                    "Gemini generated %d articles",
                    len(output),
                )
                return output[:15]

            raise ValueError(
                "No usable Gemini articles"
            )

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            KeyError,
        ) as exc:
            last_error = exc

            logging.warning(
                "Gemini attempt %d/3 failed: %s",
                attempt + 1,
                str(exc)[:200],
            )

            if attempt < 2:
                time.sleep(
                    3 * (attempt + 1)
                )

    raise RuntimeError(
        "Gemini failed after 3 attempts: "
        f"{last_error}"
    )


def compose(articles, day):
    header = (
        "KOREA MARKET | EVENING NEWS DIGEST\n"
        f"{day:%d %b %Y} | 15:00-21:00 KST"
    )

    message = header
    count = 0

    for item, topic, headline, summary in articles:
        section = (
            f"\n\n# {topic}: {headline}\n"
            f"{summary}\n"
            f"Source: {item['source']}\n"
            f"{item['url']}"
        )

        if len(message) + len(section) > 4000:
            continue

        message += section
        count += 1

    if not count:
        raise RuntimeError(
            "No article fits one Telegram message"
        )

    logging.info(
        "Single-message digest: %d stories, "
        "%d characters",
        count,
        len(message),
    )

    return message


def send(message):
    token = os.getenv(
        "TELEGRAM_BOT_TOKEN", ""
    ).strip()

    if not token:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    url = (
        f"https://api.telegram.org/bot{token}/"
        "sendMessage"
    )

    result = json.loads(
        http(url, payload, timeout=30)
    )

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram error: "
            + result.get(
                "description", "unknown"
            )
        )

    logging.info(
        "Exactly one Telegram message sent"
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(message)s"
        ),
    )

    raw_day = os.getenv(
        "DIGEST_DATE", ""
    ).strip()

    day = (
        dt.date.fromisoformat(raw_day)
        if raw_day
        else dt.datetime.now(KST).date()
    )

    items = gather(day)

    logging.info(
        "Eligible RSS entries: %d",
        len(items),
    )

    if not items:
        logging.warning(
            "No stories in requested window; "
            "nothing sent"
        )
        return

    articles = ai_digest(items)
    message = compose(articles, day)

    if DRY_RUN:
        print(message)

        logging.info(
            "DRY RUN: no Telegram message sent"
        )
    else:
        send(message)


if __name__ == "__main__":
    main()
