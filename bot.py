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

KST = ZoneInfo('Asia/Seoul')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip() or '-1004343274047'
DRY_RUN = os.getenv('DRY_RUN', '0').lower() in ('1', 'true', 'yes')
MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip()
STRICT_AI = os.getenv('STRICT_AI', '1').lower() in ('1', 'true', 'yes')
MAX_MESSAGE = 3900
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
WORDS = ['삼성', '하이닉스', '반도체', '은행', '금융', '방산', '조선', '배터리', '증시', '코스피', '코스닥', '주식', '주가', '공시', '수주', '자사주', '배당', '유상증자', '투자', '실적', '합병', '상장', 'semiconductor', 'stock', 'kospi', 'korea', 'memory', 'earnings', 'buyback']
SOURCES = ['reuters', 'bloomberg', 'ft.com', 'financial times', '연합뉴스', 'yonhap', '한국경제', 'hankyung', '매일경제', '조선비즈']


def http(url, payload=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=payload, headers={'User-Agent': 'Mozilla/5.0 KoreaDigest/3.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def feed(query):
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({'q': query + ' when:7d', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
    root = ET.fromstring(http(url, timeout=25))
    result = []
    for node in root.findall('.//item'):
        title = (node.findtext('title') or '').strip()
        link = (node.findtext('link') or '').strip()
        raw_date = node.findtext('pubDate') or ''
        source_node = node.find('source')
        if not title or not link:
            continue
        try:
            published = email.utils.parsedate_to_datetime(raw_date).astimezone(KST)
        except (ValueError, TypeError, OverflowError):
            continue
        source = source_node.text.strip() if source_node is not None and source_node.text else 'Unknown'
        result.append({'title': title, 'url': link, 'source': source, 'published': published.isoformat()})
    return result


def collect(day):
    start = dt.datetime.combine(day, dt.time(15), KST)
    end = dt.datetime.combine(day, dt.time(21), KST)
    unique = {}
    for query in QUERIES:
        try:
            items = feed(query)
            logging.info('RSS %s: %d results', query, len(items))
            for item in items:
                stamp = dt.datetime.fromisoformat(item['published'])
                if not start <= stamp <= end:
                    continue
                key = hashlib.sha256(re.sub(r'\W+', '', item['title'].casefold()).encode()).hexdigest()
                unique.setdefault(key, item)
        except Exception as exc:
            logging.warning('RSS feed failed: %s', str(exc)[:200])
    ranked = sorted(unique.values(), key=lambda x: (sum(w in x['title'].casefold() for w in WORDS) * 3 + sum(s in x['source'].casefold() for s in SOURCES) * 2, x['published']), reverse=True)
    logging.info('Eligible RSS entries: %d', len(ranked))
    return ranked[:20]


def gemini(items):
    key = os.getenv('GEMINI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GEMINI_API_KEY is missing')
    payload_items = [{'id': i, 'headline': item['title'], 'source': item['source']} for i, item in enumerate(items)]
    prompt = '''You are a factual institutional Korean equity news editor. Input is RSS HEADLINES ONLY, not article bodies. Return JSON with exactly the schema {"articles":[{"id":0,"topic":"SEMICONDUCTORS","headline":"English headline","summary":"One or two short factual sentences."}]}. Select 10-15 distinct market-relevant articles, or fewer if there are fewer meaningful articles. Translate Korean headlines to English. Preserve input IDs exactly. Use no facts beyond the supplied headline; never invent numbers, details, or context. If a headline cannot support a factual summary, write a short faithful restatement. No investment views, implications, generic commentary, speculation, numbering, or URLs. Each headline max 100 characters and summary max 170 characters. Output JSON only. INPUT: ''' + json.dumps(payload_items, ensure_ascii=False)
    body = json.dumps({'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 3500, 'responseMimeType': 'application/json'}}).encode()
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(MODEL)}:generateContent'
    last_error = None
    for attempt in range(1, 4):
        try:
            logging.info('Gemini attempt %d/3 model=%s', attempt, MODEL)
            raw = http(url, body, {'Content-Type': 'application/json', 'x-goog-api-key': key}, timeout=65)
            data = json.loads(raw)
            content = ''.join(part.get('text', '') for c in data.get('candidates', []) for part in c.get('content', {}).get('parts', []))
            if not content:
                raise RuntimeError('Empty Gemini response')
            rows = json.loads(content)['articles']
            result, seen = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                idx = row.get('id')
                if type(idx) is not int or idx < 0 or idx >= len(items) or idx in seen:
                    continue
                topic = re.sub(r'[^A-Z0-9 &/\-]', '', str(row.get('topic', 'KOREA EQUITIES')).upper())[:32] or 'KOREA EQUITIES'
                headline = re.sub(r'\s+', ' ', str(row.get('headline', ''))).strip()[:120]
                summary = re.sub(r'\s+', ' ', str(row.get('summary', ''))).strip()[:230]
                if not headline or not summary:
                    continue
                result.append({'id': idx, 'topic': topic, 'headline': headline, 'summary': summary})
                seen.add(idx)
                if len(result) == 15:
                    break
            if not result:
                raise RuntimeError('No usable articles returned by Gemini')
            logging.info('Gemini generated %d articles', len(result))
            return result
        except urllib.error.HTTPError as exc:
            details = exc.read().decode('utf-8', errors='replace')[:350].replace(key, '[REDACTED]')
            last_error = f'HTTP {exc.code}: {details}'
            logging.warning('Gemini attempt %d failed: %s', attempt, last_error)
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:
            last_error = str(exc).replace(key, '[REDACTED]')
            logging.warning('Gemini attempt %d failed: %s', attempt, last_error[:350])
        if attempt < 3:
            time.sleep(3 * attempt)
    raise RuntimeError('Gemini failed after retries: ' + str(last_error)[:300])


def compose(day, items, articles):
    header = f'KOREA MARKET | EVENING NEWS DIGEST\n{day:%d %b %Y} | 15:00-21:00 KST'
    sections = []
    for article in articles:
        original = items[article['id']]
        headline = article['headline'].replace('\n', ' ').strip()
        summary = article['summary'].replace('\n', ' ').strip()
        url = original['url']
        section = f"# {article['topic']}: {headline}\n{summary}\nSource: {original['source']}\n{url}"
        sections.append(section)
    result = header
    used = 0
    for section in sections:
        candidate = result + '\n\n' + section
        if len(candidate) > MAX_MESSAGE:
            continue
        result = candidate
        used += 1
    if not used:
        raise RuntimeError('No article fits within one Telegram message')
    logging.info('Single-message digest: %d articles, %d characters', used, len(result))
    return result


def send(message):
