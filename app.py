#!/usr/bin/env python3
"""
vrm-ev-proxy v2.4.1
Polls Victron VRM Cloud and serves a vehicle HTTP API for EVCC.
Supports LFP and NMC battery tracking, SoC history, cycle counting.
No external dependencies – pure Python stdlib only.
"""

import html
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request

VERSION    = "2.4.1"
APP_NAME   = "vrm-ev-proxy"
CONFIG_FILE = '/config/settings.json'

# ── Battery type presets ───────────────────────────────────────────────────────
BATTERY_PRESETS = {
    'LFP': {'opt_min': 10, 'opt_max': 80, 'full_reminder_days': 28,
            'color': '#22d3ee', 'label': 'LFP', 'note': 'Full charge recommended every 4 weeks for BMS balancing'},
    'NMC': {'opt_min': 20, 'opt_max': 90, 'full_reminder_days': None,
            'color': '#a78bfa', 'label': 'NMC', 'note': 'Keep below 90% for longevity. Avoid prolonged time above 80%.'},
}

# ── VRM → Tesla state mapping ──────────────────────────────────────────────────
# /ChargingState enum of com.victronenergy.ev (victronenergy/venus wiki, dbus.md):
#   0 Not charging, 1 Low power mode, 3 Charging, 244 Sustain, 245 Wake up,
#   250 Blocked, 255 Unavailable, 256 Discharging, 257 Scheduled charging.
# 0/1 don't tell plugged from unplugged – see _charging_state().
# 2/4/5/6 are undocumented, kept from earlier versions for compatibility.
CHARGING_STATE_MAP = {
    0: 'Disconnected', 1: 'Disconnected',
    3: 'Charging',
    244: 'Complete',
    245: 'Stopped', 250: 'Stopped', 256: 'Stopped', 257: 'Stopped',
    255: 'Disconnected',
    2: 'Stopped', 4: 'Complete', 5: 'Stopped', 6: 'Stopped',
}

def _charging_state(ev):
    """Map VRM EV values to a Tesla charging_state."""
    code  = int(_num(ev.get('/ChargingState'), 0))
    state = CHARGING_STATE_MAP.get(code, 'Disconnected')
    # Mgmt/Connection names the EVCS the EV is plugged into (e.g. 'evcharger:40')
    plugged = bool(ev.get('/Mgmt/Connection') or ev.get('Mgmt/Connection'))
    if code in (0, 1) and plugged:
        state = 'Stopped'
    # AtSite 0 = EV is not at this installation → can't be connected here
    at_site = ev.get('/AtSite')
    if at_site is not None and _num(at_site, 1) == 0:
        state = 'Disconnected'
    return code, state


# ── EV charging station (com.victronenergy.evcharger) ──────────────────────────
# The EV service often lacks power data (e.g. Tesla via VRM: /Ac/Power empty).
# /Mgmt/Connection names the EVCS the EV is plugged into ('evcharger:40' or '40');
# that station reports live power, session energy and status.
EVCS_MARKER_PATHS = {'/Session/Energy', '/Session/Time', '/ChargingTime', '/StartStop', '/SetCurrent'}
# /Status: 0 Disconnected, 1 Connected, 2 Charging, 3 Charged, 4-7 waiting, 8-14 errors, 21-24 transitions
EVCS_STATUS_MAP = {0: 'Disconnected', 2: 'Charging', 3: 'Complete'}

def _connection(ev):
    return ev.get('/Mgmt/Connection') or ev.get('Mgmt/Connection') or ''

def _find_evcs(records, connection):
    """Return (device_name, {dbusPath: rawValue}) of the EVCS the EV is plugged into."""
    inst = int(_num(str(connection).split(':')[-1], -1))
    if inst < 0:
        return None, {}
    devices = {}
    for r in records:
        if (r.get('Device') != 'Electric Vehicle' and r.get('dbusPath')
                and int(_num(r.get('instance'), -2)) == inst):
            devices.setdefault(r.get('Device'), {})[r['dbusPath']] = r.get('rawValue')
    for name, values in devices.items():
        if EVCS_MARKER_PATHS & values.keys():
            return name, values
    return None, {}
CHARGING_STATE_UI = {
    'Disconnected': ('🔌', 'Disconnected', '#6b7280'),
    'Stopped':      ('⏸',  'Connected',    '#f59e0b'),
    'Charging':     ('⚡',  'Charging',     '#22c55e'),
    'Complete':     ('✅',  'Charged',      '#3b82f6'),
}

# ── Shared cache ───────────────────────────────────────────────────────────────
_cache = {
    'vehicles': {},   # VIN -> {'data': {...}, 'range_km': 0, 'power_w': 0, 'last_ev_contact': 0, 'odometer': 0, 'name': ''}
    'ts': 0.0,
    'error': None,
    'error_count': 0,
    'next_poll_at': 0.0,  # absolute time of the next VRM poll
    'sticky_vins': {},  # inst -> {'vin': str, 'ts': float}
    'sessions': {},     # inst -> {'energy_kwh': float, 'state': str}
}
STICKY_VIN_GRACE = 120  # seconds to hold last known real VIN while VRM catches up
_lock     = threading.Lock()
_cfg_lock = threading.RLock()   # serialises read-modify-write of CONFIG_FILE
_start    = time.time()

# Numeric settings: key -> (type, min, max)
NUMERIC_SETTINGS = {
    'POLL_INTERVAL':      (int,   10, 3600),
    'PORT':               (int,   1,  65535),
    'CAPACITY':           (float, 1,  200),
    'OPT_MIN':            (int,   0,  50),
    'OPT_MAX':            (int,   50, 100),
    'FULL_REMINDER_DAYS': (int,   7,  90),
}

# ── Web UI language (GUI only – logs, API and EVCC fields stay English) ────────
LANGUAGES = {'auto': 'Auto (Browser)', 'de': 'Deutsch', 'en': 'English'}
_req = threading.local()   # per-request language, set by the HTTP handler

