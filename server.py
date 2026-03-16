from flask import Flask, jsonify, send_file, request
import requests
import json
import os
import datetime as dt

app = Flask(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'keys.json')

# Calendar cache: use /tmp on deployed platforms (writable), local dir otherwise
_tmp = '/tmp' if os.path.isdir('/tmp') else os.path.dirname(__file__)
CALENDAR_CACHE = os.path.join(_tmp, 'calendar_cache.json')

def load_config():
    # Environment variables take priority (set these in Render/Railway dashboard)
    cfg = {
        'news_api_key': os.environ.get('NEWS_API_KEY', ''),
        'groq_api_key': os.environ.get('GROQ_API_KEY', ''),
    }
    # Fall back to keys.json for local development
    if not cfg['news_api_key'] or not cfg['groq_api_key']:
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                cfg['news_api_key'] = cfg['news_api_key'] or data.get('news_api_key', '')
                cfg['groq_api_key'] = cfg['groq_api_key'] or data.get('groq_api_key', '')
        except Exception:
            pass
    return cfg

def save_config(cfg):
    # Only save to file locally (env vars are used on deployed platforms)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f)
    except Exception as e:
        print(f'Warning: could not save config: {e}')

config: dict = load_config()
_countries_cache = None


@app.route('/')
def index():
    return send_file('index.html')


@app.route('/api/countries')
def get_countries():
    global _countries_cache
    if _countries_cache:
        return jsonify(_countries_cache)
    try:
        r = requests.get(
            'https://raw.githubusercontent.com/datasets/geo-countries/master/data/countries.geojson',
            timeout=30
        )
        _countries_cache = r.json()
        return jsonify(_countries_cache)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    # Return masked keys so the frontend knows they exist without exposing them
    return jsonify({
        'hasNewsKey': bool(config.get('news_api_key')),
        'hasGroqKey': bool(config.get('groq_api_key')),
    })

@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.json or {}
    news = data.get('newsApiKey', '').strip()
    groq = data.get('groqApiKey', '').strip()
    if news:
        config['news_api_key'] = news
    if groq:
        config['groq_api_key'] = groq
    save_config(config)
    return jsonify({'status': 'ok'})


