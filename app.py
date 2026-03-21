#!/usr/bin/env python3
"""
vrm-ev-proxy v2.1
Polls Victron VRM Cloud and serves a vehicle HTTP API for EVCC.
Supports LFP and NMC battery tracking, SoC history, cycle counting.
No external dependencies – pure Python stdlib only.
"""

import json
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen, Request

VERSION    = "2.1"
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
CHARGING_STATE_MAP = {
    0: 'Disconnected', 1: 'Disconnected', 2: 'Stopped',
    3: 'Charging',     4: 'Complete',     5: 'Stopped', 6: 'Stopped',
}
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
    'next_retry_in': 0,
}
_lock  = threading.Lock()
_start = time.time()


# ── Config helpers ─────────────────────────────────────────────────────────────
def _load_cfg():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def _get(key, default=''):
    return _load_cfg().get(key) or os.environ.get(key, default)

def _bat():
    """Return battery preset dict for configured type."""
    return BATTERY_PRESETS.get(_get('BATTERY_TYPE', 'LFP'), BATTERY_PRESETS['LFP'])

def _is_configured():
    """Return True if both VRM_TOKEN and VRM_SITE_ID are set."""
    return bool(_get('VRM_TOKEN')) and bool(_get('VRM_SITE_ID'))


# ── VRM Poller ─────────────────────────────────────────────────────────────────
def poll_vrm():
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

            ev_records = [r for r in records if r.get('Device') == 'Electrical Vehicle']

            if not ev_records:
                raise ValueError('No EV device found in VRM – is the Tesla configured in VRM?')

            # Group records by instance to support multiple EVs
            by_instance = {}
            for r in ev_records:
                inst = r.get('instance', 0)
                if inst not in by_instance:
                    by_instance[inst] = {}
                by_instance[inst][r['dbusPath']] = r['rawValue']

            cfg = _load_cfg()
            vehicles = {}

            for inst, ev in by_instance.items():
                # Extract VIN
                vin = str(ev.get('/Serial') or ev.get('/VIN') or f'EV_{inst}')
                custom_name = str(ev.get('/CustomName') or '') or vin

                charging_raw  = int(float(ev.get('/ChargingState', 0)))
                soc           = int(float(ev.get('/Soc', 0)))
                range_km      = float(ev.get('/RangeToGo', 0))
                limit_soc     = int(float(ev.get('/TargetSoc', 100)))
                max_current   = int(float(ev.get('/Ac/MaxChargeCurrent', 16)))
                power_w       = float(ev.get('/Ac/Power', 0))
                last_contact  = float(ev.get('/LastEvContact', 0))
                odometer      = float(ev.get('/Odometer', 0))
                charging_state = CHARGING_STATE_MAP.get(charging_raw, 'Disconnected')

                data = {
                    'battery_level':    soc,
                    'battery_range':    round(range_km / 1.60934, 2),
                    'charge_limit_soc': limit_soc,
                    'charging_state':   charging_state,
                    'charge_amps':      max_current,
                }

                # ── Track last full charge (per VIN) ──────────────────────────
                lfc_key = f'last_full_charge_{vin}'
                if soc >= 100 and charging_state in ('Charging', 'Complete'):
                    cfg[lfc_key] = time.time()
                    print(f'[VRM] Full charge detected for {vin} – timestamp saved.', flush=True)

                # ── SoC history (hourly snapshots, per VIN) ────────────────────
                hist_key = f'soc_history_{vin}'
                history = cfg.get(hist_key, [])
                now = time.time()
                if not history or now - history[-1][0] >= 3600:
                    history.append([int(now), soc])
                    cfg[hist_key] = history[-168:]

                # ── Charge cycle counter (per VIN) ─────────────────────────────
                capacity = float(_get('CAPACITY', '0'))
                cycles_key = f'charge_cycles_{vin}'
                last_soc_key = f'last_soc_for_cycles_{vin}'
                last_soc = cfg.get(last_soc_key, soc)
                if capacity > 0 and soc > last_soc:
                    delta_kwh = (soc - last_soc) / 100.0 * capacity
                    cfg[cycles_key] = cfg.get(cycles_key, 0.0) + delta_kwh / capacity
                cfg[last_soc_key] = soc

                # ── Time above optimal (per VIN) ────────────────────────────────
                bat       = _bat()
                opt_max   = int(_get('OPT_MAX', str(bat['opt_max'])))
                interval  = int(_get('POLL_INTERVAL', '60'))

                week_start_key = f'time_above_week_start_{vin}'
                time_above_key = f'time_above_optimal_{vin}'
                week_start = cfg.get(week_start_key, now)
                if now - week_start >= 7 * 86400:
                    cfg[time_above_key] = 0
                    cfg[week_start_key] = now
                elif week_start_key not in cfg:
                    cfg[week_start_key] = now

                if soc > opt_max:
                    cfg[time_above_key] = cfg.get(time_above_key, 0) + interval

                vehicles[vin] = {
                    'data':            data,
                    'range_km':        range_km,
                    'power_w':         power_w,
                    'last_ev_contact': last_contact,
                    'odometer':        odometer,
                    'name':            custom_name,
                }

                print(f'[VRM] OK – VIN={vin}  SoC={soc}%  Range={range_km}km  '
                      f'State={charging_state}  Power={power_w}W', flush=True)

            _save_cfg(cfg)

            with _lock:
                _cache['vehicles']    = vehicles
                _cache['ts']          = time.time()
                _cache['error']       = None
                _cache['error_count'] = 0
                _cache['next_retry_in'] = 0

        except Exception as exc:
            with _lock:
                _cache['error'] = str(exc)
                _cache['error_count'] += 1
                wait = min(int(_get('POLL_INTERVAL', '60')) * (2 ** _cache['error_count']), 600)
                _cache['next_retry_in'] = wait
            print(f'[VRM] Error (attempt {_cache["error_count"]}): {exc}', flush=True)
            time.sleep(wait)
            continue

        time.sleep(int(_get('POLL_INTERVAL', '60')))


