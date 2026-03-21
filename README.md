# vrm-ev-proxy

Connects your EV from **Victron VRM** to your local **EVCC** instance.

Your Victron Cerbo GX already reads the EV and syncs the data to VRM. This proxy fetches that data and makes it available to EVCC as a local vehicle endpoint – with a built-in status page and settings UI.

```
Cerbo GX  ◄──►  EV
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

## Install

**Requires:** Docker + Docker Compose

```bash
git clone https://github.com/fl0wb0b/vrm-ev-proxy.git
cd vrm-ev-proxy
docker compose up -d
```

Then open **`http://<your-server-ip>:8080/settings`** in your browser and enter your VRM credentials. That's it.

---

## Configuration

Everything is configured in the browser at `/settings` – no config files, no terminal.

| Setting | Where to find |
|---------|--------------|
| **VRM Token** | VRM Portal → avatar top right → Settings → API Tokens → Generate |
| **VRM Site ID** | The number in your VRM URL: `vrm.victronenergy.com/installation/`**`12345`**`/dashboard` |

---

## Web UI

| URL | What you get |
|-----|-------------|
| `:8080/` | Live status: SoC, range, charging state, 7-day chart |
| `:8080/settings` | All settings – change anytime without restart |
| `:8080/api` | Live API responses |

---

## EVCC setup

Add a vehicle in EVCC pointing to this proxy:
- **URL:** `http://<server-ip>:8080`
- **VIN:** your vehicle VIN

---

## Battery types

Supports **LFP** and **NMC** – configurable in settings.

**LFP** – optimal range 10–80%, BMS balancing reminder, reference marker in SoC bar

**NMC** – optimal range 20–90%, weekly tracking of time above 80%

Both: color-coded SoC bar, 7-day history chart, charge cycle counter

---

## Troubleshooting

**No data showing** → check VRM Token and Site ID in `/settings`

**`No EV device found`** → EV is not configured as a device in VRM

**EVCC shows errors** → verify the vehicle URL in EVCC points to `http://<server-ip>:8080`

---

## License

MIT
