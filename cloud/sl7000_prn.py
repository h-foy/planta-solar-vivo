#!/usr/bin/env python3
"""
sl7000_prn.py  -  READ-ONLY load-profile puller for the two Actaris/Itron SL7000 meters
                  (DAGSR01P and AGRIM02P), writing the same .PRN files Ace Pilot exports.

It only ever sends:  HDLC SNRM / DISC (link open/close), AARQ / RLRQ (association open/close)
and GET requests.  There is no SET, no ACTION, no clock change anywhere in this file.

Typical use (Windows command prompt, in the folder that holds this script):

    python sl7000_prn.py                       -> yesterday's PRN for both meters
    python sl7000_prn.py --date 2026-09-30     -> one specific day
    python sl7000_prn.py --from 2026-09-25 --to 2026-09-30   -> several days, one PRN per meter per day
    python sl7000_prn.py --probe               -> find the address / interface each meter answers on
    python sl7000_prn.py --calibrate DAGSR01P_20260930.PRN AGRIM02P_20260930.PRN
                                               -> learn column order + multipliers from Ace Pilot files

Settings that work are saved in meter_settings.json next to this script, so after the first
successful --probe / --calibrate the plain command is all you need.
"""

import argparse
import csv
import datetime as dt
import json
import os
import socket
import sys
import time

try:
    from gurux_dlms import GXDLMSClient, GXReplyData, GXByteBuffer, GXDLMSException, GXDateTime
    from gurux_dlms.enums import InterfaceType, Authentication, ObjectType
    from gurux_dlms.objects import (GXDLMSProfileGeneric, GXDLMSRegister, GXDLMSExtendedRegister,
                                    GXDLMSDemandRegister)
except ImportError:
    print("Gurux library missing. Run:   pip install gurux-dlms")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "meter_settings.json")

# ------------------------------------------------------------------ meters
METERS = {
    "DAGSR01P": {"serial": "84470406", "public_ip": "", "local_ip": "192.168.0.253",
                 "port": 4000, "desc": "Property entrance"},
    "AGRIM02P": {"serial": "01406450", "public_ip": "", "local_ip": "192.168.0.254",
                 "port": 4000, "desc": "Solar site"},
}
# Optional override, used by the cloud copy so the addresses stay out of the public repository:
#   METER_IPS="DAGSR01P=1.2.3.4;AGRIM02P=1.2.3.5"
for _pair in filter(None, os.environ.get("METER_IPS", "").replace(",", ";").split(";")):
    _k, _, _v = _pair.partition("=")
    if _k.strip() in METERS and _v.strip():
        METERS[_k.strip()]["public_ip"] = _v.strip()

DEFAULT_CONN = {
    "interface": "hdlc",       # hdlc | wrapper
    "client": 16,              # 16 = public client (no password)
    "auth": "none",            # none | low
    "logical": 1,
    "physical": 1,
    "addr_size": 0,            # 0 = let Gurux choose (1 byte when it fits), 4 = force 4-byte HDLC address
    "profile": "1.0.99.1.0.255",
    "columns": None,           # list of {"index": i, "multiplier": m} learned by --calibrate
}

# DLMS unit codes that are converted from W/Wh/var/varh/VA/VAh to the k-unit used in the PRN
K_UNITS = {27, 28, 29, 30, 31, 32}
UNIT_NAMES = {27: "W", 28: "VA", 29: "var", 30: "Wh", 31: "VAh", 32: "varh", 33: "A", 35: "V", 44: "Hz"}

VERBOSE = False
OPEN_ATTEMPTS = 6      # connection attempts per meter
RETRY_WAIT = 30        # seconds between attempts


def log(msg):
    print(msg, flush=True)


def vlog(msg):
    if VERBOSE:
        print("   . " + msg, flush=True)


# ------------------------------------------------------------------ settings
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    log(f"   saved settings -> {SETTINGS_FILE}")


def conn_for(name, settings, args):
    c = dict(DEFAULT_CONN)
    c.update(settings.get(name, {}))
    # command-line overrides win
    for key in ("interface", "client", "auth", "logical", "physical", "addr_size", "profile"):
        v = getattr(args, key, None)
        if v is not None:
            c[key] = v
    if args.password is not None:
        c["password"] = args.password
    return c


