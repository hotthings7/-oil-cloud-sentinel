import os, json, hashlib, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import feedparser
from firebase_admin import credentials, firestore, initialize_app

# ---------- ENV ----------
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
FIREBASE_KEY_JSON = os.environ['FIREBASE_KEY_JSON']

firebase_key_dict = json.loads(FIREBASE_KEY_JSON)
cred = credentials.Certificate(firebase_key_dict)
initialize_app(cred)
db = firestore.client()

# ---------- CONFIG ----------
WINDOW_MINUTES = 120          # consider news from last 2 hours
EVENT_MEMORY_MINUTES = 360    # suppress same event within 6 hours
HEARTBEAT_HOURS = 6
MIN_IMPACT_SCORE = 85         # only send if score ≥ 85

# Sources (RSS feeds) – high‑quality, breaking‑news oriented
RSS_FEEDS = [
    'https://oilprice.com/rss/main',
    'https://oilprice.com/rss/energy',
    'https://news.google.com/rss/search?q=oil+OR+crude+OR+OPEC+breaking&hl=en-US&gl=US&ceid=US:en',
    'https://www.investing.com/rss/news_25.rss',               # commodities & oil
    'https://www.reddit.com/r/oil/.rss',
    'https://www.reddit.com/r/Commodities/.rss',
    'https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best-sectors-energy',  # sometimes works
]

# ---------- CATEGORIES & PATTERNS ----------
# (pattern, score, direction, category)
# Direction: 1 = bullish, -1 = bearish, 0 = depends on context (will be determined later)
RULES = [
    # Geopolitical – Hormuz / Iran / Houthi / Saudi / Russia / Ukraine
    (r'(iran|tehran)\s+(attack|strike|missile|drone|blow|hit|sink)\s+(tanker|vessel|ship|port|refinery|oil)', 100, 1, 'Geopolitical'),
    (r'(iran|tehran)\s+(close|block|mine|seize|detain)\s+(hormuz|strait)', 100, 1, 'Geopolitical'),
    (r'hormuz\s+(close|block|mine|shut|under\s+attack)', 100, 1, 'Geopolitical'),
    (r'(houthi|yemen)\s+(attack|strike|missile|drone|hit)\s+(tanker|vessel|ship|saudi|refinery|port)', 98, 1, 'Geopolitical'),
    (r'(saudi|aramco|ras\s+tanura|yanbu|jeddah)\s+(refinery|oil\s+facilit|terminal)\s+(attack|explosion|fire|drone|missile)', 99, 1, 'Geopolitical'),
    (r'(uae|abu\s+dhabi|fujairah)\s+(attack|drone|missile|explosion)', 97, 1, 'Geopolitical'),
    (r'(russia|moscow)\s+(export\s+ban|oil\s+ban|production\s+cut|force\s+majeure)', 96, 1, 'Geopolitical'),
    (r'(ukraine|kyiv)\s+(strike|attack)\s+(russian\s+oil|refinery|pipeline)', 95, 1, 'Geopolitical'),
    (r'(venezuela|caracas)\s+(sanctions|export\s+halt|force\s+majeure)', 92, 1, 'Geopolitical'),
    (r'(force\s+majeure)\s+(declared|on\s+oil|crude)', 94, 1, 'Geopolitical'),
    (r'(pipeline|oil\s+facilit)\s+(explosion|attack|blast|sabotage)', 93, 1, 'Geopolitical'),

    # OPEC / Production Cuts / Quotas
    (r'opec\s*\+?\s*(cut|reduc|emergency\s+meeting|quota)', 97, 1, 'OPEC'),
    (r'(saudi|iraq|kuwait|uae)\s+(voluntary\s+cut|extend\s+cut|deepen\s+cut)', 96, 1, 'OPEC'),
    (r'(opec|oil\s+producer)\s+(increase\s+production|ramp\s+up|ease\s+cuts)', 95, -1, 'OPEC'),

    # Scheduled Reports – EIA, API, CPI, FOMC
    (r'(eia|api)\s+(crude|oil|gasoline|distillate)\s+inventor', 92, 0, 'Scheduled'),  # direction unknown until we see build/draw
    (r'crude\s+inventor\s+(draw|drop|fell|decline)\s+(\d+\.?\d*\s?million)', 94, 1, 'Scheduled'),
    (r'crude\s+inventor\s+(build|rise|increase|surge)\s+(\d+\.?\d*\s?million)', 94, -1, 'Scheduled'),
    (r'gasoline\s+(draw|drop|fell)\s+(\d+\.?\d*\s?million)', 92, 1, 'Scheduled'),
    (r'distillate\s+(draw|drop|fell)\s+(\d+\.?\d*\s?million)', 92, 1, 'Scheduled'),
    (r'(fomc|fed|interest\s+rate)\s+(hike|raise|increase)', 90, -1, 'Scheduled'),
    (r'(fomc|fed|interest\s+rate)\s+(cut|lower|reduce)', 90, 1, 'Scheduled'),
    (r'(cpi|inflation)\s+(beat|surge|above\s+forecast)', 87, -1, 'Scheduled'),
    (r'(gdp|nfp|payrolls)\s+(beat|strong|surge)', 86, 1, 'Scheduled'),

    # Shipping – Hormuz, Bab el‑Mandeb, Red Sea, Suez
    (r'(hormuz|bab\s+el.?mandeb|red\s+sea|suez)\s+(tanker|vessel|ship|traffic)\s+(halt|stop|under\s+attack|closed)', 100, 1, 'Shipping'),
    (r'(tanker|vessel)\s+(attacked|hit|struck|seized|detained)', 99, 1, 'Shipping'),
    (r'(war\s+risk|insurance|premium)\s+(surge|spike|jump)\s+(tanker|shipping|hormuz)', 95, 1, 'Shipping'),
    (r'(port|terminal)\s+(closed|shut|force\s+majeure)\s+(oil|crude)', 94, 1, 'Shipping'),

    # Extreme events (nuclear, war escalation)
    (r'(nuclear|atomic)\s+(threat|strike|weapon)', 100, 1, 'Geopolitical'),
    (r'(us|united\s+states)\s+(sanction|ban)\s+(iran|venezuela|russia)\s+oil', 98, 1, 'Geopolitical'),
    (r'(israel)\s+(attack|strike)\s+(iran|refinery|oil\s+facility)', 100, 1, 'Geopolitical'),

    # Additional high‑impact OPEC/ Supply
    (r'(output|production)\s+cut\s+(\d+\.?\d*\s?million\s?bpd)', 97, 1, 'OPEC'),
    (r'(extend|prolong)\s+output\s+cut', 95, 1, 'OPEC'),
    (r'(supply|export)\s+(disruption|halt|stop)', 93, 1, 'Supply'),
]