# ── SoC color ──────────────────────────────────────────────────────────────────
def _soc_color(soc):
    bat     = _bat()
    opt_min = int(_get('OPT_MIN', str(bat['opt_min'])))
    opt_max = int(_get('OPT_MAX', str(bat['opt_max'])))
    if soc < opt_min:    return '#ef4444'   # below min → red
    if soc <= opt_max:   return '#22c55e'   # in range  → green
    return '#f59e0b'                         # above max → amber warning


# ── SVG History Chart ──────────────────────────────────────────────────────────
def _build_chart(history, opt_min, opt_max):
    if len(history) < 2:
        return '<div style="color:#475569;text-align:center;padding:1rem;font-size:.8rem">Not enough data yet (needs 2+ hours)</div>'

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
        day = time.strftime('%a', time.localtime(ts))
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
.toggle-pw { font-size: .72rem; color: #3b82f6; cursor: pointer; display: inline-block; }
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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} – {APP_NAME}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <h1>⚡ {APP_NAME}</h1>
  <p class="subtitle">v{VERSION} &nbsp;·&nbsp; Victron VRM → EVCC</p>
  <nav>
    <a href="/"         class="{'active' if nav_active=='status'   else ''}">📊 Status</a>
    <a href="/settings" class="{'active' if nav_active=='settings' else ''}">⚙️ Settings</a>
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
    el.textContent='in '+secs+'s'; secs--;
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
        next_retry   = _cache['next_retry_in']

    cfg      = _load_cfg()
    bat      = _bat()
    bat_type = _get('BATTERY_TYPE', 'LFP')
    opt_min  = int(_get('OPT_MIN', str(bat['opt_min'])))
    opt_max  = int(_get('OPT_MAX', str(bat['opt_max'])))
    capacity = float(_get('CAPACITY', '0'))
    interval = int(_get('POLL_INTERVAL', '60'))

    age       = int(time.time() - ts) if ts else 0
    next_poll = max(0, interval - age)
    uptime    = int(time.time() - _start)
    up_str    = f'{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s'
    ts_str    = time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(ts)) if ts else '–'

    error_box = f'<div class="error-box">⚠️ {error}</div>' if error else ''

    # Build next poll / retry display
    if error and error_count > 0:
        next_poll_display = f'<span class="meta-val" id="countdown">in {next_retry}s (attempt {error_count})</span>'
        countdown_val = next_retry
    else:
        next_poll_display = f'<span class="meta-val" id="countdown">in {next_poll}s</span>'
        countdown_val = next_poll

    main_cards = ''

    if not vehicles:
        main_cards = '<div class="card" style="text-align:center;padding:2rem;color:#f59e0b">⏳ Waiting for first VRM poll…</div>'
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
                if days_ago < 1:     lf_str = 'Today'
                elif days_ago < 2:   lf_str = 'Yesterday'
                else:                lf_str = f'{int(days_ago)}d ago ({time.strftime("%d.%m.%Y", time.localtime(last_full))})'
            else:
                lf_str = 'Not recorded yet'

            # Time above optimal this week (per VIN)
            time_above = cfg.get(f'time_above_optimal_{vin}', 0)
            ta_hours   = time_above / 3600
            ta_str     = f'{ta_hours:.1f}h this week' if time_above else '0h this week'

            # Charge cycles (per VIN)
            cycles     = cfg.get(f'charge_cycles_{vin}', 0.0)
            cycles_str = f'{cycles:.1f}' if capacity > 0 else '–'

            soc       = data['battery_level']
            limit_soc = data['charge_limit_soc']
            state     = data['charging_state']
            icon, state_label, state_color = CHARGING_STATE_UI.get(state, ('❓', state, '#6b7280'))
            bar_color = _soc_color(soc)

            warnings = ''

            # Warning: above optimal range
            if soc > opt_max:
                warnings += (f'<div class="warning-box">⚠️ SoC ({soc}%) is above the optimal maximum '
                             f'of {opt_max}% for {bat_type}. '
                             f'{"Reduce charging limit to protect the battery." if bat_type == "NMC" else "OK for occasional full charge, but limit to 80% for daily use."}</div>')

            # LFP full charge reminder
            if bat_type == 'LFP' and bat['full_reminder_days']:
                remind_after = int(_get('FULL_REMINDER_DAYS', str(bat['full_reminder_days'])))
                if last_full and (time.time() - last_full) / 86400 > remind_after:
                    days_overdue = int((time.time() - last_full) / 86400)
                    warnings += (f'<div class="info-box">ℹ️ LFP BMS balancing: last full charge was '
                                 f'{days_overdue} days ago. Consider charging to 100% soon.</div>')

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
              <div class="label">Charging Power</div>
              <div class="power-row" style="color:#22c55e">
                ⚡ {power_w / 1000:.1f} <span class="unit">kW</span>
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
            🚗 {veh_name}
            <span style="font-size:.7rem;color:#475569;margin-left:.5rem">VIN: {vin}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem">
            <div class="label" style="margin:0">State of Charge</div>
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
            <div class="label">Range</div>
            <div class="value">{int(range_km)}<span class="unit"> km</span></div>
          </div>
          <div class="card small">
            <div class="label">Status</div>
            <div class="value" style="font-size:.9em;color:{state_color}">{icon} {state_label}</div>
          </div>
          <div class="card small">
            <div class="label">Odometer</div>
            <div class="value" style="font-size:1.2rem">{int(odometer):,}<span class="unit"> km</span></div>
          </div>
        </div>

        <div class="card">
          <div class="label" style="margin-bottom:.6rem">SoC History – 7 days</div>
          {chart}
          <div style="display:flex;gap:1rem;margin-top:.5rem;font-size:.7rem;color:#475569">
            <span style="color:#22c55e">━</span> Optimal zone
            <span style="color:{bat['color']}">╷</span> {opt_max}% limit
            <span style="color:#38bdf8">━</span> SoC
          </div>
        </div>

        <div class="card">
          <div class="meta-row">
            <span>Battery type</span>
            <span class="meta-val">{bat_type} · {bat['note'][:50]}…</span>
          </div>
          <div class="meta-row">
            <span>Optimal range</span>
            <span class="meta-val">{opt_min}% – {opt_max}%</span>
          </div>
          <div class="meta-row">
            <span>Time above {opt_max}%</span>
            <span class="meta-val">{ta_str}</span>
          </div>
          <div class="meta-row">
            <span>Charge cycles</span>
            <span class="meta-val">{cycles_str}</span>
          </div>
          <div class="meta-row">
            <span>Last full charge</span>
            <span class="meta-val">{lf_str}</span>
          </div>
          <div class="meta-row">
            <span>Last EV contact</span>
            <span class="meta-val">{lc_str}</span>
          </div>
        </div>