# ------------------------------------------------------------------ transport
class Link:
    """One TCP connection to the Smec gateway + Gurux client state. Read-only by design."""

    def __init__(self, host, port, conn, timeout=8.0, retries=1):
        self.host, self.port, self.timeout, self.retries = host, port, timeout, retries
        iface = InterfaceType.WRAPPER if conn["interface"] == "wrapper" else InterfaceType.HDLC
        auth = Authentication.LOW if conn.get("auth") == "low" else Authentication.NONE
        pw = conn.get("password") if auth == Authentication.LOW else None
        if conn["interface"] == "wrapper":
            server = conn["logical"]
        else:
            server = GXDLMSClient.getServerAddress(conn["logical"], conn["physical"], conn.get("addr_size", 0))
        self.client = GXDLMSClient(True, conn["client"], server, auth, pw, iface)
        if conn["interface"] != "wrapper" and conn.get("addr_size"):
            self.client.serverAddressSize = conn["addr_size"]
        self.sock = None
        self.state = "none"          # none -> link (HDLC UA received) -> assoc (AARE accepted)

    # -- socket
    def open(self, attempts=None):
        # The gateway takes one connection at a time (CAMMESA's SMEC polling, Ace Pilot, or a previous
        # session that has not timed out yet can be holding it), so a refusal is retried a few times.
        attempts = attempts if attempts is not None else OPEN_ATTEMPTS
        for n in range(1, attempts + 1):
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                break
            except (ConnectionRefusedError, ConnectionResetError, socket.timeout, TimeoutError) as e:
                if n == attempts:
                    raise ConnectionRefusedError(
                        f"{self.host}:{self.port} is busy or refusing connections ({type(e).__name__}). "
                        "The gateway may be in use by another reading system - try again in a few minutes.")
                log(f"   gateway {self.host} busy/refused ({type(e).__name__}), retrying in {RETRY_WAIT}s "
                    f"[{n}/{attempts - 1}]")
                time.sleep(RETRY_WAIT)
        self.sock.settimeout(self.timeout)
        time.sleep(0.3)
        self._drain()

    def _drain(self):
        try:
            self.sock.settimeout(0.2)
            while self.sock.recv(4096):
                pass
        except (socket.timeout, BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

    def close_socket(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    # -- packets
    def _packet(self, data, reply):
        if data is None and not reply.isStreaming():
            return
        notify = GXReplyData()
        reply.error = 0
        rd = GXByteBuffer()
        if data:
            vlog("TX " + bytes(data).hex(" "))
            self.sock.sendall(bytes(data))
        attempts = 0
        while True:
            if rd.size > 0 and self.client.getData(rd, reply, notify):
                break
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                attempts += 1
                if attempts > self.retries:
                    raise TimeoutError("no answer from meter")
                if data:
                    vlog("timeout, resending")
                    self.sock.sendall(bytes(data))
                continue
            if not chunk:
                raise ConnectionError("gateway closed the connection")
            vlog("RX " + chunk.hex(" "))
            rd.set(chunk)
        if reply.error != 0:
            raise GXDLMSException(reply.error)

    def block(self, data, reply):
        if not data:
            return
        if isinstance(data, list):
            for it in data:
                reply.clear()
                self.block(it, reply)
            return
        self._packet(data, reply)
        while reply.isMoreData():
            nxt = None if reply.isStreaming() else self.client.receiverReady(reply)
            self._packet(nxt, reply)

    # -- association (read-only services only)
    def snrm(self):
        reply = GXReplyData()
        data = self.client.snrmRequest()
        if data:
            self._packet(data, reply)
            self.client.parseUAResponse(reply.data)
        self.state = "link"

    def associate(self):
        if self.client.interfaceType == InterfaceType.HDLC:
            self.snrm()
        reply = GXReplyData()
        self.block(self.client.aarqRequest(), reply)
        self.client.parseAareResponse(reply.data)
        self.state = "assoc"

    def get(self, obj, attr):
        reply = GXReplyData()
        self.block(self.client.read(obj, attr), reply)
        return self.client.updateValue(obj, attr, reply.value)

    def rows_by_range(self, pg, start, end):
        reply = GXReplyData()
        self.block(self.client.readRowsByRange(pg, start, end), reply)
        return self.client.updateValue(pg, 2, reply.value)

    def release(self):
        reply = GXReplyData()
        if self.sock and self.state == "assoc":
            try:
                self.block(self.client.releaseRequest(), reply)
            except Exception:
                pass
        if self.sock and self.state in ("link", "assoc") and self.client.interfaceType == InterfaceType.HDLC:
            try:
                reply.clear()
                self.block(self.client.disconnectRequest(), reply)
            except Exception:
                pass
        self.state = "none"
        self.close_socket()


# ------------------------------------------------------------------ profile reading
def scaled_unit(obj):
    try:
        return int(obj.unit)
    except Exception:
        return None


def stamp_info(v):
    """('marker', D 00:15) for a date-only day marker, ('midnight', D+1 00:00) for a 00:00 stamp,
    ('time', datetime) for any other full stamp; None when the row carries no stamp."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)) and len(v) == 12:
        ts = find_timestamp(v)
        if ts is None:
            return None
        if v[5] == 0xFF:
            return ("marker", ts)
        if v[5] == 0 and v[6] == 0:
            return ("midnight", ts)
        return ("time", ts)
    if isinstance(v, (list, tuple)):
        for x in v:
            st = stamp_info(x)
            if st:
                return st
        return None
    ts = find_timestamp(v)
    return ("time", ts) if ts else None


def find_timestamp(v):
    """Returns the interval-END time a row is stamped with, or None.

    Accepts a GXDateTime, a datetime, a 12-byte DLMS date-time octet string, or a structure
    containing one. The SL7000 (0.0.99.1.0.255, column 0.0.96.55.1) stamps only some rows:
      * full stamp with a time, e.g. 2026-09-30 10:15  -> that interval end
      * full stamp at 00:00 of day D                   -> midnight closing day D = D+1 00:00
        (verified 2026-10-01: '2026-09-29 00:00' sits on the row Ace Pilot shows as 9/29 24:00)
      * date-only stamp (time bytes 0xFF) for day D    -> first interval of day D = D 00:15
    """
    if v is None:
        return None
    if isinstance(v, GXDateTime):
        v = v.value
    if isinstance(v, dt.datetime):
        return dt.datetime(v.year, v.month, v.day, v.hour, v.minute)
    if isinstance(v, (bytes, bytearray)) and len(v) == 12:
        y = (v[0] << 8) | v[1]
        mo, d, h, mi = v[2], v[3], v[5], v[6]
        if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
            return None
        day = dt.datetime(y, mo, d)
        if h == 0xFF:                                   # date-only marker: a new day starts here
            return day + dt.timedelta(minutes=15)
        if h > 23 or mi > 59:
            return None
        if h == 0 and mi == 0:                          # end of day D
            return day + dt.timedelta(days=1)
        return day + dt.timedelta(hours=h, minutes=mi)
    if isinstance(v, (list, tuple)):
        for x in v:
            t = find_timestamp(x)
            if t:
                return t
    return None


def read_profile(link, conn, start, end):
    """Returns (columns_meta, rows) where rows = [(datetime, [v1, v2, ...]), ...] in PRN units."""
    pg = GXDLMSProfileGeneric(conn["profile"])
    link.get(pg, 3)                       # capture objects
    try:
        link.get(pg, 4)                   # capture period (s)
    except Exception:
        pg.capturePeriod = 900
    period = pg.capturePeriod or 900

    meta = []                             # one entry per capture column
    scalers_ok = True
    for i, (obj, ca) in enumerate(pg.captureObjects):
        info = {"col": i, "obis": obj.logicalName, "class": int(obj.objectType), "attr": ca.attributeIndex,
                "unit": None, "kind": "other"}
        if obj.objectType == ObjectType.CLOCK:
            info["kind"] = "clock"
        elif isinstance(obj, (GXDLMSRegister, GXDLMSExtendedRegister, GXDLMSDemandRegister)):
            if scalers_ok:
                try:
                    link.get(obj, 3 if not isinstance(obj, GXDLMSDemandRegister) else 4)
                except Exception as e:
                    scalers_ok = False   # reading access level often cannot see scalers; calibration covers it
                    vlog(f"scaler read refused for {obj.logicalName}: {e}")
            info["unit"] = scaled_unit(obj)
            info["kind"] = "value"
        meta.append(info)

    link.rows_by_range(pg, start, end)

    clock_col = next((m["col"] for m in meta if m["kind"] == "clock"), None)
    value_cols = [m for m in meta if m["kind"] == "value"]
    cols_to_scan = [clock_col] if clock_col is not None else [m["col"] for m in meta if m["kind"] != "value"]
    step = dt.timedelta(seconds=period)

    # 1) what each row is stamped with (most rows: nothing)
    stamps = []
    for r in pg.buffer:
        st = None
        for c in cols_to_scan:
            st = stamp_info(r[c])
            if st:
                break
        stamps.append(st)

    # 2) anchors: date-only day markers and mid-day stamps are reliable; the SL7000's
    #    midnight stamp is not (it has been seen carrying either the old or the new date),
    #    so it is used only when nothing else in the block can anchor the rows.
    anchors = {i: st[1] for i, st in enumerate(stamps) if st and st[0] in ("marker", "time")}
    if not anchors:
        anchors = {i: st[1] for i, st in enumerate(stamps) if st}
    if not anchors:
        return value_cols, [], period
    first = min(anchors)
    times, cur = [], None
    for i in range(len(pg.buffer)):
        if i in anchors:
            cur = anchors[i]
        elif i < first:
            cur = anchors[first] - step * (first - i)       # rows before the first anchor
        else:
            cur = cur + step
        times.append(cur)

    rows = []
    for r, ts in zip(pg.buffer, times):
        vals = []
        for m in value_cols:
            x = r[m["col"]]
            x = float(x) if isinstance(x, (int, float)) else 0.0
            if m["unit"] in K_UNITS:
                x /= 1000.0
            vals.append(x)
        rows.append((ts, vals))
    return value_cols, rows, period


def connect_and_read(name, conn, host, port, start, end, timeout):
    link = Link(host, port, conn, timeout)
    link.open()
    try:
        link.associate()
        return read_profile(link, conn, start, end)
    finally:
        link.release()


def endpoint(m, args):
    host = args.host or (m["local_ip"] if args.local else m["public_ip"])
    return host, (args.port or m["port"])


def pull_day(name, conn, args, day):
    m = METERS[name]
    host, port = endpoint(m, args)
    # ask for a slightly wider window, then keep exactly 00:15 .. 24:00 of the requested day
    start = dt.datetime.combine(day, dt.time(0, 0)) - dt.timedelta(minutes=15)
    end = dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(days=1, minutes=15)
    cols, rows, period = connect_and_read(name, conn, host, port, start, end, args.timeout)
    lo = dt.datetime.combine(day, dt.time(0, 0))
    hi = lo + dt.timedelta(days=1)
    rows = [(t, v) for (t, v) in rows if lo < t <= hi]
    return cols, rows, period


# ------------------------------------------------------------------ PRN in / out
def prn_stamp(ts, day, first_two, last):
    if ts == dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(days=1):
        hhmm = "24:00"
    else:
        hhmm = ts.strftime("%H:%M")
    if first_two or last:
        return f"{day.month:>2}/{day.day:02d}/{day.year % 100:02d} {hhmm}"
    return hhmm


def apply_columns(conn, vals):
    spec = conn.get("columns")
    if not spec:
        return vals[:6]
    out = []
    for c in spec:
        i = c["index"]
        out.append(vals[i] * c["multiplier"] if i is not None and i < len(vals) else 0.0)
    return out


def write_prn(name, conn, day, rows, outdir, full_hours=False):
    """Full day -> NAME_YYYYMMDD.PRN with 96 intervals.
    Today (day still running) -> NAME_YYYYMMDD_hasta_HHMM.PRN with only the intervals the meter has
    closed so far; the last line carries the date, like the 24:00 line of a full day."""
    expected = [dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=15 * k) for k in range(1, 97)]
    by_ts = {t: v for (t, v) in rows}
    partial = day >= dt.date.today()
    if partial and full_hours:
        # keep only whole clock hours: drop trailing quarter-hours until the last kept one ends on :00
        by_ts = {t: v for t, v in by_ts.items()
                 if t <= max([x for x in by_ts if x.minute == 0] or [dt.datetime.combine(day, dt.time(0))])}
    if partial and not by_ts:
        return None, [], 0                     # no complete hour yet today
    if partial and by_ts:
        last = max(by_ts)
        expected = [t for t in expected if t <= last]
        path = os.path.join(outdir, f"{name}_{day:%Y%m%d}_hasta_{last:%H%M}.PRN")
    else:
        path = os.path.join(outdir, f"{name}_{day:%Y%m%d}.PRN")
    missing = [t for t in expected if t not in by_ts]
    n = len(expected)
    with open(path, "w", newline="", encoding="ascii") as f:
        f.write('"kwh"\r\n')
        f.write('"Time ",' + ",".join([f'"{name}"'] * 6) + "\r\n")
        for k, t in enumerate(expected):
            vals = apply_columns(conn, by_ts[t]) if t in by_ts else [0.0] * 6
            while len(vals) < 6:
                vals.append(0.0)
            stamp = prn_stamp(t, day, k < 2, k == n - 1)
            f.write(f'"{stamp}",')
            f.write(",".join(f"{x:.3f}" for x in vals[:6]) + "\r\n")
    return path, missing, n


def write_raw(name, day, cols, rows, outdir):
    path = os.path.join(outdir, f"{name}_{day:%Y%m%d}_raw.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time"] + [f"{c['obis']} ({unit_label(c['unit'])})" for c in cols])
        for t, v in rows:
            w.writerow([t.strftime("%Y-%m-%d %H:%M")] + [f"{x:.6f}" for x in v])
    return path


def unit_label(u):
    name = UNIT_NAMES.get(u, str(u))
    return ("k" + name) if u in K_UNITS else name


def read_prn(path):
    """Reads an Ace Pilot PRN -> (meter_name, day, [[6 values] x 96])"""
    with open(path, "r", encoding="latin-1") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    hdr = next(csv.reader([lines[1]]))
    name = hdr[1].strip()
    first = next(csv.reader([lines[2]]))[0].strip()
    datepart = first.split()[0]
    mo, d, y = [int(x) for x in datepart.split("/")]
    day = dt.date(2000 + y, mo, d)
    data = []
    for ln in lines[2:]:
        r = next(csv.reader([ln]))
        data.append([float(x) for x in r[1:7]])
    return name, day, data


# ------------------------------------------------------------------ calibration
def snap_multiplier(mult, ref, raw):
    """PRN values are rounded to 3 decimals, so the fitted ratio is slightly noisy.
    Try 'rounder' versions of it and keep the one that reproduces the most PRN values exactly."""
    ratios = sorted(a / b for a, b in zip(ref, raw) if b and a)
    cands = {mult}
    if ratios:
        cands.add(ratios[len(ratios) // 2])
    for base in list(cands):
        for sig in (2, 3, 4, 5, 6):
            cands.add(float(f"{base:.{sig}g}"))

    def score(m):
        return sum(1 for a, b in zip(ref, raw) if abs(round(b * m, 3) - a) < 0.0005)

    best = max(sorted(cands, key=lambda m: len(f"{m:g}")), key=score)
    # Ace Pilot rounds half-way values its own way; prefer a round ratio if it loses at most 1 value
    for sig in (1, 2, 3, 4):
        r = float(f"{best:.{sig}g}")
        if score(r) >= score(best) - 1:
            return r
    return best


def calibrate(ref_rows, raw_rows):
    """For each PRN column find the meter column + multiplier that reproduces it best."""
    n_raw = len(raw_rows[0]) if raw_rows else 0
    spec, report, used = [], [], set()
    for j in range(6):
        ref = [r[j] for r in ref_rows]
        sref = sum(abs(x) for x in ref)
        best = None
        for i in range(n_raw):
            raw = [r[i] for r in raw_rows]
            sraw = sum(abs(x) for x in raw)
            if sref == 0:
                err = 0.0 if sraw == 0 else 1.0
                mult = 1.0
            elif sraw == 0:
                continue
            else:
                mult = sum(ref) / sum(raw) if sum(raw) else sref / sraw
                err = sum(abs(a - mult * b) for a, b in zip(ref, raw)) / sref
            pen = err + (0.001 if i in used else 0)
            if best is None or pen < best[0]:
                best = (pen, i, mult, err)
        if best is None:
            spec.append({"index": None, "multiplier": 0.0})
            report.append((j + 1, None, 0.0, 1.0))
        else:
            _, i, mult, err = best
            mult = float(f"{snap_multiplier(mult, [r[j] for r in ref_rows], [r[i] for r in raw_rows]):.10g}")
            if sref:
                err = sum(abs(r[j] - mult * x[i]) for r, x in zip(ref_rows, raw_rows)) / sref
            used.add(i)
            spec.append({"index": i, "multiplier": mult})
            report.append((j + 1, i, mult, err))
    return spec, report


# ------------------------------------------------------------------ probe
def probe_candidates(serial):
    phys = [1, 16, 17]
    try:
        phys.append(GXDLMSClient.getServerAddressFromSerialNumber(int(serial), 0))
    except Exception:
        pass
    phys += [int(serial[-4:]), int(serial[-3:]), 0x3FFF]
    seen, out = set(), []
    for p in phys:
        for size in (0, 4):
            for logical in (1,):
                key = (p, size, logical)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"interface": "hdlc", "physical": p, "addr_size": size, "logical": logical})
    out.append({"interface": "wrapper", "logical": 1, "physical": 1, "addr_size": 0})
    return out


def probe(name, args, settings):
    m = METERS[name]
    host, port = endpoint(m, args)
    log(f"\n== PROBE {name} ({m['desc']}) at {host}:{port}  serial {m['serial']}")
    try:
        s = socket.create_connection((host, port), timeout=args.timeout)
        s.close()
        log("   TCP port is open")
    except OSError as e:
        log(f"   cannot open TCP connection: {e}")
        log("   -> this PC cannot reach the gateway. Try the other address (--local / without --local).")
        return False

    found, replied = None, []
    cands = probe_candidates(m["serial"])
    if args.physical is not None or args.interface or args.logical is not None or args.addr_size is not None:
        one = {"interface": args.interface or "hdlc", "physical": args.physical if args.physical is not None else 1,
               "logical": args.logical if args.logical is not None else 1, "addr_size": args.addr_size or 0}
        cands = [one]
    for cand in cands:
        conn = dict(DEFAULT_CONN)
        conn.update(cand)
        label = (f"{cand['interface']:7s} logical={cand['logical']} physical={cand['physical']}"
                 f" addr_size={cand['addr_size'] or 'auto'}")
        link = Link(host, port, conn, timeout=min(args.timeout, 4.0), retries=0)
        try:
            link.open(attempts=1)
            if cand["interface"] == "hdlc":
                link.snrm()
            else:
                link.associate()
            log(f"   ANSWER   {label}")
            found = cand
        except (TimeoutError, socket.timeout):
            log(f"   silent   {label}")
        except Exception as e:
            # bytes came back but did not parse as a normal reply: the meter is there, keep it as a lead
            log(f"   REPLIED  {label}   (unexpected reply: {type(e).__name__}: {e})")
            replied.append(cand)
        finally:
            link.release()
            time.sleep(0.6)
        if found:
            break
    if not found and replied:
        found = replied[0]
        log("   Using the first address that replied; trying to associate on it.")
    if not found:
        log("   No answer on any address. Check in Ace Pilot which 'device/server address' and")
        log("   'client' it uses for this meter, then pass them with --physical / --logical / --client.")
        return False

    # try associations, public first, then low-level password
    tries = [{"client": 16, "auth": "none"}, {"client": 1, "auth": "none"}]
    pw = args.password or settings.get(name, {}).get("password")
    for c in (1, 2, 3):
        tries.append({"client": c, "auth": "low", "password": pw or "ABCDEFGH"})
    if args.client is not None:
        tries = [{"client": args.client, "auth": args.auth or ("low" if pw else "none"), "password": pw}]
    for t in tries:
        conn = dict(DEFAULT_CONN)
        conn.update(found)
        conn.update(t)
        label = f"client={t['client']} auth={t['auth']}"
        link = Link(host, port, conn, timeout=args.timeout)
        try:
            link.open()
            link.associate()
            pg = GXDLMSProfileGeneric(conn["profile"])
            try:
                link.get(pg, 3)
            except Exception:
                conn["profile"] = "0.0.99.1.0.255"
                pg = GXDLMSProfileGeneric(conn["profile"])
                link.get(pg, 3)
            log(f"   ASSOCIATED {label}; load profile {conn['profile']} has {len(pg.captureObjects)} columns:")
            for i, (o, ca) in enumerate(pg.captureObjects):
                log(f"        col {i}: {o.logicalName}  class {int(o.objectType)}  attr {ca.attributeIndex}")
            entry = settings.get(name, {})
            for k in ("interface", "physical", "addr_size", "logical", "client", "auth", "profile"):
                entry[k] = conn[k]
            if t["auth"] == "low":
                entry["password"] = conn["password"]   # needed for the daily runs (stored only on this PC)
            settings[name] = entry
            save_settings(settings)
            return True
        except Exception as e:
            log(f"   refused  {label}   ({type(e).__name__}: {e})")
        finally:
            link.release()
            time.sleep(0.6)
    log("   The meter answers but would not open an association. Use the client/password Ace Pilot uses:")
    log("   python sl7000_prn.py --probe --client N --auth low --password XXXXXXXX")
    return False


# ------------------------------------------------------------------ main
def parse_day(txt):
    """Accepts 2026-09-22, 22/09/2026, 22-09-2026 or 22/09/26 (day first, as used in Argentina)."""
    t = txt.strip()
    if t.lower() in ("today", "hoy"):
        return dt.date.today()
    if t.lower() in ("yesterday", "ayer"):
        return dt.date.today() - dt.timedelta(days=1)
    try:
        return dt.date.fromisoformat(t)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"Could not understand the date '{txt}'. Use for example 22/09/2026 or 2026-09-22.")


def daterange(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Read-only SL7000 load profile -> PRN")
    ap.add_argument("--date", help="day to read, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--from", dest="date_from", help="first day of a range, YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="last day of a range, YYYY-MM-DD")
    ap.add_argument("--meter", choices=list(METERS), action="append", help="only this meter (repeatable)")
    ap.add_argument("--local", action="store_true", help="use the 192.168.0.x addresses (on the meter LAN)")
    ap.add_argument("--host", help="override the IP address (use together with --meter)")
    ap.add_argument("--port", type=int, help="override the TCP port (default 4000)")
    ap.add_argument("--outdir", default=HERE, help="where to write PRN files (default: script folder)")
    ap.add_argument("--probe", action="store_true", help="find interface/address/client each meter answers on")
    ap.add_argument("--calibrate", nargs="+", metavar="PRN", help="Ace Pilot PRN files to learn column mapping from")
    ap.add_argument("--full-hours", action="store_true",
                    help="for today: stop at the last complete clock hour (4 of 4 quarter-hours)")
    ap.add_argument("--raw", action="store_true", help="also write a _raw.csv with every profile column + OBIS code")
    ap.add_argument("--interface", choices=["hdlc", "wrapper"])
    ap.add_argument("--client", type=int)
    ap.add_argument("--auth", choices=["none", "low"])
    ap.add_argument("--password")
    ap.add_argument("--remember-password", action="store_true", help="store the password in meter_settings.json")
    ap.add_argument("--logical", type=int)
    ap.add_argument("--physical", type=int)
    ap.add_argument("--addr-size", dest="addr_size", type=int, choices=[0, 1, 2, 4])
    ap.add_argument("--profile", help="load profile OBIS (default 1.0.99.1.0.255)")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("-v", "--verbose", action="store_true", help="print every frame sent/received")
    args = ap.parse_args()
    VERBOSE = args.verbose
    if args.password and args.auth is None:
        args.auth = "low"

    settings = load_settings()
    names = args.meter or list(METERS)
    os.makedirs(args.outdir, exist_ok=True)

    if args.probe:
        ok = all([probe(n, args, settings) for n in names])
        sys.exit(0 if ok else 2)

    if args.calibrate:
        for path in args.calibrate:
            name, day, ref = read_prn(path)
            if name not in METERS:
                log(f"{path}: unknown meter name {name}")
                continue
            conn = conn_for(name, settings, args)
            log(f"\n== CALIBRATE {name} from {os.path.basename(path)} ({day})")
            try:
                cols, rows, _ = pull_day(name, conn, args, day)
            except Exception as e:
                log(f"   FAILED: {type(e).__name__}: {e}")
                continue
            if len(rows) < 90:
                log(f"   only {len(rows)} intervals came back for {day}; need the full day to calibrate")
                continue
            expected = [dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=15 * k) for k in range(1, 97)]
            by_ts = {t: v for t, v in rows}
            pairs = [(ref[k], by_ts[t]) for k, t in enumerate(expected) if t in by_ts]
            spec, rep = calibrate([p[0] for p in pairs], [p[1] for p in pairs])
            for col, idx, mult, err in rep:
                obis = cols[idx]["obis"] if idx is not None else "-"
                flag = "OK" if err < 0.02 else ("CHECK" if err < 0.05 else "BAD")
                exact = sum(1 for a, b in pairs if idx is not None and abs(round(b[idx] * mult, 3) - a[col - 1]) < 0.0005)
                log(f"   PRN col {col}: meter col {idx} {obis:18s} x {mult:<10g} {exact:3d}/{len(pairs)} values identical  {flag}")
            write_raw(name, day, cols, rows, args.outdir)
            entry = settings.get(name, {})
            for k in ("interface", "physical", "addr_size", "logical", "client", "auth", "profile"):
                entry[k] = conn[k]
            entry["columns"] = spec
            entry["column_obis"] = [cols[s["index"]]["obis"] if s["index"] is not None else None for s in spec]
            settings[name] = entry
            save_settings(settings)
        return

    if args.date_from:
        d0 = parse_day(args.date_from)
        d1 = parse_day(args.date_to) if args.date_to else dt.date.today() - dt.timedelta(days=1)
        days = list(daterange(d0, d1))
    elif args.date:
        days = [parse_day(args.date)]
    else:
        days = [dt.date.today() - dt.timedelta(days=1)]

    failures = 0
    for name in names:
        conn = conn_for(name, settings, args)
        if not conn.get("columns"):
            log(f"   NOTE {name}: not calibrated yet - PRN columns are the first 6 meter channels, unscaled by CT/VT."
                f" Run --calibrate once.")
        for day in days:
            log(f"== {name}  {day}")
            try:
                cols, rows, _ = pull_day(name, conn, args, day)
            except Exception as e:
                failures += 1
                log(f"   FAILED: {type(e).__name__}: {e}")
                continue
            if not rows:
                failures += 1
                log("   FAILED: the meter returned no intervals for this day (not written)")
                continue
            path, missing, n = write_prn(name, conn, day, rows, args.outdir, args.full_hours)
            if path is None:
                log("   no complete hour recorded yet today - nothing written")
                continue
            if n < 96:
                last = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(minutes=15 * n)
                log(f"   wrote {path}  (today so far: {n - len(missing)}/{n} intervals, latest {last:%H:%M})")
            else:
                log(f"   wrote {path}  ({96 - len(missing)}/96 intervals)")
            if missing:
                log(f"   WARNING: {len(missing)} intervals missing (written as 0.000): "
                    + ", ".join(t.strftime('%H:%M') for t in missing[:12]) + (" ..." if len(missing) > 12 else ""))
            if args.raw:
                log(f"   raw  {write_raw(name, day, cols, rows, args.outdir)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