# English source text → German. Placeholders use str.format syntax.
_DE = {
    # navigation / page frame
    'Status': 'Status', 'Settings': 'Einstellungen', 'API': 'API',
    'in {n}s': 'in {n}s',
    # charging states
    'Disconnected': 'Getrennt', 'Connected': 'Verbunden', 'Charging': 'Lädt', 'Charged': 'Geladen',
    # status page
    'Waiting for first VRM poll…': 'Warte auf erste VRM-Abfrage…',
    'Today': 'Heute', 'Yesterday': 'Gestern',
    '{d}d ago ({date})': 'vor {d} Tagen ({date})',
    'Not recorded yet': 'Noch nicht erfasst',
    '{h}h this week': '{h} h diese Woche',
    'SoC ({soc}%) is above the optimal maximum of {opt_max}% for {bat_type}.':
        'SoC ({soc} %) liegt über dem optimalen Maximum von {opt_max} % für {bat_type}.',
    'Reduce charging limit to protect the battery.': 'Ladelimit senken, um den Akku zu schonen.',
    'OK for occasional full charge, but limit to 80% for daily use.':
        'Gelegentlich voll laden ist ok, im Alltag aber auf 80 % begrenzen.',
    'LFP BMS balancing: last full charge was {d} days ago. Consider charging to 100% soon.':
        'LFP-BMS-Balancing: letzte Vollladung vor {d} Tagen. Bald auf 100 % laden.',
    'Charging Power': 'Ladeleistung',
    'State of Charge': 'Ladezustand',
    'Range': 'Reichweite', 'Odometer': 'Kilometerstand',
    'SoC History – 7 days': 'SoC-Verlauf – 7 Tage',
    'Not enough data yet (needs 2+ hours)': 'Noch zu wenig Daten (mind. 2 Stunden)',
    'Optimal zone': 'Optimaler Bereich', '{opt_max}% limit': '{opt_max} % Grenze',
    'Battery type': 'Akkutyp', 'Optimal range': 'Optimaler Bereich',
    'Time above {opt_max}%': 'Zeit über {opt_max} %',
    'Charge cycles': 'Ladezyklen', 'Last full charge': 'Letzte Vollladung',
    'Last EV contact': 'Letzter Fahrzeugkontakt',
    'Bridge': 'Bridge', 'Online': 'Online', 'Last update': 'Letzte Aktualisierung',
    'Data age': 'Datenalter', 'Next poll': 'Nächste Abfrage', 'Uptime': 'Laufzeit',
    'in {n}s (attempt {count})': 'in {n}s (Versuch {count})',
    'Full charge recommended every 4 weeks for BMS balancing':
        'Vollladung alle 4 Wochen für BMS-Balancing empfohlen',
    'Keep below 90% for longevity. Avoid prolonged time above 80%.':
        'Für lange Lebensdauer unter 90 % halten, längere Zeit über 80 % vermeiden.',
    # settings page
    'Settings saved – taking effect on next poll.': 'Einstellungen gespeichert – gelten ab der nächsten Abfrage.',
    'Welcome to vrm-ev-proxy! Enter your VRM credentials below to get started.':
        'Willkommen bei vrm-ev-proxy! Trage unten deine VRM-Zugangsdaten ein.',
    'VRM Connection': 'VRM-Verbindung', 'VRM API Token': 'VRM-API-Token',
    'VRM Site / Installation ID': 'VRM Site- / Installations-ID',
    'Leave empty to keep current': 'Leer lassen, um den aktuellen zu behalten',
    'Current:': 'Aktuell:', '(not set)': '(nicht gesetzt)',
    'e.g. 123456': 'z.B. 123456',
    'Found in VRM URL: …/installation/<b>XXXXX</b>/dashboard':
        'Steht in der VRM-URL: …/installation/<b>XXXXX</b>/dashboard',
    'Battery': 'Akku', 'Battery Chemistry': 'Zellchemie',
    'LFP – Lithium Iron Phosphate (optimal: 10–80%)': 'LFP – Lithium-Eisenphosphat (optimal: 10–80 %)',
    'NMC – Nickel Manganese Cobalt (optimal: 20–90%)': 'NMC – Nickel-Mangan-Kobalt (optimal: 20–90 %)',
    'Optimal SoC Range (%)': 'Optimaler SoC-Bereich (%)',
    'Min (🔴 below = warning)': 'Min (🔴 darunter = Warnung)',
    'Max (🟡 above = warning)': 'Max (🟡 darüber = Warnung)',
    'Battery Capacity (kWh)': 'Akkukapazität (kWh)', 'e.g. 60': 'z.B. 60',
    'Leave empty to use the capacity reported by VRM (<code>/BatteryCapacity</code>)':
        'Leer lassen, um die von VRM gemeldete Kapazität zu nutzen (<code>/BatteryCapacity</code>)',
    'LFP Full Charge Reminder (days)': 'LFP-Erinnerung Vollladung (Tage)',
    'Show reminder if no full charge in this many days (LFP only)':
        'Erinnerung, wenn so viele Tage nicht voll geladen wurde (nur LFP)',
    'Polling': 'Abfrage', 'Poll Interval (seconds)': 'Abfrageintervall (Sekunden)',
    'HTTP Port': 'HTTP-Port', 'Restart required after port change.': 'Nach Portänderung Neustart nötig.',
    'Interface': 'Oberfläche', 'Language': 'Sprache',
    'Save Settings': 'Einstellungen speichern',
    'API Endpoints': 'API-Endpunkte', 'view live ↗': 'live ansehen ↗',
    'Status page': 'Statusseite', 'Health check (JSON)': 'Health-Check (JSON)',
    # validation messages
    'VRM Site ID must be numeric.': 'Die VRM Site-ID muss eine Zahl sein.',
    'Unknown battery type: {v}': 'Unbekannter Akkutyp: {v}',
    'Unknown language: {v}': 'Unbekannte Sprache: {v}',
    '{key}: "{raw}" is not a valid number.': '{key}: „{raw}“ ist keine gültige Zahl.',
    '{key} must be between {lo} and {hi}.': '{key} muss zwischen {lo} und {hi} liegen.',
    'Optimal minimum must be lower than optimal maximum.': 'Das optimale Minimum muss unter dem Maximum liegen.',
    # API page
    'Endpoints': 'Endpunkte', 'Status page (UI)': 'Statusseite (UI)', 'Settings (UI)': 'Einstellungen (UI)',
    'Health check · JSON': 'Health-Check · JSON', 'open ↗': 'öffnen ↗', 'age: {age}s': 'Alter: {age}s',
    'Raw VRM values (debug)': 'VRM-Rohwerte (Debug)',
}
_WEEKDAYS = {'de': ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']}

def _lang():
    return getattr(_req, 'lang', 'en')

def _resolve_lang(accept_language=''):
    """Configured LANGUAGE, or the browser's preference when set to auto."""
    lang = _get('LANGUAGE', 'auto')
    if lang in ('de', 'en'):
        return lang
    for part in (accept_language or '').split(','):
        code = part.split(';')[0].strip().lower()[:2]
        if code in ('de', 'en'):
            return code
    return 'en'

def _dec(value, digits=1):
    """Format a decimal number with the current language's decimal separator."""
    out = f'{value:.{digits}f}'
    return out.replace('.', ',') if _lang() == 'de' else out

def _t(text, **kw):
    """Translate a GUI string into the current request language."""
    if _lang() == 'de':
        text = _DE.get(text, text)
    return text.format(**kw) if kw else text


# ── Config helpers ─────────────────────────────────────────────────────────────
def _load_cfg():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg):
    # Atomic write: a crash mid-write must never leave a truncated settings file
    # (which _load_cfg would silently turn into {} → all settings/history lost).
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)

def _get(key, default=''):
    return _load_cfg().get(key) or os.environ.get(key, default)

