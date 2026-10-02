# vrm-ev-proxy

[English](README.en.md) · [Deutsch](README.md)

Bringt den Fahrzeugstatus aus Victron VRM in evcc – für Automarken, die VRM anbindet (bisher nur mit Teslas getestet, siehe „Getestet mit“) – und automatisiert zusätzlich regelmäßige Vollladungen für LFP-Akkus, wenn die Solarprognose es erlaubt.

## Warum es dieses Projekt gibt

**Der Ausgangspunkt:** evcc braucht den Ladestand und die Reichweite des Autos, um sinnvoll zu laden. Wer ein Victron-System hat, hat das Auto meist schon in VRM: Die Anbindung der einzelnen Hersteller übernimmt VRM. Statt das Auto ein zweites Mal direkt in evcc anzubinden, reicht dieser Proxy die VRM-Daten im Tesla-Format an evcc weiter. Es entsteht keine zweite Verbindung zum Fahrzeug, und der Proxy ist nicht an eine Marke gebunden. Das ist der Kern des Projekts.

**Die Zusatzfunktion:** Auf dieser Brücke sitzt die Vollladungs-Automatik. „Wann habe ich das letzte Mal auf 100 % geladen?“ soll keine weitere Frage sein, an die man denken muss.

Gebaut hat das ein Besitzer von LFP-Elektroautos mit Victron VRM und evcc. LFP-Akkus brauchen regelmäßige Vollladungen für das Balancing der Zellen durch das BMS und die Kalibrierung des Ladestands: Wegen der flachen Spannungskurve zählt das BMS Energie, und diese Schätzung driftet. Der UI-Hinweis im Code nennt grob **3–5 % in 3–4 Wochen oder 100–150 kWh**; das ist keine Messung des eigenen Akkus. Im Alltag lädt man meist nur bis etwa 80 %, um die Zeit bei hohem Ladestand zu begrenzen.

Von Hand bedeutet das: letzte Vollladung merken, einen sonnigen Tag erwischen, das evcc-Limit auf 100 % setzen und später wieder zurückstellen. Der Proxy übernimmt diese Folge: 100 % je Fahrzeug erfassen, die nächste Vollladung fällig stellen, eine passende Solarprognose abwarten, das evcc-Limit vorübergehend anheben und nach Ladeende den Alltagswert wiederherstellen.

Das Ziel ist **zero-touch nach der Einrichtung**: wie gewohnt anstecken; evcc übernimmt das Überschussladen und der Proxy den Zeitpunkt für die Vollladung. Der Betreiber nutzt **14 Tage oder 190 kWh für einen 60-kWh-LFP-Akku**. Das sind einstellbare Werte, keine Standardwerte oder allgemeine Akkuempfehlung. Genügend Sonne und eine passende evcc-Konfiguration bleiben Voraussetzung. Beide Schwellen auf 0 deaktivieren die Vollladeautomatik; Fahrzeug-Neuerkennung und evcc-basierte Statusinformationen bleiben bei eingerichteter `EVCC_URL` aktiv.

## Screenshots

Alle Screenshots verwenden **erfundene Demodaten**: VINs mit `VF1DEMO…`, Site-ID `123456` und einen Demo-Token. Sie zeigen die deutsche Oberfläche; Englisch ist ebenfalls verfügbar.

![Desktop-Status mit zwei Demoautos, Ladeinformationen und immer sichtbaren Fahrzeugdetails](docs/screenshots/status-desktop.png)

*Desktop: Zwei Fahrzeuge mit Ladestand, Reichweite, Ladeziel, geschätzter Endzeit und Solaranteil; die Fahrzeugdetails sind dauerhaft sichtbar.*

![Immer sichtbare Fahrzeugdetails mit Kilometerstand, Fahrzeugkontakt, Wochenenergie, SoC-Verlauf und Automatikhinweisen](docs/screenshots/status-details.png)

*Details der beiden Fahrzeuge: Kilometerstand, letzter Fahrzeugkontakt, Wochenenergie, SoC-Verlauf und Automatikhinweise bleiben immer sichtbar.*

<img src="docs/screenshots/status-mobile.png" alt="Mobiler Status mit zwei Demoautos untereinander, Ladeinformationen und dauerhaft sichtbaren Details" width="360">

*Mobil: Die beiden Karten stehen mit dauerhaft sichtbaren Details untereinander; Ladeziel und Solaranteil bleiben auf dem Handy gut lesbar.*

<img src="docs/screenshots/settings.png" alt="Einstellungen mit VRM-Demozugang, globalem Akkuprofil, evcc-Anbindung mit gesteuerten Ladepunkten und Fahrzeugen mit Bild, evcc-Zuordnung, Akkutyp und Kapazität je VIN" width="440">