"""

    # Global meta card
    main_cards += f"""
        <div class="card">
          <div class="meta-row">
            <span>Bridge</span>
            <span class="meta-val"><span class="dot green"></span>Online</span>
          </div>
          <div class="meta-row"><span>VRM Site ID</span><span class="meta-val">{_get('VRM_SITE_ID','–')}</span></div>
          <div class="meta-row"><span>Last update</span><span class="meta-val">{ts_str}</span></div>
          <div class="meta-row"><span>Data age</span><span class="meta-val">{age}s</span></div>
          <div class="meta-row"><span>Next poll</span>{next_poll_display}</div>
          <div class="meta-row"><span>Uptime</span><span class="meta-val">{up_str}</span></div>
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
    masked   = ('*' * 8 + token[-6:]) if len(token) > 6 else '(not set)'

    lfp_sel = 'selected' if bat_type == 'LFP' else ''
    nmc_sel = 'selected' if bat_type == 'NMC' else ''

    notice = ''
    if saved:
        notice = '<div class="success-box">✅ Settings saved – taking effect on next poll.</div>'
    elif error_msg:
        notice = f'<div class="error-box">⚠️ {error_msg}</div>'

    # First-run wizard welcome banner
    welcome_banner = ''
    if not _is_configured():
        welcome_banner = '''<div class="info-box" style="margin-bottom:1.2rem">
    👋 Welcome to vrm-ev-proxy! Enter your VRM credentials below to get started.
  </div>'''

    # Step badges for VRM token and site ID labels (shown when not configured)
    if not _is_configured():
        token_label = '<label><span class="step-badge">1</span>VRM API Token</label>'
        siteid_label = '<label><span class="step-badge">2</span>VRM Site / Installation ID</label>'
    else:
        token_label = '<label>VRM API Token</label>'
        siteid_label = '<label>VRM Site / Installation ID</label>'

    body = f"""
    {welcome_banner}
    {notice}
    <div class="card">
      <form method="POST" action="/settings">

        <div class="section-title" style="margin-top:0;border-top:none;padding-top:0">VRM Connection</div>
        {token_label}
        <input type="password" name="VRM_TOKEN" id="tok"
               placeholder="Leave empty to keep current" autocomplete="off">
        <div class="hint">
          Current: <span id="tok-masked">{masked}</span>
          <span id="tok-full" style="display:none;word-break:break-all">{token}</span>
          &nbsp;<span class="toggle-pw" onclick="
            var m=document.getElementById('tok-masked');
            var f=document.getElementById('tok-full');
            var shown=f.style.display!=='none';
            m.style.display=shown?'':'none';
            f.style.display=shown?'none':'';
            this.textContent=shown?'show':'hide';
          ">show</span>
        </div>

        {siteid_label}
        <input type="text" name="VRM_SITE_ID" value="{site_id}" placeholder="e.g. 123456">
        <div class="hint">Found in VRM URL: …/installation/<b>XXXXX</b>/dashboard</div>

        <div class="section-title">Battery</div>

        <label>Battery Chemistry</label>
        <select name="BATTERY_TYPE" onchange="updatePreset(this.value)">
          <option value="LFP" {lfp_sel}>LFP – Lithium Iron Phosphate (optimal: 10–80%)</option>
          <option value="NMC" {nmc_sel}>NMC – Nickel Manganese Cobalt (optimal: 20–90%)</option>
        </select>

        <label>Optimal SoC Range (%)</label>
        <div class="field-row">
          <div>
            <input type="number" name="OPT_MIN" id="opt_min" value="{opt_min}" min="0" max="50">
            <div class="hint">Min (🔴 below = warning)</div>
          </div>
          <div>
            <input type="number" name="OPT_MAX" id="opt_max" value="{opt_max}" min="50" max="100">
            <div class="hint">Max (🟡 above = warning)</div>
          </div>
        </div>

        <label>Battery Capacity (kWh)</label>
        <input type="number" name="CAPACITY" value="{capacity}" placeholder="e.g. 60" min="1" max="200" step="0.1">
        <div class="hint">Required for charge cycle counting</div>

        <label>LFP Full Charge Reminder (days)</label>
        <input type="number" name="FULL_REMINDER_DAYS" value="{reminder}"
               placeholder="28" min="7" max="90">
        <div class="hint">Show reminder if no full charge in this many days (LFP only)</div>

        <div class="section-title">Polling</div>
        <label>Poll Interval (seconds)</label>
        <input type="number" name="POLL_INTERVAL" value="{interval}" min="10" max="300">

        <label>HTTP Port</label>
        <input type="number" name="PORT" value="{port}" min="1" max="65535">
        <div class="hint">Restart required after port change.</div>

        <button type="submit">💾 Save Settings</button>
      </form>
    </div>

    <div class="card">
      <div class="label">API Endpoints –
        <a href="/api" style="color:#3b82f6;font-size:.75rem;text-decoration:none">view live ↗</a>
      </div>
      <div class="meta-row">
        <span><a href="/" style="color:#3b82f6;text-decoration:none"><code>/</code></a></span>
        <span class="meta-val">Status page</span>
      </div>
      <div class="meta-row">
        <span><a href="/api/health" target="_blank" style="color:#3b82f6;text-decoration:none"><code>/api/health</code></a></span>
        <span class="meta-val">Health check (JSON)</span>
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
      <div class="label">Endpoints</div>
      <div class="meta-row">
        <span><a href="/" style="color:#3b82f6;text-decoration:none"><code>/</code></a></span>
        <span class="meta-val">Status page (UI)</span>
      </div>
      <div class="meta-row">
        <span><a href="/settings" style="color:#3b82f6;text-decoration:none"><code>/settings</code></a></span>
        <span class="meta-val">Settings (UI)</span>
      </div>
      <div class="meta-row">
        <span><a href="/api/health" target="_blank" style="color:#3b82f6;text-decoration:none"><code>/api/health</code></a></span>
        <span class="meta-val">Health check · JSON</span>
      </div>
    </div>
    <div class="card">
      <div class="label">GET /api/health – Live
        <a href="/api/health" target="_blank"
           style="color:#3b82f6;font-size:.75rem;margin-left:.5rem;text-transform:none">open ↗</a>
      </div>
      <pre style="background:#0f172a;border-radius:8px;padding:.85rem;font-size:.78rem;
                  color:#94a3b8;overflow-x:auto;margin-top:.5rem;line-height:1.5">{health_json}</pre>
    </div>
    <div class="card">
      <div class="label">GET /api/1/vehicles/&lt;VIN&gt;/vehicle_data – Live
        <span style="color:#475569;font-size:.72rem">(age: {age}s)</span>
      </div>
      <pre style="background:#0f172a;border-radius:8px;padding:.85rem;font-size:.78rem;
                  color:#94a3b8;overflow-x:auto;margin-top:.5rem;line-height:1.5">{vehicle_json}</pre>
    </div>"""

    return _page('API', 'api', body)


# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
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

            veh = None
            if vin and vin in vehicles:
                veh = vehicles[vin]
            else:
                # Fallback: serve first available vehicle
                veh = next(iter(vehicles.values()))

            self._json({'response': {'response': {'charge_state': veh['data']}}},
                       headers={'X-Data-Age-Seconds': str(age)})
        elif path == '/api/health':
            with _lock:
                ok    = bool(_cache['vehicles'])
                error = _cache['error']
                ts    = _cache['ts']
            self._json({
                'status': 'ok' if ok else 'error', 'error': error,
                'data_age': int(time.time() - ts) if ts else None,
                'site_id': _get('VRM_SITE_ID'), 'version': VERSION,
            }, 200 if ok else 503)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path == '/settings':
            length = int(self.headers.get('Content-Length', 0))
            params = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            cfg    = _load_cfg()
            for key in ('VRM_SITE_ID', 'POLL_INTERVAL', 'PORT', 'BATTERY_TYPE',
                        'CAPACITY', 'OPT_MIN', 'OPT_MAX', 'FULL_REMINDER_DAYS'):
                if params.get(key):
                    cfg[key] = params[key].strip()
            if params.get('VRM_TOKEN'):
                cfg['VRM_TOKEN'] = params['VRM_TOKEN'].strip()
            try:
                _save_cfg(cfg)
                print('[CFG] Settings saved.', flush=True)
                self._html(build_settings_page(saved=True))
            except Exception as exc:
                self._html(build_settings_page(error_msg=str(exc)))
        else:
            self.send_response(404); self.end_headers()

    def _html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/vehicle_data' in msg or 'POST' in msg:
            print(f'[HTTP] {msg}', flush=True)


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(_get('PORT', '8080'))
    print(f'[{APP_NAME}] v{VERSION}  port={port}  '
          f'site={_get("VRM_SITE_ID","?")}  poll={_get("POLL_INTERVAL","60")}s', flush=True)
    threading.Thread(target=poll_vrm, daemon=True).start()
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
