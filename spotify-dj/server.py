#!/usr/bin/env python3
"""Spotify Smart DJ v2.0.0 — HA Add-on
Adaptive music with Event Bus integration, learning, and real-time reactions.

v2.0 additions:
  - Event Bus SSE subscriber: reacts to events in real-time
  - TV on = auto-pause music
  - Lights dimming = switch to calmer playlist
  - Weather change = adjust playlist mood
  - Room motion = follow-me music (future)
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

spotify_token = None
token_expires = 0
recent = []
kids_mode = False
current_playlist_id = None
DATA_FILE = '/data/dj_stats.json'

# v2.0: Event-driven state
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
    ],
    'night': [
        {'id':'5eDufIy8WtiArgp9aPd9su','name':'Late Night Vibes','vibe':'night owl'},
        {'id':'37i9dQZF1DWZd79rJ6a7lp','name':'Sleep Jazz','vibe':'dreamy'},
        {'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient','vibe':'sleep'},
    ],
    'chill':      [{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'},{'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening'},{'id':'0CFuMybe6s77w6QQrJjW7d','name':'Chillhop'}],
    'energetic':  [{'id':'37i9dQZF1DX6ziVCJnEm59','name':'Morning Motivation'},{'id':'37i9dQZF1DX76Wlfdnj7AP','name':'Beast Mode'},{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'}],
    'focus':      [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DWZeKCadgRdKQ','name':'Deep Focus'},{'id':'0CFuMybe6s77w6QQrJjW7d','name':'Chillhop'}],
    'party':      [{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'},{'id':'37i9dQZF1DXa2PjGhjTnEG','name':'Party Starters'}],
    'sleep':      [{'id':'37i9dQZF1DWZd79rJ6a7lp','name':'Sleep Jazz'},{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'}],
    'romantic':   [{'id':'37i9dQZF1DX6VdMW310YC7','name':'Chill R&B'},{'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening'}],
    'rainy':      [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DX3Ogo9pFvBkY','name':'Ambient'}],
    'sunny':      [{'id':'37i9dQZF1DX4OzrY981I1W','name':'Indie Folk'},{'id':'37i9dQZF1DX6ziVCJnEm59','name':'Morning Motivation'}],
    'kids':       [{'id':'37i9dQZF1DX6aTaZa0K6VA','name':'Disney Hits'},{'id':'37i9dQZF1DWVlYsZJXBFMo','name':'Kids Pop'},{'id':'37i9dQZF1DX2M1RktxUUHE','name':'Family Road Trip'},{'id':'37i9dQZF1DXa8NOEUWPn9W','name':'Happy Hits'}],
    'dinner':     [{'id':'37i9dQZF1DX4xuWVBs4FgJ','name':'Dinner Jazz'},{'id':'37i9dQZF1DWVqJMsgEN0F4','name':'Acoustic Evening'}],
    'workout':    [{'id':'37i9dQZF1DX76Wlfdnj7AP','name':'Beast Mode'},{'id':'37i9dQZF1DX0BcQWzuB7ZO','name':'Dance Hits'}],
    'morning_coffee': [{'id':'37i9dQZF1DX1n9whBbBKoL','name':'Lo-fi Cafe'},{'id':'37i9dQZF1DWXe9gFZP0gtP','name':'Chill Morning'}],
}

def sp_token():
    global spotify_token, token_expires
    if spotify_token and time.time() < token_expires: return spotify_token
    auth = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
    r = http.post('https://accounts.spotify.com/api/token', headers={'Authorization':f'Basic {auth}','Content-Type':'application/x-www-form-urlencoded'}, data={'grant_type':'client_credentials'}, timeout=10)
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
        r = http.get(f'{HA_URL}/api/states/weather.forecast_home', headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
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
        for i, w in enumerate(weights):
            cumul += w
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

def play_sonos(pid, vol=None):
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/select_source', headers=hh, json={'entity_id':SONOS,'source':'Spotify'}, timeout=5)
    except: pass
    time.sleep(1)
    if vol is None:
        vol = {'morning_early':0.12,'morning_late':0.15,'afternoon':0.16,'evening':0.14,'night':0.10}.get(time_slot(), 0.14)
        if kids_mode: vol = min(vol + 0.05, 0.25)
    http.post(f'{HA_URL}/api/services/media_player/volume_set', headers=hh, json={'entity_id':SONOS,'volume_level':vol}, timeout=5)
    http.post(f'{HA_URL}/api/services/media_player/play_media', headers=hh, json={'entity_id':SONOS,'media_content_id':f'spotify:playlist:{pid}','media_content_type':'playlist'}, timeout=5)
    time.sleep(1)
    http.post(f'{HA_URL}/api/services/media_player/shuffle_set', headers=hh, json={'entity_id':SONOS,'shuffle':True}, timeout=5)

def pause_sonos():
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/media_pause', headers=hh, json={'entity_id':SONOS}, timeout=5)
    except: pass

def resume_sonos():
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/media_play', headers=hh, json={'entity_id':SONOS}, timeout=5)
    except: pass

def handle_event(ev):
    """v2.0: React to Event Bus events in real-time."""
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
    
    # Weather change = adjust mood
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
    
    # Lights dimming = calmer music
    elif 'light.living_room' in eid and sig:
        # If brightness drops significantly, switch to calmer
        if new == 'off' or (old != 'off' and new != old):
            logger.info('EVENT: Living room lights changed — considering calmer music')
            action = 'lights_dimmed_noted'
    
    # Presence away = stop music
    elif 'presence' in eid:
        if new == 'off':
            logger.info('EVENT: Departure — pausing music')
            pause_sonos()
            action = 'pause_for_departure'
        elif new == 'on' and old == 'off':
            logger.info('EVENT: Arrival — starting music')
            p = pick()
            play_sonos(p['id'])
            action = 'play_for_arrival'
    
    if action:
        event_actions.append({'time': datetime.now().isoformat(), 'event': eid, 'action': action, 'old': old, 'new': new})

def event_bus_subscriber():
    """v2.0: SSE subscriber thread."""
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
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE error: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)

@app.route('/')
def index():
    moods = [k for k in LIB if k not in ['morning_early','morning_late','afternoon','evening','night']]
    return jsonify({'name':'Spotify Smart DJ','version':'2.0.0','slot':time_slot(),'weather':get_weather(),'kids_mode':kids_mode,'moods':moods,'total_plays':stats['total_plays'],'tv_paused':music_paused_by_tv})

@app.route('/health')
def health():
    return jsonify({'status':'ok' if sp_token() else 'auth_failed','slot':time_slot(),'kids_mode':kids_mode,'event_bus':'connected' if event_actions else 'waiting'})

@app.route('/recommend')
def recommend():
    p = pick(mood=request.args.get('mood'))
    return jsonify({'id':p['id'],'name':p.get('name','?'),'vibe':p.get('vibe',''),'slot':time_slot(),'weather':get_weather(),'score':round(score_playlist(p),1)})

@app.route('/play', methods=['POST','GET'])
def play():
    p = pick(mood=request.args.get('mood'))
    play_sonos(p['id'])
    return jsonify({'success':True,'playing':p.get('name',p['id']),'vibe':p.get('vibe',''),'slot':time_slot(),'weather':get_weather(),'kids_mode':kids_mode})

@app.route('/play/<pid>', methods=['POST','GET'])
def play_id(pid):
    global current_playlist_id
    play_sonos(pid)
    current_playlist_id = pid
    return jsonify({'success':True,'playing':pid})

@app.route('/mood/<mood>', methods=['POST','GET'])
def mood_play(mood):
    if mood not in LIB:
        return jsonify({'error':f'Unknown. Try: {[k for k in LIB if k not in ["morning_early","morning_late","afternoon","evening","night"]]}'}),400
    p = pick(mood=mood)
    play_sonos(p['id'])
    return jsonify({'success':True,'mood':mood,'playing':p.get('name',p['id']),'vibe':p.get('vibe','')})

@app.route('/kids', methods=['POST','GET'])
def kids_on():
    global kids_mode
    kids_mode = True
    p = pick(mood='kids')
    play_sonos(p['id'])
    return jsonify({'success':True,'kids_mode':True,'playing':p.get('name',p['id'])})

@app.route('/kids/off', methods=['POST','GET'])
def kids_off():
    global kids_mode
    kids_mode = False
    p = pick()
    play_sonos(p['id'])
    return jsonify({'success':True,'kids_mode':False,'playing':p.get('name',p['id'])})

@app.route('/like', methods=['POST','GET'])
def like():
    if current_playlist_id:
        stats['likes'][current_playlist_id] = stats['likes'].get(current_playlist_id, 0) + 1
        save_stats()
        return jsonify({'success':True,'liked':current_playlist_id,'total_likes':stats['likes'][current_playlist_id]})
    return jsonify({'error':'Nothing playing'}),400

@app.route('/skip', methods=['POST','GET'])
def skip():
    if current_playlist_id:
        stats['skips'][current_playlist_id] = stats['skips'].get(current_playlist_id, 0) + 1
        save_stats()
        p = pick()
        play_sonos(p['id'])
        return jsonify({'success':True,'skipped':current_playlist_id,'now_playing':p.get('name',p['id'])})
    return jsonify({'error':'Nothing playing'}),400

@app.route('/stats')
def get_stats():
    all_ids = set(list(stats['plays'].keys()) + list(stats['likes'].keys()))
    scored = []
    for pid in all_ids:
        name = '?'
        for cat in LIB.values():
            for p in cat:
                if p['id'] == pid: name = p.get('name', pid); break
        scored.append({'id':pid,'name':name,'plays':stats['plays'].get(pid,0),'likes':stats['likes'].get(pid,0),'skips':stats['skips'].get(pid,0),'score':round(stats['likes'].get(pid,0)*3 - stats['skips'].get(pid,0)*2 + stats['plays'].get(pid,0)*0.5, 1)})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'total_plays':stats['total_plays'],'top_playlists':scored[:10],'kids_mode':kids_mode})

@app.route('/volume/<int:level>', methods=['POST','GET'])
def volume(level):
    vol = max(0, min(100, level)) / 100
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    http.post(f'{HA_URL}/api/services/media_player/volume_set', headers=hh, json={'entity_id':SONOS,'volume_level':vol}, timeout=5)
    return jsonify({'success':True,'volume':vol})

@app.route('/speaker/<entity>', methods=['POST','GET'])
def switch_speaker(entity):
    global SONOS
    SONOS = entity
    return jsonify({'success':True,'speaker':SONOS})

@app.route('/playlists')
def playlists():
    return jsonify(LIB)

@app.route('/search/<query>')
def search(query):
    t = sp_token()
    if not t: return jsonify({'error':'auth failed'}),500
    r = http.get('https://api.spotify.com/v1/search', headers={'Authorization':f'Bearer {t}'}, params={'q':query,'type':'playlist','limit':5}, timeout=5)
    if r.status_code == 200:
        return jsonify([{'id':p['id'],'name':p['name'],'tracks':p.get('tracks',{}).get('total','?')} for p in r.json().get('playlists',{}).get('items',[])])
    return jsonify({'error':'failed'}),500

@app.route('/now-playing')
def now_playing():
    try:
        r = http.get(f'{HA_URL}/api/states/{SONOS}', headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200:
            d = r.json()
            return jsonify({'state':d['state'],'title':d['attributes'].get('media_title'),'artist':d['attributes'].get('media_artist'),'volume':d['attributes'].get('volume_level'),'source':d['attributes'].get('source'),'kids_mode':kids_mode,'tv_paused':music_paused_by_tv})
    except: pass
    return jsonify({'error':'failed'}),500

@app.route('/event-log')
def event_log():
    return jsonify(list(event_actions)[-20:])

if __name__ == '__main__':
    logger.info(f'Spotify Smart DJ v2.0.0 on port {API_PORT}')
    logger.info(f'Sonos: {SONOS}')
    load_stats()
    logger.info(f'Spotify auth: {"OK" if sp_token() else "FAILED"}')
    # v2.0: Start Event Bus SSE subscriber
    threading.Thread(target=event_bus_subscriber, daemon=True).start()
    logger.info('Event Bus subscriber started')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
