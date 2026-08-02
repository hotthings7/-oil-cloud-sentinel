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
WINDOW_MINUTES = 120          # articles from last 2 hours
EVENT_MEMORY_MINUTES = 45     # block exactly identical event for 45 minutes (still allows variations)
HEARTBEAT_HOURS = 6
MIN_IMPACT_SCORE = 85         # only send if score ≥ 85

# Sources – ultra‑fast breaking news
RSS_FEEDS = [
    # FinancialJuice (direct market chatter)
    'https://feeds.financialjuice.com/feeds/headlines/rss',
    # ZeroHedge (geopolitical)
    'https://feeds.feedburner.com/zerohedge/feed',
    # Google News filtered for breaking oil
    'https://news.google.com/rss/search?q=oil+OR+crude+OR+OPEC+breaking&hl=en-US&gl=US&ceid=US:en',
    # OilPrice (but we take only the first 5 entries to keep it fresh)
    'https://oilprice.com/rss/main',
    # Nitter search – catches tweets instantly
    'https://nitter.net/search/rss?f=tweets&q=iran+OR+trump+OR+hormuz+OR+opec+OR+oil&since=1h',
    # Another Nitter instance as fallback
    'https://nitter.unixfox.eu/search/rss?f=tweets&q=iran+OR+trump+OR+hormuz+OR+opec+OR+oil&since=1h',
]

# ---------- RULE ENGINE (expanded) ----------
# (pattern, score, direction, category)
RULES = [
    # --- Iran / Hormuz / Strait actions (bullish if closure/attack, bearish if opening/deal) ---
    (r'(iran|houthi)\s+(attack|strike|hit|target|bomb|missile|drone)\s+(tanker|vessel|ship|oil|refinery|saudi|port)', 100, 1, 'Geopolitical'),
    (r'hormuz\s+(close|shut|block|mine|under attack|tensions|risk)', 100, 1, 'Geopolitical'),
    (r'(iran|houthi)\s+(seize|detain)\s+(tanker|vessel)', 100, 1, 'Geopolitical'),
    (r'(saudi|aramco|yemen|houthi)\s+(refinery|oil facility|pipeline)\s+(attack|drone|missile|fire|explosion)', 99, 1, 'Geopolitical'),
    (r'(israel|us|united states)\s+(attack|strike|bomb)\s+(iran|refinery|oil target|energy target)', 100, 1, 'Geopolitical'),
    (r'iran\s+(nuclear|atomic)\s+(threat|weapon|program)', 100, 1, 'Geopolitical'),

    # --- Ceasefire, Deal, Opening of Hormuz (bearish for oil) ---
    (r'(iran|us|trump)\s+(peace|deal|ceasefire|truce|talk|negotiation|diplomacy)', 98, -1, 'Geopolitical'),
    (r'hormuz\s+(open|reopen|resume|normal traffic|safe passage)', 100, -1, 'Geopolitical'),
    (r'(iran|houthi)\s+(halt|stop|suspend|hold off)\s+(attack|strike|operation)', 97, -1, 'Geopolitical'),
    (r'sanctions\s+(lift|ease|remove|relief)\s+(iran|venezuela|russia)', 96, -1, 'Geopolitical'),
    (r'(iran|us)\s+(agreement|deal|accord)\s+(nuclear|oil|hormuz|sanctions)', 99, -1, 'Geopolitical'),

    # --- Trump statements (often move oil immediately) ---
    (r'(trump|president)\s+(says|announce|declare|tweet|truth social)\s+(iran|oil|opec|hormuz|deal|attack)', 99, 0, 'Geopolitical'),  # direction determined later
    (r'trump\s+(to\s+iran|iran\s+deal|oil\s+price|gas\s+price)', 95, 0, 'Geopolitical'),

    # --- OPEC / Production cuts / Quotas ---
    (r'opec\s*\+?\s*(cut|reduc|emergency\s+meeting|quota)', 97, 1, 'OPEC'),
    (r'(saudi|iraq|kuwait|uae)\s+(voluntary\s+cut|extend\s+cut|deepen\s+cut)', 96, 1, 'OPEC'),
    (r'(opec|oil\s+producer)\s+(increase\s+production|ramp\s+up|ease\s+cuts)', 95, -1, 'OPEC'),

    # --- Scheduled Reports (EIA/API/CPI/FOMC) ---
    (r'(eia|api)\s+(crude|oil|gasoline|distillate)\s+inventor', 92, 0, 'Scheduled'),
    (r'crude\s+inventor\s+(draw|drop|fell|decline)\s+(\d+\.?\d*\s?million)', 94, 1, 'Scheduled'),
    (r'crude\s+inventor\s+(build|rise|increase|surge)\s+(\d+\.?\d*\s?million)', 94, -1, 'Scheduled'),
    (r'(fomc|fed|interest\s+rate)\s+(hike|raise|increase)', 90, -1, 'Scheduled'),
    (r'(fomc|fed|interest\s+rate)\s+(cut|lower|reduce)', 90, 1, 'Scheduled'),
    (r'(cpi|inflation)\s+(beat|surge|above\s+forecast)', 87, -1, 'Scheduled'),

    # --- Shipping disruptions / Attacks ---
    (r'(hormuz|bab\s+el.?mandeb|red\s+sea|suez)\s+(tanker|vessel|ship|traffic)\s+(halt|stop|under\s+attack|closed|interrupted)', 100, 1, 'Shipping'),
    (r'(tanker|vessel)\s+(attacked|hit|struck|seized|detained)', 99, 1, 'Shipping'),
    (r'war\s+risk\s+(insurance|premium)\s+(surge|spike|jump)\s+(tanker|shipping|hormuz)', 95, 1, 'Shipping'),
    (r'(port|terminal)\s+(closed|shut|force\s+majeure)\s+(oil|crude)', 94, 1, 'Shipping'),

    # --- Supply disruptions (force majeure, pipeline blast) ---
    (r'(force\s+majeure)\s+(declared|on\s+oil|crude)', 94, 1, 'Supply'),
    (r'(pipeline|oil\s+facilit)\s+(explosion|attack|blast|sabotage)', 93, 1, 'Supply'),
    (r'(output|production)\s+cut\s+(\d+\.?\d*\s?million\s?bpd)', 97, 1, 'Supply'),
    (r'(supply|export)\s+(disruption|halt|stop)', 93, 1, 'Supply'),
]

