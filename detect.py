import asyncio, time, json, urllib.request
from shazamio import Shazam

STREAM_URL    = 'http://stream.principeactif.net/hdr.mp3'
FIRESTORE_URL = 'https://firestore.googleapis.com/v1/projects/radiohdr-39922/databases/(default)/documents/nowplaying/current'
ICECAST_URL   = 'http://stream.principeactif.net/status-json.xsl'
INTERVAL_SECS = 30
RETRY_SECS    = 15

def main():
    asyncio.run(loop())

async def loop():
    while True:
        try:
            recognized = await run_detection()
            wait = INTERVAL_SECS if recognized else RETRY_SECS
        except Exception as e:
            print(f'[erreur] {e}')
            wait = RETRY_SECS
        await asyncio.sleep(wait)

async def run_detection():
    mp3_data = capture_stream()
    if not mp3_data:
        save_to_firestore({})  # échec capture → vider
        return False

    shazam = Shazam()
    result = await shazam.recognize(mp3_data)
    track  = result.get('track')

    if not track:
        save_to_firestore({})  # échec reconnaissance → vider
        print('[shazam] Titre non reconnu — Firestore vidé')
        return False

    title  = track.get('title')
    artist = track.get('subtitle')
    cover  = track.get('images', {}).get('coverarthq') or track.get('images', {}).get('coverart')
    album  = None
    for section in track.get('sections', []):
        for meta in section.get('metadata', []):
            if meta.get('title', '').lower() == 'album':
                album = meta.get('text')
                break
        if album: break

    listeners = fetch_listeners()
    save_to_firestore({
        'title':    title,
        'artist':   artist,
        'album':    album,
        'cover':    cover,
        'listeners': listeners,
    })
    print(f'[shazam] ✅ {title} — {artist}')
    return True

def fetch_listeners():
    try:
        req = urllib.request.Request(ICECAST_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data    = json.loads(resp.read())
            sources = data.get('icestats', {}).get('source', [])
            if isinstance(sources, list):
                for s in sources:
                    if 'hdr.mp3' in s.get('listenurl', ''):
                        return s.get('listeners', 0)
            elif isinstance(sources, dict):
                return sources.get('listeners', 0)
            return 0
    except:
        return 0

def capture_stream():
    bytes_needed = 150_000
    req = urllib.request.Request(STREAM_URL, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; RadioHDR/1.0)',
        'Range':      f'bytes=0-{bytes_needed}',
        'Icy-MetaData': '0',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(bytes_needed)
            return data if len(data) > 1000 else None
    except:
        return None

def save_to_firestore(data):
    """Écrit les données dans Firestore. Si data vide → efface titre/artist/album/cover."""
    body = json.dumps({'fields': {
        'title':      {'stringValue':  data.get('title')    or ''},
        'artist':     {'stringValue':  data.get('artist')   or ''},
        'album':      {'stringValue':  data.get('album')    or ''},
        'cover':      {'stringValue':  data.get('cover')    or ''},
        'listeners':  {'integerValue': str(data.get('listeners') or 0)},
        'updated_at': {'integerValue': str(int(time.time()))},
    }}).encode()
    req = urllib.request.Request(
        FIRESTORE_URL, data=body, method='PATCH',
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10):
        pass

if __name__ == '__main__':
    main()