*Einstellungen: VRM-Verbindung, globales Akkuprofil, Abfrageintervall, evcc-Anbindung mit gesteuerten Ladepunkten und Vollladeschwellen; danach Fahrzeuge mit Bild, evcc-Zuordnung, Akkutyp und Kapazität je VIN.*

## Funktionen

- **Regelmäßige Vollladungen ohne Kalender:** Tage **oder** geladene kWh machen ein Fahrzeug fällig; ein viel gefahrenes Auto kann dadurch früher an die Reihe kommen.
- **Fahrzeugerkennung nach schnellem Wechsel:** Tauscht man zwei Autos zwischen evcc-Abfragen, kann das alte am Ladepunkt stehen bleiben. Erkennt der Proxy die falsche Zuordnung während des Ladens, fordert er eine neue Erkennung an.
- **Fahrzeugstatus aus VRM in evcc, markenunabhängig:** VRM bindet die Hersteller an; der Proxy stellt deren Daten im Tesla-Format bereit, sodass evcc sie über das Template `tesla-ble` abruft. Es entsteht keine zweite Verbindung zum Fahrzeug.
- **Status für Handy oder Wandtablet:** Teilaktualisierungen ersetzen nur geänderte Seitenbereiche; auch die Detailwerte laufen live mit. Die Ladeanimation und der geöffnete Bridge-Systembereich bleiben erhalten. Nach 30 Minuten oder einer unerwarteten Strukturänderung wird vollständig neu geladen.
- **Antworten beim Laden:** „Ladeziel 80 % · ca. HH:MM“ und der Solaranteil der Sitzung zeigen, wann das Laden voraussichtlich fertig ist und wie viel davon aus Sonne stammt. Die Endzeit ist aus der aktuellen Leistung geschätzt. Die Wochenzeile mit kWh/Solar stammt aus evcc-Ladesitzungen.
- **Ehrliche Aktualität:** „VRM-Abruf vor X s“ oder „Daten veraltet“ bezieht sich auf den VRM-Abruf. Der separat angezeigte letzte Fahrzeugkontakt kann älter sein. Ein erfolgreicher VRM-Abruf bedeutet keinen gerade erfolgten Kontakt zum Auto.
- **Akkukontext und Bilder:** LFP/NMC-Profile, Alltagsbereich im SoC-Balken, Hinweis darüber, stündlicher Verlauf, geschätzte Ladezyklen und Zeit über dem eingestellten Maximum. Die fließende Ladeanimation ähnelt der Anzeige im Auto; Bilder helfen, Fahrzeuge zu unterscheiden.

## So funktioniert die Vollladung

1. **Gemeldete 100 % selbst erfassen.** Der Proxy speichert je VIN den ersten beobachteten Sprung auf 100 %, unabhängig vom Ladeort. Bleibt das Auto bei 100 % stehen, wandert das Datum nicht weiter. Der SoC wird abgeschnitten: 99,6 % bleiben 99 %. Beim Start kann der Proxy den Zeitpunkt der letzten Vollladung aus dem gespeicherten stündlichen SoC-Verlauf ergänzen oder korrigieren, insbesondere wenn ältere Versionen ihn während einer 100-%-Phase weitergeschoben haben.
2. **Nach Zeit oder Energie fällig werden.** `FULL_CHARGE_DAYS` zählt Kalendertage seit der letzten Vollladung; `FULL_CHARGE_KWH` zählt seitdem geladene Energie. Eine erreichte Schwelle genügt. Die Energie stammt aus evcc-Sitzungen plus beobachteten SoC-Anstiegen × Kapazität, während das Auto außerhalb der Anlage Laden meldet. So zählt neben der Zeit auch die Nutzung. Ohne erfasste Vollladung dient die erste Prüfung als Zeitreferenz.
3. **Einen passenden Sonnentag abwarten.** Höchstens alle fünf Minuten prüft der Proxy die evcc-Prognose und einen verbundenen Ladepunkt mit dem zugeordneten Fahrzeug. Er zieht die Hausgrundlast ab (Standard 1500 W), begrenzt den Überschuss auf die Ladepunktleistung und vertraut 80 % des Rests. Die fehlende Akkuenergie wird mit 90 % Ladeeffizienz berechnet. Die Restprognose muss sie decken; außerdem muss der **aktuelle Prognose-Zeitschritt** über der Grundlast liegen. Diese Tageslichtprüfung misst weder Sonnenschein noch tatsächliche PV-Leistung.
4. **Nur das evcc-Fahrzeuglimit anheben.** Der Proxy merkt sich das bisherige Limit und setzt es auf 100 %. Er erstellt keinen Plan und verändert weder Lademodus noch Tarifeinstellung oder Strom. **evcc muss für Überschussladen im Modus `pv` eingerichtet sein**, ohne Pläne oder Smart-Cost-Einstellungen, die Netzladen erlauben; die Energiequelle bestimmt weiterhin evcc. Das Limit im Auto muss 100 % bereits erlauben.
5. **Das Auto fertig balancieren lassen.** Manche Autos melden 100 %, bevor das Nachladen abgeschlossen ist (zum Beispiel Tesla). Der Proxy wartet, bis VRM 100 % und **30 Minuten** lang kein Laden des Fahrzeugs meldet; eine Lademeldung setzt den Timer zurück. Dann stellt er den gespeicherten Wert am evcc-Fahrzeug **und**, falls noch verbunden, am Ladepunkt wieder her. Beide Schreibzugriffe sind nötig: evcc reicht ein gesenktes Fahrzeuglimit sonst nicht an einen bereits verbundenen Ladepunkt weiter.
6. **Den Versuch passend beenden.** Das gespeicherte Limit kommt auch zurück, wenn die Funktion deaktiviert wird, ein neuer Tag beginnt oder unter 100 % weniger als 0,5 kWh prognostizierter Restüberschuss für heute übrig ist. Wurden während des Versuchs 100 % erfasst, beenden auch Abstecken oder Fahren den Versuch. **Abstecken vor 100 % bricht ihn nicht sofort ab; eine vorbeiziehende Wolke ebenfalls nicht.** Eine unvollständige Vollladung bleibt fällig und kann an einem passenden Sonnentag erneut versucht werden.

