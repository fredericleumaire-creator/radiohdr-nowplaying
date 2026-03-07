"""
HDR Now Playing — Render Background Worker
Tourne en boucle infinie, détecte le morceau toutes les 60 secondes
et écrit directement dans Firestore (sans passer par WordPress)
"""

import struct
import math
import time
import uuid
import base64
import json
import urllib.request
import os

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
    offset = 0
    for i in range(min(len(mp3_data) - 4, 2048)):
        if mp3_data[i] == 0xFF and (mp3_data[i+1] & 0xE0) == 0xE0:
            offset = i
            break
    pcm, frame_size, pos = [], 417, offset
    target = SAMPLE_RATE * CAPTURE_SECS
    while pos < len(mp3_data) and len(pcm) < target:
        pos += 4
        payload = min(frame_size - 4, len(mp3_data) - pos)
        for j in range(0, payload - 1, 2):
            if pos + j + 1 >= len(mp3_data): break
            s = (mp3_data[pos+j] << 8) | mp3_data[pos+j+1]
            if s > 32767: s -= 65536
            pcm.append(s / 32768.0)
        pos += frame_size
    return pcm

def dft(samples):
    n = min(len(samples), 512)
    s = samples[:n]
    re, im = [0.0]*n, [0.0]*n
    for k in range(n):
        for j in range(n):
            a = -2 * math.pi * k * j / n
            re[k] += s[j] * math.cos(a)
            im[k] += s[j] * math.sin(a)
    return re, im

def find_peaks(pcm):
    fft_size, hop_size = 2048, 64
    bands = [(250,520),(520,1450),(1450,3500),(3500,5500)]
    peaks, n_frames = [], (len(pcm) - fft_size) // hop_size
    for frame in range(n_frames):
        chunk = pcm[frame*hop_size: frame*hop_size+fft_size]
        chunk = [s * 0.5*(1-math.cos(2*math.pi*i/(fft_size-1))) for i,s in enumerate(chunk)]
        re, im = dft(chunk)
        mags = [math.sqrt(re[i]**2+im[i]**2) for i in range(fft_size//2)]
        for bi,(lo,hi) in enumerate(bands):
            lo_b = int(lo*fft_size/SAMPLE_RATE)
            hi_b = min(int(hi*fft_size/SAMPLE_RATE), len(mags)-1)
            bm, bb = 0, lo_b
            for b in range(lo_b, hi_b+1):
                if mags[b] > bm: bm, bb = mags[b], b
            if bm > 0.01:
                peaks.append({'freq': int(bb*SAMPLE_RATE/fft_size), 'time': frame, 'band': bi})
    return peaks

def encode_signature(peaks, n_samples):
    h  = struct.pack('<IIII', 0xcafe2580, 0, 0x0001520, 0)
    h += struct.pack('<IIII', n_samples, 0, 0, 0)
    body = b''
    for band in range(4):
        bp = [p for p in peaks if p['band']==band]
        if not bp: continue
        enc = b''.join(struct.pack('<I',(p['time']<<16)|(int(p['freq']*1e6/SAMPLE_RATE)&0xFFFF)) for p in bp)
        sz  = len(enc)
        pad = (4 - sz%4) % 4
        body += struct.pack('<II', 0x60030040+band, sz) + enc + b'\x00'*pad
    return 'data:audio/vnd.shazam.sig;base64,' + base64.b64encode(h+body).decode()

def recognize(mp3_data):
    pcm = mp3_to_pcm(mp3_data)
    if len(pcm) < 1000: return None
    peaks = find_peaks(pcm)
    if not peaks: return None
    sig = encode_signature(peaks, len(pcm))
    url = f'https://amp.shazam.com/discovery/v5/fr/FR/android/-/tag/{uuid.uuid4()}/{uuid.uuid4()}'
    body = json.dumps({
        'timezone': 'Europe/Paris',
        'signature': {'uri': sig, 'samplems': CAPTURE_SECS*1000},
        'timestamp': int(time.time()*1000),
        'context': {}, 'geolocation': {},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data  = json.loads(resp.read())
            track = data.get('track')
            if not track: return None
            result = {
                'title':   track.get('title'),
                'artist':  track.get('subtitle'),
                'cover':   track.get('images',{}).get('coverarthq') or track.get('images',{}).get('coverart'),
                'spotify': None,
            }
            for p in track.get('hub',{}).get('providers',[]):
                if p.get('type')=='SPOTIFY':
                    result['spotify'] = p.get('actions',[{}])[0].get('uri')
                    break
            return result if result['title'] and result['artist'] else None
    except Exception as e:
        print(f'[shazam] {e}')
        return None

def save_to_firestore(data):
    body = json.dumps({'fields': {
        'title':      {'stringValue':  data['title']   or ''},
        'artist':     {'stringValue':  data['artist']  or ''},
        'cover':      {'stringValue':  data['cover']   or ''},
        'spotify':    {'stringValue':  data['spotify'] or ''},
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
