"""Korea equity evening digest -> private Telegram channel.
Requires OPENAI_API_KEY and TELEGRAM_BOT_TOKEN in environment.
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '-1004343274047')
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
DRY_RUN = os.getenv('DRY_RUN', '0') == '1'
TOPICS = [
    'site:reuters.com South Korea stocks semiconductor',
    'site:yonhapnews.co.kr 증시 기업 공시',
    'site:hankyung.com 증시 반도체 기업',
    'site:mk.co.kr/news 주식 기업 투자',
    'site:biz.chosun.com 기업 주식 반도체',
    'site:ft.com South Korea stocks',
    'site:bloomberg.com South Korea stocks',
    'site:dart.fss.or.kr 공시',
    '한국 증시 반도체 조선 방산 은행 배터리',
]


def request(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'KoreaMarketNewsBot/1.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def get_feed(query):
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
        'q': query + ' when:1d', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
    root = ET.fromstring(request(url))
    entries = []
    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        published = item.findtext('pubDate') or ''
        source = item.find('source')
        try:
            timestamp = email.utils.parsedate_to_datetime(published).astimezone(KST)
        except (ValueError, TypeError):
            continue
        entries.append({'title': title, 'url': link, 'published_kst': timestamp.isoformat(),
                        'source': source.text if source is not None else ''})
    return entries


def collect(date):
    start = dt.datetime.combine(date, dt.time(15, 0), KST)
    end = dt.datetime.combine(date, dt.time(21, 0), KST)
    found = {}
    for topic in TOPICS:
        try:
            for item in get_feed(topic):
                stamp = dt.datetime.fromisoformat(item['published_kst'])
                if not start <= stamp <= end:
                    continue
                key = hashlib.sha256(re.sub(r'\W+', '', item['title'].lower()).encode()).hexdigest()
                found.setdefault(key, item)
        except Exception as exc:
            logging.warning('RSS query failed: %s: %s', topic, exc)
        time.sleep(0.3)
    return sorted(found.values(), key=lambda x: x['published_kst'], reverse=True)


def openai_digest(items, date):
    api_key = os.environ['OPENAI_API_KEY']
    instructions = (
        'You are a meticulous Korea institutional equity news editor. '
        'Use ONLY the supplied RSS entries; do not invent facts, numbers, quotes, or links. '
        'RSS headlines are NOT full articles: never infer detailed figures or claims absent from headlines. '
        'Select up to 15 genuinely important unique Korean equity market stories, prioritize Reuters, Bloomberg, FT, '
        'DART, Yonhap, Hankyung, Maeil and Chosun Biz. Return plain text in ENGLISH. '
        'Each item MUST use this structure, with no numbers or bullets: '
        '# TOPIC: One or two factual sentences strictly supported by the supplied headline.\nSource: URL\n\n'
        'Use short topic labels such as SEMICONDUCTORS, BANKS, DEFENSE, or SAMSUNG. '
        'No implications, opinions, investor questions, positioning, or commentary. '
        'Do not include stories outside the supplied date window. '
        'RSS links can be Google News redirect URLs; preserve them verbatim rather than inventing a publisher URL. '
        'Do not claim to have verified full article content. If fewer than 10 high-quality stories are available, output fewer.'
    )
    payload = {'model': MODEL, 'instructions': instructions,
               'input': json.dumps({'date_kst': date.isoformat(), 'items': items[:100]}, ensure_ascii=False),
               'max_output_tokens': 4500}
    raw = request('https://api.openai.com/v1/responses', json.dumps(payload).encode(),
                  {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, timeout=90)
    result = json.loads(raw)
    chunks = []
    for block in result.get('output', []):
        for content in block.get('content', []):
            if content.get('type') == 'output_text':
                chunks.append(content['text'])
    text = '\n'.join(chunks).strip()
    if not text or '# ' not in text or 'Source:' not in text:
        raise RuntimeError('Model did not return a valid digest')
    return text


def split_telegram(text, max_length=3900):
    # Telegram message limit is 4096 characters; split only if necessary.
    if len(text) <= max_length:
        return [text]
    blocks = re.split(r'\n\s*\n', text)
    chunks, current = [], ''
    for block in blocks:
        candidate = current + ('\n\n' if current else '') + block
        if len(candidate) > max_length and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
        if len(current) > max_length:
            raise RuntimeError('Single news item exceeds Telegram limit')
    if current:
        chunks.append(current)
    return chunks


def telegram_send(text):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    for chunk in split_telegram(text):
        body = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': chunk,
                                       'disable_web_page_preview': 'true'}).encode()
        response = json.loads(request(url, body, {'Content-Type': 'application/x-www-form-urlencoded'}))
        if not response.get('ok'):
            raise RuntimeError('Telegram rejected message: ' + str(response))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    now = dt.datetime.now(KST)
    date = dt.date.fromisoformat(os.environ['DIGEST_DATE']) if os.getenv('DIGEST_DATE') else now.date()
    articles = collect(date)
    logging.info('Found %s eligible RSS entries for %s 15:00–21:00 KST', len(articles), date)
    if not articles:
        raise RuntimeError('No in-window news found. Refusing to send fabricated digest.')
    digest = openai_digest(articles, date)
    message = f'KOREA MARKET NEWS | {date.isoformat()} | 15:00–21:00 KST\n\n' + digest
    if DRY_RUN:
        print(message)
    else:
        telegram_send(message)
        logging.info('Telegram digest sent to %s', CHAT_ID)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Digest failed')
        sys.exit(1)
