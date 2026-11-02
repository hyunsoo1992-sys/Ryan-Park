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

KST = ZoneInfo('Asia/Seoul')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip() or '-1004343274047'
MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip()
DRY_RUN = os.getenv('DRY_RUN', '0').strip().lower() in ('1', 'true', 'yes')
QUERIES = [
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


def fetch(url, data=None, headers=None, timeout=25):
    request = urllib.request.Request(
        url, data=data,
        headers={'User-Agent': 'Mozilla/5.0', **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def collect(date):
    start = dt.datetime.combine(date, dt.time(15), tzinfo=KST)
    end = dt.datetime.combine(date, dt.time(21), tzinfo=KST)
    articles = {}
    for query in QUERIES:
        url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
            'q': query + ' when:7d', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko',
        })
        try:
            root = ET.fromstring(fetch(url))
            count = 0
            for item in root.findall('.//item'):
                title = (item.findtext('title') or '').strip()
                link = (item.findtext('link') or '').strip()
                pubdate = item.findtext('pubDate') or ''
                source_node = item.find('source')
                source = (source_node.text or '').strip() if source_node is not None else 'News'
                if not title or not link:
                    continue
                try:
                    published = email.utils.parsedate_to_datetime(pubdate).astimezone(KST)
                except (ValueError, TypeError, OverflowError):
                    continue
                if not start <= published <= end:
                    continue
                count += 1
                key = re.sub(r'\W+', '', title.casefold())
                articles.setdefault(key, {'title': title, 'url': link, 'source': source,
                                          'published': published.isoformat()})
            logging.info('RSS %s: %s eligible', query, count)
        except Exception as exc:
            logging.warning('RSS source failed: %s: %s', query, exc)
    return sorted(articles.values(), key=lambda a: a['published'], reverse=True)


def gemini_call(items):
    key = os.getenv('GEMINI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GEMINI_API_KEY missing')
    prompt = '''You are an institutional Korea equity news editor. Produce a factual English
news digest from the provided RSS headlines. Headlines are NOT full articles.
Do not invent facts, figures, context, quotations, price movements or implications.
Translate Korean headlines into accurate English. Select 10-15 distinct, material
Korea equity stories where available. For each, return one concise headline and
1-2 SHORT factual sentences (only facts explicitly supported by its headline).
Avoid generic filler. No opinions or trading recommendations.
Return JSON only: {"articles":[{"id":0,"topic":"SEMICONDUCTORS",
"headline":"...","summary":"..."}]}. Each id must match the input.
Keep each headline under 90 characters and each summary under 170 characters.
Input headlines:\n''' + json.dumps([
        {'id': i, 'title': x['title'], 'source': x['source']}
        for i, x in enumerate(items)
    ], ensure_ascii=False)
    endpoint = ('https://generativelanguage.googleapis.com/v1beta/models/'
                + urllib.parse.quote(MODEL) + ':generateContent')
    payload = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
