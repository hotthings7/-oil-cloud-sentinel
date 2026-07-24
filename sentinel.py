import os, json, hashlib, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import feedparser
from firebase_admin import credentials, firestore, initialize_app
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ---------- ENV ----------
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
HF_TOKEN = os.environ['HF_TOKEN']
firebase_key_raw = os.environ['FIREBASE_KEY_JSON']
firebase_key_dict = json.loads(firebase_key_raw)
cred = credentials.Certificate(firebase_key_dict)
initialize_app(cred)
db = firestore.client()

# ---------- CONFIG ----------
HF_MODEL_URL = "https://api-inference.huggingface.co/models/cardiffnlp/twitter-roberta-base-sentiment-latest"

# Feeds with User-Agent header
FEED_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

RSS_FEEDS = [
    ('https://oilprice.com/rss/main', None),  # already working
    ('https://oilprice.com/rss/energy', None), # broader energy
    ('https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+oil+price&hl=en-US&gl=US&ceid=US:en', None),
    # Reuters energy news (direct feed still works with user-agent)
    ('https://www.reuters.com/arc/outboundfeeds/v3/all/?outputType=xml&category=energy', None),
    # MarketWatch oil
    ('https://feeds.marketwatch.com/marketwatch/topics/subject/oil-markets', None),
    # Nitter search – multiple fallback instances
    ('https://nitter.net/search/rss?f=tweets&q=crude+oil+OR+OPEC+OR+oil+price&since=1h', None),
    ('https://nitter.unixfox.eu/search/rss?f=tweets&q=crude+oil+OR+OPEC+OR+oil+price&since=1h', None),
    ('https://nitter.1d4.us/search/rss?f=tweets&q=crude+oil+OR+OPEC+OR+oil+price&since=1h', None),
]

OIL_KEYWORDS = [... same as before ...]  # I'll keep them unchanged but you can copy from last version

WINDOW_MINUTES = 240
HEARTBEAT_HOURS = 6

# Rule patterns unchanged ... (copy from previous code)

# VADER analyzer (local, no internet)
vader = SentimentIntensityAnalyzer()

# Fallback sentiment words (last resort)
NEGATIVE_WORDS = ['fall', 'drop', 'decline', 'plunge', 'tumble', 'sink', 'slide', 'loss', 'bearish', 'sell-off', 'crash', 'weak']
POSITIVE_WORDS = ['rise', 'gain', 'surge', 'jump', 'rally', 'spike', 'soar', 'climb', 'bullish', 'rebound', 'strong', 'record high']

# ---------- HELPERS ----------
def is_oil_related(text):
    t = text.lower()
    return any(k in t for k in OIL_KEYWORDS)

def dedup_key(title, link):
    return hashlib.sha256((title + link).encode()).hexdigest()

def is_new(item_id):
    doc_ref = db.collection('seen_news').document(item_id)
    doc = doc_ref.get()
    if doc.exists:
        print(f"  [SKIP] duplicate: {item_id[:12]}...")
        return False
    doc_ref.set({'created_at': datetime.now(timezone.utc)})
    if hash(item_id) % 100 == 0:
        cleanup_old()
    return True

def cleanup_old():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    old_docs = db.collection('seen_news').where('created_at', '<', cutoff).limit(50).stream()
    for doc in old_docs:
        doc.reference.delete()

def fetch_rss():
    items = []
    now = datetime.now(timezone.utc)
    for url, extra_headers in RSS_FEEDS:
        print(f"Fetching {url}")
        try:
            # feedparser can accept extra headers via the 'agent' parameter? Not directly.
            # We'll use urllib to add User-Agent globally for feedparser.
            # Custom opener
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-Agent', FEED_HEADERS['User-Agent'])]
            # feedparser can take a 'handlers' or we can pass the response object.
            # Simpler: set global default for urllib
            # Actually, we'll set the User-Agent for feedparser via its USER_AGENT setting.
            if not hasattr(feedparser, 'USER_AGENT'):
                feedparser.USER_AGENT = FEED_HEADERS['User-Agent']
            # Use feedparser.parse with custom agent
            feed = feedparser.parse(url, agent=FEED_HEADERS['User-Agent'])
            print(f"  Feed entries: {len(feed.entries)}")
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                link = entry.get('link', '')
                full_text = f"{title} {summary}"

                pub_dt = now
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                delta = (now - pub_dt).total_seconds() / 60
                print(f"  Article: '{title[:80]}...' | age: {delta:.1f} mins | keyword: {is_oil_related(full_text)}")
                if delta > WINDOW_MINUTES:
                    print(f"    -> too old ({delta:.1f} > {WINDOW_MINUTES})")
                    continue

                if not is_oil_related(full_text):
                    print("    -> not oil-related")
                    continue

                item = {
                    'id': dedup_key(title, link),
                    'title': title,
                    'summary': summary,
                    'link': link
                }
                print(f"    -> ACCEPTED, id={item['id'][:12]}...")
                items.append(item)
        except Exception as e:
            print(f"  ERROR fetching {url}: {e}")
    return items