def _num(value, default=0.0):
    """Parse a VRM rawValue / setting to float; None, '' or garbage → default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _get_int(key, default):
    return int(_num(_get(key, str(default)), default))

def _get_float(key, default):
    return _num(_get(key, str(default)), default)

def _esc(value):
    return html.escape(str(value), quote=True)

def _bat():
    """Return battery preset dict for configured type."""
    return BATTERY_PRESETS.get(_get('BATTERY_TYPE', 'LFP'), BATTERY_PRESETS['LFP'])

def _interval():
    return max(10, _get_int('POLL_INTERVAL', 60))

def _stale_after():
    """Seconds without a successful VRM poll after which cached data is refused."""
    return max(1800, 10 * _interval())

def _is_configured():
    """Return True if both VRM_TOKEN and VRM_SITE_ID are set."""
    return bool(_get('VRM_TOKEN')) and bool(_get('VRM_SITE_ID'))


# ── VRM Poller ─────────────────────────────────────────────────────────────────
def poll_vrm():
    last_poll = 0.0
    while True:
        try:
            token   = _get('VRM_TOKEN')
            site_id = _get('VRM_SITE_ID')
            if not token or not site_id:
                raise ValueError('VRM_TOKEN and VRM_SITE_ID are not configured')

            url = (f'https://vrmapi.victronenergy.com/v2/installations'
                   f'/{site_id}/diagnostics?count=1000')
            req = Request(url, headers={'X-Authorization': f'Token {token}'})
            with urlopen(req, timeout=30) as resp:
                records = json.loads(resp.read())['records']

            ev_records = [r for r in records
                          if r.get('Device') == 'Electric Vehicle' and r.get('dbusPath')]

            if not ev_records:
                raise ValueError('No EV device found in VRM – is the Tesla configured in VRM?')

            # Group records by instance to support multiple EVs
            by_instance = {}
            for r in ev_records:
                inst = r.get('instance', 0)
                if inst not in by_instance:
                    by_instance[inst] = {}
                by_instance[inst][r['dbusPath']] = r['rawValue']

            now      = time.time()
            elapsed  = min(now - last_poll, 2 * _interval()) if last_poll else 0
            last_poll = now
            capacity = _get_float('CAPACITY', 0)
            bat      = _bat()
            opt_max  = _get_int('OPT_MAX', bat['opt_max'])
            vehicles = {}

            with _cfg_lock:
                cfg = _load_cfg()

                for inst, ev in by_instance.items():
                    # Extract VIN
                    raw_vin = str(ev.get('/VIN') or ev.get('/Serial') or '').strip()
                    fallback = f'EV_{inst}'
                    vin = raw_vin or fallback
                    brand_model = ' '.join(str(ev.get(k) or '') for k in ('/Brand', '/Model')).strip()
                    custom_name = str(ev.get('/CustomName') or '') or brand_model or vin

                    charging_raw, charging_state = _charging_state(ev)
                    evcs_name, evcs = _find_evcs(records, _connection(ev))
                    evcs_status = evcs.get('/Status')
                    if evcs_status is not None:
                        # Live EVCS status beats the (possibly stale) vehicle API state
                        charging_state = EVCS_STATUS_MAP.get(int(_num(evcs_status, 1)), 'Stopped')
                    # Truncate like the Tesla app does: 99.6 % must stay 99 %, otherwise
                    # EVCC sees 100 % (limit reached) and a full charge is logged too early.
                    soc           = max(0, min(100, int(_num(ev.get('/Soc'), 0))))
                    range_km      = _num(ev.get('/RangeToGo'), 0)
                    limit_soc     = int(_num(ev.get('/TargetSoc'), 100)) or 100
                    max_current   = int(_num(ev.get('/Ac/MaxChargeCurrent'), 0)
                                        or _num(evcs.get('/SetCurrent'), 0) or 16)
                    power_w       = (_num(ev.get('/Ac/Power'), 0) or _num(ev.get('/Dc/Power'), 0)
                                     or _num(evcs.get('/Ac/Power'), 0))
                    energy_total  = ev.get('/Ac/Energy/Forward')
                    energy_total  = _num(energy_total, None) if energy_total is not None else None
                    evcs_session  = evcs.get('/Session/Energy')
                    evcs_session  = _num(evcs_session, None) if evcs_session is not None else None
                    last_contact  = _num(ev.get('/LastUpdated/EvContact') or ev.get('LastUpdated/EvContact')
                                         or ev.get('/LastEvContact'), 0)
                    odometer      = _num(ev.get('/Odometer'), 0)
                    veh_capacity  = capacity or _num(ev.get('/BatteryCapacity'), 0)

                    # ── Sticky VIN: hold last known real VIN while VRM catches up ──
                    now_t = time.time()
                    with _lock:
                        sticky = _cache['sticky_vins'].get(inst, {})
                    if raw_vin and charging_state != 'Disconnected':
                        # Real VIN seen while actively connected – refresh sticky record
                        with _lock:
                            _cache['sticky_vins'][inst] = {'vin': raw_vin, 'ts': now_t}
                    elif charging_state != 'Disconnected' and sticky.get('vin') and \
                            (now_t - sticky.get('ts', 0)) < STICKY_VIN_GRACE:
                        # No VIN yet but vehicle connected – use last known real VIN
                        vin = sticky['vin']
                        print(f'[VRM] Sticky VIN for inst={inst}: using {vin} during identification grace period.', flush=True)
                    elif not raw_vin and charging_state == 'Disconnected' and cfg.get(f'vin_inst_{inst}'):
                        # Not plugged in and no VIN reported (e.g. after restart) – keep
                        # history/stats under the last real VIN instead of EV_<inst>
                        vin = cfg[f'vin_inst_{inst}']
                    if raw_vin:
                        cfg[f'vin_inst_{inst}'] = raw_vin

                    # ── Session energy ─────────────────────────────────────────────
                    # EVCC queries charge_energy_added; a missing field makes its jq
                    # return null, which EVCC fails to parse on every cycle.
                    # Prefer the /Ac/Energy/Forward meter, else integrate power.
                    with _lock:
                        sess = _cache['sessions'].get(inst) or {'energy_kwh': 0.0, 'state': 'Disconnected'}
                        if sess['state'] == 'Disconnected' and charging_state != 'Disconnected':
                            sess = {'energy_kwh': 0.0, 'state': charging_state}   # new plug-in → new session
                        if evcs_session is not None:
                            sess['energy_kwh'] = evcs_session
                        elif energy_total is not None:
                            if sess.get('meter_start') is None or energy_total < sess['meter_start']:
                                sess['meter_start'] = energy_total - sess['energy_kwh']
                            sess['energy_kwh'] = energy_total - sess['meter_start']
                        elif charging_state == 'Charging' and power_w > 0 and elapsed > 0:
                            sess['energy_kwh'] += power_w * elapsed / 3_600_000
                        sess['state'] = charging_state
                        _cache['sessions'][inst] = sess
                    energy_added = sess['energy_kwh']

                    # ── Estimated minutes to reach charge limit ────────────────────
                    minutes_to_full = 0
                    if charging_state == 'Charging' and power_w > 100 and veh_capacity > 0 and soc < limit_soc:
                        kwh_left = (limit_soc - soc) / 100.0 * veh_capacity
                        minutes_to_full = int(kwh_left / (power_w / 1000) * 60)

                    data = {
                        'battery_level':          soc,
                        'usable_battery_level':   soc,
                        'battery_range':          round(range_km / 1.60934, 2),
                        'charge_limit_soc':       limit_soc,
                        'charging_state':         charging_state,
                        'charge_amps':            max_current,
                        'charge_current_request': max_current,
                        'charger_power':          round(power_w / 1000),
                        'charge_energy_added':    round(energy_added, 2),
                        'minutes_to_full_charge': minutes_to_full,
                        'time_to_full_charge':    round(minutes_to_full / 60, 2),
                        'timestamp':              int(now * 1000),
                    }

                    # ── Track last full charge (per VIN) ──────────────────────────
                    lfc_key = f'last_full_charge_{vin}'
                    if soc >= 100 and charging_state in ('Charging', 'Complete', 'Stopped'):
                        if now - cfg.get(lfc_key, 0) > 3600:
                            print(f'[VRM] Full charge detected for {vin} – timestamp saved.', flush=True)
                        cfg[lfc_key] = now

                    # ── SoC history (hourly snapshots, per VIN) ────────────────────
                    hist_key = f'soc_history_{vin}'
                    history = cfg.get(hist_key, [])
                    if not history or now - history[-1][0] >= 3600:
                        history.append([int(now), soc])
                        cfg[hist_key] = history[-168:]

                    # ── Charge cycle counter (per VIN) ─────────────────────────────
                    cycles_key = f'charge_cycles_{vin}'
                    last_soc_key = f'last_soc_for_cycles_{vin}'
                    last_soc = cfg.get(last_soc_key, soc)
                    # Only count SoC gains while plugged in – ignores SoC jitter
                    # (55→54→55 %) of a parked car.
                    if veh_capacity > 0 and soc > last_soc and charging_state != 'Disconnected':
                        cfg[cycles_key] = cfg.get(cycles_key, 0.0) + (soc - last_soc) / 100.0
                    cfg[last_soc_key] = soc

                    # ── Time above optimal (per VIN) ────────────────────────────────
                    week_start_key = f'time_above_week_start_{vin}'
                    time_above_key = f'time_above_optimal_{vin}'
                    week_start = cfg.get(week_start_key, now)
                    if now - week_start >= 7 * 86400:
                        cfg[time_above_key] = 0
                        cfg[week_start_key] = now
                    elif week_start_key not in cfg:
                        cfg[week_start_key] = now

                    if soc > opt_max:
                        # Real elapsed time (capped), not the nominal interval – so
                        # error backoffs and settings changes don't skew the counter.
                        cfg[time_above_key] = cfg.get(time_above_key, 0) + elapsed

                    vehicles[vin] = {
                        'data':            data,
                        'capacity':        veh_capacity,
                        'range_km':        range_km,
                        'power_w':         power_w,
                        'last_ev_contact': last_contact,
                        'odometer':        odometer,
                        'name':            custom_name,
                        'raw':             {'ev': ev, 'evcs_device': evcs_name, 'evcs': evcs},
                    }

                    print(f'[VRM] OK – VIN={vin}  SoC={soc}%  Range={range_km}km  '
                          f'State={charging_state}  Power={power_w}W  raw=ChargingState:{charging_raw}'
                          f' Connection:{_connection(ev) or "-"} AtSite:{ev.get("/AtSite", "-")}'
                          f' EVCS:{evcs_name or "-"}/Status:{evcs_status if evcs_status is not None else "-"}',
                          flush=True)

                _save_cfg(cfg)

            with _lock:
                _cache['vehicles']    = vehicles
                _cache['ts']          = time.time()
                _cache['error']       = None
                _cache['error_count'] = 0
                _cache['next_poll_at'] = time.time() + _interval()

        except Exception as exc:
            # Never let the poller thread die – it is the only data source.
            with _lock:
                _cache['error'] = str(exc)
                _cache['error_count'] += 1
                count = _cache['error_count']
                wait = min(_interval() * (2 ** min(count, 6)), max(600, _interval()))
                _cache['next_poll_at'] = time.time() + wait
            print(f'[VRM] Error (attempt {count}): {exc}', flush=True)
            time.sleep(wait)
            continue

        time.sleep(_interval())


# ── SoC color ──────────────────────────────────────────────────────────────────
def _soc_color(soc):
    bat     = _bat()
    opt_min = _get_int('OPT_MIN', bat['opt_min'])
    opt_max = _get_int('OPT_MAX', bat['opt_max'])
    if soc < opt_min:    return '#ef4444'   # below min → red
    if soc <= opt_max:   return '#22c55e'   # in range  → green
    return '#f59e0b'                         # above max → amber warning


# ── SVG History Chart ──────────────────────────────────────────────────────────
def _build_chart(history, opt_min, opt_max):
    if len(history) < 2:
        return '<div style="color:#475569;text-align:center;padding:1rem;font-size:.8rem">' + _t('Not enough data yet (needs 2+ hours)') + '</div>'

    W, H   = 440, 120
    PAD_L  = 28
    PAD_B  = 20
    PAD_T  = 8
    PAD_R  = 8
    pw     = W - PAD_L - PAD_R
    ph     = H - PAD_B - PAD_T

    ts_min = history[0][0]
    ts_max = history[-1][0]
    ts_rng = max(ts_max - ts_min, 1)

    def tx(ts):
        return PAD_L + (ts - ts_min) / ts_rng * pw

    def ty(s):
        return PAD_T + (1 - s / 100) * ph

    # Optimal zone
    y_top = ty(opt_max)
    y_bot = ty(opt_min)
    zone  = (f'<rect x="{PAD_L}" y="{y_top:.1f}" '
             f'width="{pw}" height="{y_bot - y_top:.1f}" '
             f'fill="#22c55e" opacity=".08"/>')

    # Grid lines at 20/40/60/80/100
    grid = ''
    for pct in (20, 40, 60, 80, 100):
        y = ty(pct)
        grid += (f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                 f'stroke="#1e293b" stroke-width="1"/>'
                 f'<text x="{PAD_L - 3}" y="{y + 4:.1f}" text-anchor="end" '
                 f'fill="#475569" font-size="9">{pct}</text>')

    # Optimal boundary lines
    bound = ''
    for pct, col in ((opt_max, '#22c55e'), (opt_min, '#ef4444')):
        y = ty(pct)
        bound += (f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                  f'stroke="{col}" stroke-width="1" stroke-dasharray="3,3" opacity=".6"/>')

    # SoC line
    pts = ' '.join(f'{tx(t):.1f},{ty(s):.1f}' for t, s in history)
    line = (f'<polyline points="{pts}" fill="none" stroke="#38bdf8" '
            f'stroke-width="1.5" stroke-linejoin="round"/>')

    # Current dot
    last_t, last_s = history[-1]
    dot = (f'<circle cx="{tx(last_t):.1f}" cy="{ty(last_s):.1f}" '
           f'r="3" fill="#38bdf8"/>')

    # X-axis day labels
    xlabels = ''
    seen_days = set()
    for ts, _ in history:
        lt  = time.localtime(ts)
        day = _WEEKDAYS[_lang()][lt.tm_wday] if _lang() in _WEEKDAYS else time.strftime('%a', lt)
        x   = tx(ts)
        if day not in seen_days and x > PAD_L + 20:
            seen_days.add(day)
            xlabels += (f'<text x="{x:.1f}" y="{H - 4}" text-anchor="middle" '
                        f'fill="#475569" font-size="9">{day}</text>')

    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;display:block">'
            f'{zone}{grid}{bound}{line}{dot}{xlabels}</svg>')


# ── CSS ────────────────────────────────────────────────────────────────────────
_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #0f172a; color: #e2e8f0; min-height: 100vh;
  display: flex; flex-direction: column; align-items: center; padding: 1.5rem 1rem;
}
h1 { font-size: 1.2rem; font-weight: 700; color: #f1f5f9; }
.subtitle { color: #475569; font-size: .78rem; margin-bottom: 1.2rem; }
nav { display: flex; gap: .4rem; margin-bottom: 1.5rem; }
nav a {
  padding: .35rem .9rem; border-radius: 8px; font-size: .82rem;
  text-decoration: none; color: #94a3b8; border: 1px solid #334155;
  transition: all .15s;
}
nav a.active, nav a:hover { background: #1e40af; color: #fff; border-color: #1e40af; }
.container { width: 100%; max-width: 480px; }
.card {
  background: #1e293b; border-radius: 12px; padding: 1.1rem 1.4rem;
  margin-bottom: .85rem; border: 1px solid #334155;
}
.label { font-size: .7rem; color: #64748b; text-transform: uppercase;
         letter-spacing: .06em; margin-bottom: .35rem; }
.value { font-size: 2rem; font-weight: 700; color: #f1f5f9; }
.value.big { font-size: 3.2rem; }
.unit { font-size: .95rem; color: #94a3b8; font-weight: 400; }
.bar-wrap {
  position: relative; background: #334155; border-radius: 999px;
  height: 10px; margin: .65rem 0 .3rem; overflow: visible;
}
.bar-zone {
  position: absolute; top: 0; height: 100%; border-radius: 999px; opacity: .15;
}
.bar-fill { height: 100%; border-radius: 999px; transition: width .5s ease; }
.bar-marker {
  position: absolute; top: -4px; width: 2px; height: 18px;
  border-radius: 2px; transform: translateX(-50%);
}
.bar-labels {
  position: relative; display: flex; justify-content: space-between;
  font-size: .68rem; color: #475569; margin-top: .25rem;
}
.pin-label {
  position: absolute; transform: translateX(-50%); font-size: .68rem; white-space: nowrap;
}
.grid3 { display: flex; gap: .6rem; margin-bottom: .85rem; }
.card.small { flex: 1; text-align: center; padding: .9rem .6rem; }
.power-row { display: flex; align-items: center; gap: .5rem;
             font-size: 1.35rem; font-weight: 700; margin-top: .2rem; }
.meta-row {
  display: flex; justify-content: space-between; padding: .28rem 0;
  border-bottom: 1px solid #0f172a; font-size: .8rem; align-items: center;
}
.meta-row:last-child { border-bottom: none; }
.meta-val { color: #94a3b8; text-align: right; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
.dot.green { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
.dot.red   { background: #ef4444; }
.badge {
  display: inline-block; padding: .15rem .55rem; border-radius: 999px;
  font-size: .72rem; font-weight: 600; letter-spacing: .04em;
}
.warning-box {
  background: #2d1f0a; border: 1px solid #92400e; border-radius: 10px;
  color: #fbbf24; padding: .65rem 1rem; font-size: .8rem; margin-bottom: .85rem;
}
.info-box {
  background: #0d2137; border: 1px solid #1d4ed8; border-radius: 10px;
  color: #93c5fd; padding: .65rem 1rem; font-size: .8rem; margin-bottom: .85rem;
}
.error-box {
  background: #2d1a1a; border: 1px solid #7f1d1d; border-radius: 10px;
  color: #fca5a5; padding: .7rem 1rem; font-size: .8rem;
  margin-bottom: .85rem; word-break: break-word;
}
.success-box {
  background: #14291a; border: 1px solid #166534; border-radius: 10px;
  color: #86efac; padding: .7rem 1rem; font-size: .8rem; margin-bottom: .85rem;
}
label { display: block; font-size: .8rem; color: #94a3b8;
        margin-bottom: .3rem; margin-top: .9rem; }
label:first-of-type { margin-top: 0; }
.field-row { display: flex; gap: .5rem; }
.field-row > * { flex: 1; }
input[type=text], input[type=number], input[type=password], select {
  width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 8px;
  color: #f1f5f9; padding: .5rem .8rem; font-size: .88rem; outline: none;
  transition: border-color .15s; appearance: none;
}
select { cursor: pointer; }
input:focus, select:focus { border-color: #3b82f6; }
.hint { font-size: .72rem; color: #475569; margin-top: .2rem; }
.section-title {
  font-size: .72rem; font-weight: 600; color: #475569; text-transform: uppercase;
  letter-spacing: .08em; margin: 1.2rem 0 .5rem;
  border-top: 1px solid #0f172a; padding-top: .8rem;
}
button[type=submit] {
  margin-top: 1.1rem; width: 100%; background: #1e40af; color: #fff;
  border: none; border-radius: 8px; padding: .6rem; font-size: .92rem;
  cursor: pointer; transition: background .15s;
}
button[type=submit]:hover { background: #2563eb; }
.footer { margin-top: 1.2rem; font-size: .7rem; color: #334155; text-align: center; }
#countdown { font-variant-numeric: tabular-nums; }
code { background: #0f172a; padding: .1rem .35rem; border-radius: 4px;
       font-size: .8rem; color: #94a3b8; }
.step-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%; background: #1e40af;
  color: #fff; font-size: .72rem; font-weight: 700; margin-right: .4rem;
}
"""