# Urgency words – give +20 score if present
URGENCY_PATTERNS = [
    r'\b(FLASH|BREAKING|JUST\s+IN|URGENT|ALERT|LIVE)\b',
]

# Garbage patterns – words/phrases indicating opinion, prediction, analysis
GARBAGE_PATTERNS = [
    r'\b(analyst\s+(says|predict|expect|forecast|see|believe)|opinion|commentary|explainer|analysis|podcast)\b',
    r'\b(could|may|might|possibly|perhaps|predicted|expected|forecast|anticipated)\s+(rise|fall|increase|decrease|move|impact)',
    r'\b(interview|webinar|podcast|weekly\s+outlook|monthly\s+forecast|quarterly\s+report)\b',
    r'\b(technical\s+analysis|chart|resistance|support|MACD|RSI)\b',
]

# Entity extraction – simple lists for Who / Where
ENTITIES_WHO = ['iran', 'tehran', 'israel', 'saudi', 'aramco', 'houthi', 'yemen', 'russia', 'moscow', 'ukraine', 'kyiv',
                'venezuela', 'caracas', 'opec', 'eia', 'api', 'fomc', 'fed', 'us', 'united states', 'china', 'beijing',
                'uae', 'iraq', 'kuwait', 'qatar', 'turkey', 'erbil']
ENTITIES_WHERE = ['hormuz', 'strait of hormuz', 'red sea', 'bab el-mandeb', 'suez', 'yanbu', 'ras tanura', 'fujairah',
                  'jeddah', 'gulf of oman', 'persian gulf', 'black sea', 'bosporus', 'corpus christi', 'houston',
                  'rotterdam', 'singapore', 'china port']

# ---------- HELPERS ----------
def extract_event_signature(title, summary, category):
    text = (title + ' ' + summary).lower()
    # Find who and where
    who = [e for e in ENTITIES_WHO if e in text]
    where = [e for e in ENTITIES_WHERE if e in text]
    # Build a signature: category + main entity + key action
    # Simplify: just take first matched who/where and the highest scoring rule phrase?
    # For dedup we'll use a hash of category + who + where + first 100 chars of text
    base = f"{category}|{','.join(sorted(set(who)))}|{','.join(sorted(set(where)))}|{text[:100]}"
    return hashlib.sha256(base.encode()).hexdigest()