# ---------- SENTIMENT (3‑tier: HF → VADER → keyword) ----------
def hf_sentiment(text):
    """Try Hugging Face; if fails, use VADER."""
    payload = json.dumps({"inputs": text[:3000]}).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {HF_TOKEN}',
        'Content-Type': 'application/json'
    }
    try:
        req = urllib.request.Request(HF_MODEL_URL, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
            scores = result[0]
            best = max(scores, key=lambda x: x['score'])
            label = best['label']
            if label == 'positive': return 'POS'
            elif label == 'negative': return 'NEG'
            else: return 'NEU'
    except Exception as e:
        print(f"HF error: {e}")
    # Fallback to VADER
    return vader_sentiment(text)

def vader_sentiment(text):
    """VADER rule-based sentiment (offline)."""
    score = vader.polarity_scores(text)
    if score['compound'] >= 0.05:
        return 'POS'
    elif score['compound'] <= -0.05:
        return 'NEG'
    else:
        return 'NEU'

def keyword_sentiment(text):
    """Last resort if VADER somehow fails (not needed but keep)."""
    t = text.lower()
    neg = sum(1 for w in NEGATIVE_WORDS if w in t)
    pos = sum(1 for w in POSITIVE_WORDS if w in t)
    if neg > pos:
        return 'NEG'
    elif pos > neg:
        return 'POS'
    else:
        return 'NEU'

def oil_signal(title, summary):
    text = f"{title} {summary}".lower()
    # Rules first
    for pat in BEARISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BEARISH', f"Rule: {m.group()}")
    for pat in BULLISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BULLISH', f"Rule: {m.group()}")
    # AI sentiment (HF → VADER)
    sentiment = hf_sentiment(text)
    if sentiment == 'POS':
        return ('BULLISH', 'Sentiment positive')
    elif sentiment == 'NEG':
        return ('BEARISH', 'Sentiment negative')
    else:
        return ('NEUTRAL', 'Sentiment neutral')

# ---------- TELEGRAM ----------
def send_telegram(text):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': 'true'
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Telegram sent: {resp.read().decode()[:100]}")
    except Exception as e:
        print(f"Telegram send error: {e}")

def maybe_send_heartbeat():
    heartbeat_doc = db.collection('state').document('last_heartbeat')
    doc = heartbeat_doc.get()
    now = datetime.now(timezone.utc)
    if doc.exists:
        last = doc.to_dict().get('timestamp')
        if last:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt) < timedelta(hours=HEARTBEAT_HOURS):
                return
    msg = f"💓 Cloud Sentinel heartbeat — online and scanning every 5 minutes. Next check in {HEARTBEAT_HOURS}h."
    send_telegram(msg)
    heartbeat_doc.set({'timestamp': now.isoformat()})

# ---------- MAIN ----------
def main():
    now = datetime.now(timezone.utc)
    print(f"\n=== RUNNING at {now.isoformat()} ===")
    items = fetch_rss()
    print(f"Total oil candidates after filters: {len(items)}")
    sent = 0
    for item in items:
        if not is_new(item['id']):
            continue
        signal, reason = oil_signal(item['title'], item['summary'])
        if signal in ('BULLISH', 'BEARISH'):
            emoji = '🟢' if signal == 'BULLISH' else '🔴'
            msg = f"{emoji} *{signal}* for Oil\n📰 {item['title']}\n💡 {reason}\n🔗 [Source]({item['link']})"
            send_telegram(msg)
            sent += 1
        else:
            print(f"  Neutral signal skipped: {item['title'][:80]}")
    print(f"Sent {sent} signals this run.")
    maybe_send_heartbeat()

if __name__ == '__main__':
    main()
