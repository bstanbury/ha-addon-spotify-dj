#!/usr/bin/env python3
"""Spotify Smart DJ — HA Add-on
Adaptive music based on time, weather, mood, and listening patterns.

Endpoints:
  GET  /health              — Health check
  GET  /recommend           — Get recommended playlist for now
  POST /play                — Play recommended playlist on Sonos
  POST /play/<playlist_id>  — Play specific playlist
  GET  /playlists           — Browse playlist library by slot/mood
  GET  /search/<query>      — Search Spotify playlists
  GET  /now-playing         — Current Sonos state
  POST /mood/<mood>         — Play by mood (chill/energetic/focus/party/sleep/romantic/rainy/sunny)
"""
import os, json, time, logging, random, base64
from flask import Flask, jsonify, request
import requests as http

CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
HA_URL = os.environ.get('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.environ.get('HA_TOKEN', '')
API_PORT = int(os.environ.get('API_PORT', '8097'))
SONOS = os.environ.get('SONOS_ENTITY', 'media_player.living_room')

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('spotify-dj')

spotify_token = None
token_expires = 0
recent = []

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
    from datetime import datetime
    h = datetime.now().hour
    if 6<=h<9: return 'morning_early'
    if 9<=h<12: return 'morning_late'
    if 12<=h<17: return 'afternoon'
    if 17<=h<21: return 'evening'
    return 'night'

def weather():
    try:
        r = http.get(f'{HA_URL}/api/states/weather.forecast_home', headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200: return r.json()['state']
    except: pass
    return 'unknown'

def pick(slot=None, mood=None):
    global recent
    w = weather()
    if mood and mood in LIB: cands = LIB[mood]
    elif w in ['rainy','pouring']: cands = LIB.get('rainy', LIB.get(slot or time_slot(), []))
    elif w in ['sunny','clear-night','partlycloudy'] and time_slot() in ['morning_late','afternoon']: cands = LIB.get('sunny', LIB.get(slot or time_slot(), []))
    else: cands = LIB.get(slot or time_slot(), [])
    if not cands: cands = LIB['afternoon']
    avail = [p for p in cands if p['id'] not in recent[-3:]]
    if not avail: avail = cands
    p = random.choice(avail)
    recent.append(p['id'])
    recent = recent[-10:]
    return p

def play_sonos(pid, vol=None):
    hh = {'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    try: http.post(f'{HA_URL}/api/services/media_player/select_source', headers=hh, json={'entity_id':SONOS,'source':'Spotify'}, timeout=5)
    except: pass
    time.sleep(1)
    if vol is None:
        vol = {'morning_early':0.12,'morning_late':0.15,'afternoon':0.16,'evening':0.14,'night':0.10}.get(time_slot(), 0.14)
    http.post(f'{HA_URL}/api/services/media_player/volume_set', headers=hh, json={'entity_id':SONOS,'volume_level':vol}, timeout=5)
    http.post(f'{HA_URL}/api/services/media_player/play_media', headers=hh, json={'entity_id':SONOS,'media_content_id':f'spotify:playlist:{pid}','media_content_type':'playlist'}, timeout=5)
    time.sleep(1)
    http.post(f'{HA_URL}/api/services/media_player/shuffle_set', headers=hh, json={'entity_id':SONOS,'shuffle':True}, timeout=5)

@app.route('/')
def index():
    return jsonify({'name':'Spotify Smart DJ','version':'0.1.0','slot':time_slot(),'weather':weather(),'moods':['chill','energetic','focus','party','sleep','romantic','rainy','sunny']})

@app.route('/health')
def health():
    return jsonify({'status':'ok' if sp_token() else 'auth_failed','slot':time_slot()})

@app.route('/recommend')
def recommend():
    p = pick(mood=request.args.get('mood'))
    return jsonify({'id':p['id'],'name':p.get('name','?'),'vibe':p.get('vibe',''),'slot':time_slot(),'weather':weather(),'url':f'https://open.spotify.com/playlist/{p["id"]}'})

@app.route('/play', methods=['POST','GET'])
def play():
    p = pick(mood=request.args.get('mood'))
    play_sonos(p['id'])
    return jsonify({'success':True,'playing':p.get('name',p['id']),'vibe':p.get('vibe',''),'slot':time_slot(),'weather':weather()})

@app.route('/play/<pid>', methods=['POST','GET'])
def play_id(pid):
    play_sonos(pid)
    return jsonify({'success':True,'playing':pid})

@app.route('/mood/<mood>', methods=['POST','GET'])
def mood_play(mood):
    if mood not in LIB: return jsonify({'error':f'Unknown mood. Try: {[k for k in LIB if k not in ["morning_early","morning_late","afternoon","evening","night"]]}'}),400
    p = pick(mood=mood)
    play_sonos(p['id'])
    return jsonify({'success':True,'mood':mood,'playing':p.get('name',p['id']),'vibe':p.get('vibe','')})

@app.route('/playlists')
def playlists():
    return jsonify(LIB)

@app.route('/search/<query>')
def search(query):
    t = sp_token()
    if not t: return jsonify({'error':'auth failed'}),500
    r = http.get('https://api.spotify.com/v1/search', headers={'Authorization':f'Bearer {t}'}, params={'q':query,'type':'playlist','limit':5}, timeout=5)
    if r.status_code == 200:
        return jsonify([{'id':p['id'],'name':p['name'],'tracks':p.get('tracks',{}).get('total','?'),'owner':p.get('owner',{}).get('display_name','?')} for p in r.json().get('playlists',{}).get('items',[])])
    return jsonify({'error':'search failed'}),500

@app.route('/now-playing')
def now_playing():
    try:
        r = http.get(f'{HA_URL}/api/states/{SONOS}', headers={'Authorization':f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200:
            d = r.json()
            return jsonify({'state':d['state'],'title':d['attributes'].get('media_title'),'artist':d['attributes'].get('media_artist'),'volume':d['attributes'].get('volume_level'),'source':d['attributes'].get('source')})
    except: pass
    return jsonify({'error':'failed'}),500

if __name__ == '__main__':
    logger.info(f'Spotify Smart DJ v0.1.0 on port {API_PORT}')
    logger.info(f'Sonos: {SONOS}')
    logger.info(f'Spotify auth: {"OK" if sp_token() else "FAILED"}')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
