# Bug-Liste vrm-ev-proxy

Quellen für die Prüfung:
- EVCC-Template `templates/definition/vehicle/tesla-ble.yaml` (evcc-io/evcc, Stand 23.09.2026)
- Victron D-Bus-Doku `com.victronenergy.ev` (victronenergy/venus Wiki, `dbus.md`, Stand 06.09.2026)

Status: ✅ behoben · ⚠️ teilweise / mit Annahme · ⏭️ bewusst nicht geändert

## EVCC-Kompatibilität

| # | Fehler | Auswirkung | Status |
|---|--------|-----------|--------|
| E1 | `charge_energy_added` fehlte im `charge_state` | EVCC-jq liefert `null` → „`<nil>`“ lässt sich nicht als Zahl parsen → Fehler bei jedem Zyklus | ✅ 9e98921 |
| E2 | `minutes_to_full_charge` fehlte | keine Ladeende-Prognose in EVCC | ✅ 9e98921 |
| E3 | README: URL `http://ip:8080` angegeben, Template hängt aber selbst `:{{port}}` an | EVCC ruft `http://ip:8080:8080` auf → nichts funktioniert | ✅ 9e98921 |
| E4 | VIN-Vergleich case-sensitive, unbekannte VIN wird stumm auf das 1. Fahrzeug umgeleitet | falsches Auto bei mehreren EVs, kein Hinweis | ✅ 9e98921 |

## Victron-Datenmodell (`com.victronenergy.ev`)

| # | Fehler | Auswirkung | Status |
|---|--------|-----------|--------|
| V1 | `CHARGING_STATE_MAP` nutzt Codes 2/4/5/6, die Victron nicht dokumentiert. Die offiziellen Codes 244 (Sustain), 245 (Wake up), 250 (Blocked), 256 (Discharging), 257 (Scheduled charging) fehlen | Auto erscheint in EVCC als „Disconnected“ (Status A), obwohl es eingesteckt ist | ⚠️ siehe unten |
| V2 | „Last EV contact“ liest `/LastEvContact`. Laut Victron heißt der Pfad `/LastUpdated/EvContact` | Status-Seite zeigt immer „–“ | ✅ |
| V3 | VIN-Pfad: `/Serial` wird vor `/VIN` gelesen, dokumentiert ist nur `/VIN` | ggf. falsche Kennung, falls beide gesetzt | ✅ |
| V4 | `/BatteryCapacity` (kWh) liefert Victron mit, wird aber ignoriert | Zyklen und Ladeende-Prognose brauchen manuell gepflegte Kapazität | ✅ Fallback, wenn in `/settings` nichts eingetragen ist |
| V5 | Nur `/Ac/Power` wird gelesen, `/Dc/Power` nicht | beim DC-Laden 0 W, keine Energie, keine Prognose | ✅ |
| V6 | `/Ac/Energy/Forward` (Zählerstand kWh) wird ignoriert | Sitzungsenergie nur geschätzt (Leistung × Zeit bei 60-s-Polling) | ✅ Zählerdifferenz bevorzugt, Integration als Fallback |
| V7 | `/AtSite = 0` (Auto nicht vor Ort) wird ignoriert | Auto unterwegs kann als „verbunden“ erscheinen | ✅ erzwingt „Disconnected“ |
| V8 | Fahrzeugname nur aus `/CustomName` (nicht dokumentiert) | Anzeige der VIN statt „Brand Model“ | ✅ Fallback `/Brand` + `/Model` |
| V9 | Tesla über VRM liefert keine Leistung (`Power=0.0W` bei `ChargingState 3`, im Live-Log bestätigt) | `charge_energy_added` und `minutes_to_full_charge` immer 0 | ✅ Werte kommen von der Ladestation (EVCS), auf die `Mgmt/Connection` zeigt: `/Ac/Power`, `/Session/Energy`, `/SetCurrent` |
| V10 | Fahrzeugstatus kommt aus der Hersteller-API, die bei schlafendem Auto veraltet sein kann | EVCC sieht z.B. „Charging“, obwohl die Ladestation längst fertig ist | ✅ Live-`/Status` der Ladestation hat Vorrang (0 → Disconnected, 2 → Charging, 3 → Complete, sonst Stopped) |

**Annahme bei V1:** Victron fasst „eingesteckt, lädt nicht“ und „nicht eingesteckt“ im Code `0` zusammen. Unterscheiden lässt sich das nur über `Mgmt/Connection` (EVCS, an der das Auto hängt). Mapping jetzt:

| VRM-Code | Bedeutung (Victron) | → Tesla `charging_state` | EVCC-Status |
|---|---|---|---|
| 0, 1 | Not charging / Low power mode | `Stopped`, wenn `Mgmt/Connection` gesetzt, sonst `Disconnected` | B / A |
| 3 | Charging | `Charging` | C |
| 244 | Sustain (Ziel erreicht, hält) | `Complete` | B |
| 245, 250, 256, 257 | Wake up / Blocked / Discharging / Scheduled | `Stopped` | B |
| 255 | Unavailable | `Disconnected` | A |
| 2, 4, 5, 6 | undokumentiert (Altbestand) | wie bisher: 4 → `Complete`, sonst `Stopped` | B |