Implementiert in [`poll_vrm` und `_full_charge_pv`](app.py). Gespeichert wird ein evcc-Fahrzeuglimit von **1–99 %**, kein separates vorheriges Ladepunktlimit. Vor dem Aktivieren ein Alltagslimit wie 80 % am evcc-Fahrzeug einstellen.

### Die drei Limit-Ebenen

| Limit | Zweck | Verhalten des Proxys |
|---|---|---|
| Im Auto | Obergrenze des Fahrzeugs selbst | Nie verändert; muss für eine Vollladung 100 % erlauben. |
| Am Fahrzeug in evcc | Alltagswert, z. B. 80 % | Vorübergehend auf 100 % gesetzt; der gespeicherte Alltagswert kommt zurück. |
| Am Ladepunkt in evcc | Hier stoppt evcc das Laden tatsächlich | Wird ausdrücklich auf den gespeicherten **Fahrzeugwert** zurückgesetzt, solange dieses Auto dort verbunden ist. |

### Checkliste für den Betrieb ohne Handgriffe

Einmal einrichten; danach muss niemand mehr fragen „wann habe ich zuletzt auf 100 % geladen?“.

- [ ] Im Auto: Ladelimit so setzen, dass **100 %** erlaubt sind. Der Proxy ändert es nie.
- [ ] In evcc: **Tageslimit des Fahrzeugs** (z. B. 80 %) setzen und den Ladepunkt im **`pv`-Modus** ohne Pläne oder Netzladung über Smart-Cost betreiben.
- [ ] In evcc: die Solarprognose aktivieren; der Proxy verlässt sich darauf.
- [ ] In den Proxy-Einstellungen: `FULL_CHARGE_DAYS` und `FULL_CHARGE_KWH` (eines von beiden macht eine Vollladung fällig) sowie die evcc-URL eintragen.
- [ ] Statusseite prüfen: Jedes Auto zeigt unter „Details & Verlauf“ die letzte Vollladung und wann die nächste fällig ist.

### Wo man es sieht

- **Statusseite, Fahrzeugkarte:** Eine laufende Vollladung erscheint direkt unter dem SoC: „Vollladung mit PV läuft seit … – EVCC-Ladeziel 100 %.“ Bei Ruhe steht die letzte Vollladung zusätzlich oben.
- **Statusseite, Details & Verlauf:** Letzte Vollladung als „Heute“, „Gestern“ oder mit älterem Datum sowie anstehende oder verhinderte Vollladung: „Nächste Vollladung mit PV fällig: …“, „Vollladung fällig – heute reicht die PV nicht (Prognose … kWh, nötig … kWh).“, „Vollladung: Das Auto hängt an einem Ladepunkt, den der Proxy nicht steuern soll.“ oder „Vollladung fällig – EVCC liefert keine Solarprognose …“.
- **Log:** Aktivierung, Rückstellung und evcc-Aufruffehler werden protokolliert; den jeweiligen Wartegrund zeigt die Statusseite. Beispiele:

```text
[FULL] <Auto>: last 100 % 15 days / 120 kWh ago, PV today 38.2 kWh ≥ 14.9 kWh needed – EVCC limit 80 → 100 %
[FULL] <Auto>: 100 % and charging finished for 30 min – EVCC limit back to 80 %
```

