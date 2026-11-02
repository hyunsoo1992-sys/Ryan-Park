
"""Korea Market Evening Digest: Gemini AI + RSS + Telegram."""

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

CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID", "").strip()
    or "-1004343274047"
)

DRY_RUN = os.getenv("DRY_RUN", "0").lower() in ("1", "true", "yes")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

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

SOURCE_PRIORITY = {
    "reuters": 10,
    "bloomberg": 10,
    "financial times": 9,
    "ft.com": 9,
    "연합뉴스": 8,
    "yonhap": 8,
    "한국경제": 7,
    "hankyung": 7,
    "매일경제": 7,
    "maeil": 7,
    "조선비즈": 7,
    "chosunbiz": 7,
}

TOPIC_WORDS = {
    "SEMICONDUCTORS": [
        "반도체", "삼성전자", "하이닉스", "hbm",
        "dram", "nand", "memory", "semiconductor",
        "sk hynix", "micron",
    ],
    "AI & TECHNOLOGY": [
        "인공지능", "데이터센터", "ai ", "gpu",
        "nvidia", "cloud", "서버",
    ],
    "BANKS": [
        "은행", "금융지주", "bank", "kb금융",
        "신한", "하나금융", "우리금융",
    ],
    "DEFENSE": [
        "방산", "방위", "국방", "defense",
        "한화에어로", "lig넥스원",
    ],
    "SHIPBUILDING": [
        "조선", "선박", "수주", "shipbuilding",
        "현대중공업", "한화오션",
    ],
    "BATTERIES": [
        "배터리", "2차전지", "이차전지", "battery",
        "lg에너지솔루션", "삼성sdi",
    ],
    "AUTOS": [
        "자동차", "현대차", "기아", "automotive",
        "hyundai motor",
    ],
    "ENERGY": [
        "정유", "원유", "석유", "oil", "refining",
        "lng", "가스",
    ],
    "CAPITAL MARKETS": [
        "증시", "코스피", "코스닥", "주가",
        "자사주", "배당", "공시", "유상증자",
        "인수합병", "ipo", "kospi", "kosdaq",
        "buyback", "dividend", "merger",
    ],
}


def request(url, data=None, headers=None, timeout=35):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 KoreaMarketDigest/2.0",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def get_feed(query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": query + " when:7d",
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    })

    root = ET.fromstring(request(url))
    entries = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = item.findtext("pubDate") or ""
        source = item.find("source")

        if not title or not link:
            continue

        try:
            timestamp = email.utils.parsedate_to_datetime(
                published
            ).astimezone(KST)
        except (ValueError, TypeError, OverflowError):
            continue

        entries.append({
            "title": title,
            "url": link,
            "published_kst": timestamp.isoformat(),
            "source": (
                source.text.strip()
                if source is not None and source.text
                else "Unknown"
            ),
        })

    return entries


def collect(date):
    start = dt.datetime.combine(date, dt.time(15, 0), KST)
    end = dt.datetime.combine(date, dt.time(21, 0), KST)

    found = {}

    for topic in TOPICS:
        try:
            entries = get_feed(topic)
            logging.info("RSS %s: %d results", topic, len(entries))

            for item in entries:
                stamp = dt.datetime.fromisoformat(
                    item["published_kst"]
                )

                if not start <= stamp <= end:
                    continue

                normalized = re.sub(
                    r"\W+", "", item["title"].casefold()
                )
                key = hashlib.sha256(
                    normalized.encode("utf-8")
                ).hexdigest()

                found.setdefault(key, item)

        except Exception as exc:
            logging.warning("RSS query failed: %s", exc)

        time.sleep(0.2)

    return list(found.values())


def classify(title):
    title_lower = title.casefold()

    for topic, keywords in TOPIC_WORDS.items():
        if any(
            word.casefold() in title_lower
            for word in keywords
        ):
            return topic

    return "KOREA EQUITIES"


def priority(item):
    source = item["source"].casefold()
    title = item["title"].casefold()

    source_score = max(
        (
            value
            for name, value in SOURCE_PRIORITY.items()
            if name in source
        ),
        default=1,
    )

    relevance = sum(
        1
        for words in TOPIC_WORDS.values()
        for word in words
        if word.casefold() in title
    )

    return (
        source_score * 10 + min(relevance, 10),
        item["published_kst"],
    )


def shortlist(items):
    ranked = sorted(items, key=priority, reverse=True)

    selected = []
    seen = set()

    for item in ranked:
        # Prevent identical headlines from appearing twice.
        title = re.sub(
            r"\s*[-|]\s*[^-|]+$", "", item["title"]
        ).strip().casefold()

        if title in seen:
            continue

        seen.add(title)
        selected.append(item)

        if len(selected) >= 15:
            break

    return selected


