"""IPTV Player ev sunucusu.

- Oynatıcıyı (index.html) http üzerinden sunar.
- /hls/start?url=...&t=...  MKV/AVI gibi iPad'in açamadığı filmleri ffmpeg ile
  anında HLS'ye çevirir (H.264 ise video kopyalanır, değilse NVENC/x264 ile
  kodlanır; ses AAC stereo olur; metin altyazı varsa WebVTT olarak eklenir).

Çalıştırma: python server.py  (IPTV_BASLAT.bat bunu yapar)
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8795
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(tempfile.gettempdir(), 'iptv-hls')
SEG = 6  # saniye
# ffmpeg'in mpegts çıktısı zaman damgalarına sabit 1.4 sn ekler (90 kHz)
MPEGTS_OFFSET = 126000
UA = 'VLC/3.0.20 LibVLC/3.0.20'
TEXT_SUBS = ('subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text')

jobs = {}
jobs_lock = threading.Lock()
NVENC = None


def has_nvenc():
    global NVENC
    if NVENC is None:
        try:
            r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                                'testsrc=size=320x240', '-t', '0.2', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                               capture_output=True, timeout=20)
            NVENC = r.returncode == 0
        except Exception:
            NVENC = False
    return NVENC


def probe(url):
    """ffprobe olmadan: ffmpeg -i çıktısından akışları okur."""
    r = subprocess.run(['ffmpeg', '-hide_banner', '-user_agent', UA, '-i', url],
                       capture_output=True, timeout=40)
    err = r.stderr.decode('utf-8', 'replace')
    streams = []
    for m in re.finditer(r'Stream #0:(\d+)(?:\((\w+)\))?[^:]*: (Video|Audio|Subtitle): (\w+)([^\n]*)', err):
        idx, lang, kind, codec, rest = m.groups()
        s = {'index': int(idx), 'lang': (lang or '').lower(), 'kind': kind, 'codec': codec, 'rest': rest}
        if kind == 'Video':
            hm = re.search(r', (\d{2,5})x(\d{2,5})', rest)
            s['height'] = int(hm.group(2)) if hm else 0
            s['yuv420p'] = 'yuv420p,' in rest or 'yuv420p(' in rest or rest.rstrip().endswith('yuv420p')
            s['profile'] = (re.search(r'\(([^)]+)\)', rest) or [None, ''])[1]
            if 'attached pic' in rest:
                continue
        streams.append(s)
    dm = re.search(r'Duration: (\d+):(\d+):([\d.]+)', err)
    duration = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)) if dm else 0
    if not any(s['kind'] == 'Video' for s in streams):
        tail = '\n'.join(err.strip().splitlines()[-3:])
        raise RuntimeError('Film açılamadı: ' + tail)
    return streams, duration


def pick(streams, kind, prefer=('tur', 'tr')):
    cand = [s for s in streams if s['kind'] == kind]
    if kind == 'Subtitle':
        cand = [s for s in cand if s['codec'] in TEXT_SUBS]
    for s in cand:
        if s['lang'] in prefer:
            return s
    return cand[0] if cand else None


class Job:
    def __init__(self, jid, url, start):
        self.id, self.url, self.start = jid, url, start
        self.dir = os.path.join(WORK_DIR, jid)
        self.proc = None
        self.error = None
        self.sub = None
        self.sub_default = False
        self.done = False
        self.ready = threading.Event()

    def run(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            streams, _ = probe(self.url)
            v = pick(streams, 'Video')
            a = pick(streams, 'Audio')
            self.sub = pick(streams, 'Subtitle')
            # Türkçe dublaj varsa altyazı varsayılan kapalı, yoksa açık
            self.sub_default = not (a and a['lang'] in ('tur', 'tr'))
            copy = (v['codec'] == 'h264' and v.get('yuv420p') and 0 < v.get('height', 0) <= 1080
                    and v.get('profile', '') in ('High', 'Main', 'Baseline', 'Constrained Baseline'))
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
            if self.start > 0:
                cmd += ['-ss', str(self.start)]
            if self.url.startswith('http'):
                cmd += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5']
            cmd += ['-user_agent', UA, '-i', self.url, '-map', '0:%d' % v['index']]
            if a:
                cmd += ['-map', '0:%d' % a['index']]
            if copy:
                cmd += ['-c:v', 'copy']
            else:
                scale = ['-vf', 'scale=-2:1080'] if v.get('height', 0) > 1080 else []
                enc = (['-c:v', 'h264_nvenc', '-preset', 'fast'] if has_nvenc()
                       else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22'])
                cmd += scale + enc + ['-profile:v', 'high', '-level', '4.1', '-pix_fmt', 'yuv420p',
                                      '-b:v', '4500k', '-maxrate', '6M', '-bufsize', '9M', '-g', '150']
            cmd += ['-c:a', 'aac', '-ac', '2', '-b:a', '160k',
                    '-f', 'hls', '-hls_time', str(SEG), '-hls_list_size', '0', '-hls_playlist_type', 'event',
                    '-hls_segment_filename', os.path.join(self.dir, 'v_%05d.ts'),
                    os.path.join(self.dir, 'video.m3u8')]
            if self.sub:
                cmd += ['-map', '0:%d' % self.sub['index'], '-c:s', 'webvtt', '-flush_packets', '1',
                        '-f', 'webvtt', os.path.join(self.dir, 'subs.vtt')]
            with open(os.path.join(self.dir, 'ffmpeg.log'), 'wb') as log:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log,
                                             stdin=subprocess.DEVNULL)
            print('  [%s] %s, video %s, ses %s, altyazı %s' % (
                self.id, 'kopya' if copy else 'kodlama', v['codec'], a['codec'] if a else '-',
                self.sub['lang'] or self.sub['codec'] if self.sub else '-'))
            threading.Thread(target=self._wait, daemon=True).start()
            deadline = time.time() + 90
            while time.time() < deadline:
                if self.segments_ready(2) or self.proc.poll() is not None:
                    break
                time.sleep(0.3)
            if not os.path.exists(os.path.join(self.dir, 'video.m3u8')):
                self.error = 'Dönüştürme başlamadı: ' + self.log_tail()
        except Exception as e:  # noqa: BLE001 — kullanıcıya mesaj olarak döner
            self.error = str(e)
        self.ready.set()

    def _wait(self):
        self.proc.wait()
        self.done = True

    def log_tail(self):
        try:
            with open(os.path.join(self.dir, 'ffmpeg.log'), 'rb') as f:
                return f.read().decode('utf-8', 'replace').strip()[-300:] or 'bilinmeyen hata'
        except OSError:
            return 'bilinmeyen hata'

    def video_playlist(self):
        try:
            with open(os.path.join(self.dir, 'video.m3u8'), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def segments_ready(self, n):
        return self.video_playlist().count('#EXTINF') >= n

    def video_seconds(self):
        return sum(float(x) for x in re.findall(r'#EXTINF:([\d.]+)', self.video_playlist()))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    # ---- altyazı: ffmpeg'in yazdığı subs.vtt'yi 6 sn'lik HLS parçalarına böler ----
    def subs_playlist(self):
        total = self.video_seconds()
        n = int(total // SEG) + (1 if self.done and total % SEG else 0)
        out = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:%d' % SEG,
               '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-PLAYLIST-TYPE:EVENT']
        for i in range(n):
            out += ['#EXTINF:%.3f,' % min(SEG, total - i * SEG), 'sub_%d.vtt' % i]
        if self.done:
            out.append('#EXT-X-ENDLIST')
        return '\n'.join(out) + '\n'

    def subs_segment(self, i):
        try:
            with open(os.path.join(self.dir, 'subs.vtt'), encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            text = ''
        lo, hi = i * SEG, (i + 1) * SEG
        cues = []
        for block in re.split(r'\n\s*\n', text):
            m = re.search(r'([\d:.]+) --> ([\d:.]+)', block)
            if m and to_sec(m.group(1)) < hi and to_sec(m.group(2)) > lo:
                cues.append(block[block.index(m.group(0)):].strip())
        head = 'WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:%d,LOCAL:00:00:00.000\n\n' % MPEGTS_OFFSET
        return head + '\n\n'.join(cues) + '\n'

    def master(self):
        out = ['#EXTM3U']
        if self.sub:
            out.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Altyazı",LANGUAGE="%s",'
                       'DEFAULT=%s,AUTOSELECT=YES,URI="subs.m3u8"' % (
                           'tr' if self.sub['lang'] in ('tur', 'tr', '') else self.sub['lang'],
                           'YES' if self.sub_default else 'NO'))
            out.append('#EXT-X-STREAM-INF:BANDWIDTH=6000000,SUBTITLES="subs"')
        else:
            out.append('#EXT-X-STREAM-INF:BANDWIDTH=6000000')
        out.append('video.m3u8')
        return '\n'.join(out) + '\n'


def to_sec(t):
    parts = [float(p) for p in t.split(':')]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def get_job(url, start):
    jid = hashlib.sha1(('%s|%d' % (url, start)).encode()).hexdigest()[:12]
    with jobs_lock:
        job = jobs.get(jid)
        if job and not job.error:
            return job, False
        # Tek izleyici: önceki dönüştürmeleri durdur, eski klasörleri temizle
        for other in list(jobs.values()):
            other.stop()
            shutil.rmtree(other.dir, ignore_errors=True)
        jobs.clear()
        job = Job(jid, url, start)
        jobs[jid] = job
    return job, True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=APP_DIR, **kw)

    def log_message(self, fmt, *args):
        pass  # konsolu sade tut

    def send_body(self, code, body, ctype):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith('/hls/'):
            return super().do_GET()
        q = parse_qs(u.query)
        if u.path == '/hls/ping':
            return self.send_body(200, json.dumps({'ok': True}), 'application/json')
        if u.path == '/hls/start':
            url = (q.get('url') or [''])[0]
            if not re.match(r'^https?://', url):
                return self.send_body(400, 'url gerekli', 'text/plain; charset=utf-8')
            try:
                start = max(0, int(float((q.get('t') or ['0'])[0])))
            except ValueError:
                start = 0
            job, new = get_job(url, start)
            if new:
                threading.Thread(target=job.run, daemon=True).start()
            job.ready.wait(120)
            if job.error:
                print('  [hata] ' + job.error)
                return self.send_body(502, job.error, 'text/plain; charset=utf-8')
            self.send_response(302)
            self.send_header('Location', '/hls/%s/master.m3u8' % job.id)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        m = re.match(r'^/hls/([0-9a-f]{12})/([\w.]+)$', u.path)
        job = jobs.get(m.group(1)) if m else None
        if not job:
            return self.send_body(404, 'yok', 'text/plain')
        name = m.group(2)
        mpegurl = 'application/vnd.apple.mpegurl'
        if name == 'master.m3u8':
            return self.send_body(200, job.master(), mpegurl)
        if name == 'video.m3u8':
            return self.send_body(200, job.video_playlist(), mpegurl)
        if name == 'subs.m3u8' and job.sub:
            return self.send_body(200, job.subs_playlist(), mpegurl)
        sm = re.match(r'^sub_(\d+)\.vtt$', name)
        if sm and job.sub:
            return self.send_body(200, job.subs_segment(int(sm.group(1))), 'text/vtt; charset=utf-8')
        if re.match(r'^v_\d{5}\.ts$', name):
            path = os.path.join(job.dir, name)
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    return self.send_body(200, f.read(), 'video/mp2t')
        return self.send_body(404, 'yok', 'text/plain')


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)
    if not shutil.which('ffmpeg'):
        print('UYARI: ffmpeg bulunamadı; MKV dönüştürme çalışmaz.')
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print()
    print('  IPTV Player çalışıyor. Bu pencere açık kaldığı sürece iPad\'den açabilirsiniz.')
    print()
    print('    iPad adresi / MKV dönüştürücü:  http://%s:%d/' % (lan_ip(), port))
    print()
    print('  Kapatmak için bu pencereyi kapatın.')
    print()
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