## Was der Proxy in evcc tut

Benötigt `EVCC_URL`, z. B. `http://evcc.example:7070`. Ladepunktnummern `<n>` beginnen bei 1; `<name>` ist die Fahrzeugkennung in evcc.

| Aktion | Endpunkt | Wann |
|---|---|---|
| Zustand lesen | `GET /api/state` | Erkennungsprüfung während des Ladens; Vollladeprüfung; Statusanzeige (20 s Cache für die Anzeige). Zusätzlich beim Rendern der Einstellungsseite, auch nach Speichern oder Validierungsfehlern: Scan der Fahrzeuge und Ladepunkte ohne Statuscache; Scanfehler erscheinen dort. |
| Ladesitzungen lesen | `GET /api/sessions` | Wochenstatistik und kWh-Schwelle; 15 Minuten zwischengespeichert. |
| Fahrzeugerkennung anfordern | `PATCH /api/loadpoints/<n>/vehicle` | Ausgewählter ladender Ladepunkt zeigt ein eindeutig zugeordnetes, hier getrenntes Fahrzeug oder kein Fahrzeug bei passender Ladeleistung; keine laufende Erkennung und mindestens ein VRM-Fahrzeug im Ladezustand; höchstens einmal je fünf Minuten und Ladepunkt. |
| Vollladung erlauben | `POST /api/vehicles/<name>/limitsoc/100` | Fahrzeug fällig, SoC unter 100 %, an ausgewähltem Ladepunkt verbunden und Prognoseprüfungen bestanden; POST nur bei evcc-Fahrzeuglimit ungleich 100 %. |
| Fahrzeuglimit zurücksetzen | `POST /api/vehicles/<name>/limitsoc/<old>` | Bei Abschluss oder den genannten Endbedingungen, falls ein Alltagslimit gespeichert wurde. |
| Ladepunktlimit zurücksetzen | `POST /api/loadpoints/<n>/limitsoc/<old>` | Bei derselben Rücksetzung, falls das zugeordnete Auto dort noch verbunden ist. |

Die Fahrzeugzuordnung nutzt Reichweite und bei Bedarf SoC; mehrdeutige Treffer werden übersprungen. VRM kann die Stationsverbindung eines früheren Autos behalten: Stationsdaten erhält das Fahrzeug mit eigener Aktivität, sonst der zuletzt kontaktierte Kandidat. Andere Kandidaten übernehmen diese Stationswerte nicht; ihr eigener VRM-Ladezustand wird weiterhin ausgewertet.

## Wie der Proxy dein evcc-Fahrzeug findet

Damit der Proxy auf jeder Installation das Limit ändern kann, braucht er den **Fahrzeugnamen in evcc** (Aufruf `POST /api/vehicles/<name>/limitsoc/…`). Er findet ihn selbst:

1. **evcc-Adresse:** `EVCC_URL` (in `.env` oder in den Einstellungen).
2. **Zuordnung VIN → evcc-Fahrzeug:**
   - **Durch Lernen:** während der Vollladeprüfung, wenn mindestens eine Schwelle aktiv ist oder ein gespeicherter Versuch abgewickelt wird, ordnet der Proxy das evcc-Auto über Reichweite und bei Bedarf Ladestand dem VRM-Auto zu und merkt sich das. Statusabruf und Einstellungs-Scan lernen keine Zuordnung. Mehrdeutige Treffer überspringt er. Bei ausgeschalteter Automatik ohne vorhandene Zuordnung oder bei mehrdeutigen Treffern das evcc-Fahrzeug manuell auswählen (Punkt 4).
3. **Ladepunkt:** Sein Limit liest der Proxy aus dem evcc-Zustand und setzt es bei der Rückstellung zusätzlich zurück.

4. **Von Hand, wenn die Erkennung nicht reicht:** Im Abschnitt zur **evcc-Anbindung** wählt **Gesteuerte Ladepunkte** aus dem Scan, an welchen Wallboxen der Proxy Vollladungen starten und die Fahrzeugerkennung neu anstoßen darf. Standard sind alle. Danach folgt **Fahrzeuge** mit einer Zeile je VIN: Bild/Modell, **Fahrzeug in EVCC** (die Liste kommt aus einem Scan von evcc), **Akkutyp** und **Akkukapazität**. Die manuelle evcc-Zuordnung hat Vorrang vor der gelernten; beim Akkutyp übernimmt „Automatisch“ das globale Profil.

Das Limit im Auto ändert der Proxy nie. Hat evcc eine Anmeldung, funktioniert die Anbindung nicht: Der Proxy sendet keine evcc-Zugangsdaten.

## Schnellstart

