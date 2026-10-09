"""Camera photo for the live page (runs on GitHub Actions every 5 minutes, after the meters).

The farm's Dahua camera uploads a snapshot every 15 minutes by FTP to a free InfinityFree account.
This script logs into that FTP, takes the newest photo, hands it to the Cloudflare Worker
(which keeps exactly one photo, overwriting the previous one), then deletes every photo it saw
on the FTP so nothing piles up. No photo history is kept anywhere.

Secrets provided by the workflow (never stored in this public repository):
  CAM_FTP_HOST, CAM_FTP_USER, CAM_FTP_PASS   the InfinityFree FTP login
  CAM_KEY                                    shared key the Worker checks before accepting a photo
Optional:
  CAM_FTP_DIR   folder the camera uploads into (default /htdocs)
  CAM_API       Worker address (default: "api" in pivotes/config.json)

Never fails the workflow: problems are printed and the script exits 0.
"""
import datetime as dt
import ftplib
import io
import json
import os
import posixpath
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PHOTO = ('.jpg', '.jpeg')
MAX_FILES = 5000           # safety limit for the folder walk


def say(msg):
    print(f'{dt.datetime.now():%Y-%m-%d %H:%M:%S}  cámara: {msg}', flush=True)


def worker_url():
    url = os.environ.get('CAM_API', '').strip()
    if not url:
        with open(os.path.join(ROOT, 'pivotes', 'config.json'), encoding='utf-8') as f:
            url = json.load(f)['api']
    return url.rstrip('/')


def walk(ftp, top):
    """Every photo under top, as (path, modified 'YYYYMMDDHHMMSS' or '', size), plus folders seen."""
    photos, dirs, todo = [], [], [top]
    while todo and len(photos) < MAX_FILES:
        d = todo.pop()
        try:
            entries = list(ftp.mlsd(d, facts=['type', 'modify', 'size']))
        except ftplib.error_perm:
            entries = None
        if entries is None:                              # server without MLSD: NLST + try to enter
            try:
                names = [posixpath.basename(n) for n in ftp.nlst(d)]
            except ftplib.error_perm:
                continue
            entries = []
            for n in names:
                p = posixpath.join(d, n)
                try:
                    ftp.cwd(p); ftp.cwd('/')
                    entries.append((n, {'type': 'dir'}))
                except ftplib.error_perm:
                    entries.append((n, {'type': 'file'}))
        for name, facts in entries:
            if name in ('.', '..'):
                continue
            p = posixpath.join(d, name)
            if facts.get('type') == 'dir':
                todo.append(p); dirs.append(p)
            elif facts.get('type', 'file') == 'file' and name.lower().endswith(PHOTO):
                photos.append((p, facts.get('modify', ''), int(facts.get('size', 0) or 0)))
    return photos, dirs


def main():
    host = os.environ.get('CAM_FTP_HOST', '').strip()
    user = os.environ.get('CAM_FTP_USER', '').strip()
    pw = os.environ.get('CAM_FTP_PASS', '')
    key = os.environ.get('CAM_KEY', '')
    top = os.environ.get('CAM_FTP_DIR', '').strip() or '/htdocs'
    if not (host and user and pw and key):
        say('sin configurar (faltan secretos CAM_*), nada que hacer'); return

    ftp = ftplib.FTP(host, timeout=40)
    try:
        ftp.login(user, pw)
        try:
            photos, dirs = walk(ftp, top)
        except ftplib.error_perm as e:
            say(f'carpeta {top} no disponible todavía ({e})'); return
        photos = [p for p in photos if p[2] != 0 or not p[1]]   # skip zero-byte files still being written
        if not photos:
            say('no hay fotos nuevas'); return

        # newest by server time, else by path (Dahua paths contain date and time)
        newest = max(photos, key=lambda p: (p[1], p[0]))
        buf = io.BytesIO()
        ftp.retrbinary('RETR ' + newest[0], buf.write)
        data = buf.getvalue()
        if len(data) < 1000 or not data.startswith(b'\xff\xd8'):
            say(f'foto inválida {newest[0]} ({len(data)} bytes), se reintenta en la próxima vuelta'); return

        when = newest[1]
        hora = (f'{when[:4]}-{when[4:6]}-{when[6:8]}T{when[8:10]}:{when[10:12]}:{when[12:14]}Z'
                if len(when) >= 14 else dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        req = urllib.request.Request(worker_url() + '/foto', data=data, method='PUT', headers={
            'Content-Type': 'image/jpeg', 'X-Cam-Key': key, 'X-Foto-Hora': hora,
            'User-Agent': 'planta-solar-camara'})
        with urllib.request.urlopen(req, timeout=40) as r:
            if r.status != 200:
                say(f'el Worker respondió {r.status}, no se borra nada'); return
        say(f'foto publicada: {newest[0]} ({len(data)//1024} KB, {hora})')

        # clear the mailbox: every photo seen in this run, then empty folders (deepest first)
        gone = 0
        for p, _, _ in photos:
            try:
                ftp.delete(p); gone += 1
            except ftplib.Error:
                pass
        for d in sorted(dirs, key=len, reverse=True):
            try:
                ftp.rmd(d)
            except ftplib.Error:
                pass
        say(f'{gone} foto(s) borradas del FTP')
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                                # never break the meter workflow
        say(f'error: {type(e).__name__}: {e}')
    sys.exit(0)
