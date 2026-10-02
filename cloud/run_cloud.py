"""One cycle of the live page on GitHub Actions (runs every 5 minutes; reads the meters only
when a new 15-minute reading is due).

Secrets provided by the workflow (never stored in this public repository):
  METER_SETTINGS  contents of meter_settings.json (meter address, login, column mapping)
  METER_IPS       "DAGSR01P=<ip>;AGRIM02P=<ip>"

Writes (and the workflow then commits):
  nube/index.html                    the phone page   -> https://h-foy.github.io/planta-solar-vivo/nube/
  data/YYYY/<METER>_YYYYMMDD.PRN     each finished day, as an off-site archive
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .../cloud
ROOT = os.path.dirname(HERE)                                # repository root
OUT = os.path.join(HERE, 'Output')                          # scratch, not committed
PAGE_DIR = os.path.join(ROOT, os.environ.get('PAGE_DIR', 'nube'))
DATA = os.path.join(ROOT, 'data')
METERS = ('AGRIM02P', 'DAGSR01P')


def say(msg):
    print(f'{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}', flush=True)


def step(name, args, timeout=480):
    r = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            say(f'  {name}: {line.rstrip()}')
    say(f'{name}: {"ok" if r.returncode == 0 else "ERROR " + str(r.returncode)}')
    return r.returncode == 0


def page_state():
    """(date, 'HH:MM') of the data currently published, or (None, None)."""
    try:
        with open(os.path.join(PAGE_DIR, 'index.html'), encoding='utf-8') as f:
            html = f.read()
        d = re.search(r'"date":"(\d{4}-\d{2}-\d{2})"', html)
        last = re.search(r'"last":"(\d{2}:\d{2})"', html)
        return (d.group(1) if d else None), (last.group(1) if last else None)
    except FileNotFoundError:
        return None, None


def archived(day):
    return all(os.path.exists(os.path.join(DATA, f'{day:%Y}', f'{m}_{day:%Y%m%d}.PRN')) for m in METERS)


def main():
    settings = os.environ.get('METER_SETTINGS', '').strip()
    if not settings:
        raise SystemExit('Missing secret METER_SETTINGS')
    json.loads(settings)                                     # fail early on a bad paste
    with open(os.path.join(HERE, 'meter_settings.json'), 'w', encoding='utf-8') as f:
        f.write(settings)
    os.makedirs(OUT, exist_ok=True)

    now = dt.datetime.now()                                  # TZ=America/Argentina/Buenos_Aires in the workflow
    today, yday = now.date(), (now - dt.timedelta(days=1)).date()
    due = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=now.minute % 15)
    due_txt = '24:00' if due.time() == dt.time(0, 0) else due.strftime('%H:%M')
    pdate, plast = page_state()
    need_yday = now.hour < 2 and not archived(yday)      # archive attempts only 00:00-01:59
    page_current = (pdate == today.isoformat() and plast is not None and plast >= due.strftime('%H:%M')) or \
                   (due.time() == dt.time(0, 0) and pdate == yday.isoformat() and plast == '24:00')
    if page_current and not need_yday:
        say(f'Nothing new: page already has {pdate} {plast} (due {due_txt}).')
        return
    say(f'Cycle: page has {pdate} {plast}, due {due_txt}, archive yesterday: {need_yday}')

    if need_yday:
        if step('read yesterday', ['sl7000_prn.py', '--date', yday.isoformat(), '--outdir', OUT]):
            os.makedirs(os.path.join(DATA, f'{yday:%Y}'), exist_ok=True)
            for m in METERS:
                src = os.path.join(OUT, f'{m}_{yday:%Y%m%d}.PRN')
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(DATA, f'{yday:%Y}', os.path.basename(src)))
    if not (due.time() == dt.time(0, 0)):
        step('read today', ['sl7000_prn.py', '--date', 'today', '--outdir', OUT])
    if step('build page', ['make_live_page.py', OUT, PAGE_DIR], 120):
        say(f'Page state now: {page_state()}')


if __name__ == '__main__':
    main()