# ── Page wrapper ───────────────────────────────────────────────────────────────
def _page(title, nav_active, body, countdown=0):
    return f"""<!DOCTYPE html>
<html lang="{_lang()}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_t(title)} – {APP_NAME}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <h1>⚡ {APP_NAME}</h1>
  <p class="subtitle">v{VERSION} &nbsp;·&nbsp; Victron VRM → EVCC</p>
  <nav>
    <a href="/"         class="{'active' if nav_active=='status'   else ''}">📊 {_t('Status')}</a>
    <a href="/settings" class="{'active' if nav_active=='settings' else ''}">⚙️ {_t('Settings')}</a>
    <a href="/api"      class="{'active' if nav_active=='api'      else ''}">🔗 API</a>
  </nav>
  {body}
  <div class="footer">{APP_NAME} v{VERSION}</div>
</div>
<script>
(function(){{
  var secs={countdown}, el=document.getElementById('countdown');
  if(!el||secs<=0) return;
  (function tick(){{
    if(secs<=0){{ location.reload(); return; }}
    el.textContent={json.dumps(_t('in {n}s'))}.replace('{{n}}', secs); secs--;
    setTimeout(tick,1000);
  }})();
}})();
</script>
</body>
</html>"""


# ── Status page ────────────────────────────────────────────────────────────────
def build_status_page():
    with _lock:
        vehicles     = dict(_cache['vehicles'])
        ts           = _cache['ts']
        error        = _cache['error']
        error_count  = _cache['error_count']
        next_poll_at = _cache['next_poll_at']

    cfg      = _load_cfg()
    bat      = _bat()
    bat_type = _get('BATTERY_TYPE', 'LFP')
    if bat_type not in BATTERY_PRESETS:
        bat_type = 'LFP'
    opt_min  = _get_int('OPT_MIN', bat['opt_min'])
    opt_max  = _get_int('OPT_MAX', bat['opt_max'])
    age       = int(time.time() - ts) if ts else 0
    next_poll = max(1, int(next_poll_at - time.time())) if next_poll_at else _interval()
    uptime    = int(time.time() - _start)
    up_str    = f'{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s'
    ts_str    = time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(ts)) if ts else '–'

    error_box = f'<div class="error-box">⚠️ {_esc(error)}</div>' if error else ''

    # Build next poll / retry display
    if error and error_count > 0:
        next_poll_display = f'<span class="meta-val" id="countdown">{_t("in {n}s (attempt {count})", n=next_poll, count=error_count)}</span>'
    else:
        next_poll_display = f'<span class="meta-val" id="countdown">{_t("in {n}s", n=next_poll)}</span>'
    countdown_val = next_poll + 2   # reload shortly after the poll has finished

    main_cards = ''

    if not vehicles:
        main_cards = '<div class="card" style="text-align:center;padding:2rem;color:#f59e0b">⏳ ' + _t('Waiting for first VRM poll…') + '</div>'
    else:
        for vin, veh in vehicles.items():
            data         = veh['data']
            range_km     = veh['range_km']
            power_w      = veh['power_w']
            last_contact = veh['last_ev_contact']
            odometer     = veh['odometer']
            veh_name     = veh['name']

            lc_str = (time.strftime('%d.%m.%Y %H:%M', time.localtime(last_contact))
                      if last_contact else '–')

            # Last full charge (per VIN)
            lfc_key = f'last_full_charge_{vin}'
            last_full = cfg.get(lfc_key, 0)
            if last_full:
                days_ago = (time.time() - last_full) / 86400
                if days_ago < 1:     lf_str = _t('Today')
                elif days_ago < 2:   lf_str = _t('Yesterday')
                else:                lf_str = _t('{d}d ago ({date})', d=int(days_ago),
                                                 date=time.strftime("%d.%m.%Y", time.localtime(last_full)))
            else:
                lf_str = _t('Not recorded yet')

            # Time above optimal this week (per VIN)
            time_above = cfg.get(f'time_above_optimal_{vin}', 0)
            ta_hours   = time_above / 3600
            ta_str     = _t('{h}h this week', h=_dec(ta_hours))

            # Charge cycles (per VIN)
            cycles     = cfg.get(f'charge_cycles_{vin}', 0.0)
            cycles_str = _dec(cycles) if veh.get('capacity') else '–'

            soc       = data['battery_level']
            limit_soc = data['charge_limit_soc']
            state     = data['charging_state']
            icon, state_label, state_color = CHARGING_STATE_UI.get(state, ('❓', state, '#6b7280'))
            state_label = _t(state_label)
            bar_color = _soc_color(soc)

            warnings = ''

            # Warning: above optimal range
            if soc > opt_max:
                advice = ('Reduce charging limit to protect the battery.' if bat_type == 'NMC'
                          else 'OK for occasional full charge, but limit to 80% for daily use.')
                warnings += (f'<div class="warning-box">⚠️ '
                             f'{_t("SoC ({soc}%) is above the optimal maximum of {opt_max}% for {bat_type}.", soc=soc, opt_max=opt_max, bat_type=bat_type)} '
                             f'{_t(advice)}</div>')

            # LFP full charge reminder
            if bat_type == 'LFP' and bat['full_reminder_days']:
                remind_after = _get_int('FULL_REMINDER_DAYS', bat['full_reminder_days'])
                if last_full and (time.time() - last_full) / 86400 > remind_after:
                    days_overdue = int((time.time() - last_full) / 86400)
                    warnings += (f'<div class="info-box">ℹ️ '
                                 f'{_t("LFP BMS balancing: last full charge was {d} days ago. Consider charging to 100% soon.", d=days_overdue)}</div>')

            # Optimal zone band in bar
            zone_html = (f'<div class="bar-zone" style="left:{opt_min}%;'
                         f'width:{opt_max - opt_min}%;background:#22c55e"></div>')

            # Limit marker (orange) – only if meaningfully below 100% and not overlapping
            limit_html = lim_label = ''
            if limit_soc < 98 and abs(soc - limit_soc) >= 3:
                limit_html = (f'<div class="bar-marker" style="left:{limit_soc}%;'
                              f'background:#f59e0b"></div>')
                lim_label  = (f'<span class="pin-label" style="left:{limit_soc}%;color:#f59e0b">'
                              f'▲ {limit_soc}%</span>')

            # Optimal max marker (battery type color)
            opt_html  = (f'<div class="bar-marker" style="left:{opt_max}%;'
                         f'background:{bat["color"]};width:2px;opacity:.8"></div>')
            opt_label = (f'<span class="pin-label" style="left:{opt_max}%;color:{bat["color"]}">'
                         f'╷ {opt_max}%</span>')

            # Power row
            power_html = ''
            if state == 'Charging' and power_w > 100:
                power_html = f'''
            <div class="card">
              <div class="label">{_t('Charging Power')}</div>
              <div class="power-row" style="color:#22c55e">
                ⚡ {_dec(power_w / 1000)} <span class="unit">kW</span>
              </div>
            </div>'''

            # History chart (per VIN)
            history = cfg.get(f'soc_history_{vin}', [])
            chart   = _build_chart(history, opt_min, opt_max)

            bat_badge = (f'<span class="badge" style="background:{bat["color"]}22;'
                         f'color:{bat["color"]};border:1px solid {bat["color"]}44">'
                         f'{bat_type}</span>')

            main_cards += warnings + f"""
        <div class="card" style="border-color:#334155">
          <div style="font-size:.85rem;font-weight:600;color:#94a3b8;margin-bottom:.6rem">
            🚗 {_esc(veh_name)}
            <span style="font-size:.7rem;color:#475569;margin-left:.5rem">VIN: {_esc(vin)}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem">
            <div class="label" style="margin:0">{_t('State of Charge')}</div>
            {bat_badge}
          </div>
          <div class="value big" style="color:{bar_color}">{soc}<span class="unit"> %</span></div>
          <div class="bar-wrap">
            {zone_html}
            <div class="bar-fill" style="width:{soc}%;background:{bar_color};position:relative;z-index:1"></div>
            {limit_html}
            {opt_html}
          </div>
          <div class="bar-labels">
            <span>0%</span>
            {lim_label}
            {opt_label}
            <span>100%</span>
          </div>
        </div>
        {power_html}
        <div class="grid3">
          <div class="card small">
            <div class="label">{_t('Range')}</div>
            <div class="value">{int(range_km)}<span class="unit"> km</span></div>
          </div>
          <div class="card small">
            <div class="label">{_t('Status')}</div>
            <div class="value" style="font-size:.9em;color:{state_color}">{icon} {state_label}</div>
          </div>
          <div class="card small">
            <div class="label">{_t('Odometer')}</div>
            <div class="value" style="font-size:1.2rem">{f"{int(odometer):,}".replace(",", "." if _lang() == "de" else ",")}<span class="unit"> km</span></div>
          </div>
        </div>

        <div class="card">
          <div class="label" style="margin-bottom:.6rem">{_t('SoC History – 7 days')}</div>
          {chart}
          <div style="display:flex;gap:1rem;margin-top:.5rem;font-size:.7rem;color:#475569">
            <span style="color:#22c55e">━</span> {_t('Optimal zone')}
            <span style="color:{bat['color']}">╷</span> {_t('{opt_max}% limit', opt_max=opt_max)}
            <span style="color:#38bdf8">━</span> SoC
          </div>
        </div>

        <div class="card">
          <div class="meta-row">
            <span>{_t('Battery type')}</span>
            <span class="meta-val">{bat_type} · {_t(bat['note'])}</span>
          </div>
          <div class="meta-row">
            <span>{_t('Optimal range')}</span>
            <span class="meta-val">{opt_min}% – {opt_max}%</span>
          </div>
          <div class="meta-row">
            <span>{_t('Time above {opt_max}%', opt_max=opt_max)}</span>
            <span class="meta-val">{ta_str}</span>
          </div>
          <div class="meta-row">
            <span>{_t('Charge cycles')}</span>
            <span class="meta-val">{cycles_str}</span>
          </div>
          <div class="meta-row">
            <span>{_t('Last full charge')}</span>
            <span class="meta-val">{lf_str}</span>
          </div>
          <div class="meta-row">
            <span>{_t('Last EV contact')}</span>
            <span class="meta-val">{lc_str}</span>
          </div>
        </div>
"""

    # Global meta card
    main_cards += f"""
        <div class="card">
          <div class="meta-row">
            <span>{_t('Bridge')}</span>
            <span class="meta-val"><span class="dot green"></span>{_t('Online')}</span>
          </div>
          <div class="meta-row"><span>VRM Site ID</span><span class="meta-val">{_esc(_get('VRM_SITE_ID','–'))}</span></div>
          <div class="meta-row"><span>{_t('Last update')}</span><span class="meta-val">{ts_str}</span></div>
          <div class="meta-row"><span>{_t('Data age')}</span><span class="meta-val">{age}s</span></div>
          <div class="meta-row"><span>{_t('Next poll')}</span>{next_poll_display}</div>
          <div class="meta-row"><span>{_t('Uptime')}</span><span class="meta-val">{up_str}</span></div>
        </div>"""

    body = error_box + main_cards
    return _page('Status', 'status', body, countdown=countdown_val)


