"""
HDR Now Playing — Railway Background Worker
Utilise ShazamIO pour la reconnaissance musicale
"""

import asyncio
import time
import json
import urllib.request
from shazamio import Shazam

STREAM_URL    = 'http://stream.principeactif.net/hdr.mp3'
FIRESTORE_URL = 'https://firestore.googleapis.com/v1/projects/radiohdr-39922/databases/(default)/documents/nowplaying/current'
INTERVAL_SECS = 60

def main():
    print('🎵 HDR Now Playing Worker démarré')
    asyncio.run(loop())

async def loop():
    while True:
        try:
            await run_detection()
        except Exception as e:
            print(f'[erreur] {e}')
        print(f'⏳ Pause {INTERVAL_SECS}s...')
        await asyncio.sleep(INTERVAL_SECS)

async def run_detection():
    print(f'\n[{time.strftime("%H:%M:%S")}] Détection...')

    mp3_data = capture_stream()
    if not mp3_data:
        print('❌ Stream inaccessible')
        return
    print(f'✅ {len(mp3_data)} bytes')

    shazam = Shazam()
    result = await shazam.recognize(mp3_data)

    track = result.get('track')
    if not track:
        print('❌ Non reconnu')
        return

    title  = track.get('title')
    artist = track.get('subtitle')
    cover  = track.get('images', {}).get('coverarthq') or track.get('images', {}).get('coverart')
    spotify = None
    for p in track.get('hub', {}).get('providers', []):
        if p.get('type') == 'SPOTIFY':
            spotify = p.get('actions', [{}])[0].get('uri')
            break

    print(f'🎵 {artist} — {title}')
    save_to_firestore({'title': title, 'artist': artist, 'cover': cover, 'spotify': spotify})

def capture_stream():
    bytes_needed = 80_000
    req = urllib.request.Request(STREAM_URL, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; RadioHDR/1.0)',
        'Range': f'bytes=0-{bytes_needed}',
        'Icy-MetaData': '0',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(bytes_needed)
            return data if len(data) > 1000 else None
    except Exception as e:
        print(f'[capture] {e}')
        return None

def save_to_firestore(data):
    body = json.dumps({'fields': {
        'title':      {'stringValue':  data.get('title')   or ''},
        'artist':     {'stringValue':  data.get('artist')  or ''},
        'cover':      {'stringValue':  data.get('cover')   or ''},
        'spotify':    {'stringValue':  data.get('spotify') or ''},
        'updated_at': {'integerValue': str(int(time.time()))},
    }}).encode()
    req = urllib.request.Request(FIRESTORE_URL, data=body, method='PATCH',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10):
            print('✅ Firestore mis à jour')
    except Exception as e:
        print(f'[firestore] {e}')

if __name__ == '__main__':
    main()
