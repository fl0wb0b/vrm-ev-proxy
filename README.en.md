# vrm-ev-proxy

[English](README.en.md) · [Deutsch](README.md)

Brings vehicle status from Victron VRM into evcc – for car brands VRM supports (so far tested with Teslas only, see "Tested with") – and additionally automates periodic full charges for LFP batteries when the solar forecast allows it.

## Why this exists

**The starting point:** evcc needs the car's state of charge and range to charge sensibly. Anyone with a Victron system usually already has the car in VRM: VRM handles the integration of the individual manufacturers. Instead of connecting the car a second time directly in evcc, this proxy passes the VRM data on to evcc in Tesla format. No second connection to the vehicle is created, and the proxy is not tied to one brand. That is the core of the project.

**The add-on:** the full-charge automation sits on top of this bridge. "When did I last charge to 100 %?" should not be another thing to remember.

This was built by an owner of LFP electric cars using Victron VRM and evcc. LFP batteries need periodic full charges for BMS cell balancing and SoC calibration: their flat voltage curve makes the BMS depend on counting energy, and that estimate drifts. The code's UI hint gives a rough estimate of **3–5 % in 3–4 weeks or 100–150 kWh**; this is not a measurement of your battery. Everyday charging usually stops around 80 % to reduce time at high SoC.

Doing this manually means remembering the last full charge, finding a sunny day, raising the evcc limit to 100 %, and remembering to put it back. The proxy handles that sequence: record 100 % per vehicle, decide when another full charge is due, wait for a suitable solar forecast, temporarily raise the evcc limit, and restore the daily value after charging finishes.

The goal is **zero-touch after setup**: plug in as usual; evcc handles surplus charging, and the proxy handles when to allow a full charge. The operator uses **14 days or 190 kWh for a 60 kWh LFP battery**. These are adjustable choices, not defaults or a universal battery recommendation. Enough sunshine and the right evcc setup remain prerequisites. Both thresholds at 0 disable full-charge automation; vehicle detection requests and evcc-based status information remain active with `EVCC_URL` configured.

## Screenshots

All screenshots use **invented demo data**: `VF1DEMO…` VINs, site ID `123456`, and a demo token. They show the German UI; English is also available.

![Desktop status with two demo vehicles, charging information and always-visible vehicle details](docs/screenshots/status-desktop.png)

*Desktop: two vehicles with SoC, range, target, estimated finish time and solar share; vehicle details are always visible.*

![Always-visible vehicle details with odometer, vehicle contact, weekly energy, SoC history and automation messages](docs/screenshots/status-details.png)

*Details of both vehicles: odometer, last vehicle contact, weekly energy, SoC history and automation messages are always visible.*

<img src="docs/screenshots/status-mobile.png" alt="Mobile status with two stacked demo vehicle cards, charging information and always-visible details" width="360">

*Mobile: both cards are stacked with always-visible details; the charging target and solar share stay readable on a phone.*

<img src="docs/screenshots/settings.png" alt="Settings with demo VRM credentials, global battery profile, evcc integration with controlled loadpoints and vehicles with picture, evcc mapping, battery type and capacity per VIN" width="440">

*Settings: VRM connection, global battery profile, polling, evcc integration with controlled loadpoints and full-charge thresholds; then vehicles with picture, evcc mapping, battery type and capacity per VIN.*

## Features

- **Periodic full charges without a calendar:** days **or** charged kWh make a vehicle due, so a frequently driven car can qualify earlier.
- **Vehicle detection after a quick swap:** swapping two cars between evcc polls can leave the old vehicle on the loadpoint. The proxy requests detection again while charging when it can identify the mismatch.
- **Vehicle status from VRM into evcc, brand-independent:** VRM integrates the manufacturers; the proxy serves their data in Tesla format so evcc fetches it through the `tesla-ble` template. No second connection to the vehicle is created.
- **Status for a phone or wall tablet:** partial refreshes update only changed page sections; detail values update live too. Charging animations and the open Bridge system section are preserved. A full reload happens after 30 minutes or an unexpected page structure change.
- **Answers while charging:** “Target 80 % · at HH:MM” (`Ladeziel 80 % · ca. HH:MM` in German) and the session's solar share answer when charging should finish and how solar-powered it is. ETA uses current power and is an estimate. The seven-day kWh/solar row comes from evcc sessions.
- **Honest freshness:** “VRM poll X s ago” or “Data is stale” describes the VRM fetch. The separate last vehicle contact can be older. A successful VRM fetch does not mean the car was just contacted.
- **Battery context and pictures:** LFP/NMC profiles, a daily SoC band, a warning above it, hourly history, estimated charge cycles and time above the configured maximum. The flowing charging bar resembles the car display; pictures help distinguish vehicles.