@app.route('/api/analyze', methods=['GET'])
def analyze():
    news_key   = config.get('news_api_key', '')
    groq_key = config.get('groq_api_key', '')

    if not news_key or not groq_key:
        return jsonify({'error': 'API keys not configured. Click Config and enter your keys.'}), 400

    # ── Fetch news ────────────────────────────────────────────────────
    raw_articles = []
    last_api_error = None

    queries = [
        # Broad market headlines
        {'url': 'https://newsapi.org/v2/top-headlines',
         'params': {'category': 'business',   'language': 'en', 'pageSize': 40, 'apiKey': news_key}},
        {'url': 'https://newsapi.org/v2/top-headlines',
         'params': {'category': 'technology', 'language': 'en', 'pageSize': 30, 'apiKey': news_key}},
        # Targeted macro / monetary policy news
        {'url': 'https://newsapi.org/v2/everything',
         'params': {'q': 'Federal Reserve OR interest rates OR inflation OR CPI OR jobs report OR NFP OR GDP OR recession',
                    'language': 'en', 'sortBy': 'publishedAt', 'pageSize': 30, 'apiKey': news_key}},
        # Targeted equity / trade / earnings news
        {'url': 'https://newsapi.org/v2/everything',
         'params': {'q': 'stock market OR earnings OR tariff OR trade war OR S&P 500 OR Nasdaq OR tech stocks OR semiconductors',
                    'language': 'en', 'sortBy': 'publishedAt', 'pageSize': 30, 'apiKey': news_key}},
        # Geopolitical / risk-off triggers
        {'url': 'https://newsapi.org/v2/top-headlines',
         'params': {'category': 'general',    'language': 'en', 'pageSize': 20, 'apiKey': news_key}},
    ]

    for q in queries:
        try:
            r = requests.get(q['url'], params=q['params'], timeout=10)
            data = r.json()
            if r.ok:
                raw_articles.extend(data.get('articles', []))
            else:
                # Capture the actual API error message
                last_api_error = data.get('message') or data.get('code') or f'HTTP {r.status_code}'
        except Exception as e:
            last_api_error = str(e)

    # Deduplicate & clean
    seen, articles = set(), []
    for a in raw_articles:
        title = (a.get('title') or '').strip()
        if not title or title == '[Removed]' or title in seen:
            continue
        seen.add(title)
        articles.append({
            'title':       title,
            'description': (a.get('description') or '')[:150],
            'source':      (a.get('source') or {}).get('name', 'Unknown'),
            'publishedAt': a.get('publishedAt', ''),
            'url':         a.get('url', ''),
        })

    if not articles:
        err_detail = f' NewsAPI said: {last_api_error}' if last_api_error else ''
        return jsonify({'error': f'Could not fetch any news. Verify your NewsAPI key.{err_detail}'}), 400

    # ── Build prompt ──────────────────────────────────────────────────
    headlines_text = '\n'.join(
        f"[{a['source']}] {a['title']}. {a['description']}"
        for a in articles[:45]
    )

    prompt = f"""You are a professional futures market analyst specializing in ES1 (S&P 500 E-mini) and NQ1 (Nasdaq 100 E-mini). Analyze the news headlines below and produce a precise, objective directional bias for each instrument.

STRICT SCORE-TO-BIAS MAPPING (follow exactly):
  STRONG BEARISH : score -100 to -61  (systemic risk, crash panic, shock rate hike, severe recession fears)
  BEARISH        : score  -60 to -21  (meaningful headwinds, risk-off tone, negative macro surprise, tariff escalation)
  NEUTRAL        : score  -20 to +20  (mixed/conflicting signals, no clear edge — this is valid and common)
  BULLISH        : score  +21 to +60  (positive catalysts, improving macro, risk-on tone, earnings beats)
  STRONG BULLISH : score  +61 to +100 (exceptional tailwinds, major policy easing, blowout beats, relief rally)

WEIGHTING GUIDE (highest impact first):
  1. Fed / central bank signals — rate cuts are bullish; hikes/hawkish surprises are strongly bearish
  2. Macro data surprises — CPI hot = bearish; NFP miss = bearish; GDP beat = bullish
  3. Trade war / tariff escalation — bearish; de-escalation = bullish
  4. Geopolitical conflict escalation — risk-off, bearish
  5. Tech sector (AI, semis, big-tech earnings) — HIGH weight for NQ1, moderate for ES1
  6. Corporate earnings beats/misses — moderate weight
  7. General political news without direct market impact — low weight

NQ1 vs ES1 DIFFERENTIATION RULES:
  - NQ1 is 2-3x more sensitive to rate expectations than ES1 — if rates/Fed dominate, NQ1 score diverges more negative
  - NQ1 is more sensitive to tech/AI/semis news — positive tech news pushes NQ1 score higher than ES1
  - ES1 captures financials, energy, industrials, consumer — broader economic health
  - Do NOT give ES1 and NQ1 identical scores unless news is truly undifferentiated

OBJECTIVITY RULES:
  - Do NOT default to bullish. If news is ambiguous or mixed, use NEUTRAL.
  - Risk/uncertainty events (unknown outcome) are mildly bearish for near-term positioning.
  - Base the score ONLY on the specific news provided — do not assume market conditions.
  - Confidence (0-100) = how clearly the news supports your directional call. Low confidence = near-neutral score.

NEWS HEADLINES TO ANALYZE:
{headlines_text}

Respond with ONLY valid JSON — no markdown, no text outside the JSON object:
{{
  "ES1": {{
    "bias": "<STRONG BULLISH | BULLISH | NEUTRAL | BEARISH | STRONG BEARISH>",
    "score": <integer from -100 to 100, matching the bias range above>,
    "confidence": <integer 0-100>,
    "summary": "<2-3 sentences citing specific headlines that drive this bias>",
    "key_drivers": ["<specific news-based driver>", "<specific driver>", "<specific driver>"],
    "key_risks": ["<specific risk>", "<specific risk>", "<specific risk>"]
  }},
  "NQ1": {{
    "bias": "<STRONG BULLISH | BULLISH | NEUTRAL | BEARISH | STRONG BEARISH>",
    "score": <integer from -100 to 100, matching the bias range above>,
    "confidence": <integer 0-100>,
    "summary": "<2-3 sentences citing specific headlines that drive this bias>",
    "key_drivers": ["<specific news-based driver>", "<specific driver>", "<specific driver>"],
    "key_risks": ["<specific risk>", "<specific risk>", "<specific risk>"]
  }},
  "market_context": "<2-3 sentences: dominant macro themes, key risks, and overall market backdrop based on the headlines>",
  "top_themes": ["<theme 1>", "<theme 2>", "<theme 3>", "<theme 4>", "<theme 5>"]
}}"""

    # ── Call Groq ─────────────────────────────────────────────────────
    try:
        resp = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {groq_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': 'llama-3.1-8b-instant',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.15,
                'max_tokens': 1000,
            },
            timeout=45,
        )

        if not resp.ok:
            err = resp.json()
            msg = err.get('error', {}).get('message', 'Unknown Groq API error')
            return jsonify({'error': f'Groq API error: {msg}'}), 400

        content = resp.json()['choices'][0]['message']['content']

        # Strip markdown fences if model wraps in ```json ... ```
        content = content.strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[-1]
            content = content.rsplit('```', 1)[0]

        start = content.find('{')
        end   = content.rfind('}') + 1
        if start == -1 or end == 0:
            return jsonify({'error': 'Could not parse AI response. Try again.'}), 500

        analysis = json.loads(content[start:end])
        return jsonify({'analysis': analysis, 'articles': articles[:30]})

    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse AI response JSON. Please try again.'}), 500
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Try again.'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


