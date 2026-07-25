import os, json, hashlib, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import feedparser
from firebase_admin import credentials, firestore, initialize_app
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ---------- ENV ----------
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
HF_TOKEN = os.environ['HF_TOKEN']
FIREBASE_KEY_JSON = os.environ['FIREBASE_KEY_JSON']

firebase_key_dict = json.loads(FIREBASE_KEY_JSON)
cred = credentials.Certificate(firebase_key_dict)
initialize_app(cred)
db = firestore.client()

# ---------- CONFIG ----------
HF_MODEL_URL = "https://api-inference.huggingface.co/models/cardiffnlp/twitter-roberta-base-sentiment-latest"

RSS_FEEDS = [
    'https://oilprice.com/rss/main',
    'https://oilprice.com/rss/energy',
    'https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+oil+price&hl=en-US&gl=US&ceid=US:en',
    'https://www.investing.com/rss/news_301.rss',
    'https://www.investing.com/rss/news_25.rss',
    'https://www.fxstreet.com/feeds/news/radar/energy',
    'https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best-sectors-energy',
    # Reddit – instant chatter
    'https://www.reddit.com/r/oil/.rss',
    'https://www.reddit.com/r/energy/.rss',
    'https://www.reddit.com/r/crudeoil/.rss',
    'https://www.reddit.com/r/Commodities/.rss',
    'https://www.reddit.com/r/oilandgasworkers/.rss',
    # Fast financial news
    'https://www.cnbc.com/id/100000000/device/rss/rss.html',
    'https://feeds.reuters.com/reuters/businessNews',
]

OIL_KEYWORDS = [
    'crude', 'oil', 'wti', 'brent', 'opec', 'petroleum', 'eia', 'energy',
    'gasoline', 'distillate', 'barrel', 'rig count', 'shale', 'pipeline',
    'refinery', 'sanctions', 'geopolitical', 'supply', 'demand',
    'inventory', 'stockpile', 'production cut', 'output cut'
]

WINDOW_MINUTES = 1440    # 24 hours – catch every relevant story
HEARTBEAT_HOURS = 6

BULLISH_PATTERNS = [
    r'supply disruption', r'output cut', r'production cut',
    r'opec\s*\+?\s*cut', r'extends cuts', r'voluntary cuts',
    r'geopolitical tension', r'sanctions on (iran|venezuela|russia)',
    r'hurricane\s+\w+\s+shuts', r'pipeline outage', r'force majeure',
    r'demand surge', r'recovery in demand', r'economic stimulus',
    r'china\s+(oil\s+)?imports?\s+(surge|rise|record)',
    r'eia.*crude.*draw', r'inventories.*draw', r'stockpile.*decline',
    r'(oil|crude|wti|brent)\s*(prices?\s*)?(surge|jump|spike|rally|soar|explode|rocket|skyrocket|climb|gain|rise|advance)',
    r'(bullish|upward)\s+(for|on)\s+(oil|crude)',
    r'oil\s+(hits|breaks)\s+(new\s+)?(high|record)',
    r'oil\s+prices?\s+(rebound|recover)',
]

BEARISH_PATTERNS = [
    r'increase\s+production', r'ramp\s+up\s+output', r'easing\s+cuts',
    r'opec\s*\+?\s*raise', r'opec\s*\+?\s*boost',
    r'demand destruction', r'recession fears', r'economy slows',
    r'crude build', r'inventories rise', r'stockpiles surge',
    r'eia.*crude.*build', r'inventories.*build',
    r'interest rate hike', r'fed tapering', r'stronger dollar',
    r'alternative energy surge', r'electric vehicle adoption',
    r'(oil|crude|wti|brent)\s*(prices?\s*)?(fall|drop|plunge|tumble|sink|slide|decline|slip|dip|crash|collapse)',
    r'(bearish|downward)\s+(for|on)\s+(oil|crude)',
    r'oil\s+(hits|falls\s+to)\s+(new\s+)?(low|multi-year low)',
    r'oil\s+prices?\s+(extend\s+losses|weaken)',
]

vader = SentimentIntensityAnalyzer()

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

# ---------- RSS ----------
def fetch_rss():
    items = []
    now = datetime.now(timezone.utc)
    for url in RSS_FEEDS:
        print(f"Fetching RSS: {url}")
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            print(f"  Entries: {len(feed.entries)}")
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
                print(f"  Title: '{title[:70]}...' | age: {delta:.0f}m | oil: {is_oil_related(full_text)}")
                if delta > WINDOW_MINUTES:
                    continue
                if not is_oil_related(full_text):
                    continue
                item = {
                    'id': dedup_key(title, link),
                    'title': title,
                    'summary': summary,
                    'link': link
                }
                print(f"    -> ACCEPTED")
                items.append(item)
        except Exception as e:
            print(f"  RSS error: {e}")
    return items

# ---------- SENTIMENT ----------
def get_sentiment(text):
    payload = json.dumps({"inputs": text[:3000]}).encode('utf-8')
    headers = {'Authorization': f'Bearer {HF_TOKEN}', 'Content-Type': 'application/json'}
    try:
        req = urllib.request.Request(HF_MODEL_URL, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
        if isinstance(result, list) and result[0] and isinstance(result[0], list):
            best = max(result[0], key=lambda x: x['score'])
            label = best['label']
            if label == 'positive': return 'POS'
            elif label == 'negative': return 'NEG'
    except:
        pass
    return vader_sentiment(text)

def vader_sentiment(text):
    score = vader.polarity_scores(text)
    if score['compound'] >= 0.05:
        return 'POS'
    elif score['compound'] <= -0.05:
        return 'NEG'
    return 'NEU'

def oil_signal(title, summary):
    text = f"{title} {summary}".lower()
    for pat in BEARISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BEARISH', f"Rule: {m.group()}")
    for pat in BULLISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BULLISH', f"Rule: {m.group()}")
    sentiment = get_sentiment(text)
    if sentiment == 'POS':
        return ('BULLISH', 'AI positive')
    elif sentiment == 'NEG':
        return ('BEARISH', 'AI negative')
    return ('NEUTRAL', 'AI neutral')

# ---------- TELEGRAM ----------
def send_telegram(text):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': 'true'
    }).encode('utf-8')
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5)

def maybe_send_heartbeat():
    doc_ref = db.collection('state').document('last_heartbeat')
    doc = doc_ref.get()
    now = datetime.now(timezone.utc)
    if doc.exists:
        last = doc.to_dict().get('timestamp')
        if last and (now - datetime.fromisoformat(last)) < timedelta(hours=HEARTBEAT_HOURS):
            return
    send_telegram(f"💓 Heartbeat — alive, every 5 min. Next in {HEARTBEAT_HOURS}h.")
    doc_ref.set({'timestamp': now.isoformat()})

# ---------- MAIN ----------
def main():
    print(f"\n=== RUNNING at {datetime.now(timezone.utc).isoformat()} ===")
    items = fetch_rss()
    print(f"Total oil candidates: {len(items)}")
    sent = 0
    for item in items:
        if not is_new(item['id']):
            continue
        signal, reason = oil_signal(item['title'], item['summary'])
        if signal in ('BULLISH', 'BEARISH'):
            emoji = '🟢' if signal == 'BULLISH' else '🔴'
            msg = f"{emoji} *{signal}*\n📰 {item['title']}\n💡 {reason}\n🕒 {datetime.utcnow().strftime('%H:%M UTC')}\n[Source]({item['link']})"
            send_telegram(msg)
            sent += 1
    print(f"Signals sent: {sent}")
    maybe_send_heartbeat()

if __name__ == '__main__':
    main()