## How the full charge works

1. **Record actual reported 100 %.** The proxy stores the first observed transition to 100 % per VIN, wherever charging happened. Sitting at 100 % does not move the date forward. SoC is truncated: 99.6 % remains 99 %. At startup, the proxy can fill in or correct the last full-charge timestamp from stored hourly SoC history, especially if older versions kept moving it forward during a 100 % phase.
2. **Become due by time or energy.** `FULL_CHARGE_DAYS` counts calendar days from the last full charge; `FULL_CHARGE_KWH` counts charged energy since then. Either threshold is enough. Energy comes from evcc sessions plus observed SoC gains × capacity while the car reports charging away from the site. This accounts for use as well as time. With no recorded full charge, the first evaluation provides the time reference.
3. **Wait for a suitable solar day.** At most every five minutes, the proxy checks evcc's forecast and a connected loadpoint with the mapped vehicle. It subtracts house base load (default 1500 W), caps surplus at loadpoint power and trusts 80 % of the remainder. The missing battery energy is calculated with 90 % charging efficiency. The remaining forecast must cover it, and the **current forecast slot** must exceed base load. This daytime check does not measure sunshine or actual PV power.
4. **Raise only the evcc vehicle limit.** The proxy remembers its previous limit and sets it to 100 %. It creates no plan and changes no charging mode, tariff setting or current. **Configure evcc for surplus charging in `pv` mode**, without plans or smart-cost settings that permit grid charging; evcc remains responsible for the energy source. The limit inside the car must already allow 100 %.
5. **Let the car finish balancing.** Some cars report 100 % before topping off has finished (Tesla, for example). The proxy waits until VRM reports 100 % and no vehicle charging for **30 minutes**; a charging report resets this timer. It then restores the saved value to the evcc vehicle **and**, if still connected, its loadpoint. Both writes matter: evcc otherwise does not pass a reduced vehicle limit to an already connected loadpoint.
6. **End the attempt when appropriate.** The saved limit also returns when the feature is disabled, on a new day, or below 100 % when forecast surplus remaining today falls below 0.5 kWh. After 100 % has been recorded during the attempt, unplugging or driving also ends it. **Unplugging before 100 % does not immediately cancel it; a passing cloud does not either.** An unfinished full charge remains due and can be attempted again on a suitable sunny day.

Implemented in [`poll_vrm` and `_full_charge_pv`](app.py). Restoration saves an evcc vehicle limit of **1–99 %**; it does not save a separate previous loadpoint limit. Set a daily vehicle limit such as 80 % before enabling the feature.

### The three limit levels

| Limit | Purpose | Proxy behavior |
|---|---|---|
| Inside the car | The car's own upper limit | Never changed; must allow 100 % for a full charge. |
| Vehicle in evcc | Daily value, e.g. 80 % | Temporarily set to 100 %; the saved daily value is restored. |
| Loadpoint in evcc | Where evcc actually stops charging | Explicitly restored to the saved **vehicle** value while that vehicle is still connected. |

### Hands-off checklist

Do this once; after that nobody has to ask "when did I last charge to 100 %?" again.

- [ ] In the car: set the charge limit so it allows **100 %**. The proxy never touches it.
- [ ] In evcc: set the **daily vehicle limit** (e.g. 80 %) and run the loadpoint in **`pv` mode** without plans or smart-cost grid charging.
- [ ] In evcc: enable the solar forecast; the proxy relies on it.
- [ ] In the proxy settings: set `FULL_CHARGE_DAYS` and `FULL_CHARGE_KWH` (either one makes a full charge due) and the evcc URL.
- [ ] Check the status page: each car should show its last full charge and when the next one is due under “Details & history”.

### Where you see it

