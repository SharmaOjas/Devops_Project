# Dark Ship Watch

[![CI - build and deploy to Kubernetes](https://github.com/SharmaOjas/Devops_Project/actions/workflows/ci.yml/badge.svg)](https://github.com/SharmaOjas/Devops_Project/actions/workflows/ci.yml)

A live ship tracker for the Gulf of Finland, built as an ADBMS mini-project and deployed on Kubernetes for the DevOps project
(Group 17: Model Deployment using Kubernetes). See [k8s/README.md](k8s/README.md) for the Docker and Kubernetes setup, and
[.github/workflows/ci.yml](.github/workflows/ci.yml) for the CI pipeline.

A collector keeps pulling real ship positions, ship details, sea state and wind from free sources into
PostgreSQL with PostGIS. The database checks every record, keeps each ship's latest position and its full
history, keeps a history of destination and arrival time changes, notices port arrivals and departures, and
finds ships that went silent. When a ship goes silent without a normal reason, it draws the sea area the ship
could have reached, adjusted for wind and waves. That area shows what was possible, not what happened.

Main track: B (Mobile). Bonus tracks: A (Temporal) and C (GIS).

## 1. Files

| File | What it does |
|---|---|
| `database.sql` | The whole database. Part 1 tables, part 2 logic (checks, triggers, silence detection), part 3 DBA (logins, monitoring, test data). After `-- @@DEMO` there is a demo to run by hand in psql. |
| `setup_db.py` | Creates the database, applies `database.sql`, copies the limits from `db.py` into the `pipeline_config` table. `--check` only reports, `--reset --yes` rebuilds, `--land` loads the land map. |
| `db.py` | All settings (they can be changed with `DSW_*` environment variables) and the database connection helpers. |
| `collector.py` | The collector that polls the live sources. The second half is the possible-area engine. |
| `app.py` | The web server. It also holds the plain-language description of every table. |
| `simulate_demo.py` | The demo ships, the locking and index experiments, the terminal demo, and `--stress`. |
| `verify.py` | About 40 checks that prove the numbers on screen are the numbers in the database. |
| `backup_restore.sh` | Backup, restore into a scratch database, and a row count comparison. |
| `web/` | The website (no build step). |
| `scripts/` | `dsw.ps1` starts and stops everything, `setup_portable_db.ps1` makes the private database. |
| `docs/VIVA_GUIDE.md` | Demo script, useful queries and likely questions. |

## 2. Start it (Windows)

You need Python 3.10 or newer, an installed PostgreSQL 18 (only its program files are copied), and internet.

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\setup_portable_db.ps1
python setup_db.py
python setup_db.py --land
```

`setup_portable_db.ps1` downloads the official PostGIS Windows bundle (about 118 MB) and adds it to a copy of the
PostgreSQL program files in `.pgportable`. Your own PostgreSQL installation is not touched.

After that, double-click `start_all.bat`. It starts the database, the collector and the website at
http://127.0.0.1:8000. Run `start_all.bat stop` to stop the collector and website.

To run the parts yourself:

```powershell
python collector.py       # keeps running, press Ctrl-C to stop
python app.py             # the website
python simulate_demo.py   # the demo in the terminal
```

To use your own PostgreSQL with PostGIS, set `DSW_DB_HOST`, `DSW_DB_PORT`, `DSW_DB_USER` and `DSW_DB_PASSWORD`
first. `DSW_RBAC=0` makes everything log in as the owner if you skip the permission setup.

## 3. How the data moves

```
Digitraffic (ship positions, ship details, sea state)   FMI (wind, waves)
                         \                                  /
                          collector.py  (retry, heartbeat)
                                  |
                           staging_ais   (waiting room, no rules)
                                  |
                  validate_and_migrate_ais()   one all-or-nothing step
                    /                         \
        location_events + latest_position      rejected_events (with the reason)
                    |
        triggers: port visits, silences, destination history
                    |
        every 5 minutes: sync_open_gaps() and the possible-area engine
                    |
        app.py (read-only login)  ->  website
```

The nine checks, in order: missing ship ID, ship ID out of range, missing fields, impossible coordinates, outside the
Gulf of Finland, speed above 60 knots, time in the future, duplicate, and a jump faster than 50 knots (over 500 m).

A silence is sorted into: in port, parked (under 0.5 knots), left the area, receiver outage (the ships around it also
went quiet), or going dark (none of those). Only going dark gets a possible area:

```
usable speed = top speed x weather slowdown
reach        = usable speed x time silent
possible area = circle(last position, reach) overlap circle(next position, reach), with the land cut out
```

The slowdown tables (`speed_factor_lookup`, `ship_class_speed`) are my own assumptions, not maritime science.

## 4. Demo ships and real evidence

Everything on the map and in the numbers comes from the live feeds. For showing every rule on demand there are
clearly labelled demo ships (`simulate_demo.py`): MMSI 799000000 to 799999999, `is_synthetic = true`, names start with
"SYN", and they appear with a dashed ring. They go through the same checking path as live data, and they never mix
with real ships. Remove them with Demo controls, then "Remove the demo ships". For the report, take screenshots of a
real silence from your own collected data.

## 5. How to test it

```powershell
python verify.py                  # about 40 checks, each PASS, FAIL or SKIP with its evidence
python simulate_demo.py           # SQL and real output for every feature
python simulate_demo.py --stress  # 10,000 rows through the checks, timed
bash backup_restore.sh            # backup, restore to a scratch database, compare row counts
```

`verify.py` recomputes things on its own instead of trusting the website: no position outside the area, no duplicates,
no speed above the limit, no impossible jump, latest position equals the newest stored one, one current history row
per ship, no overlapping history, every port entry inside the port radius, the area formulas, no land inside an area,
index use, all 15 permission tests, 5 random ships compared with the live source, and the website numbers compared
with direct SQL.

Things that should fail:

```sql
INSERT INTO staging_ais (mmsi, lat, lon, sog, event_time) VALUES (230000001, 95.2, 25.0, 10, now());
SELECT * FROM validate_and_migrate_ais();     -- rejected = 1, reason INVALID_COORDINATES
SET ROLE analyst_role; DELETE FROM location_events;   -- permission denied
RESET ROLE;
```

## 6. Using the website

* Cards with an arrow open and close when you click their title. The text at the top of each page says what it is for.
* Opening page: scroll to see how it works, then press "Enter the watch". Click the logo to come back.
* Live Map: click a ship for its trail, speed, destination history and silences. Click a red ship, or a row under
  "Ships going dark", for the possible area and the checklist. Layers (top right) switch wind, waves, ports and busy
  sea lanes. Possible areas are drawn when you pick a ship, or tick "Possible areas".
* How it works: follow one real record in eight steps, see when data is collected, check a random ship against the
  official source, and read what every table holds.
* Going Dark: the slider sets how long a ship must be quiet. Bars show normal and unexplained silences.
* Pipeline: where data comes from, how it flows, and why records were refused.
* Temporal: pick a ship to see each destination version, and look back in time.
* Ports: ships in each port and the arrivals and departures log.
* DBA Console: Health, Permissions, Two at once (locking), Speed (index test) and Backup.
* Architecture: how the system fits together, and the table diagram (ER diagram).
* Demo controls: load or remove the demo ships, fetch live data now, check for silent ships, rebuild the busy-lanes map.

## 7. If something goes wrong

| Problem | Fix |
|---|---|
| Connection refused on port 5433 | Run `scripts\dsw.ps1 db-start` or set `DSW_DB_PORT` for your own server. |
| Extension "postgis" is not available | Use `scripts\setup_portable_db.ps1`, or install PostGIS with StackBuilder. |
| Password failed for user dsw_ingest | Run `python setup_db.py` (it creates the logins) or set `DSW_RBAC=0`. |
| Map is empty or grey | The map tiles need internet. The data itself is local. |
| No going dark ships | Real silences take time to appear. Do not fake them; use the demo ships to show the rules. |
| HTTP errors from Digitraffic | The code already sends gzip and the Digitraffic-User header. |
| Collector stopped while the laptop slept | Keep it awake. `collector_heartbeat` shows exactly when it stopped. |

Restarting any part is always safe because every step can be repeated.
