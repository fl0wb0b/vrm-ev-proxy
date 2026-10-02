# vrm-ev-proxy

Connects your EV from **Victron VRM** to your local **EVCC** instance.

VRM integrates with the EV manufacturer's API and makes vehicle data available in the VRM Cloud. This proxy fetches that data periodically (default: 60s, configurable) and serves it locally to EVCC – with a built-in status page and settings UI.

```
EV Manufacturer API
        │
        ▼
  VRM Cloud API
        ▲
        │  polls every 60s
  vrm-ev-proxy  ──►  EVCC
        │
        └──  http://<your-server>:8080
```

---

**Vehicle pictures:** the status page shows a picture per car. Tesla models use Tesla's own renderings from `teslamotors/custom-wraps` (loaded by the browser from GitHub, not stored here); VW ID.4, Kia EV6, Hyundai Ioniq 5 and Ford Mustang Mach-E are shipped in `images/` (768 px JPEG, served at `/img/<id>.jpg`) and picked automatically by the car's name. Settings → *Vehicle pictures* chooses one per car, or sets your own picture URL (wins over the choice).

## Install

**Requires:** Docker + Docker Compose · Supports `linux/amd64` and `linux/arm64` (Raspberry Pi)

```bash
git clone https://github.com/fl0wb0b/vrm-ev-proxy.git
cd vrm-ev-proxy
sudo docker compose up -d
```

Then open **`http://<your-server-ip>:8080`** in your browser – the setup wizard guides you through the rest. That's it.

---

## Update

```bash
cd vrm-ev-proxy
sudo docker compose pull && sudo docker compose up -d
```