- **Status page, vehicle card:** a running full charge appears directly below the SoC: "Full charge from PV running since … – EVCC limit 100 %." When idle, the last full charge also appears at the top.
- **Status page, Details & history:** last full charge as “Today”, “Yesterday” or an older date, plus upcoming or prevented full charges: "Next full charge from PV due: …", "Full charge due – not enough PV today (forecast … kWh, needed … kWh).", "Full charge: this car is on a loadpoint the proxy is not set to control." or "Full charge due – EVCC provides no solar forecast …".
- **Log:** activation, restoration and evcc call errors are logged; the status page shows the current reason for waiting. Examples:

```text
[FULL] <car>: last 100 % 15 days / 120 kWh ago, PV today 38.2 kWh ≥ 14.9 kWh needed – EVCC limit 80 → 100 %
[FULL] <car>: 100 % and charging finished for 30 min – EVCC limit back to 80 %
```

## What the proxy does in evcc

Requires `EVCC_URL`, e.g. `http://evcc.example:7070`. Loadpoint numbers `<n>` are one-based; `<name>` is evcc's vehicle identifier.

| Action | Endpoint | When |
|---|---|---|
| Read state | `GET /api/state` | Detection checks during charging; full-charge evaluation; status display (20 s cache for display). Also when rendering the settings page, including after saving or validation errors: scan vehicles and loadpoints without the status cache; scan errors appear there. |
| Read charging sessions | `GET /api/sessions` | Weekly statistics and the kWh threshold; cached for 15 minutes. |
| Request vehicle detection | `PATCH /api/loadpoints/<n>/vehicle` | Selected charging loadpoint shows a uniquely matched vehicle now disconnected here, or no vehicle with matching charging power; no detection already running and at least one VRM vehicle in the charging state; at most once per five minutes per loadpoint. |
| Allow a full charge | `POST /api/vehicles/<name>/limitsoc/100` | Vehicle due, SoC below 100 %, connected to a selected loadpoint, and forecast checks pass; POST only if the evcc vehicle limit is not already 100 %. |
| Restore vehicle limit | `POST /api/vehicles/<name>/limitsoc/<old>` | Completion or end conditions above, if a daily limit was saved. |
| Restore loadpoint limit | `POST /api/loadpoints/<n>/limitsoc/<old>` | Same restoration, if the mapped vehicle is still connected there. |

Vehicle matching uses range, then SoC if necessary; ambiguous matches are skipped. VRM may retain an old car's station connection: station data goes to the vehicle reporting activity, otherwise the most recently contacted candidate. Other candidates do not inherit those station values; their own VRM charging state is still evaluated.

## How the proxy finds your evcc vehicle

To change the limit on any installation, the proxy needs the **vehicle name in evcc** (call `POST /api/vehicles/<name>/limitsoc/…`). It finds it on its own:

1. **evcc address:** `EVCC_URL` (in `.env` or on the settings page).
2. **Mapping VIN → evcc vehicle:**
   - **By learning:** during full-charge evaluation, when at least one threshold is active or a saved attempt is being handled, the proxy matches the evcc car to the VRM car by range and, if needed, state of charge and remembers it. Status requests and settings scans do not learn mappings. Ambiguous matches are skipped. With automation off and no existing mapping, or with ambiguous matches, select the evcc vehicle manually (point 4).
3. **Loadpoint:** the proxy reads its limit from evcc's state and also resets it when restoring.

4. **By hand, when detection is not enough:** in the **evcc integration** section, **Controlled loadpoints** selects from the scan which wallboxes the proxy may start full charges on and re-trigger vehicle detection for. The default is all of them. **Vehicles** follows with one row per VIN: picture/model, **vehicle in EVCC** (the list comes from a scan of evcc), **battery type** and **battery capacity**. Manual evcc mapping takes precedence over learned mapping; for battery type, "Automatic" uses the global profile.

The proxy never changes the limit inside the car. If evcc requires a login, the connection does not work: the proxy sends no evcc credentials.

## Quick start

Requires Docker, Docker Compose, a VRM installation exposing an Electric Vehicle device, and network access to VRM. The supplied Compose file uses **host networking**. The Dockerfile runs Python 3.12 with standard-library dependencies only.

From a checkout of this repository:

```sh
cp .env.example .env
# Edit .env: replace the example credentials, or leave both empty for browser setup.
docker compose up -d --build
```

Example `.env` (replace the invented credentials, or clear both VRM values):

