"""Free Korea equity RSS digest -> Telegram. No AI API or paid services."""
import datetime as dt
import email.utils
import hashlib
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
DRY_RUN = os.getenv('DRY_RUN', '0').lower() in ('1', 'true', 'yes')
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
PREFERRED = ('reuters', 'bloomberg', 'financial times', 'ft.com', '연합뉴스', 'yonhap', '한국경제', 'hankyung', '매일경제', 'mk.co.kr', '조선비즈', 'chosunbiz')
TOPIC_WORDS = [
    ('SEMICONDUCTORS', ('반도체', '하이닉스', '삼성전자', '메모리', 'hbm', 'semiconductor', 'chip', 'dram')),
    ('BANKS', ('은행', '금융지주', 'bank')),
    ('DEFENSE', ('방산', '방위', 'defense', 'defence')),
    ('SHIPBUILDING', ('조선', '선박', 'shipbuilding')),
    ('BATTERIES', ('배터리', '이차전지', '2차전지', 'battery')),
    ('AUTOS', ('자동차', '현대차', '기아', 'auto', 'vehicle')),
    ('MARKET', ('코스피', '코스닥', '증시', 'kospi', 'kosdaq', 'stocks')),
]


def request(url, data=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0 (compatible; KoreaDigest/2.0)'})
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.read()


def fetch(query, date):
    after = (date - dt.timedelta(days=2)).isoformat()
    before = (date + dt.timedelta(days=2)).isoformat()
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
        'q': f'{query} after:{after} before:{before}', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'
    })
    root = ET.fromstring(request(url))
    output = []
    for entry in root.findall('.//item'):
        title = (entry.findtext('title') or '').strip()
        link = (entry.findtext('link') or '').strip()
        source = entry.find('source')
        source_name = source.text.strip() if source is not None and source.text else ''
        try:
            published = email.utils.parsedate_to_datetime(entry.findtext('pubDate') or '').astimezone(KST)
        except (ValueError, TypeError):
            continue
        if title and link:
            output.append({'title': title, 'url': link, 'source': source_name, 'time': published})
    return output


def collect(date):
    start = dt.datetime.combine(date, dt.time(15), KST)
    end = dt.datetime.combine(date, dt.time(21), KST)
    found = {}
    for query in TOPICS:
        try:
            feed = fetch(query, date)
            logging.info('RSS %s: %s results', query[:45], len(feed))
            for item in feed:
                if not start <= item['time'] <= end:
                    continue
                title = re.sub(r'\s+[-–|]\s+[^-–|]{2,45}$', '', item['title']).strip()
                normalized = re.sub(r'[^\w가-힣]+', '', title.casefold())
                key = hashlib.sha256(normalized.encode()).hexdigest()
                found.setdefault(key, item)
        except Exception as exc:
            logging.warning('Feed failed (%s): %s', query, exc)
        time.sleep(0.2)
    return list(found.values())


def category(title):
    low = title.casefold()
    for name, words in TOPIC_WORDS:
        if any(word in low for word in words):
            return name
    return 'KOREA EQUITIES'


def priority(item):
    src = (item['source'] + ' ' + item['url']).casefold()
    preferred = any(word in src for word in PREFERRED)
    relevant = any(word in item["title"].casefold() for _, words in TOPIC_WORDS for word in words)
    return (int(preferred), int(relevant), item['time'])


def build_digest(items, date):
    # RSS title is shown as-is: do not pretend it is an English translation.
    # No external translation service is used in this free edition.
    selected = sorted(items, key=priority, reverse=True)[:15]
    if not selected:
        raise RuntimeError('No news in the selected 15:00–21:00 KST window')
    sections = [f'KOREA MARKET NEWS | {date.isoformat()} | 15:00–21:00 KST',
                'RSS headline edition (original language; no article-body verification)']
    for item in selected:
        sections.append(f"# {category(item['title'])}: {item['title']}\nSource: {item['source'] or 'Google News RSS'} | {item['time'].strftime('%H:%M')} KST\n{item['url']}")
    return '\n\n'.join(sections)


def split_message(message, limit=3900):
    if len(message) <= limit:
        return [message]
    blocks = message.split('\n\n')
    output, current = [], ''
    for block in blocks:
        next_text = (current + '\n\n' if current else '') + block
        if len(next_text) > limit and current:
            output.append(current)
            current = block
        else:
            current = next_text
        if len(current) > limit:
            raise RuntimeError('One article block is too long for Telegram')
    if current:
        output.append(current)
    return output


def send(message):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    for chunk in split_message(message):
        payload = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': chunk,
                                          'disable_web_page_preview': 'true'}).encode()
        request(f'https://api.telegram.org/bot{token}/sendMessage', payload)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    now = dt.datetime.now(KST)
    date = dt.date.fromisoformat(os.environ['DIGEST_DATE']) if os.getenv('DIGEST_DATE') else now.date()
    if date > now.date() or (date == now.date() and now.time() < dt.time(21)):
        raise RuntimeError('Select a past date, or run after 21:00 KST today')
    articles = collect(date)
    logging.info('Eligible entries: %s', len(articles))
    message = build_digest(articles, date)
    if DRY_RUN:
        print(message)
        logging.info('Dry run: no Telegram message sent')
    else:
        send(message)
        logging.info('Sent to Telegram channel %s', CHAT_ID)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Digest failed')
        sys.exit(1)