Benötigt Docker, Docker Compose, eine VRM-Anlage mit einem Electric-Vehicle-Gerät und Netzwerkzugriff auf VRM. Die mitgelieferte Compose-Datei nutzt **Host-Netzwerkbetrieb**. Das Dockerfile verwendet Python 3.12 und ausschließlich die Standardbibliothek.

In einem Checkout dieses Repositories:

```sh
cp .env.example .env
# .env bearbeiten: Beispielzugang ersetzen oder beide Werte für die Browsereinrichtung leeren.
docker compose up -d --build
```

Beispiel für `.env` (erfundenen Zugang ersetzen oder beide VRM-Werte leeren):

```dotenv
VRM_TOKEN=REPLACE_WITH_VRM_API_TOKEN
VRM_SITE_ID=123456
POLL_INTERVAL=60
PORT=8080
TZ=Europe/Berlin
# evcc-Adresse, optional (leer = evcc-Funktionen aus); alternativ in den Einstellungen setzen
EVCC_URL=http://evcc.example:7070
```

`http://proxy.example:8080/settings` öffnen. Sind beide VRM-Werte leer, leitet `/` zur ersten Einrichtung dorthin weiter. Einstellungen und Verlauf bleiben im Compose-Volume unter `/config` erhalten.

In evcc ein Fahrzeug hinzufügen; für jedes Auto mit dessen tatsächlicher VIN wiederholen. Das Template `tesla-ble` dient hier nur als technischer Zugang zum Proxy und ist nicht auf Tesla beschränkt; bei anderen Marken hängt es davon ab, was VRM liefert (siehe „Getestet mit“). Die folgenden Werte sind Platzhalter:

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

Bei `tesla-ble` **den Port aus `url` weglassen**: Das Template hängt das separate `port` an. Diese Schnittstelle liefert Daten; Fahrzeugbefehle werden als No-Ops bestätigt.

In den Proxy-Einstellungen `EVCC_URL` auf `http://evcc.example:7070` setzen, passende Schwellen wählen und das Alltagslimit am Fahrzeug in evcc einstellen. Alternativ lassen sich diese Werte in den Compose-Dienst schreiben (oder ein Compose-Override nutzen):

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

Für ein Quellcode-Update den Checkout aktualisieren und `docker compose up -d --build` ausführen. Für ein Update des veröffentlichten Images `docker compose pull`, danach `docker compose up -d --no-build` ausführen.

## Konfiguration

Unter `/settings` lassen sich VRM-Zugang, globales Akkuprofil, Abfrageintervall, evcc-Anbindung mit gesteuerten Ladepunkten, Fahrzeuge und Sprache einstellen. Den Token in den API-Token-Einstellungen des VRM-Portals erstellen; die Site-ID steht in `/installation/123456/dashboard`. Ein leeres Token-Feld behält den vorhandenen Token.

Nicht leere Werte in `/config/settings.json` haben Vorrang vor Umgebungsvariablen. Die meisten Änderungen gelten beim nächsten Abruf oder Seitenaufruf; die Detailwerte der Statusseite laufen live mit. **Ein geänderter `PORT` benötigt einen Neustart.** Leere Token-, Site-ID- und sonstige globale Zahlenfelder behalten den vorhandenen Wert. Das Leeren von `CAPACITY` oder `EVCC_URL` entfernt die gespeicherte Vorgabe; ein vorhandener Umgebungswert greift anschließend wieder. Einstellungen sind global, sofern nicht ausdrücklich fahrzeugbezogen (Bild, evcc-Zuordnung, Akkutyp, Kapazität und Erfassung).

| Variable | Standard / Bedeutung |
|---|---|
| `VRM_TOKEN` | Erforderlicher VRM-API-Token. |
| `VRM_SITE_ID` | Erforderliche numerische Installations-ID. |
| `POLL_INTERVAL` | `60` Sekunden; UI-Bereich 10–3600. |
| `PORT` | `8080`; nach Änderung neu starten. |
| `TZ` | `Europe/Berlin` in Compose; Container-Zeitzone, keine UI-Einstellung. |
| `BATTERY_TYPE` | `LFP`; alternativ `NMC`. Globales Anzeige-/Erfassungsprofil; je Fahrzeug überschreibbar. |
| `CAPACITY` | Globale Vorgabe 1–200 kWh. Reihenfolge: je Fahrzeug > `CAPACITY` > VRM `/BatteryCapacity`; weitere Rückfallwerte nur für Vollladeplanung und Status-Endzeit, siehe unten. |
| `OPT_MIN`, `OPT_MAX` | LFP 10–80 %, NMC 20–90 %; einstellbarer Alltagsbereich der Anzeige. |
| `FULL_REMINDER_DAYS` | `28` bei LFP; nur UI-Erinnerung, getrennt von der Automatik. |
| `FULL_CHARGE_DAYS` | `0` (aus); 0–60 Kalendertage. |
| `FULL_CHARGE_KWH` | `0` (aus); 0–2000 geladene kWh. Jede aktive Schwelle kann das Fahrzeug fällig machen. |
| `FULL_CHARGE_BASE_LOAD` | `1500` W; Abzug von der Solarprognose, einstellbar 0–20000 W. |
| `EVCC_URL` | Leer: evcc-Anbindung aus. Beispiel: `http://evcc.example:7070`. |
| `EVCC_LOADPOINTS` | Leer: alle Ladepunkte. Sonst Nummern ab 1, kommasepariert ohne Leerzeichen, z. B. `1,3`. Beschränkt Neuerkennung und den Start von Vollladungen. In der UI muss mindestens ein Ladepunkt ausgewählt bleiben. |
| `LANGUAGE` | `auto` (Browsersprache), `en` oder `de`; nur die Oberfläche. |