```dotenv
VRM_TOKEN=REPLACE_WITH_VRM_API_TOKEN
VRM_SITE_ID=123456
POLL_INTERVAL=60
PORT=8080
TZ=Europe/Berlin
# evcc address, optional (empty = evcc features off); can also be set in the settings page
EVCC_URL=http://evcc.example:7070
```

Open `http://proxy.example:8080/settings`. With both VRM values empty, `/` redirects there for first-run setup. Settings and history persist in the Compose volume mounted at `/config`.

Add a vehicle in evcc; repeat with each vehicle's actual VIN. The `tesla-ble` template is only the technical way to reach the proxy and is not limited to Tesla; for other brands it depends on what VRM delivers (see "Tested with"). The values below are placeholders:

```yaml
vehicles:
  - name: demo_ev
    type: template
    template: tesla-ble
    title: Demo EV
    vin: VF1DEMO00000000001
    url: http://proxy.example
    port: 8080
    capacity: 60
```

For `tesla-ble`, **leave the port out of `url`**: the template appends the separate `port`. This interface supplies data; vehicle commands are acknowledged as no-ops.

In the proxy's settings, enter `EVCC_URL` as `http://evcc.example:7070`, choose appropriate thresholds, and set the daily vehicle limit in evcc. Alternatively these values can be written into the Compose service (or a Compose override):

```yaml
services:
  vrm-ev-proxy:
    environment:
      EVCC_URL: http://evcc.example:7070
      FULL_CHARGE_DAYS: "14"
      FULL_CHARGE_KWH: "190"
    volumes:
      - config:/config
```

For a source update, update the checkout and run `docker compose up -d --build`. For a published-image update, run `docker compose pull`, then `docker compose up -d --no-build`.

## Configuration

Use `/settings` for VRM credentials, global battery profile, polling, evcc integration with controlled loadpoints, vehicles and language. Obtain the token in the VRM portal's API-token settings; the site ID is the number in `/installation/123456/dashboard`. Leaving the token field empty keeps the existing token.

Nonempty values saved in `/config/settings.json` take precedence over environment variables. Most changes take effect on the next poll or page request; status-page detail values update live. **Changing `PORT` requires a restart.** Empty token, site ID and other global numeric fields keep the existing value. Clearing `CAPACITY` or `EVCC_URL` removes the saved override; an existing environment value then applies again. Settings apply globally unless explicitly per vehicle (picture, evcc mapping, battery type, capacity and tracking).

| Variable | Default / meaning |
|---|---|
| `VRM_TOKEN` | Required VRM API token. |
| `VRM_SITE_ID` | Required numeric installation ID. |
| `POLL_INTERVAL` | `60` seconds; UI range 10–3600. |
| `PORT` | `8080`; restart after changing it. |
| `TZ` | `Europe/Berlin` in Compose; container timezone, not a UI setting. |
| `BATTERY_TYPE` | `LFP`; alternative `NMC`. Global display/tracking profile; can be overridden per vehicle. |
| `CAPACITY` | Global override 1–200 kWh. Order: per vehicle > `CAPACITY` > VRM `/BatteryCapacity`; further fallbacks only for full-charge planning and status ETA, see below. |
| `OPT_MIN`, `OPT_MAX` | LFP 10–80 %, NMC 20–90 %; configurable daily display band. |
| `FULL_REMINDER_DAYS` | `28` for LFP; UI reminder only, separate from automation. |
| `FULL_CHARGE_DAYS` | `0` (off); 0–60 calendar days. |
| `FULL_CHARGE_KWH` | `0` (off); 0–2000 charged kWh. Either enabled threshold makes a vehicle due. |
| `FULL_CHARGE_BASE_LOAD` | `1500` W; subtracted from the solar forecast, configurable 0–20000 W. |
| `EVCC_URL` | Empty: evcc integration off. Example: `http://evcc.example:7070`. |
| `EVCC_LOADPOINTS` | Empty: all loadpoints. Otherwise one-based numbers, comma-separated without spaces, e.g. `1,3`. Restricts detection requests and starting full charges. The UI requires at least one loadpoint to stay selected. |
| `LANGUAGE` | `auto` (browser preference), `en` or `de`; UI only. |

