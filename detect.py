"""
HDR Now Playing — Railway Background Worker
Utilise numpy pour une FFT précise compatible Shazam
"""

import struct
import time
import uuid
import base64
import json
import urllib.request
import numpy as np

STREAM_URL    = 'http://stream.principeactif.net/hdr.mp3'
FIRESTORE_URL = 'https://firestore.googleapis.com/v1/projects/radiohdr-39922/databases/(default)/documents/nowplaying/current'
INTERVAL_SECS = 60
SAMPLE_RATE   = 16000
CAPTURE_SECS  = 5

def main():
    print('🎵 HDR Now Playing Worker démarré')
    while True:
        try:
            run_detection()
        except Exception as e:
            print(f'[erreur] {e}')
        print(f'⏳ Pause {INTERVAL_SECS}s...')
        time.sleep(INTERVAL_SECS)

def run_detection():
    print(f'\n[{time.strftime("%H:%M:%S")}] Détection...')
    mp3_data = capture_stream()
    if not mp3_data:
        print('❌ Stream inaccessible')
        return
    print(f'✅ {len(mp3_data)} bytes')
    result = recognize(mp3_data)
    if not result:
        print('❌ Non reconnu')
        return
    print(f'🎵 {result["artist"]} — {result["title"]}')
    save_to_firestore(result)

def capture_stream():
    bytes_needed = SAMPLE_RATE * 2 * CAPTURE_SECS * 8
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

def mp3_to_pcm(mp3_data):
    # Chercher le sync word MP3
    offset = 0
    for i in range(min(len(mp3_data) - 4, 2048)):
        if mp3_data[i] == 0xFF and (mp3_data[i+1] & 0xE0) == 0xE0:
            offset = i
            break

    pcm        = []
    frame_size = 417
    pos        = offset
    target     = SAMPLE_RATE * CAPTURE_SECS

    while pos + frame_size < len(mp3_data) and len(pcm) < target:
        pos += 4
        payload = min(frame_size - 4, len(mp3_data) - pos)
        if payload < 2:
            break
        for j in range(0, payload - 1, 2):
            if pos + j + 1 >= len(mp3_data):
                break
            s = (mp3_data[pos + j] << 8) | mp3_data[pos + j + 1]
            if s > 32767:
                s -= 65536
            pcm.append(s / 32768.0)
        pos += frame_size

    return np.array(pcm, dtype=np.float32)

def find_peaks(pcm):
    fft_size = 2048
    hop_size = 64
    bands    = [(250, 520), (520, 1450), (1450, 3500), (3500, 5500)]
    peaks    = []

    if len(pcm) < fft_size:
        return peaks

    # Fenêtre de Hann
    window   = np.hanning(fft_size)
    n_frames = (len(pcm) - fft_size) // hop_size

    for frame in range(n_frames):
        chunk = pcm[frame * hop_size: frame * hop_size + fft_size]
        if len(chunk) < fft_size:
            break

        # FFT avec numpy — précise et rapide
        spectrum = np.fft.rfft(chunk * window)
        mags     = np.abs(spectrum)

        for bi, (lo_hz, hi_hz) in enumerate(bands):
            lo_b = max(0, int(lo_hz * fft_size / SAMPLE_RATE))
            hi_b = min(int(hi_hz * fft_size / SAMPLE_RATE), len(mags) - 1)
            if lo_b >= hi_b:
                continue
            band_mags = mags[lo_b:hi_b + 1]
            best_idx  = np.argmax(band_mags)
            best_mag  = band_mags[best_idx]
            if best_mag > 0.01:
                peaks.append({
                    'freq': int((lo_b + best_idx) * SAMPLE_RATE / fft_size),
                    'time': frame,
                    'band': bi,
                })

    return peaks

def encode_signature(peaks, n_samples):
    h  = struct.pack('<IIII', 0xcafe2580, 0, 0x0001520, 0)
    h += struct.pack('<IIII', n_samples, 0, 0, 0)
    body = b''
    for band in range(4):
        bp = [p for p in peaks if p['band'] == band]
        if not bp:
            continue
        enc = b''
        for p in bp:
            freq_mhz = int(p['freq'] * 1_000_000 / SAMPLE_RATE)
            enc += struct.pack('<I', (p['time'] << 16) | (freq_mhz & 0xFFFF))
        sz  = len(enc)
        pad = (4 - sz % 4) % 4
        body += struct.pack('<II', 0x60030040 + band, sz) + enc + b'\x00' * pad
    return 'data:audio/vnd.shazam.sig;base64,' + base64.b64encode(h + body).decode()

def recognize(mp3_data):
    pcm = mp3_to_pcm(mp3_data)
    print(f'[pcm] {len(pcm)} samples')
    if len(pcm) < 1000:
        return None

    peaks = find_peaks(pcm)
    print(f'[peaks] {len(peaks)} pics trouvés')
    if not peaks:
        return None

    sig  = encode_signature(peaks, len(pcm))
    url  = f'https://amp.shazam.com/discovery/v5/fr/FR/android/-/tag/{uuid.uuid4()}/{uuid.uuid4()}'
    body = json.dumps({
        'timezone':    'Europe/Paris',
        'signature':   {'uri': sig, 'samplems': CAPTURE_SECS * 1000},
        'timestamp':   int(time.time() * 1000),
        'context':     {},
        'geolocation': {},
    }).encode()

    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'User-Agent':   'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data  = json.loads(resp.read())
            track = data.get('track')
            if not track:
                return None
            result = {
                'title':   track.get('title'),
                'artist':  track.get('subtitle'),
                'cover':   track.get('images', {}).get('coverarthq') or track.get('images', {}).get('coverart'),
                'spotify': None,
            }
            for p in track.get('hub', {}).get('providers', []):
                if p.get('type') == 'SPOTIFY':
                    result['spotify'] = p.get('actions', [{}])[0].get('uri')
                    break
            return result if result['title'] and result['artist'] else None
    except Exception as e:
        print(f'[shazam] {e}')
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