Or use [Watchtower](https://containrrr.dev/watchtower/) to update automatically – no commands needed.

---

## Configuration

Everything is configured in the browser at `/settings` – no config files, no terminal.

| Setting | Where to find |
|---------|--------------|
| **VRM Token** | VRM Portal → avatar top right → Settings → API Tokens → Generate |
| **VRM Site ID** | The number in your VRM URL: `vrm.victronenergy.com/installation/`**`12345`**`/dashboard` |
| **Language** | Web UI in German or English – `Auto` follows the browser language |

---

## Web UI

| URL | What you get |
|-----|-------------|
| `:8080/` | Live status: SoC, range, charging state, 7-day chart |
| `:8080/settings` | All settings – change anytime without restart |
| `:8080/api/raw` | Raw VRM values of the EV and its charging station (debugging) |

---

## EVCC setup

Add a vehicle in EVCC with these settings:
- **Template:** Tesla BLE (`tesla-ble`)
- **URL:** `http://<server-ip>` – **without** port, EVCC appends `:<port>` itself
- **Port:** `8080`
- **VIN:** your vehicle VIN

```yaml
vehicles:
  - name: tesla
    type: template
    template: tesla-ble
    title: Tesla
    vin: 5YJ3E1EA0KF000001
    url: http://192.168.1.10
    port: 8080
    capacity: 60
```

The proxy serves all fields the template reads from `charge_state`: `battery_level`, `battery_range` (miles), `charge_limit_soc`, `charge_amps`, `charging_state`, `charge_energy_added` (kWh per plug-in session – from the VRM energy meter `/Ac/Energy/Forward` if available, otherwise integrated from AC/DC power) and `minutes_to_full_charge` (estimate from battery capacity – `/settings` or VRM `/BatteryCapacity`).

Known bugs and their status: [BUGS.md](BUGS.md)

### Several EVs on one charging station

VRM keeps the station link (`/Mgmt/Connection`) of a car that has left, so several EVs can point at the same station. Only one of them gets the station's status, power and session energy: the one that reports charging activity itself, otherwise the most recently seen one. All others are reported as `Disconnected`.

EVCC only identifies the vehicle again after the charger reported "disconnected". A quick swap between two cars is missed, and EVCC keeps the previous car on the loadpoint. Set **EVCC URL** (e.g. `http://192.168.1.10:7070`) in `/settings` to fix this live, for any number of loadpoints and without mapping: while one of the proxy's EVs is charging, the proxy reads EVCC's `/api/state`. A charging loadpoint whose vehicle – recognised by its range and, between cars with a similar range, its SoC – both of which EVCC takes from this proxy – is `Disconnected` here gets `PATCH /api/loadpoints/<n>/vehicle`, and EVCC picks the right car by its status (at most once per 5 minutes per loadpoint; skipped if no or several EVs match). If your EVCC API requires a login, this fails and is logged as `[EVCC] … failed`.

### Periodic full charge from PV (LFP balancing)

LFP packs have a flat voltage curve, so the BMS counts charge in and out to know the SoC – and drifts. Only a full charge re-anchors it. Rough estimate: ~1–3 % after 1–2 weeks, **~3–5 % after 3–4 weeks or 100–150 kWh** without a full charge – noticeable mainly at low SoC.

In `/settings` (needs **EVCC URL**):
- **Full charge from PV after (days)** – e.g. `28`
- **… or after charging (kWh)** – e.g. `120`: energy charged since the last 100 %, i.e. EVCC's sessions of that vehicle (`/api/sessions`) plus what the car charged away from home (Supercharger – SoC rise × capacity while the car reports charging and `AtSite` is 0)

Whichever comes first makes the car due; 0 turns that part off. Any 100 % resets both – at home, on a socket or at a Supercharger. A car that sits after a full charge stays due only by days.

Every 5 minutes the proxy checks EVCC's solar forecast: if the surplus left today – forecast minus **base load** (default 1500 W), capped at what the car's loadpoint can take, 80 % of it trusted – covers the energy up to 100 %, and the sun is up, the proxy sets that vehicle's EVCC limit to 100 % (`POST /api/vehicles/<name>/limitsoc/100`). All loadpoints are treated the same.

EVCC never stops at a limit of 100 % – the car ends the charge itself. Tesla already shows 100 % while it is still topping off (that is when the BMS balances), so the previous limit only comes back once the car has **stopped charging and sat at 100 % for 30 minutes** – a lower limit would make EVCC cut the top-off. It also comes back when the car is unplugged or driven after reaching 100 %, when the PV day is over below 100 %, on the next day, or when the feature is turned off.

Only the limit is changed: no plan, no cheap-tariff charging, so EVCC fills the car from solar surplus only (in `pv`/`smart` mode without a smart cost limit). If no day has enough sun, the status page shows the full charge as due – charge manually then.

The proxy needs to know which EVCC vehicle has which VIN. EVCC's API hides the VIN without an admin login, but EVCC's database has it – mount EVCC's data directory **read-only** and the mapping is there right away:

```yaml
  vrm-ev-proxy:
    volumes:
      - ./vrm-ev-proxy/config:/config
      - ./evcc/db:/evcc:ro          # EVCC's /root/.evcc – read-only
```

The default path is `/evcc/evcc.db` (setting **EVCC database**). Without the mount, a car is learned the first time EVCC shows it on a loadpoint (matched by range and SoC).

> **Note:** EVCC may send commands like `wake_up`, `charge_start`, or `set_charging_amps` to the proxy. These are accepted and acknowledged, but not forwarded to the vehicle – all data is sourced from VRM. If you need actual charge control, a separate [TeslaBleHttpProxy](https://github.com/wimaha/TeslaBleHttpProxy) instance is required.

---

## Battery types

Supports **LFP** and **NMC** – configurable in settings.

**LFP** – optimal range 10–80%, BMS balancing reminder, reference marker in SoC bar

**NMC** – optimal range 20–90%, weekly tracking of time above 80%

Both: color-coded SoC bar, 7-day history chart, charge cycle counter

---

## Troubleshooting

**No data showing** → check VRM Token and Site ID in `/settings`

**Wrong times / dates on the status page** → set the `TZ` environment variable, e.g. `TZ=Europe/Berlin` (default in docker-compose)

**`No EV device found`** → EV is not configured as a device in VRM

**EVCC shows errors** → URL must be `http://<server-ip>` without port, port goes into the separate `port` field (otherwise EVCC calls `http://<ip>:8080:8080`)

**Wrong vehicle / multiple EVs** → the VIN in EVCC must match the VIN shown on the status page; unknown VINs fall back to the first vehicle (logged once)

---

## License

MIT