Live-Log bestätigt: VRM liefert `Mgmt/Connection = 40` beim eingesteckten Tesla. Rohwerte: Log `[VRM] OK … raw=` bzw. `GET /api/raw`.

## Robustheit

| # | Fehler | Auswirkung | Status |
|---|--------|-----------|--------|
| R1 | Ungültiges `POLL_INTERVAL` → `int()` wirft außerhalb des `try` | Abfrage-Thread stirbt, Daten frieren still ein | ✅ 9e98921 |
| R2 | `rawValue: null` → `float(None)` | komplette Abfrage schlägt fehl | ✅ 9e98921 |
| R3 | `settings.json` nicht atomar geschrieben | Absturz beim Schreiben → Datei leer → alle Einstellungen + Verlauf weg | ✅ 9e98921 |
| R4 | Poller und Settings-Formular schreiben ohne Sperre | gespeicherte Einstellungen werden vom Poller überschrieben | ✅ 9e98921 |
| R5 | Keine Validierung im Settings-Formular | Unsinnswerte landen in der Config | ✅ 9e98921 |
| R6 | „Zeit über Optimum“ zählt nominales Intervall statt realer Zeit | Wert verfälscht bei Fehlern und Wartezeiten | ✅ 9e98921 |
| R7 | Countdown bei Fehler zeigt feste Wartezeit statt Restzeit | Seite lädt zu spät/zu früh neu | ✅ |
| R8 | Sitzungsenergie nur im RAM | nach Neustart beginnt `charge_energy_added` bei 0 | ✅ durch V9 erledigt (`/Session/Energy` der Ladestation) |
| R9 | Veraltete Daten: bei VRM-Ausfall liefert der Proxy EVCC stundenlang den letzten SoC | EVCC plant mit falschem SoC | ✅ nach max(30 min, 10 × Intervall) ohne erfolgreiche Abfrage → HTTP 503, Health-Status `stale` |
| R10 | Ladezyklen zählen jeden SoC-Anstieg, auch Schwankungen eines geparkten Autos | Zyklenzähler zu hoch | ✅ nur noch Anstiege im eingesteckten Zustand |
| R11 | Sticky VIN nur im RAM | nach Neustart ohne VIN landen Verlauf/Statistik unter `EV_0` | ✅ letzte echte VIN pro Instanz in `settings.json` |
| R12 | Healthcheck las nur `PORT` aus `.env` und galt bei VRM-Fehlern als fehlgeschlagen | `unhealthy`, obwohl der Proxy läuft | ✅ `app.py --healthcheck` liest auch `settings.json`, prüft nur Erreichbarkeit; zusätzlich `HEALTHCHECK` im Dockerfile |
| R13 | SoC wurde seit v2.2 kaufmännisch gerundet statt abgeschnitten (VRM liefert z.B. `99.6`) | EVCC zeigt 100 %, Tesla/VRM 99 %; EVCC hält das Ladeziel für erreicht, Vollladung wird zu früh als erfolgt gespeichert | ✅ v2.4.1 schneidet ab wie die Tesla-App |
| R14 | „Letzte Vollladung“ wurde bei jeder Abfrage neu gesetzt, solange das Auto mit 100 % eingesteckt war; der Backfill nahm den letzten statt den ersten 100-%-Eintrag | gestern voll geladen → heute „Heute“ | ✅ v2.4.2: nur der Moment des Erreichens zählt, Backfill repariert falsche Werte beim Start |
| R15 | „Heute/Gestern“ = weniger als 24/48 h statt Kalendertag; Container lief ohne `TZ` in UTC | 23:00 geladen → am Folgemorgen „Heute“, Datumsangaben 2 h verschoben | ✅ v2.4.2: Kalendertage, `TZ=Europe/Berlin` in docker-compose |

## Sicherheit

| # | Fehler | Auswirkung | Status |
|---|--------|-----------|--------|
| S1 | Fahrzeugname, Token, Site-ID, Fehlertexte unescaped im HTML | XSS | ✅ 9e98921 |
| S2 | `/settings` liefert den vollständigen VRM-Token im HTML aus („show“) | jeder im LAN kann den Token ohne Login auslesen | ✅ nur noch maskiert |
| S3 | Container läuft als root | größere Angriffsfläche | ⏭️ Änderung bräche Rechte auf bestehendem `/config`-Volume |

## HTTP / Betrieb

| # | Fehler | Auswirkung | Status |
|---|--------|-----------|--------|
| H1 | Single-threaded `HTTPServer` | ein langsamer Client blockiert EVCC | ✅ 9e98921 |
| H2 | Kein `Content-Length`, Body-Größe unbegrenzt | unnötige Verbindungsabbrüche, Speicher | ✅ 9e98921 |
| H3 | Jeder `vehicle_data`-Poll wird geloggt | Log-Spam (EVCC fragt pro Feld einzeln) | ✅ 9e98921 |
| H4 | CI baut nur auf `main` | Build-Fehler fallen erst nach dem Merge auf | ✅ Build ohne Push bei Pull Requests |