def event_already_fired(event_sig):
    doc_ref = db.collection('events').document(event_sig)
    doc = doc_ref.get()
    if doc.exists:
        last_time = doc.to_dict().get('timestamp')
        if last_time:
            last_dt = datetime.fromisoformat(last_time)
            if datetime.now(timezone.utc) - last_dt < timedelta(minutes=EVENT_MEMORY_MINUTES):
                return True
    # Not fired recently, or never fired: record this occurrence
    doc_ref.set({'timestamp': datetime.now(timezone.utc).isoformat()})
    return False

def compute_impact(title, summary):
    full_text = (title + ' ' + summary).lower()
    best_score = 0
    best_direction = 0
    best_category = 'Unknown'
    matched_phrase = ''

    # Check all rules
    for pattern, score, direction, category in RULES:
        m = re.search(pattern, full_text)
        if m and score > best_score:
            best_score = score
            best_direction = direction
            best_category = category
            matched_phrase = m.group()

    # Urgency bonus
    for urg in URGENCY_PATTERNS:
        if re.search(urg, full_text):
            best_score += 20
            break

    # Garbage penalty – if strong opinion language appears, reduce score significantly
    for garb in GARBAGE_PATTERNS:
        if re.search(garb, full_text):
            best_score -= 30
            break

    # Cap score 0-100
    best_score = max(0, min(best_score, 100))

    return best_score, best_direction, best_category, matched_phrase

def determine_direction_pattern(title, summary):
    """If direction from rule is 0 (unknown), try to infer from context words."""
    text = (title + ' ' + summary).lower()
    if 'draw' in text or 'decline' in text or 'fell' in text or 'drop' in text:
        return 1  # bullish for inventories
    elif 'build' in text or 'rise' in text or 'increase' in text or 'surge' in text:
        return -1
    return 0

# ---------- FETCH RSS ----------
def fetch_rss():
    items = []
    now = datetime.now(timezone.utc)
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                link = entry.get('link', '')
                full_text = f"{title} {summary}"

                # Time filter
                pub_dt = now
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                delta = (now - pub_dt).total_seconds() / 60
                if delta > WINDOW_MINUTES:
                    continue

                items.append({
                    'title': title,
                    'summary': summary,
                    'link': link,
                    'id': hashlib.sha256((title + link).encode()).hexdigest()
                })
        except Exception as e:
            print(f"RSS error {url}: {e}")
    return items

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
    send_telegram(f"💓 Heartbeat — Level 100 Event Engine alive. Next in {HEARTBEAT_HOURS}h.")
    doc_ref.set({'timestamp': now.isoformat()})

# ---------- MAIN ----------
def main():
    print(f"\n=== EVENT ENGINE RUNNING at {datetime.now(timezone.utc).isoformat()} ===")
    candidates = fetch_rss()
    print(f"Candidates fetched: {len(candidates)}")
    sent = 0

    for item in candidates:
        score, direction, category, phrase = compute_impact(item['title'], item['summary'])
        if score < MIN_IMPACT_SCORE:
            continue

        # Determine final direction
        if direction == 0:
            direction = determine_direction_pattern(item['title'], item['summary'])
        if direction == 0:
            continue  # still unknown, skip

        event_sig = extract_event_signature(item['title'], item['summary'], category)
        if event_already_fired(event_sig):
            continue

        # Expected move estimate
        if score >= 95:
            move_est = "3–8%"
        elif score >= 90:
            move_est = "2–5%"
        else:
            move_est = "1–3%"

        emoji = '🟢' if direction == 1 else '🔴'
        dir_str = 'BULLISH' if direction == 1 else 'BEARISH'

        msg = (f"🔥 *MARKET ALERT*\n"
               f"Impact: {score}/100 | {category}\n"
               f"Expected: {dir_str} → Estimated Move: {move_est}\n"
               f"📰 {item['title']}\n"
               f"💡 {phrase if phrase else 'High‑confidence pattern match'}\n"
               f"🕒 {datetime.utcnow().strftime('%H:%M UTC')}\n"
               f"[Source]({item['link']})")

        send_telegram(msg)
        sent += 1

    print(f"Alerts sent: {sent}")
    maybe_send_heartbeat()

if __name__ == '__main__':
    main()