# Urgency patterns – any of these add +20 to score
URGENCY_PATTERNS = [
    r'\b(BREAKING|FLASH|JUST\s+IN|URGENT|ALERT|LIVE|WATCH)\b',
]

# Garbage penalty only for clear opinion/forecast pieces (not breaking news with "might" as a quote)
GARBAGE_PATTERNS = [
    r'\b(analyst\s+says|opinion|commentary|podcast|weekly\s+outlook|monthly\s+forecast|quarterly\s+report)\b',
    r'\b(technical\s+analysis|chart\s+of\s+the\s+day|MACD|RSI|Fibonacci)\b',
]

# Freshness bonus: if article is less than 5 minutes old, add +10
FRESHNESS_BONUS_MINUTES = 5

# ---------- HELPERS ----------
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
                    'id': hashlib.sha256((title + link).encode()).hexdigest(),
                    'pub_dt': pub_dt
                })
        except Exception as e:
            print(f"RSS error {url}: {e}")
    return items

def compute_impact(item):
    title = item['title']
    summary = item['summary']
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

    # Freshness bonus
    delta = (datetime.now(timezone.utc) - item['pub_dt']).total_seconds() / 60
    if delta < FRESHNESS_BONUS_MINUTES:
        best_score += 10

    # Garbage penalty (only if strong opinion indicator)
    for garb in GARBAGE_PATTERNS:
        if re.search(garb, full_text):
            best_score -= 20   # less harsh
            break

    # Cap 0-100
    best_score = max(0, min(best_score, 100))

    # Determine direction if still 0 (context‑based)
    if best_direction == 0 and best_score >= MIN_IMPACT_SCORE:
        # Infer direction from surrounding words
        if re.search(r'(cut|drop|fall|decline|bear|down|plunge|ceasefire|deal|open|reopen|diplomacy|peace)', full_text):
            best_direction = -1
        elif re.search(r'(attack|strike|close|shut|disrupt|surge|spike|bull|up|sanction|cut\s+supply)', full_text):
            best_direction = 1

    return best_score, best_direction, best_category, matched_phrase

def event_already_fired(title, category):
    # Create a signature from title + category (exclude time to allow updates on same event)
    sig_text = f"{title}|{category}"
    event_sig = hashlib.sha256(sig_text.encode()).hexdigest()
    doc_ref = db.collection('events').document(event_sig)
    doc = doc_ref.get()
    if doc.exists:
        last_time = doc.to_dict().get('timestamp')
        if last_time:
            last_dt = datetime.fromisoformat(last_time)
            if datetime.now(timezone.utc) - last_dt < timedelta(minutes=EVENT_MEMORY_MINUTES):
                return True
    # Not fired recently, or never fired: record
    doc_ref.set({'timestamp': datetime.now(timezone.utc).isoformat()})
    return False

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
        score, direction, category, phrase = compute_impact(item)
        if score < MIN_IMPACT_SCORE or direction == 0:
            continue

        # Dedup using title + category (so two different Trump statements about same topic both pass)
        if event_already_fired(item['title'], category):
            continue

        # Estimated move
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
               f"🕒 {item['pub_dt'].strftime('%H:%M UTC') if item['pub_dt'] else 'now'}\n"
               f"[Source]({item['link']})")

        send_telegram(msg)
        sent += 1

    print(f"Alerts sent: {sent}")
    maybe_send_heartbeat()

if __name__ == '__main__':
    main()
