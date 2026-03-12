import asyncio, time, json, urllib.request
from shazamio import Shazam

STREAM_URL    = 'http://stream.principeactif.net/hdr.mp3'
FIRESTORE_BASE = 'https://firestore.googleapis.com/v1/projects/radiohdr-39922/databases/(default)/documents/nowplaying'
FIRESTORE_CURRENT     = f'{FIRESTORE_BASE}/current'
FIRESTORE_BEFORE      = f'{FIRESTORE_BASE}/before'
FIRESTORE_BEFOREBEFORE= f'{FIRESTORE_BASE}/beforebefore'
ICECAST_URL   = 'http://stream.principeactif.net/status-json.xsl'
INTERVAL_SECS = 30
RETRY_SECS    = 15

# ─── État en mémoire ─────────────────────────────────────────────────────────
last_current: dict | None = None
last_before:  dict | None = None

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
    global last_current

    mp3_data = capture_stream()
    if not mp3_data:
        _handle_transition(None)
        return False

    shazam = Shazam()
    result = await shazam.recognize(mp3_data)
    track  = result.get('track')

    if not track:
        print('[shazam] 1er essai échoué, retry...')
        mp3_data2 = capture_stream()
        if mp3_data2:
            result2 = await shazam.recognize(mp3_data2)
            track   = result2.get('track')

    if not track:
        _handle_transition(None)
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
    new_current = {
        'title':     title,
        'artist':    artist,
        'album':     album,
        'cover':     cover,
        'listeners': listeners,
    }

    _handle_transition(new_current)
    print(f'[shazam] ✅ {title} — {artist}')
    return True


def _handle_transition(new_data: dict | None):
    """
    Cascade :
      beforebefore ← before ← current   (si current non null et différent du nouveau)
      current ← new_data
    """
    global last_current, last_before

    new_is_different = (
        last_current is not None and
        (new_data is None or
         new_data.get('title')  != last_current.get('title') or
         new_data.get('artist') != last_current.get('artist'))
    )

    if new_is_different:
        # Si before existe déjà, on le pousse dans beforebefore
        if last_before is not None:
            save_to_firestore(last_before, FIRESTORE_BEFOREBEFORE)
            print(f'[beforebefore] ← {last_before.get("title")} — {last_before.get("artist")}')
        # On pousse current dans before
        save_to_firestore(last_current, FIRESTORE_BEFORE)
        print(f'[before] ← {last_current.get("title")} — {last_current.get("artist")}')
        last_before = last_current

    save_to_firestore(new_data or {}, FIRESTORE_CURRENT)
    last_current = new_data


def capture_stream():
    """Lit le stream en continu pendant ~8 secondes."""
    req = urllib.request.Request(STREAM_URL, headers={
        'User-Agent':   'Mozilla/5.0 (compatible; RadioHDR/1.0)',
        'Icy-MetaData': '0',
    })
    try:
        chunks   = []
        total    = 0
        target   = 320_000
        deadline = time.time() + 10

        with urllib.request.urlopen(req, timeout=15) as resp:
            while total < target and time.time() < deadline:
                chunk = resp.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)

        data = b''.join(chunks)
        return data if len(data) > 10_000 else None
    except:
        return None

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

def save_to_firestore(data: dict, url: str):
    body = json.dumps({'fields': {
        'title':      {'stringValue':  data.get('title')    or ''},
        'artist':     {'stringValue':  data.get('artist')   or ''},
        'album':      {'stringValue':  data.get('album')    or ''},
        'cover':      {'stringValue':  data.get('cover')    or ''},
        'listeners':  {'integerValue': str(data.get('listeners') or 0)},
        'updated_at': {'integerValue': str(int(time.time()))},
    }}).encode()
    req = urllib.request.Request(
        url, data=body, method='PATCH',
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10):
        pass

if __name__ == '__main__':
    main()
