"""One cycle of the live page on GitHub Actions (runs every 5 minutes; reads the meters only
when a new 15-minute reading is due).

Secrets provided by the workflow (never stored in this public repository):
  METER_SETTINGS  contents of meter_settings.json (meter address, login, column mapping)
  METER_IPS       "DAGSR01P=<ip>;AGRIM02P=<ip>"

Writes (and the workflow then commits):
  nube/index.html                    the phone page   -> https://h-foy.github.io/planta-solar-vivo/nube/
  data/YYYY/<METER>_YYYYMMDD.PRN     each finished day: off-site archive and the page's 5-day chart
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


LAST_OUTPUT = {}


def step(name, args, timeout=480):
    r = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    LAST_OUTPUT[name] = r.stdout + r.stderr
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            say(f'  {name}: {line.rstrip()}')
    say(f'{name}: {"ok" if r.returncode == 0 else "ERROR " + str(r.returncode)}')
    return r.returncode == 0


def page_state(full=False):
    """(date, 'HH:MM' of the grid data, 'HH:MM' of the solar data) currently published.
    With full=False only the first two (date, last). Missing values are None."""
    try:
        with open(os.path.join(PAGE_DIR, 'index.html'), encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        html = ''
    d = re.search(r'"date":"(\d{4}-\d{2}-\d{2})"', html)
    last = re.search(r'"last":"(\d{2}:\d{2})"', html)
    sol = re.search(r'"solLast":"(\d{2}:\d{2})?"', html)
    d, last = (d.group(1) if d else None), (last.group(1) if last else None)
    sol_last = (sol.group(1) if sol else last) if html else None    # older pages had no separate solar time
    return (d, last, sol_last) if full else (d, last)


FLAG = os.path.join(PAGE_DIR, 'estado', 'medidores_ok.txt')


def update_flag(now):
    """nube/estado/medidores_ok.txt exists only while both meters' data is less than an hour old.
    An outside monitor (cron-job.org) checks that address and emails when it disappears (404)."""
    d, last, sol_last = page_state(full=True)
    def age(hhmm):
        if not d or not hhmm:
            return 10 ** 6
        h, m = map(int, hhmm.split(':'))
        t = dt.datetime.strptime(d, '%Y-%m-%d') + dt.timedelta(hours=h, minutes=m)
        return (now - t).total_seconds() / 60
    ok = age(last) <= 60 and age(sol_last) <= 60
    if ok:
        os.makedirs(os.path.dirname(FLAG), exist_ok=True)
        with open(FLAG, 'w', encoding='utf-8') as f:
            f.write('OK - los dos medidores responden\n')
    elif os.path.exists(FLAG):
        os.remove(FLAG)
    say(f'Meters status flag: {"OK" if ok else "STALE (flag removed)"} (grid {last}, solar {sol_last})')


STATUS = os.path.join(PAGE_DIR, 'estado', 'medidores.json')
NAMES = {'DAGSR01P': 'Medidor de entrada (red)', 'AGRIM02P': 'Medidor de la planta solar'}


def _reason(text):
    t = text.lower()
    if 'refus' in t or 'busy' in t:
        return 'el equipo de comunicación rechazó la conexión (ocupado o sin servicio)'
    if 'timed out' in t or 'timeout' in t:
        return 'sin respuesta (tiempo agotado)'
    if 'unreachable' in t or 'no route' in t:
        return 'equipo de comunicación inalcanzable'
    return text.strip()[:140]


def update_status(now, read_output=None):
    try:
        _update_status(now, read_output)
    except Exception as e:                                  # never let the status note break the page update
        say(f'Meter status: not updated ({e})')


def _update_status(now, read_output=None):
    """nube/estado/medidores.json: per meter, the last reading obtained and, when the last attempt failed, when and why.
    The live page shows it in its warning when readings stop. Written only when something in it changes."""
    try:
        with open(STATUS, encoding='utf-8') as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        st = {}
    old = json.loads(json.dumps(st.get('medidores', {})))   # untouched copy, to compare at the end
    meters = json.loads(json.dumps(old))
    d, last, sol_last = page_state(full=True)
    published = {'DAGSR01P': last, 'AGRIM02P': sol_last}
    blocks = {}
    if read_output:
        for part in re.split(r'==\s+', read_output)[1:]:
            name = part.split()[0].upper() if part.split() else ''
            if name in NAMES:
                blocks[name] = part
    for m in METERS:
        cur = dict(meters.get(m, {}))
        cur['nombre'] = NAMES[m]
        best = None                                           # newest reading of today obtained this run (from the files)
        for fn in os.listdir(OUT) if os.path.isdir(OUT) else []:
            mm = re.match(rf'{m}_{now:%Y%m%d}(?:_hasta_(\d{{4}}))?\.PRN$', fn)
            if mm:
                hhmm = mm.group(1) or '2400'
                best = max(best or '', f'{hhmm[:2]}:{hhmm[2:]}')
        if best is None and d and published[m]:
            best = published[m] if d == now.strftime('%Y-%m-%d') else None
        if best and (not cur.get('ultima') or f'{now:%Y-%m-%d} {best}' >= cur['ultima']):
            cur['ultima'] = f'{now:%Y-%m-%d} {best}'
        failed = m in blocks and 'FAILED' in blocks[m]
        if failed:
            line = next((l for l in blocks[m].splitlines() if 'FAILED' in l), '')
            cur.update(ok=False, intento=f'{now:%Y-%m-%d %H:%M}', error=_reason(line.split('FAILED:', 1)[-1]))
        elif m in blocks or read_output is None:
            cur.update(ok=True); cur.pop('intento', None); cur.pop('error', None)
        meters[m] = cur
    new = {'medidores': meters}
    if meters != old:
        new['actualizado'] = f'{now:%Y-%m-%d %H:%M}'
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        with open(STATUS, 'w', encoding='utf-8') as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        say(f'Meter status: ' + '; '.join(f'{m} {"ok" if v.get("ok") else "FALLA"} (última {v.get("ultima")})' for m, v in meters.items()))


def archived(day):
    folder = os.path.join(DATA, f'{day:%Y}')
    if os.path.exists(os.path.join(folder, f'{day:%Y%m%d}_incompleto.txt')):
        return True                                       # tried before; the meters don't have the whole day
    return all(os.path.exists(os.path.join(folder, f'{m}_{day:%Y%m%d}.PRN')) for m in METERS)


def main():
    # runs come every 5 minutes, so a busy gateway is simply tried again on the next run;
    # keep each run's knocking short so we never tie up the meters' gateways
    os.environ.setdefault('METER_OPEN_ATTEMPTS', '2')
    os.environ.setdefault('METER_RETRY_WAIT', '20')
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
    pdate, plast, psol = page_state(full=True)
    # the 4 finished days before today are kept in data/ (for the 5-day chart and as an archive);
    # a missing one (yesterday first) is read from the meters, at most one per run
    missing = [d for d in (today - dt.timedelta(days=k) for k in range(1, 5)) if not archived(d)]
    need_yday = bool(missing)
    page_current = ((pdate == today.isoformat() and plast is not None and plast >= due.strftime('%H:%M')) or
                    (due.time() == dt.time(0, 0) and pdate == yday.isoformat() and plast == '24:00')) and psol == plast
    if page_current and not need_yday:
        say(f'Nothing new: page already has {pdate} {plast} (due {due_txt}).')
        update_flag(now)
        update_status(now)
        return
    say(f'Cycle: page has {pdate} {plast}, due {due_txt}, days still to archive: '
        f'{", ".join(d.isoformat() for d in missing) or "none"}')

    if missing:
        day = missing[0]
        if step(f'read {day}', ['sl7000_prn.py', '--date', day.isoformat(), '--outdir', OUT]):
            os.makedirs(os.path.join(DATA, f'{day:%Y}'), exist_ok=True)
            for m in METERS:
                src = os.path.join(OUT, f'{m}_{day:%Y%m%d}.PRN')
                if os.path.exists(src):           # only complete days get a file without _hasta_
                    shutil.copy2(src, os.path.join(DATA, f'{day:%Y}', os.path.basename(src)))
            # yesterday's last reading can lag a little after midnight: keep trying it until 02:00;
            # any other day that still comes back incomplete is noted so it is not read again every run
            if not archived(day) and (day < yday or now.hour >= 2):
                with open(os.path.join(DATA, f'{day:%Y}', f'{day:%Y%m%d}_incompleto.txt'), 'w') as f:
                    f.write('Los medidores no devolvieron el día completo; se omite del gráfico de 5 días.\n')
                say(f'{day}: incomplete in the meters, noted and skipped from now on')
    if not (due.time() == dt.time(0, 0)):
        step('read today', ['sl7000_prn.py', '--date', 'today', '--outdir', OUT])
        update_status(now, LAST_OUTPUT.get('read today', ''))
    if step('build page', ['make_live_page.py', OUT, PAGE_DIR, '--history', DATA], 120):
        say(f'Page state now: {page_state(full=True)}')
    update_flag(dt.datetime.now())


if __name__ == '__main__':
    main()