CACHE_TTL_HOURS = 1  # re-fetch at most once per hour

def _active_feed():
    """Return 'nextweek' on Saturday >= 01:00 and all day Sunday, else 'thisweek'."""
    now = dt.datetime.now()
    wd  = now.weekday()  # 0=Mon … 5=Sat, 6=Sun
    if wd == 6 or (wd == 5 and now.hour >= 1):
        return 'nextweek'
    return 'thisweek'

def _cache_fresh(path, feed):
    """Return (events, True) if cache exists, is < TTL hours old, AND matches the current feed."""
    try:
        with open(path, 'r') as f:
            cache = json.load(f)
        if cache.get('feed') != feed:
            return None, False          # feed switched — treat as stale
        saved = dt.datetime.fromisoformat(cache['saved'])
        age   = (dt.datetime.now() - saved).total_seconds() / 3600
        if age < CACHE_TTL_HOURS:
            return cache.get('events', []), True
    except Exception:
        pass
    return None, False

def _save_cache(path, events, feed):
    try:
        with open(path, 'w') as f:
            json.dump({'saved': dt.datetime.now().isoformat(), 'feed': feed, 'events': events}, f)
    except Exception:
        pass

@app.route('/api/calendar')
def get_calendar():
    feed   = _active_feed()
    cached, fresh = _cache_fresh(CALENDAR_CACHE, feed)
    if fresh:
        return jsonify(cached)

    url = (f'https://nfs.faireconomy.media/ff_calendar_{feed}.json')
    try:
        r = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://www.forexfactory.com/',
        })
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list):
            if cached is not None:
                return jsonify(cached)
            return jsonify({'error': 'Unexpected response format from calendar source.'})
        high = [e for e in events if e.get('impact') == 'High' and e.get('country') == 'USD']
        _save_cache(CALENDAR_CACHE, high, feed)
        return jsonify(high)
    except requests.exceptions.Timeout:
        if cached is not None:
            return jsonify(cached)
        return jsonify({'error': 'Calendar request timed out. Try again.'})
    except requests.exceptions.HTTPError as e:
        if cached is not None:
            return jsonify(cached)
        return jsonify({'error': f'Calendar source returned HTTP {e.response.status_code}.'})
    except Exception as e:
        if cached is not None:
            return jsonify(cached)
        return jsonify({'error': f'Calendar fetch failed: {str(e)}'})


if __name__ == '__main__':
    print()
    print('=' * 55)
    print('   AI MARKET SENTIMENT  -  NQ1 & ES1 Analyzer')
    print('=' * 55)
    print()
    port = int(os.environ.get('PORT', 5000))
    print(f'  Server starting on http://localhost:{port}')
    print()
    print('  You need two FREE API keys (no credit card):')
    print('  • NewsAPI  → https://newsapi.org')
    print('  • Groq     → https://console.groq.com  (free, no card)')
    print()
    print('  Open the browser, click Config, enter your keys.')
    print('=' * 55)
    print()
    app.run(debug=False, host='0.0.0.0', port=port)