# ── Settings page ──────────────────────────────────────────────────────────────
def build_settings_page(saved=False, error_msg=''):
    cfg      = _load_cfg()
    token    = cfg.get('VRM_TOKEN') or os.environ.get('VRM_TOKEN', '')
    site_id  = cfg.get('VRM_SITE_ID') or os.environ.get('VRM_SITE_ID', '')
    interval = cfg.get('POLL_INTERVAL') or os.environ.get('POLL_INTERVAL', '60')
    port     = cfg.get('PORT') or os.environ.get('PORT', '8080')
    bat_type = cfg.get('BATTERY_TYPE') or os.environ.get('BATTERY_TYPE', 'LFP')
    capacity = cfg.get('CAPACITY') or os.environ.get('CAPACITY', '')
    bat      = BATTERY_PRESETS.get(bat_type, BATTERY_PRESETS['LFP'])
    opt_min  = cfg.get('OPT_MIN') or os.environ.get('OPT_MIN', str(bat['opt_min']))
    opt_max  = cfg.get('OPT_MAX') or os.environ.get('OPT_MAX', str(bat['opt_max']))
    reminder = cfg.get('FULL_REMINDER_DAYS') or os.environ.get('FULL_REMINDER_DAYS', str(bat.get('full_reminder_days') or ''))
    masked   = ('*' * 8 + token[-6:]) if len(token) > 6 else _t('(not set)')
    language = cfg.get('LANGUAGE') or os.environ.get('LANGUAGE', 'auto')
    lang_opts = ''.join(f'<option value="{k}" {"selected" if k == language else ""}>{v}</option>'
                        for k, v in LANGUAGES.items())
    # The full token is never sent to the browser – the UI has no login.
    site_id, interval, port, capacity, opt_min, opt_max, reminder, masked = map(
        _esc, (site_id, interval, port, capacity, opt_min, opt_max, reminder, masked))

    lfp_sel = 'selected' if bat_type == 'LFP' else ''
    nmc_sel = 'selected' if bat_type == 'NMC' else ''

    notice = ''
    if saved:
        notice = f'<div class="success-box">✅ {_t("Settings saved – taking effect on next poll.")}</div>'
    elif error_msg:
        notice = f'<div class="error-box">⚠️ {_esc(error_msg)}</div>'

    # First-run wizard welcome banner
    welcome_banner = ''
    if not _is_configured():
        welcome_banner = f'''<div class="info-box" style="margin-bottom:1.2rem">
    👋 {_t('Welcome to vrm-ev-proxy! Enter your VRM credentials below to get started.')}
  </div>'''

    # Step badges for VRM token and site ID labels (shown when not configured)
    if not _is_configured():
        token_label = f'<label><span class="step-badge">1</span>{_t("VRM API Token")}</label>'
        siteid_label = f'<label><span class="step-badge">2</span>{_t("VRM Site / Installation ID")}</label>'
    else:
        token_label = f'<label>{_t("VRM API Token")}</label>'
        siteid_label = f'<label>{_t("VRM Site / Installation ID")}</label>'

    body = f"""
    {welcome_banner}
    {notice}
    <div class="card">
      <form method="POST" action="/settings">

        <div class="section-title" style="margin-top:0;border-top:none;padding-top:0">{_t('VRM Connection')}</div>
        {token_label}
        <input type="password" name="VRM_TOKEN" id="tok"
               placeholder="{_t('Leave empty to keep current')}" autocomplete="off">
        <div class="hint">
          {_t('Current:')} {masked}
        </div>

        {siteid_label}
        <input type="text" name="VRM_SITE_ID" value="{site_id}" placeholder="{_t('e.g. 123456')}">
        <div class="hint">{_t('Found in VRM URL: …/installation/<b>XXXXX</b>/dashboard')}</div>

        <div class="section-title">{_t('Battery')}</div>

        <label>{_t('Battery Chemistry')}</label>
        <select name="BATTERY_TYPE" onchange="updatePreset(this.value)">
          <option value="LFP" {lfp_sel}>{_t('LFP – Lithium Iron Phosphate (optimal: 10–80%)')}</option>
          <option value="NMC" {nmc_sel}>{_t('NMC – Nickel Manganese Cobalt (optimal: 20–90%)')}</option>
        </select>

        <label>{_t('Optimal SoC Range (%)')}</label>
        <div class="field-row">
          <div>
            <input type="number" name="OPT_MIN" id="opt_min" value="{opt_min}" min="0" max="50">
            <div class="hint">{_t('Min (🔴 below = warning)')}</div>
          </div>
          <div>
            <input type="number" name="OPT_MAX" id="opt_max" value="{opt_max}" min="50" max="100">
            <div class="hint">{_t('Max (🟡 above = warning)')}</div>
          </div>
        </div>

        <label>{_t('Battery Capacity (kWh)')}</label>
        <input type="number" name="CAPACITY" value="{capacity}" placeholder="{_t('e.g. 60')}" min="1" max="200" step="0.1">
        <div class="hint">{_t('Leave empty to use the capacity reported by VRM (<code>/BatteryCapacity</code>)')}</div>

        <label>{_t('LFP Full Charge Reminder (days)')}</label>
        <input type="number" name="FULL_REMINDER_DAYS" value="{reminder}"
               placeholder="28" min="7" max="90">
        <div class="hint">{_t('Show reminder if no full charge in this many days (LFP only)')}</div>

        <div class="section-title">{_t('Polling')}</div>
        <label>{_t('Poll Interval (seconds)')}</label>
        <input type="number" name="POLL_INTERVAL" value="{interval}" min="10" max="3600">

        <label>{_t('HTTP Port')}</label>
        <input type="number" name="PORT" value="{port}" min="1" max="65535">
        <div class="hint">{_t('Restart required after port change.')}</div>

        <div class="section-title">{_t('Interface')}</div>
        <label>{_t('Language')}</label>
        <select name="LANGUAGE">{lang_opts}</select>

        <button type="submit">💾 {_t('Save Settings')}</button>
      </form>
    </div>

    <div class="card">
      <div class="label">{_t('API Endpoints')} –
        <a href="/api" style="color:#3b82f6;font-size:.75rem;text-decoration:none">{_t('view live ↗')}</a>
      </div>
      <div class="meta-row">
        <span><a href="/" style="color:#3b82f6;text-decoration:none"><code>/</code></a></span>
        <span class="meta-val">{_t('Status page')}</span>
      </div>
      <div class="meta-row">
        <span><a href="/api/health" target="_blank" style="color:#3b82f6;text-decoration:none"><code>/api/health</code></a></span>
        <span class="meta-val">{_t('Health check (JSON)')}</span>
      </div>
    </div>

    <script>
    var presets = {json.dumps({k: {'opt_min': v['opt_min'], 'opt_max': v['opt_max']} for k, v in BATTERY_PRESETS.items()})};
    function updatePreset(type) {{
      var p = presets[type];
      if (!p) return;
      document.getElementById('opt_min').value = p.opt_min;
      document.getElementById('opt_max').value = p.opt_max;
    }}
    </script>"""

    return _page('Settings', 'settings', body)