Die mitgelieferte Compose-Datei übergibt VRM-Zugang, Abfrageintervall, Port, `EVCC_URL` und Zeitzone. `EVCC_URL` kann direkt in `.env` gesetzt werden; weitere Variablen wie `EVCC_LOADPOINTS` unter `environment` ergänzen oder in den Einstellungen setzen. Für Vollladeplanung und Status-Endzeit gilt: je Fahrzeug > `CAPACITY` > VRM > evcc-Fahrzeug > 60 kWh. API-Endzeit, Zyklen und Auswärtsenergie verwenden nur die ersten drei Quellen; deshalb eine passende Kapazität hinterlegen.

Zum Abschalten der Vollladeautomatik **beide Schwellen auf 0** setzen. Bei einem laufenden Versuch `EVCC_URL` erreichbar lassen, bis das gespeicherte Limit zurückgesetzt wurde. Die Automatik prüft `BATTERY_TYPE` nicht; die Auswahl NMC allein deaktiviert sie nicht.

## Fahrzeugbilder

Im Abschnitt **Fahrzeuge** der Einstellungen bietet jedes Auto automatische Auswahl, ein bestimmtes Modell, kein Bild oder eine **eigene HTTP(S)-Bild-URL**; die eigene URL hat Vorrang.

Tesla-Renderings lädt **der Browser** aus [github.com/teslamotors/custom-wraps](https://github.com/teslamotors/custom-wraps) über GitHubs Raw-Content-Host. Sie werden **nicht in diesem Repository gespeichert**. Die automatische Tesla-Auswahl nutzt Modellname/VIN und Modelljahr; weitere Tesla-Varianten sind manuell auswählbar.

Die mitgelieferten JPEGs in [`images/`](images/) sind **KI-generierte Studio-Darstellungen** für VW ID.4, Kia EV6, Hyundai Ioniq 5, Ford Mustang Mach-E, Mini Countryman, Porsche Macan und Volvo EX40/XC40. Die lokale Auswahl nutzt Teile des Modellnamens. Der Proxy liefert sie unter `/img/<id>.jpg`; ein nicht erreichbares externes Bild wird ausgeblendet.

## Endpunkte

| Methode | Pfad | Ergebnis |
|---|---|---|
| `GET` | `/`, `/status` | Statusseite; Weiterleitung zu den Einstellungen, solange VRM-Zugang fehlt. |
| `GET` / `POST` | `/settings` | Einstellungsformular / Einstellungen speichern. |
| `GET` | `/api/1/vehicles/<VIN>/vehicle_data` | Tesla-Format `response.response.charge_state`; HTTP 503 ohne Daten oder bei zu alten Daten. |
| `POST` | `/api/1/vehicles/<VIN>/command/<command>` | Bestätigter No-Op, z. B. `wake_up`, `charge_start`, `set_charging_amps`. |
| `GET` | `/api/health` | JSON-Status `ok`, `error` oder `stale`, Alter, Fehler, Site-ID und Version; HTTP 503 bei error/stale. |
| `GET` | `/api/raw` | Zwischengespeicherte VRM-Rohwerte je Fahrzeug samt zugeordneten Ladestationswerten. |
| `GET` | `/img/<id>.jpg` | Mitgeliefertes Modellbild, sofern die ID unterstützt wird. |

`charge_state` enthält `battery_level`, `usable_battery_level`, `battery_range` (**Meilen**), `charge_limit_soc` (das von VRM gemeldete Autolimit), `charge_amps`, `charge_current_request`, `charging_state`, `charger_power`, `charge_energy_added` (kWh), `minutes_to_full_charge`, `time_to_full_charge` (Stunden) und `timestamp`. API-Zeitschätzungen nutzen das Autolimit; die Statusseite bevorzugt dagegen das wirksame evcc-Limit für Ladeziel/Endzeit. Die Sitzungsenergie stammt zuerst aus Stations-`/Session/Energy`, dann aus Differenzen von Fahrzeug-`/Ac/Energy/Forward`, zuletzt aus Leistungsintegration.

Die Oberfläche warnt nach `max(180 s, 3 × POLL_INTERVAL)` ohne erfolgreichen VRM-Abruf. Fahrzeugdaten und Health gelten nach `max(1800 s, 10 × POLL_INTERVAL)` als veraltet. Erfolgreiche Fahrzeugantworten und HTTP-503-Antworten wegen veralteter Daten enthalten `X-Data-Age-Seconds`; ohne vorhandene Fahrzeugdaten fehlt der Header. Der Docker-Healthcheck prüft nur die Erreichbarkeit des HTTP-Servers und akzeptiert eine 503-Antwort; „healthy“ garantiert keine frischen VRM-Daten.

## Fehlerbehebung

- **Keine Daten:** Token und Site-ID unter `/settings` prüfen, danach `docker compose logs --tail=100 vrm-ev-proxy` und `/api/health`.
- **`No EV device found`:** Die Anlage muss in VRM ein Electric-Vehicle-Gerät bereitstellen; der Proxy legt keines an.
- **Falsche Uhrzeit/Datumsangaben:** `TZ` in Compose auf die gewünschte Zeitzone setzen und den Container neu erstellen.
- **evcc-Verbindungsfehler:** `url: http://proxy.example` und `port: 8080` verwenden, ohne den Port in der URL zu wiederholen.
- **Falsches Fahrzeug:** VIN mit der Proxy-Anzeige abgleichen. Groß-/Kleinschreibung spielt keine Rolle; unbekannte VINs liefern das erste Fahrzeug. Bei mehreren Fahrzeugen wird je unbekannter VIN einmal gewarnt. Mehrdeutige Treffer lösen keine Neuerkennung aus.
- **Keine evcc-Statistik oder Automatik:** Ist `EVCC_URL` gesetzt, aber falsch (nicht erreichbar, falscher Port oder Pfad, Anmeldung nötig, keine evcc-Antwort), zeigt die Statusseite oben eine rote Fehlermeldung mit dem Grund. Außerdem: `EVCC_URL`, Erreichbarkeit, VIN-Zuordnung (Einstellungen → Fahrzeuge → Fahrzeug in EVCC) prüfen. Der Client sendet keine evcc-Anmeldedaten; eine API mit Authentifizierung schlägt fehl. Im Log nach `[EVCC]`, `[LIVE]` oder `[FULL]` suchen.
- **Vollladung bleibt fällig:** Fahrzeugzuordnung, Verbindung, ausgewählten Ladepunkt und ausreichende evcc-Solarprognose prüfen. Fehlende Zuordnung, ausgeschlossener Ladepunkt und fehlende Prognose werden unter „Details & Verlauf“ getrennt angezeigt. Im Winter kann das dauern. Das Auto muss selbst 100 % erlauben und evcc PV-Überschussladen nutzen.
- **Limit kommt nicht zurück:** 30 Minuten nach Ladeende abwarten, evcc-API-Fehler prüfen und `EVCC_URL` eingerichtet lassen. Nur ein gespeichertes Alltagslimit von 1–99 % kann wiederhergestellt werden.

Weitere Implementierungshinweise: [BUGS.md](BUGS.md).

## Getestet mit – und was darüber hinaus nur angenommen ist

Entwickelt und getestet wurde auf **einer** Installation: Victron EV Charging Station, zwei Teslas über VRM, evcc mit in der Oberfläche angelegten Fahrzeugen, aktiver Solarprognose, ohne evcc-Anmeldung. Dass es dort läuft, heißt nicht, dass es überall läuft. Nicht geprüft sind:

- **Andere Marken in VRM.** Der Proxy liest das VRM-Gerät „Electric Vehicle“ (`/Soc`, Reichweite, `/ChargingState`, optional `/VIN`, `/BatteryCapacity`). Liefert VRM bei einer Marke andere oder weniger Felder, fehlen Werte oder die Zuordnung. Fehlt eine Fahrzeugkennung, verwendet der Proxy eine Ersatzkennung. Die Zuordnung zum evcc-Fahrzeug erfolgt über Reichweite und gegebenenfalls Ladestand beim Lernen oder durch Handauswahl in den Einstellungen.
- **Andere Wallboxen.** Die Stationslogik (`/Mgmt/Connection`, Status, Sitzungsenergie) gilt nur für Victron-Ladestationen. Das wirksame Ladeziel kann aus evcc kommen. Ladeleistung und daraus berechnete Endzeit benötigen verwertbare VRM-Leistungswerte beziehungsweise Spannung, Strom und Phasen.
- **Fahrzeuge aus der `evcc.yaml`.** Bei Fahrzeugen aus der Datei hilft das Lernen (setzt voraus, dass das evcc-Fahrzeug Reichweite und Ladestand **vom Proxy** bezieht, Template `tesla-ble`) oder die **Handauswahl** je Auto in den Einstellungen.
- **Andere evcc-Versionen.** Gebraucht werden `/api/state` mit Ladepunkten, Fahrzeug-Limit, Solarprognose-Zeitreihe und die Schreibaufrufe unter `/api/vehicles/<name>/limitsoc/…`. Eine Mindestversion ist nicht bestimmt.
- **Gleiche Fahrzeugtitel.** evcc-Sitzungen werden über den Titel zugeordnet; zwei Autos mit gleichem Titel vermischen ihre kWh.
- **Akkukapazität und Akkutyp.** Für Vollladeplanung und Status-Endzeit gilt: je Fahrzeug > `CAPACITY` > VRM > evcc-Fahrzeug > 60 kWh; API-Endzeit, Zyklen und Auswärtsenergie verwenden nur die ersten drei Quellen. Bei verschieden großen Autos je Auto eintragen. Den Akkutyp (LFP/NMC) je Fahrzeug einstellen oder die globale Einstellung übernehmen; die Zellchemie wird nicht automatisch erkannt. Weicht der Fahrzeugtyp vom globalen Typ ab, gelten seine voreingestellten SoC-Grenzen.

Fehlende Zuordnung, ausgeschlossener Ladepunkt und fehlende Solarprognose werden getrennt angezeigt. Ist evcc falsch eingetragen, zeigt die Statusseite einen Fehler.

## Grenzen

- **Keine Steuerung im Auto:** evcc-Befehle sind No-Ops. Der Proxy kann das Auto weder wecken noch dessen Limit verändern, Laden starten oder den Strom setzen. Echte Fahrzeugsteuerung benötigt eine separate Integration.
- **Keine Änderungen an Modus, Plänen, Tarif oder Strom:** Ein höheres Limit wählt nicht selbst Solarstrom aus. Bestehende evcc-Einstellungen bestimmen, ob Netzstrom genutzt werden kann; der Proxy garantiert keine netzstromfreie Ladung.
- **evcc für VRM-Daten optional, für evcc-Funktionen erforderlich:** Vollladeautomatik, Neuerkennung, wirksame Ladeziele und Solar-/Sitzungsstatistik benötigen `EVCC_URL`. evcc-basierte Solar- und Wochenstatistiken brauchen eine manuelle oder gelernte Fahrzeugzuordnung; SoC-Verlauf und Akku-Erfassung sind davon unabhängig.
- **VRM-Daten werden abgefragt, sind nicht live:** Standardintervall 60 Sekunden, bei Fehlern längere Wartezeiten. Der Fahrzeugkontakt kann noch älter sein. Eine 100-%-Meldung und ein Timer beweisen kein abgeschlossenes Zellbalancing.
- **Vollladungen hängen von Sonne ab:** Eine zu schwache oder fehlende Prognose lässt das Auto fällig bleiben. Es wird kein Netzstrom-Ersatzplan angelegt.
- **Erfassung beruht auf Beobachtung:** Nicht beobachtete Ladungen/SoC-Änderungen können fehlen; auswärts geladene Energie und Zyklen sind Schätzungen. evcc-Sitzungen werden nach Fahrzeugtitel zugeordnet; die Wochenzeile zählt im Zeitraum begonnene Sitzungen.
- **Keine HTTP-Authentifizierung:** Wer den Server erreichen kann, kann Status/Rohdaten ansehen und Einstellungen ändern. Das Einstellungsformular liefert den vollständigen VRM-Token nicht zurück.

## Danksagung und Bildhinweis

Nutzt Fahrzeugdaten aus Victron VRM und die HTTP-API von evcc; die Tesla-Fahrzeugdatenschnittstelle ist mit evccs Template `tesla-ble` kompatibel. Danke an diese Projekte und die oben verlinkte Quelle der Tesla-Renderings.

Produktnamen, Marken und Herstellerlogos gehören ihren jeweiligen Rechteinhabern. Das Projekt beansprucht keine Rechte an Herstellerlogos und stellt keine Herstellerzugehörigkeit oder Unterstützung durch Hersteller dar. Fahrzeugbilder sind illustrativ; KI-generierte Darstellungen können vom tatsächlichen Modell abweichen.

## Lizenz

MIT. Das Repository enthält noch keine separate `LICENSE`-Datei.
