import os, json, hashlib, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import feedparser
from firebase_admin import credentials, firestore, initialize_app

# ---------- ENV VARS ----------
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

RSS_FEEDS = [
    'https://oilprice.com/rss/main',
    'https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+oil+price&hl=en-US&gl=US&ceid=US:en',
    'https://www.forexfactory.com/ff_calendar_thisweek.xml',
    'https://nitter.net/OPECSecretariat/rss',
    'https://nitter.net/EIAgov/rss',
    'https://feeds.reuters.com/reuters/USenergyNews'   # reliable energy news
]

OIL_KEYWORDS = [
    'crude', 'oil', 'wti', 'brent', 'opec', 'petroleum', 'eia', 'energy',
    'gasoline', 'distillate', 'barrel', 'rig count', 'shale', 'pipeline',
    'refinery', 'sanctions', 'geopolitical', 'supply', 'demand',
    'inventory', 'stockpile', 'production cut', 'output cut', 'barrel'
]

WINDOW_MINUTES = 180          # 3 hours – catches all major moves
HEARTBEAT_HOURS = 6

# ---------- RULE ENGINE (massively expanded) ----------
BULLISH_PATTERNS = [
    # Actions
    r'supply disruption', r'output cut', r'production cut',
    r'opec\s*\+?\s*cut', r'extends cuts', r'voluntary cuts',
    r'geopolitical tension', r'sanctions on (iran|venezuela|russia)',
    r'hurricane\s+\w+\s+shuts', r'pipeline outage', r'force majeure',
    r'demand surge', r'recovery in demand', r'economic stimulus',
    r'china\s+(oil\s+)?imports?\s+(surge|rise|record)',
    r'eia.*crude.*draw', r'inventories.*draw', r'stockpile.*decline',
    # Price moves
    r'(oil|crude|wti|brent)\s*(prices?\s*)?(surge|jump|spike|rally|soar|explode|rocket|skyrocket|climb|gain|rise|advance)',
    r'(bullish|upward)\s+(for|on)\s+(oil|crude)',
    r'oil\s+(hits|breaks)\s+(new\s+)?(high|record)',
    r'stocks\s+(surge|jump|rally)\s+as\s+oil',
    r'oil\s+prices?\s+(rebound|recover)',
]

BEARISH_PATTERNS = [
    # Actions
    r'increase\s+production', r'ramp\s+up\s+output', r'easing\s+cuts',
    r'opec\s*\+?\s*raise', r'opec\s*\+?\s*boost',
    r'demand destruction', r'recession fears', r'economy slows',
    r'crude build', r'inventories rise', r'stockpiles surge',
    r'eia.*crude.*build', r'inventories.*build',
    r'interest rate hike', r'fed tapering', r'stronger dollar',
    r'alternative energy surge', r'electric vehicle adoption',
    # Price moves
    r'(oil|crude|wti|brent)\s*(prices?\s*)?(fall|drop|plunge|tumble|sink|slide|decline|slip|dip|crash|collapse)',
    r'(bearish|downward)\s+(for|on)\s+(oil|crude)',
    r'oil\s+(hits|falls\s+to)\s+(new\s+)?(low|multi-year low)',
    r'stocks\s+(fall|drop|slide)\s+as\s+oil',
    r'oil\s+prices?\s+(extend\s+losses|weaken)',
]

# Fallback sentiment words (used if Hugging Face fails)
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
    for url in RSS_FEEDS:
        print(f"Fetching {url}")
        try:
            if 'forexfactory.com/ff_calendar' in url:
                cal_items = parse_calendar(url, now)
                items += cal_items
                continue

            feed = feedparser.parse(url)
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

def parse_calendar(url, now):
    """Parse ForexFactory weekly calendar XML and return oil-related events happening today or tomorrow."""
    items = []
    try:
        feed = feedparser.parse(url)
        print(f"  Calendar entries found: {len(feed.entries)}")
        for entry in feed.entries:
            title = entry.get('title', '')
            date = entry.get('date', '')   # e.g., "07-24-2026"
            time_str = entry.get('time', '')  # e.g., "10:30am"
            impact = entry.get('impact', '')
            # Only high impact events with oil keywords
            if 'high' not in impact.lower():
                continue
            if not is_oil_related(title):
                continue
            # We don't know the result yet, but we can alert about the event if it's today
            try:
                event_dt = datetime.strptime(f"{date} {time_str}", "%m-%d-%Y %I:%M%p")
                event_dt = event_dt.replace(tzinfo=timezone.utc)
                # Alert if event is within the next 24 hours
                if (event_dt - now) > timedelta(hours=24) or (now - event_dt) > timedelta(hours=1):
                    continue
            except:
                pass  # if date parse fails, still include

            item = {
                'id': dedup_key(title, date+time_str),
                'title': f"📅 {title} ({impact} impact)",
                'summary': f"Scheduled: {date} {time_str}",
                'link': 'https://www.forexfactory.com/calendar'
            }
            print(f"  Calendar event accepted: {title} at {date} {time_str}")
            items.append(item)
    except Exception as e:
        print(f"  Calendar parse error: {e}")
    return items

# ---------- SENTIMENT (HF + FALLBACK) ----------
def hf_sentiment(text):
    """Try Hugging Face, with retry and fallback to keyword heuristic."""
    # First attempt
    payload = json.dumps({"inputs": text[:3000]}).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {HF_TOKEN}',
        'Content-Type': 'application/json'
    }
    for attempt in range(2):
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
            return 'NEU'
        except Exception as e:
            print(f"HF API attempt {attempt+1} error: {e}")
            if attempt == 0:
                time.sleep(2)  # brief pause before retry
            else:
                # Fallback: simple keyword heuristic
                return keyword_sentiment(text)

def keyword_sentiment(text):
    """Simple word-count fallback when HF is down."""
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
    # 1. Rule engine first
    for pat in BEARISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BEARISH', f"Rule: {m.group()}")
    for pat in BULLISH_PATTERNS:
        m = re.search(pat, text)
        if m:
            return ('BULLISH', f"Rule: {m.group()}")
    # 2. Hugging Face (with fallback)
    sentiment = hf_sentiment(text)
    if sentiment == 'POS':
        return ('BULLISH', 'AI positive')
    elif sentiment == 'NEG':
        return ('BEARISH', 'AI negative')
    else:
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
    msg = f"💓 Cloud Sentinel heartbeat — online and scanning every minute. Next check in {HEARTBEAT_HOURS}h."
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