# ── API overview page ──────────────────────────────────────────────────────────
def build_api_page():
    with _lock:
        vehicles = dict(_cache['vehicles'])
        ts       = _cache['ts']
        error    = _cache['error']

    age = int(time.time() - ts) if ts else 0

    health_json  = json.dumps({
        'status': 'ok' if vehicles else 'error', 'error': error,
        'data_age': age, 'site_id': _get('VRM_SITE_ID'), 'version': VERSION,
    }, indent=2)

    # Build vehicle_data JSON showing all cached vehicles
    if vehicles:
        vd_payload = {
            vin: {'response': {'response': {'charge_state': veh['data']}}}
            for vin, veh in vehicles.items()
        }
    else:
        vd_payload = {'error': error or 'No data yet'}
    vehicle_json = json.dumps(vd_payload, indent=2)

    body = f"""
    <div class="card">
      <div class="label">{_t('Endpoints')}</div>
      <div class="meta-row">
        <span><a href="/" style="color:#3b82f6;text-decoration:none"><code>/</code></a></span>
        <span class="meta-val">{_t('Status page (UI)')}</span>
      </div>
      <div class="meta-row">
        <span><a href="/settings" style="color:#3b82f6;text-decoration:none"><code>/settings</code></a></span>
        <span class="meta-val">{_t('Settings (UI)')}</span>
      </div>
      <div class="meta-row">
        <span><a href="/api/health" target="_blank" style="color:#3b82f6;text-decoration:none"><code>/api/health</code></a></span>
        <span class="meta-val">{_t('Health check · JSON')}</span>
      </div>
      <div class="meta-row">
        <span><a href="/api/raw" target="_blank" style="color:#3b82f6;text-decoration:none"><code>/api/raw</code></a></span>
        <span class="meta-val">{_t('Raw VRM values (debug)')}</span>
      </div>
    </div>
    <div class="card">
      <div class="label">GET /api/health – Live
        <a href="/api/health" target="_blank"
           style="color:#3b82f6;font-size:.75rem;margin-left:.5rem;text-transform:none">{_t('open ↗')}</a>
      </div>
      <pre style="background:#0f172a;border-radius:8px;padding:.85rem;font-size:.78rem;
                  color:#94a3b8;overflow-x:auto;margin-top:.5rem;line-height:1.5">{_esc(health_json)}</pre>
    </div>
    <div class="card">
      <div class="label">GET /api/1/vehicles/&lt;VIN&gt;/vehicle_data – Live
        <span style="color:#475569;font-size:.72rem">({_t('age: {age}s', age=age)})</span>
      </div>
      <pre style="background:#0f172a;border-radius:8px;padding:.85rem;font-size:.78rem;
                  color:#94a3b8;overflow-x:auto;margin-top:.5rem;line-height:1.5">{_esc(vehicle_json)}</pre>
    </div>"""

    return _page('API', 'api', body)