def gemini_digest(items):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing")

    input_items = [
        {
            "id": i,
            "title": item["title"],
            "source": item["source"],
            "published_kst": item["published_kst"],
        }
        for i, item in enumerate(items)
    ]

    prompt = """
You are an institutional Korea equity news editor.

Create an English evening news digest for Telegram.

IMPORTANT:
- You have RSS HEADLINES, not verified full articles.
- Use only the supplied headlines.
- Never invent figures, company announcements, quotes,
  market moves, dates or other details.
- Translate Korean headlines into fluent financial English.
- Produce a concise English headline and 1-2 factual sentences.
- Do not pad summaries with generic commentary.
- Do not add implications, trade ideas, positioning,
  investment opinions or investor questions.
- No numbering.
- Avoid duplicate stories.
- Prioritize market-moving Korean equity news.
- Keep original article IDs unchanged.
- Do not invent URLs; URLs will be attached by Python.
- Return ONLY a valid JSON object.

JSON schema:
{
  "articles": [
    {
      "id": 0,
      "topic": "SEMICONDUCTORS",
      "headline": "Concise factual English headline",
      "summary": "One or two short factual sentences."
    }
  ]
}

Topic labels should be concise, in uppercase.
Do not include markdown fences.

RSS ITEMS:
""" + json.dumps(input_items, ensure_ascii=False)

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{urllib.parse.quote(MODEL)}:generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4500,
            "responseMimeType": "application/json",
        },
    }

    raw = request(
        url,
        json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        timeout=90,
    )

    response = json.loads(raw)

    text = "".join(
        part.get("text", "")
        for candidate in response.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ).strip()

    if not text:
        raise RuntimeError("Gemini returned an empty response")

    parsed = json.loads(text)
    generated = parsed.get("articles", [])

    if not isinstance(generated, list):
        raise RuntimeError("Invalid Gemini article list")

    valid = []
    used_ids = set()

    for article in generated:
        if not isinstance(article, dict):
            continue

        index = article.get("id")

        if type(index) is not int:
            continue

        if not 0 <= index < len(items):
            continue

        if index in used_ids:
            continue

        headline = str(article.get("headline", "")).strip()
        summary = str(article.get("summary", "")).strip()
        topic = str(article.get("topic", "")).strip()

        if not headline or not summary:
            continue

        # Restrict formatting and remove embedded URLs.
        topic = re.sub(r"[^A-Z0-9 &/-]", "", topic.upper())
        topic = topic[:40] or classify(items[index]["title"])

        headline = re.sub(r"\s+", " ", headline)[:220]
        summary = re.sub(r"https?://\S+", "", summary)
        summary = re.sub(r"\s+", " ", summary).strip()[:650]

        valid.append({
            "id": index,
            "topic": topic,
            "headline": headline,
            "summary": summary,
        })

        used_ids.add(index)

    if not valid:
        raise RuntimeError("Gemini produced no valid articles")

    return valid[:15]


def fallback_digest(items):
    """Free fallback: original RSS headlines, no invented summary."""
    return [
        {
            "id": i,
            "topic": classify(item["title"]),
            "headline": item["title"],
            "summary": "",
        }
        for i, item in enumerate(items)
    ]


def build_message(items, date):
    try:
        articles = gemini_digest(items)
        mode = "AI-edited headlines"
        logging.info("Gemini generated %d articles", len(articles))

    except Exception as exc:
        logging.warning(
            "Gemini unavailable; using RSS headline fallback: %s",
            str(exc)[:300],
        )
        articles = fallback_digest(items)
        mode = "RSS headlines only"

    sections = [
        f"KOREA MARKET | EVENING NEWS DIGEST\n"
        f"{date:%d %b %Y} | 15:00-21:00 KST\n"
        f"{mode}\n"
    ]

    for article in articles:
        item = items[article["id"]]

        section = (
            f"# {article['topic']}: {article['headline']}\n"
        )

        if article["summary"]:
            section += f"{article['summary']}\n"

        section += (
            f"Source: {item['source']}\n"
            f"{item['url']}"
        )

        sections.append(section)

    return sections


def split_messages(sections, max_length=3800):
    chunks = []
    current = ""

    for section in sections:
        candidate = (
            current + "\n\n" + section
            if current else section
        )

        if len(candidate) > max_length and current:
            chunks.append(current)
            current = section
        else:
            current = candidate

        if len(current) > max_length:
            raise RuntimeError(
                "Single news item exceeds Telegram limit"
            )

    if current:
        chunks.append(current)

    return chunks


def send(sections):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()

    if not CHAT_ID:
        raise RuntimeError("Telegram chat ID is empty")

    for index, chunk in enumerate(
        split_messages(sections), start=1
    ):
        payload = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        url = f"https://api.telegram.org/bot{token}/sendMessage"

        try:
            raw = request(url, payload)
            result = json.loads(raw)

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description", "Telegram error")
                )

            logging.info(
                "Telegram message %d sent successfully",
                index,
            )

        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode(
                "utf-8", errors="replace"
            ).replace(token, "[REDACTED]")

            logging.error(
                "Telegram HTTP %d: %s",
                exc.code,
                error_body,
            )
            raise RuntimeError(
                f"Telegram delivery failed: HTTP {exc.code}"
            ) from None


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    today = dt.datetime.now(KST).date()

    date = (
        dt.date.fromisoformat(os.environ["DIGEST_DATE"].strip())
        if os.getenv("DIGEST_DATE", "").strip()
        else today
    )

    articles = collect(date)

    logging.info(
        "Eligible RSS entries for %s: %d",
        date,
        len(articles),
    )

    if not articles:
        logging.warning("No news found for requested window")
        return

    selected = shortlist(articles)
    sections = build_message(selected, date)

    if DRY_RUN:
        logging.info("DRY RUN: Telegram delivery disabled")
        for chunk in split_messages(sections):
            print("\n" + chunk + "\n")
    else:
        send(sections)

    logging.info("Digest completed successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Digest failed")
        sys.exit(1)
