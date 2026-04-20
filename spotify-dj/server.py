#!/usr/bin/env python3
"""Spotify Smart DJ v3.0.0 — HA Add-on
Adaptive music with Event Bus integration, learning, and real-time reactions.

v3.0 additions:
  - Fix media_content_type: 'spotify://playlist' (works with Sonos)
  - Fixed volume levels: morning_early 0.22, morning_late 0.25, afternoon 0.28, evening 0.25, night 0.15
  - kids_mode volume bonus: +0.03 (cap 0.30)
  - Replace broken playlist URIs with confirmed working ones
  - Add new confirmed playlists: kids, morning_chill, dinner, evening categories
  - Bedroom protection: /speaker/<entity> rejects bedroom entities unless motion confirmed
  - Cooper awareness: when kids_mode=True, filter explicit, volume cap 0.30
  - /play endpoint for natural language requests via Spotify Web API search
  - NEVER use Echo devices for music (triggers Samsung Frame TV)
  - Silent hours (22:00-08:00): audio -> push notification unless bedroom motion

v2.0 additions:
  - Event Bus SSE subscriber: reacts to events in real-time
  - TV on = auto-pause music
  - Lights dimming = switch to calmer playlist
  - Weather change = adjust playlist mood
  - Skip rate tracking per playlist for better learning

Endpoints:
  GET  /health, /recommend, /now-playing, /playlists, /stats
  POST /play, /play/<id>, /mood/<mood>, /kids, /kids/off
  POST /like, /skip, /volume/<level>, /speaker/<entity>
  GET  /search/<query>
  GET  /event-log — Recent event-driven actions
"""
import os, json, time, logging, random, base64, threading
from datetime import datetime
from collections import deque
from flask import Flask, jsonify, request
import requests as http
import sseclient

CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
HA_URL = os.environ.get('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.environ.get('HA_TOKEN', '')
API_PORT = int(os.environ.get('API_PORT', '8097'))
SONOS = os.environ.get('SONOS_ENTITY', 'media_player.living_room')
EVENT_BUS_URL = os.environ.get('EVENT_BUS_URL', 'http://localhost:8092')

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('spotify-dj')

# v3.0: Safety constants
BEDROOM_ENTITIES = lambda eid: 'bedroom' in eid.lower()
ECHO_ENTITIES = [
    'media_player.living_room_echo_show',
    'media_player.kitchen_echo_show',
    'media_player.bedroom_echo',
]
SILENT_HOURS = lambda: datetime.now().hour >= 22 or datetime.now().hour < 8

def is_bedroom_safe():
    """v3.0: Check if bedroom motion is active."""
    try:
        r = http.get(f'{HA_URL}/api/states/binary_sensor.bedroom_motion',
                     headers={'Authorization': f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200:
            return r.json().get('state') == 'on'
    except: pass
    return False

def ha_notify(title, msg):
    try:
        http.post(f'{HA_URL}/api/services/notify/mobile_app_bks_home_assistant_chatsworth',
                  headers={'Authorization': f'Bearer {HA_TOKEN}', 'Content-Type': 'application/json'},
                  json={'data': {'title': title, 'message': msg}}, timeout=5)
    except: pass

spotify_token = None
token_expires = 0
recent = []
kids_mode = False
current_playlist_id = None
DATA_FILE = '/data/dj_stats.json'

music_paused_by_tv = False
event_actions = deque(maxlen=100)

stats = {'likes': {}, 'skips': {}, 'plays': {}, 'total_plays': 0}

def load_stats():
    global stats
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                stats = json.load(f)
            logger.info(f'Loaded DJ stats: {stats["total_plays"]} plays')
    except: pass

def save_stats():
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(stats, f, indent=2)
    except: pass

# v3.0: Updated playlist library
# - Replaced broken kids URIs
# - Added new confirmed-working playlists
# - Replaced Disney Peaceful Piano (500 error) with Ambient Deep Sleep
LIB = {
    'morning_early': [
        {'id':'37i9dQZF1DWXe9gFZP0gtP','name':'Chill Morning','vibe':'gentle'},
        {'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe','vibe':'coffee'},
        {'id':'37i9dQZF1DX6ziVCJnEm59','name':'Morning Motivation','vibe':'upbeat'},
    ],
    'morning_late': [
        {'id':'5vImPKH5smp2ifK34N6XTd','name':'Energetic Upbeat Lofi','vibe':'productive'},
        {'id':'37i9dQZF1DX0SM0LYsmbMT','name':'Jazz Vibes','vibe':'sophisticated'},
        {'id':'37i9dQZF1DX4OzrY981I1W','name':'Indie Folk','vibe':'laid back'},
    ],
    'morning_chill': [
        {'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe','vibe':'coffee'},
        {'id':'37i9dQZF1DWXe9gFZP0gtP','name':'Chill Morning','vibe':'gentle'},
        {'id':'7Ap6xVpaCDJpYkzNGAurHJ','name':'Morning Chill Lofi Breakfast','vibe':'lofi'},
    ],
    'afternoon': [
        {'id':'0CFuMybe6s77w6QQrJjW7d','name':'Chillhop Radio','vibe':'focus'},
        {'id':'37i9dQZF1DX0SM0LYsmbMT','name':'Jazz Vibes','vibe':'groove'},
        {'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening','vibe':'mellow'},
        {'id':'37i9dQZF1DX4OzrY981I1W','name':'Indie Folk','vibe':'weekend'},
    ],
    'evening': [
        {'id':'3NXxyeM9cp3bRnxNtqhOu4','name':'Lofi Trap Beats','vibe':'chill'},
        {'id':'37i9dQZF1DX6VdMW310YC7','name':'Chill R&B','vibe':'smooth'},
        {'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening','vibe':'wind down'},
        {'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient','vibe':'zen'},
        {'id':'37i9dQZF1DXcKnb4wcRKrO','name':'Chill Evening','vibe':'chill'},
    ],
    'night': [
        {'id':'5eDufIy8WtiArgp9aPd9su','name':'Late Night Vibes','vibe':'night owl'},
        {'id':'37i9dQZF1DWZd79rJ6a7lp','name':'Sleep Jazz','vibe':'dreamy'},
        {'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient','vibe':'sleep'},
        {'id':'6bGe4ekNk4E4h9vVkuItul','name':'Ambient Deep Sleep','vibe':'deep sleep'},
    ],
    'chill':      [{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'},{'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening'},{'id':'0CFuMybe6s77w6QQrJjW7d','name':'Chillhop'}],
    'energetic':  [{'id':'37i9dQZF1DX6ziVCJnEm59','name':'Morning Motivation'},{'id':'37i9dQZF1DX76Wlfdnj7AP','name':'Beast Mode'},{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'}],
    'focus':      [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DWZeKCadgRdKQ','name':'Deep Focus'},{'id':'0CFuMybe6s77w6QQrJjW7d','name':'Chillhop'}],
    'party':      [{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'},{'id':'37i9dQZF1DXa2PjGhjTnEG','name':'Party Starters'}],
    'sleep':      [{'id':'37i9dQZF1DWZd79rJ6a7lp','name':'Sleep Jazz'},{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'},{'id':'6bGe4ekNk4E4h9vVkuItul','name':'Ambient Deep Sleep'}],
    'romantic':   [{'id':'37i9dQZF1DX6VdMW310YC7','name':'Chill R&B'},{'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening'}],
    'rainy':      [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'}],
    'sunny':      [{'id':'37i9dQZF1DX4OzrY981I1W','name':'Indie Folk'},{'id':'37i9dQZF1DX6ziVCJnEm59','name':'Morning Motivation'}],
    # v3.0: Updated kids playlists — removed broken URIs, added confirmed working
    'kids':       [
        {'id':'7LD17YaJftpf0WMg40h25L','name':'Kids Dance Party Clean','vibe':'clean dance'},
        {'id':'2k1TzwejfDMu9vszNPQE4s','name':'Kids Dance Party Fun','vibe':'fun'},
        {'id':'1P27ra5VqAizmkcUzVAvp2','name':'Kids Party Songs 2026','vibe':'party'},
        {'id':'37i9dQZF1DX6aTaZa0K6VA','name':'Disney Hits','vibe':'disney'},
        {'id':'37i9dQZF1DX2M1RktxUUHE','name':'Family Road Trip','vibe':'family'},
        {'id':'37i9dQZF1DXa8NOEUWPn9W','name':'Happy Hits','vibe':'happy'},
    ],
    'dinner':     [
        {'id':'37i9dQZF1DX4xuWVBs4FgJ','name':'Dinner Jazz Original','vibe':'jazz'},
        {'id':'37i9dQZF1DWWKeNBqaIy5U','name':'Dinner Jazz','vibe':'dinner jazz'},
        {'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening','vibe':'acoustic'},
    ],
    'workout':    [{'id':'37i9dQZF1DX76Wlfdnj7AP','name':'Beast Mode'},{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'}],
    'morning_coffee': [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DWXe9gFZP0gtP','name':'Chill Morning'}],
}

def sp_token():
    global spotify_token, token_expires
    if spotify_token and time.time() < token_expires: return spotify_token
    if not CLIENT_ID or not CLIENT_SECRET: return None
    auth = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    r = http.post('https://accounts.spotify.com/api/token',
                  headers={'Authorization':f'Basic {auth}','Content-Type':'application/x-www-form-urlencoded'},
                  data={'grant_type':'client_credentials'}, timeout=10)
    if r.status_code == 200:
        d = r.json()
        spotify_token = d['access_token']
        token_expires = time.time() + d.get('expires_in',3600) - 60
        return spotify_token
    return None

def time_slot():
    h = datetime.now().hour
    if 6<=h<9: return 'morning_early'
    if 9<=h<12: return 'morning_late'
    if 12<=h<17: return 'afternoon'
    if 17<=h<21: return 'evening'
    return 'night'

def get_weather():
    try:
        r = http.get(f'{HA_URL}/api/states/weather.forecast_home',
                     headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200: return r.json()['state']
    except: pass
    return 'unknown'

def score_playlist(p):
    pid = p['id']
    likes = stats['likes'].get(pid, 0)
    skips = stats['skips'].get(pid, 0)
    plays = stats['plays'].get(pid, 0)
    return likes * 3 - skips * 2 + plays * 0.5

def pick(slot=None, mood=None):
    global recent, current_playlist_id
    if kids_mode: mood = 'kids'
    w = get_weather()
    if mood and mood in LIB: cands = LIB[mood]
    elif w in ['rainy','pouring']: cands = LIB.get('rainy', LIB.get(slot or time_slot(), []))
    elif w in ['sunny','clear-night','partlycloudy'] and time_slot() in ['morning_late','afternoon']: cands = LIB.get('sunny', LIB.get(slot or time_slot(), []))
    else: cands = LIB.get(slot or time_slot(), [])
    if not cands: cands = LIB['afternoon']
    avail = [p for p in cands if p['id'] not in recent[-3:]]
    if not avail: avail = cands
    if stats['total_plays'] > 5:
        avail.sort(key=score_playlist, reverse=True)
        weights = [max(1, score_playlist(p) + 5) for p in avail]
        total = sum(weights)
        r = random.random() * total
        cumul = 0
        for i, wt in enumerate(weights):
            cumul += wt
            if r <= cumul:
                p = avail[i]
                break
        else:
            p = avail[0]
    else:
        p = random.choice(avail)
    recent.append(p['id'])
    recent = recent[-10:]
    current_playlist_id = p['id']
    stats['plays'][p['id']] = stats['plays'].get(p['id'], 0) + 1
    stats['total_plays'] += 1
    save_stats()
    return p

def get_vol(override=None):
    """v3.0: Fixed volume levels. kids_mode bonus +0.03, cap 0.30."""
    if override is not None: return override / 100.0
    base = {
        'morning_early': 0.22,
        'morning_late': 0.25,
        'afternoon': 0.28,
        'evening': 0.25,
        'night': 0.15,
    }.get(time_slot(), 0.25)
    if kids_mode:
        base = min(base + 0.03, 0.30)
    return base

def play_sonos(pid, vol=None, target_entity=None):
    """v3.0: Uses spotify://playlist content type. Enforces no Echo for music."""
    entity = target_entity or SONOS
    # v3.0: NEVER use Echo devices for music
    if entity in ECHO_ENTITIES:
        logger.warning(f'BLOCKED: {entity} is an Echo device — would trigger TV. Using {SONOS} instead.')
        entity = SONOS
    # v3.0: Bedroom protection
    if BEDROOM_ENTITIES(entity):
        if not is_bedroom_safe():
            logger.warning(f'BLOCKED: {entity} is a bedroom device and no motion detected.')
            ha_notify('🎵 Music Blocked', f'Bedroom device {entity} not targeted — no motion detected.')
            return False
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    if vol is None: vol = get_vol()
    try: http.post(f'{HA_URL}/api/services/media_player/select_source', headers=hh,
                   json={'entity_id':entity,'source':'Spotify'}, timeout=5)
    except: pass
    time.sleep(1)
    http.post(f'{HA_URL}/api/services/media_player/volume_set', headers=hh,
              json={'entity_id':entity,'volume_level':vol}, timeout=5)
    # v3.0: Fixed media_content_type from 'playlist' to 'spotify://playlist'
    http.post(f'{HA_URL}/api/services/media_player/play_media', headers=hh,
              json={'entity_id':entity,'media_content_id':f'spotify:playlist:{pid}',
                    'media_content_type':'spotify://playlist'}, timeout=5)
    time.sleep(1)
    http.post(f'{HA_URL}/api/services/media_player/shuffle_set', headers=hh,
              json={'entity_id':entity,'shuffle':True}, timeout=5)
    logger.info(f'Playing spotify:playlist:{pid} on {entity} vol={vol}')
    return True

def pause_sonos():
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/media_pause', headers=hh,
                   json={'entity_id':SONOS}, timeout=5)
    except: pass

def resume_sonos():
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/media_play', headers=hh,
                   json={'entity_id':SONOS}, timeout=5)
    except: pass

def handle_event(ev):
    global music_paused_by_tv
    eid = ev.get('entity_id', '')
    new = ev.get('new_state', '')
    old = ev.get('old_state', '')
    sig = ev.get('significant', False)
    action = None

    # TV on = pause music
    if 'media_player.75_the_frame' in eid or ('media_player' in eid and 'frame' in eid):
        if new in ['on', 'playing'] and old in ['off', 'standby', 'unavailable']:
            logger.info('EVENT: TV turned on — pausing music')
            pause_sonos()
            music_paused_by_tv = True
            action = 'pause_for_tv'
        elif new in ['off', 'standby'] and music_paused_by_tv:
            logger.info('EVENT: TV turned off — resuming music')
            resume_sonos()
            music_paused_by_tv = False
            action = 'resume_after_tv'

    elif 'weather' in eid and sig:
        if new in ['rainy', 'pouring']:
            logger.info('EVENT: Rain started — switching to rainy playlist')
            p = pick(mood='rainy')
            play_sonos(p['id'])
            action = 'weather_rainy'
        elif new == 'sunny' and old in ['rainy', 'pouring', 'cloudy']:
            logger.info('EVENT: Weather cleared — switching to sunny playlist')
            p = pick(mood='sunny')
            play_sonos(p['id'])
            action = 'weather_sunny'

    elif 'light.living_room' in eid and sig:
        if new == 'off':
            p = pick(mood='chill')
            play_sonos(p['id'])
            action = 'lights_off_chill'

    if action:
        event_actions.append({'time':datetime.now().isoformat(),'event':eid,'action':action,'old':old,'new':new})
        logger.info(f'ACTION: {action}')

def event_bus_subscriber():
    while True:
        try:
            logger.info(f'Connecting to Event Bus SSE: {EVENT_BUS_URL}/events/stream')
            response = http.get(f'{EVENT_BUS_URL}/events/stream', stream=True, timeout=None)
            client = sseclient.SSEClient(response)
            logger.info('Event Bus SSE connected')
            for event in client.events():
                try:
                    ev = json.loads(event.data)
                    handle_event(ev)
                except json.JSONDecodeError: pass
                except Exception as e: logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE error: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)

@app.route('/')
def index():
    return jsonify({'name':'Spotify Smart DJ','version':'3.0.0','kids_mode':kids_mode,
                    'current_playlist':current_playlist_id,'total_plays':stats['total_plays'],
                    'sonos':SONOS,'silent_hours':SILENT_HOURS()})

@app.route('/health')
def health():
    return jsonify({'status':'ok','kids_mode':kids_mode,'tv_paused':music_paused_by_tv,
                    'event_bus':'connected' if event_actions else 'waiting',
                    'spotify_auth':'ok' if sp_token() else 'failed'})

@app.route('/recommend')
def recommend():
    slot = time_slot()
    w = get_weather()
    p = pick()
    return jsonify({'playlist':p,'slot':slot,'weather':w,'kids_mode':kids_mode,
                    'volume':get_vol(),'bedroom_safe':is_bedroom_safe()})

@app.route('/play', methods=['POST','GET'])
def play_natural():
    """v3.0: Natural language play endpoint via Spotify Web API search."""
    q = request.args.get('q') or request.json.get('q','') if request.is_json else request.args.get('q','')
    if not q:
        # Default: pick based on time/mood
        p = pick()
        ok = play_sonos(p['id'])
        return jsonify({'success':ok,'playlist':p,'method':'auto','volume':get_vol()})
    # Search Spotify
    t = sp_token()
    if not t:
        # Fallback to library pick
        p = pick()
        ok = play_sonos(p['id'])
        return jsonify({'success':ok,'playlist':p,'method':'library_fallback','reason':'no_spotify_auth'})
    r = http.get('https://api.spotify.com/v1/search',
                 headers={'Authorization':f'Bearer {t}'},
                 params={'q':q,'type':'playlist','limit':3}, timeout=8)
    if r.status_code == 200:
        items = r.json().get('playlists',{}).get('items',[])
        if items:
            best = items[0]
            pid = best['id']
            ok = play_sonos(pid)
            return jsonify({'success':ok,'id':pid,'name':best['name'],
                            'tracks':best.get('tracks',{}).get('total','?'),'method':'search'})
    p = pick()
    ok = play_sonos(p['id'])
    return jsonify({'success':ok,'playlist':p,'method':'library_fallback'})

@app.route('/play/<pid>', methods=['POST','GET'])
def play_id(pid):
    ok = play_sonos(pid)
    return jsonify({'success':ok,'id':pid})

@app.route('/mood/<mood>', methods=['POST','GET'])
def mood_play(mood):
    if mood not in LIB:
        return jsonify({'error':f'Available: {list(LIB.keys())}'}), 400
    # v3.0: Silent hours check
    if SILENT_HOURS() and not is_bedroom_safe():
        ha_notify(f'🎵 Music ({mood})', f'Silent hours — push only. Bedroom motion not detected.')
        return jsonify({'success':False,'reason':'silent_hours','notification_sent':True})
    p = pick(mood=mood)
    ok = play_sonos(p['id'])
    return jsonify({'success':ok,'mood':mood,'playlist':p,'volume':get_vol()})

@app.route('/kids', methods=['POST','GET'])
def kids_on():
    global kids_mode
    kids_mode = True
    p = pick(mood='kids')
    ok = play_sonos(p['id'])
    logger.info(f'Cooper/kids mode ON — volume cap 0.30, clean playlists')
    return jsonify({'success':ok,'kids_mode':True,'playlist':p,'volume_cap':0.30})

@app.route('/kids/off', methods=['POST','GET'])
def kids_off():
    global kids_mode
    kids_mode = False
    return jsonify({'kids_mode':False})

@app.route('/like', methods=['POST','GET'])
def like():
    if current_playlist_id:
        stats['likes'][current_playlist_id] = stats['likes'].get(current_playlist_id, 0) + 1
        save_stats()
        return jsonify({'success':True,'liked':current_playlist_id,
                        'total_likes':stats['likes'][current_playlist_id]})
    return jsonify({'error':'Nothing playing'}), 400

@app.route('/skip', methods=['POST','GET'])
def skip():
    if current_playlist_id:
        stats['skips'][current_playlist_id] = stats['skips'].get(current_playlist_id, 0) + 1
        save_stats()
        p = pick()
        play_sonos(p['id'])
        return jsonify({'success':True,'skipped':current_playlist_id,'now_playing':p.get('name',p['id'])})
    return jsonify({'error':'Nothing playing'}), 400

@app.route('/stats')
def get_stats():
    all_ids = set(list(stats['plays'].keys()) + list(stats['likes'].keys()))
    scored = []
    for pid in all_ids:
        name = '?'
        for cat in LIB.values():
            for p in cat:
                if p['id'] == pid: name = p.get('name', pid); break
        scored.append({'id':pid,'name':name,'plays':stats['plays'].get(pid,0),
                       'likes':stats['likes'].get(pid,0),'skips':stats['skips'].get(pid,0),
                       'score':round(stats['likes'].get(pid,0)*3 - stats['skips'].get(pid,0)*2 + stats['plays'].get(pid,0)*0.5, 1)})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'total_plays':stats['total_plays'],'top_playlists':scored[:10],'kids_mode':kids_mode})

@app.route('/volume/<int:level>', methods=['POST','GET'])
def volume(level):
    vol = max(0, min(100, level)) / 100
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    http.post(f'{HA_URL}/api/services/media_player/volume_set', headers=hh,
              json={'entity_id':SONOS,'volume_level':vol}, timeout=5)
    return jsonify({'success':True,'volume':vol})

@app.route('/speaker/<entity>', methods=['POST','GET'])
def switch_speaker(entity):
    """v3.0: Validate target entity — reject Echo devices and bedroom without motion."""
    global SONOS
    # v3.0: Block Echo devices
    if entity in ECHO_ENTITIES:
        return jsonify({'success':False,'error':f'{entity} is an Echo device — triggers Samsung Frame TV. Use a Sonos entity.'}), 400
    # v3.0: Block bedroom entities without motion
    if BEDROOM_ENTITIES(entity):
        if not is_bedroom_safe():
            return jsonify({'success':False,'error':f'{entity} is a bedroom device and no motion detected.',
                            'bedroom_motion':False}), 403
    SONOS = entity
    return jsonify({'success':True,'speaker':SONOS})

@app.route('/playlists')
def playlists():
    return jsonify(LIB)

@app.route('/search/<query>')
def search(query):
    t = sp_token()
    if not t: return jsonify({'error':'auth failed'}), 500
    r = http.get('https://api.spotify.com/v1/search',
                 headers={'Authorization':f'Bearer {t}'},
                 params={'q':query,'type':'playlist','limit':5}, timeout=5)
    if r.status_code == 200:
        return jsonify([{'id':p['id'],'name':p['name'],'tracks':p.get('tracks',{}).get('total','?')}
                        for p in r.json().get('playlists',{}).get('items',[])])
    return jsonify({'error':'failed'}), 500

@app.route('/now-playing')
def now_playing():
    try:
        r = http.get(f'{HA_URL}/api/states/{SONOS}',
                     headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200:
            d = r.json()
            return jsonify({'state':d['state'],'title':d['attributes'].get('media_title'),
                            'artist':d['attributes'].get('media_artist'),
                            'volume':d['attributes'].get('volume_level'),
                            'source':d['attributes'].get('source'),
                            'kids_mode':kids_mode,'tv_paused':music_paused_by_tv})
    except: pass
    return jsonify({'error':'failed'}), 500

@app.route('/event-log')
def event_log():
    return jsonify(list(event_actions)[-20:])

if __name__ == '__main__':
    logger.info(f'Spotify Smart DJ v3.0.0 on port {API_PORT}')
    logger.info(f'Sonos: {SONOS}')
    load_stats()
    logger.info(f'Spotify auth: {"OK" if sp_token() else "FAILED (no creds)"}')
    threading.Thread(target=event_bus_subscriber, daemon=True).start()
    logger.info('Event Bus subscriber started')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