The supplied Compose file passes VRM credentials, polling interval, port, `EVCC_URL` and timezone. `EVCC_URL` can be set directly in `.env`; add other variables such as `EVCC_LOADPOINTS` under `environment` or set them on the settings page. Full-charge planning and status ETA use: per vehicle > `CAPACITY` > VRM > evcc vehicle > 60 kWh. API ETA, cycles and away energy use only the first three sources, so supply an accurate capacity.

To disable full-charge automation, set **both thresholds to 0**. During an active run, keep `EVCC_URL` reachable until the saved limit has been restored. The automation is not gated by `BATTERY_TYPE`; choosing NMC alone does not disable it.

## Vehicle pictures

In the **Vehicles** section of the settings each car offers automatic selection, a specific model, no picture, or an **own HTTP(S) image URL**; an own URL takes precedence.

Tesla renderings are loaded **by the browser** from [github.com/teslamotors/custom-wraps](https://github.com/teslamotors/custom-wraps) via GitHub's raw-content host. They are **not stored in this repository**. Automatic Tesla selection uses model name/VIN and model year; other Tesla variants can be chosen manually.

The bundled JPEGs in [`images/`](images/) are **AI-generated studio illustrations** for VW ID.4, Kia EV6, Hyundai Ioniq 5, Ford Mustang Mach-E, Mini Countryman, Porsche Macan and Volvo EX40/XC40. Local selection uses model-name fragments. The proxy serves them at `/img/<id>.jpg`; an unavailable external image is hidden.

## Endpoints

| Method | Path | Result |
|---|---|---|
| `GET` | `/`, `/status` | Status page; redirects to settings until VRM credentials exist. |
| `GET` / `POST` | `/settings` | Settings form / save settings. |
| `GET` | `/api/1/vehicles/<VIN>/vehicle_data` | Tesla-style `response.response.charge_state`; HTTP 503 without data or when too old. |
| `POST` | `/api/1/vehicles/<VIN>/command/<command>` | Acknowledged no-op, e.g. `wake_up`, `charge_start`, `set_charging_amps`. |
| `GET` | `/api/health` | JSON status `ok`, `error` or `stale`, age, error, site ID and version; HTTP 503 for error/stale. |
| `GET` | `/api/raw` | Cached raw VRM values per vehicle, including linked charging-station values. |
| `GET` | `/img/<id>.jpg` | Bundled model image, if the ID is supported. |

`charge_state` includes `battery_level`, `usable_battery_level`, `battery_range` (**miles**), `charge_limit_soc` (the car's VRM-reported limit), `charge_amps`, `charge_current_request`, `charging_state`, `charger_power`, `charge_energy_added` (kWh), `minutes_to_full_charge`, `time_to_full_charge` (hours) and `timestamp`. API time estimates use the car limit; the status page instead prefers evcc's effective limit for target/ETA. Session energy uses station `/Session/Energy`, then vehicle `/Ac/Energy/Forward` differences, then power integration.

The UI warns after `max(180 s, 3 × POLL_INTERVAL)` without a successful VRM fetch. Vehicle data and health become stale after `max(1800 s, 10 × POLL_INTERVAL)`. Successful vehicle responses and HTTP 503 responses due to stale data include `X-Data-Age-Seconds`; without existing vehicle data, the header is absent. Docker's healthcheck tests HTTP-server liveness and accepts a 503 response; “healthy” does not guarantee fresh VRM data.

## Troubleshooting

- **No data:** check token and site ID in `/settings`, then `docker compose logs --tail=100 vrm-ev-proxy` and `/api/health`.
- **`No EV device found`:** the installation must expose an Electric Vehicle device in VRM; the proxy does not create one.
- **Wrong dates/times:** set `TZ` to the required timezone in Compose and recreate the container.
- **evcc connection errors:** use `url: http://proxy.example` and `port: 8080`, not a URL with the port repeated.
- **Wrong vehicle:** match the VIN shown by the proxy. Matching is case-insensitive; unknown VINs fall back to the first vehicle. The warning is logged once per unknown VIN when multiple vehicles exist. Detection requests skip ambiguous matches.
- **No evcc statistics or automation:** If `EVCC_URL` is set but wrong (unreachable, wrong port or path, login required, not an evcc answer), the status page shows a red error with the reason at the top. Also check: check `EVCC_URL`, reachability, VIN mapping (settings → Vehicles → Vehicle in EVCC). The client sends no evcc login credentials; an API requiring authentication fails. Look for `[EVCC]`, `[LIVE]` or `[FULL]` errors in the log.
- **Full charge stays due:** check vehicle mapping, connection, selected loadpoint and sufficient evcc solar forecast. Missing mapping, excluded loadpoint and missing forecast are shown separately under “Details & history”. Winter can mean a long wait. Verify that the car itself allows 100 % and evcc uses PV surplus charging.
- **Limit not restored:** allow the 30-minute hold after charging stops, check evcc API errors, and keep `EVCC_URL` configured. Only a saved daily vehicle limit of 1–99 % can be restored.

Further implementation notes: [BUGS.md](BUGS.md).

## Tested with – and what is only assumed beyond that

Developed and tested on **one** installation: a Victron EV Charging Station, two Teslas through VRM, evcc with vehicles created in its UI, an active solar forecast, no evcc login. That it runs there does not mean it runs everywhere. Not verified:

- **Other brands in VRM.** The proxy reads VRM's "Electric Vehicle" device (`/Soc`, range, `/ChargingState`, optionally `/VIN`, `/BatteryCapacity`). If VRM supplies different or fewer fields for a brand, values or the mapping are missing. Without a vehicle identifier, the proxy uses a substitute id. Mapping to the evcc vehicle uses range and, if needed, state of charge during learning, or manual selection in the settings.
- **Other wallboxes.** The station logic (`/Mgmt/Connection`, status, session energy) applies to Victron stations only. The effective charging target can come from evcc. Charging power and the ETA calculated from it need usable VRM power values or voltage, current and phases.
- **Vehicles from `evcc.yaml`.** For file-based vehicles, learning helps (it requires the evcc vehicle to take its range and state of charge **from the proxy**, template `tesla-ble`), or choose the vehicle **by hand** per car in the settings.
- **Other evcc versions.** Needed: `/api/state` with loadpoints, vehicle limit, the solar-forecast time series and the write calls under `/api/vehicles/<name>/limitsoc/…`. No minimum version has been determined.
- **Identical vehicle titles.** evcc sessions are matched by title; two cars with the same title mix their kWh.
- **Battery capacity and type.** Full-charge planning and status ETA use: per vehicle > `CAPACITY` > VRM > evcc vehicle > 60 kWh; API ETA, cycles and away energy use only the first three sources. Enter it per car when sizes differ. Set the battery type (LFP/NMC) per vehicle or use the global setting; cell chemistry is not detected automatically. If a vehicle's type differs from the global type, its preset SoC limits apply.

Missing mapping, excluded loadpoint and missing solar forecast are shown separately. If evcc is entered wrongly, the status page shows an error.

## Limits

- **No control inside the car:** commands from evcc are no-ops. The proxy cannot wake the car, change its limit, start charging or set its current. Real vehicle control requires a separate integration.
- **No mode, plan, tariff or current changes:** raising a limit does not itself select solar power. Existing evcc settings determine whether grid power can be used; the proxy cannot guarantee a grid-free charge.
- **evcc is optional for VRM data, required for evcc features:** full-charge automation, detection requests, effective targets and solar/session statistics need `EVCC_URL`. evcc-based solar and weekly statistics need a manual or learned vehicle mapping; SoC history and battery tracking are independent of that mapping.
- **VRM data is polled, not live:** default interval 60 seconds, with backoff after errors. Vehicle contact can be older still. A 100 % report and a timer do not prove cell balancing is complete.
- **Sun-dependent full charges can wait:** an insufficient or missing forecast keeps the vehicle due. No grid fallback is scheduled.
- **Tracking is observational:** unobserved charging/SoC changes may be missed; away energy and cycle counts are estimates. evcc sessions are matched by vehicle title, and the seven-day row includes sessions started in that period.
- **No HTTP authentication:** anyone able to reach the server can view status/raw data and change settings. The full VRM token is not returned in the settings form.

## Credits and image notice

Uses vehicle data provided through Victron VRM and evcc's HTTP API; the Tesla-style data interface is compatible with evcc's `tesla-ble` template. Thanks to these projects and to the Tesla rendering source linked above.

Product names, trademarks and manufacturer logos belong to their respective owners. This project claims no rights to manufacturer logos and implies no manufacturer affiliation or endorsement. Vehicle pictures are illustrative; AI-generated images may differ from the real models.

## License

MIT. The repository does not contain a separate `LICENSE` file yet.