# ── HTTP Handler ───────────────────────────────────────────────────────────────
_warned_vins = set()

def _find_vehicle(vehicles, vin):
    """Match requested VIN (case-insensitive); fall back to the first vehicle."""
    if vin:
        for key, veh in vehicles.items():
            if key.upper() == vin.upper():
                return veh
        if len(vehicles) > 1 and vin not in _warned_vins:
            _warned_vins.add(vin)
            print(f'[HTTP] VIN {vin} not found in VRM data {list(vehicles)} – '
                  f'serving first vehicle. Check the VIN in EVCC.', flush=True)
    return next(iter(vehicles.values()))


def _parse_settings(params):
    """Validate submitted settings. Returns (updates, error_msg)."""
    updates = {}
    site_id = params.get('VRM_SITE_ID', '').strip()
    if site_id:
        if not site_id.isdigit():
            return {}, _t('VRM Site ID must be numeric.')
        updates['VRM_SITE_ID'] = site_id
    bat_type = params.get('BATTERY_TYPE', '').strip()
    if bat_type:
        if bat_type not in BATTERY_PRESETS:
            return {}, _t('Unknown battery type: {v}', v=bat_type)
        updates['BATTERY_TYPE'] = bat_type
    language = params.get('LANGUAGE', '').strip()
    if language:
        if language not in LANGUAGES:
            return {}, _t('Unknown language: {v}', v=language)
        updates['LANGUAGE'] = language
    for key, (typ, lo, hi) in NUMERIC_SETTINGS.items():
        raw = params.get(key, '').strip()
        if not raw:
            if key == 'CAPACITY' and key in params:
                updates[key] = None   # cleared → fall back to VRM /BatteryCapacity
            continue
        try:
            val = typ(raw)
        except ValueError:
            return {}, _t('{key}: "{raw}" is not a valid number.', key=key, raw=raw)
        if not lo <= val <= hi:
            return {}, _t('{key} must be between {lo} and {hi}.', key=key, lo=lo, hi=hi)
        updates[key] = str(val)
    if int(updates.get('OPT_MIN', 0)) >= int(updates.get('OPT_MAX', 100)):
        return {}, _t('Optimal minimum must be lower than optimal maximum.')
    token = params.get('VRM_TOKEN', '').strip()
    if token:
        updates['VRM_TOKEN'] = token
    return updates, ''


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        _req.lang = _resolve_lang(self.headers.get('Accept-Language', ''))
        path = urlparse(self.path).path
        if path in ('/', '/status'):
            if not _is_configured():
                self.send_response(302)
                self.send_header('Location', '/settings')
                self.end_headers()
                return
            self._html(build_status_page())
        elif path == '/settings':
            self._html(build_settings_page())
        elif path == '/api':
            self._html(build_api_page())
        elif '/vehicle_data' in path:
            # Extract VIN from path: /api/1/vehicles/<VIN>/vehicle_data
            vin = None
            parts = path.split('/')
            try:
                vi_idx = parts.index('vehicles')
                vin = parts[vi_idx + 1]
            except (ValueError, IndexError):
                pass

            with _lock:
                vehicles = dict(_cache['vehicles'])
                ts       = _cache['ts']
                error    = _cache['error']

            age = int(time.time() - ts) if ts else 0

            if not vehicles:
                self._json({'error': error or 'Waiting for first VRM poll'}, 503)
                return
            if age > _stale_after():
                # Don't let EVCC plan with an hours-old SoC while VRM is unreachable
                self._json({'error': f'VRM data is {age}s old: {error or "no successful poll"}'}, 503,
                           headers={'X-Data-Age-Seconds': str(age)})
                return

            veh = _find_vehicle(vehicles, vin)

            self._json({'response': {'response': {'charge_state': veh['data']}}},
                       headers={'X-Data-Age-Seconds': str(age)})
        elif path == '/api/health':
            with _lock:
                ok    = bool(_cache['vehicles'])
                error = _cache['error']
                ts    = _cache['ts']
            age = int(time.time() - ts) if ts else None
            if ok and age > _stale_after():
                status = 'stale'
            else:
                status = 'ok' if ok else 'error'
            self._json({
                'status': status, 'error': error, 'data_age': age,
                'site_id': _get('VRM_SITE_ID'), 'version': VERSION,
            }, 200 if status == 'ok' else 503)
        elif path == '/api/raw':
            # Debug: raw VRM values of each EV and the EVCS it is plugged into
            with _lock:
                vehicles = dict(_cache['vehicles'])
            self._json({vin: veh.get('raw', {}) for vin, veh in vehicles.items()})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        _req.lang = _resolve_lang(self.headers.get('Accept-Language', ''))
        path = urlparse(self.path).path
        if path == '/settings':
            params = {k: v[0] for k, v in parse_qs(self._read_body().decode(errors='replace'), keep_blank_values=True).items()}
            updates, err = _parse_settings(params)
            if err:
                self._html(build_settings_page(error_msg=err))
                return
            try:
                with _cfg_lock:
                    cfg = _load_cfg()
                    for key, val in updates.items():
                        if val is None:
                            cfg.pop(key, None)
                        else:
                            cfg[key] = val
                    _save_cfg(cfg)
                print('[CFG] Settings saved.', flush=True)
                _req.lang = _resolve_lang(self.headers.get('Accept-Language', ''))   # language may have changed
                self._html(build_settings_page(saved=True))
            except Exception as exc:
                self._html(build_settings_page(error_msg=str(exc)))
        elif '/command/' in path:
            self._read_body()   # drain request body
            parts = path.split('/')
            command = parts[-1] if parts else 'unknown'
            vin = None
            try:
                vi_idx = parts.index('vehicles')
                vin = parts[vi_idx + 1]
            except (ValueError, IndexError):
                pass
            print(f'[CMD] {command} (VIN={vin}) – no-op, data served from VRM', flush=True)
            self._json({'response': {'result': True, 'reason': '', 'vin': vin or '', 'command': command}})
        else:
            self.send_response(404); self.end_headers()

    def _read_body(self, limit=64 * 1024):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = 0
        return self.rfile.read(min(max(length, 0), limit)) if length > 0 else b''

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        self._send(200, 'text/html; charset=utf-8', content.encode())

    def _json(self, obj, code=200, headers=None):
        self._send(code, 'application/json', json.dumps(obj).encode(), headers)

    def log_message(self, fmt, *args):
        # EVCC polls vehicle_data several times per cycle (one request per field) –
        # only log failures and POSTs to keep the container log readable.
        msg = fmt % args
        if 'POST' in msg or (len(args) > 1 and str(args[1])[:1] in '45'):
            print(f'[HTTP] {msg}', flush=True)


