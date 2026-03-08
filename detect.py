import asyncio, time, json, urllib.request, subprocess, tempfile, os
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
        save_to_firestore({})
        return False

    # Décoder MP3 → WAV PCM pour meilleure reconnaissance
    audio_data = decode_to_wav(mp3_data) or mp3_data

    shazam = Shazam()
    result = await shazam.recognize(audio_data)
    track  = result.get('track')

    if not track:
        print('[shazam] 1er essai échoué, retry...')
        mp3_data2  = capture_stream()
        if mp3_data2:
            audio_data2 = decode_to_wav(mp3_data2) or mp3_data2
            result2 = await shazam.recognize(audio_data2)
            track   = result2.get('track')

    if not track:
        save_to_firestore({})
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
        'title':     title,
        'artist':    artist,
        'album':     album,
        'cover':     cover,
        'listeners': listeners,
    })
    print(f'[shazam] ✅ {title} — {artist}')
    return True

def decode_to_wav(mp3_data: bytes) -> bytes | None:
    """Décode le MP3 en WAV PCM 16bit 44100Hz via ffmpeg."""
    try:
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_in:
            tmp_in.write(mp3_data)
            tmp_in_path = tmp_in.name

        tmp_out_path = tmp_in_path.replace('.mp3', '.wav')

        result = subprocess.run([
            'ffmpeg', '-y',
            '-i', tmp_in_path,
            '-ar', '44100',   # sample rate standard Shazam
            '-ac', '1',       # mono
            '-sample_fmt', 's16',
            tmp_out_path
        ], capture_output=True, timeout=15)

        if result.returncode != 0:
            return None

        with open(tmp_out_path, 'rb') as f:
            wav_data = f.read()

        return wav_data if len(wav_data) > 1000 else None

    except Exception as e:
        print(f'[ffmpeg] erreur: {e}')
        return None
    finally:
        for p in [tmp_in_path, tmp_out_path]:
            try: os.unlink(p)
            except: pass

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

def save_to_firestore(data):
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