# ── Main ───────────────────────────────────────────────────────────────────────
def _backfill_last_full_charge():
    """On startup: scan SoC history and set last_full_charge if a 100% entry exists
    but no timestamp is recorded yet (or the history entry is more recent)."""
    cfg = _load_cfg()
    changed = False
    for key, value in list(cfg.items()):
        if not key.startswith('soc_history_'):
            continue
        vin = key[len('soc_history_'):]
        lfc_key = f'last_full_charge_{vin}'
        history = value  # list of [ts, soc]
        # Find most recent 100% entry
        full_entries = [ts for ts, soc in history if soc >= 100]
        if not full_entries:
            continue
        best_ts = max(full_entries)
        existing = cfg.get(lfc_key, 0)
        if best_ts > existing:
            cfg[lfc_key] = best_ts
            print(f'[startup] Backfilled last_full_charge for {vin}: '
                  f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(best_ts))}', flush=True)
            changed = True
    if changed:
        _save_cfg(cfg)


def _healthcheck():
    """Docker liveness probe: exit 0 if the HTTP server answers at all.
    Reads the port from settings.json too, so a port changed in the UI is honoured.
    VRM errors (HTTP 503) still count as alive – restarting won't fix VRM."""
    import sys
    from urllib.error import HTTPError
    try:
        urlopen(f'http://127.0.0.1:{_get_int("PORT", 8080)}/api/health', timeout=5)
    except HTTPError:
        pass
    except Exception as exc:
        print(f'unhealthy: {exc}')
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    import sys
    if '--healthcheck' in sys.argv:
        _healthcheck()
    _backfill_last_full_charge()
    port = _get_int('PORT', 8080)
    print(f'[{APP_NAME}] v{VERSION}  port={port}  '
          f'site={_get("VRM_SITE_ID","?")}  poll={_get("POLL_INTERVAL","60")}s', flush=True)
    threading.Thread(target=poll_vrm, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
