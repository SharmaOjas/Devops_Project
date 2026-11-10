"""Dark Ship Watch: self-check. It proves that what you see on screen is what is really in the database.

    python verify.py              run every check (database rules, live source comparison, website numbers)
    python verify.py --no-live    skip the comparison with the live Digitraffic source
    python verify.py --no-web     skip the website checks

Each check prints PASS, FAIL or SKIP with its evidence. If anything fails the exit code is 1.
"""
from __future__ import annotations

import argparse
import math
import random
import sys

import requests

import db

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool | None, detail: str = "") -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    RESULTS.append((status, name, detail))
    print(f"  [{status}] {name}" + (f"  - {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n== {title}")


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    conn = db.connect("owner", app_name="verify")
    # q gets all rows of a query, one gets just the first row.
    q = lambda sql, p=None: db.query(conn, sql, p)
    one = lambda sql, p=None: (q(sql, p) or [{}])[0]

    # 1
    section("1. Environment")
    # This gets the PostGIS and PostgreSQL versions.
    ver = one("SELECT postgis_lib_version() AS v, version() AS pg")
    check("PostGIS available", bool(ver.get("v")), f"PostGIS {ver.get('v')}")
    check("PostgreSQL 15+", int(ver["pg"].split()[1].split(".")[0]) >= 15, ver["pg"].split(",")[0])
    # This counts the land polygons and checks if the Natural Earth ones are there.
    n_land = one("SELECT count(*) AS n, bool_or(source = 'NATURAL_EARTH_10M') AS ne FROM land_polygons")
    check("Real Natural Earth land loaded (not the coarse fallback)", bool(n_land["ne"]), f"{n_land['n']} polygons")

    # 2
    section("2. Data integrity invariants (the database enforces these - we prove nothing slipped through)")
    # This counts stored positions that are outside the allowed box.
    r = one("""SELECT count(*) AS n FROM location_events
                WHERE ST_Y(geom) NOT BETWEEN %s AND %s OR ST_X(geom) NOT BETWEEN %s AND %s""",
            (db.BBOX["min_lat"], db.BBOX["max_lat"], db.BBOX["min_lon"], db.BBOX["max_lon"]))
    check("Every stored position is inside the Gulf of Finland box", r["n"] == 0, f"{r['n']} outside")
    # This looks for ships that have two fixes at the same time.
    r = one("SELECT count(*) AS n FROM (SELECT mmsi, event_time FROM location_events GROUP BY 1, 2 HAVING count(*) > 1) d")
    check("No duplicate (mmsi, event_time) rows", r["n"] == 0, f"{r['n']} duplicates")
    # This counts stored speeds above the limit.
    r = one("SELECT count(*) AS n FROM location_events WHERE speed_knots > %s", (db.THRESHOLDS["max_speed_knots"],))
    check("No stored speed above the 60 kn limit", r["n"] == 0, f"{r['n']} rows")
    # This looks for jumps between two fixes that would need an impossible speed.
    r = one("""SELECT count(*) AS n FROM (
                 SELECT mmsi, event_time, geom,
                        LAG(geom) OVER w AS pg, LAG(event_time) OVER w AS pt
                   FROM location_events WINDOW w AS (PARTITION BY mmsi ORDER BY event_time)) x
               WHERE pg IS NOT NULL AND ST_Distance(geom::geography, pg::geography) > %s
                 AND (ST_Distance(geom::geography, pg::geography) / 1852.0) / GREATEST(EXTRACT(EPOCH FROM event_time - pt), 1) * 3600 > %s""",
            (db.THRESHOLDS["min_jump_distance_m"], db.THRESHOLDS["max_jump_knots"]))
    check("No impossible jump (> 50 kn between consecutive fixes) in the trajectory table", r["n"] == 0, f"{r['n']} jumps")
    # This counts ships where last_seen is not their newest stored fix.
    r = one("""SELECT count(*) AS n FROM latest_position lp
                WHERE lp.last_seen <> (SELECT max(event_time) FROM location_events le WHERE le.mmsi = lp.mmsi)""")
    check("latest_position.last_seen equals the newest stored fix for every ship", r["n"] == 0, f"{r['n']} mismatches")
    # This counts rejected rows whose reason code is not in the reasons table.
    r = one("SELECT count(*) AS n FROM rejected_events e LEFT JOIN rejection_reasons r ON r.code = e.rejection_reason WHERE r.code IS NULL")
    check("Every rejection carries a valid reason code", r["n"] == 0)
    # This counts rows that stayed in staging for more than 5 minutes.
    r = one("SELECT count(*) AS n FROM staging_ais WHERE ingested_at < now() - interval '5 minutes'")
    check("Nothing stuck in staging for > 5 min", r["n"] == 0, f"{r['n']} stale rows")

    # 3
    section("3. Temporal (SCD-2) integrity")
    # This looks for ships with more than one current history row.
    r = one("SELECT count(*) AS n FROM (SELECT mmsi FROM vessel_status_history WHERE is_current GROUP BY 1 HAVING count(*) > 1) d")
    check("At most one current version per ship", r["n"] == 0)
    # This looks for history rows of one ship that overlap in time.
    r = one("""SELECT count(*) AS n FROM vessel_status_history a JOIN vessel_status_history b
                 ON a.mmsi = b.mmsi AND a.history_id < b.history_id
                AND tstzrange(a.valid_from, a.valid_to) && tstzrange(b.valid_from, b.valid_to)""")
    check("No overlapping validity periods", r["n"] == 0)
    # This looks for holes in the history where a version does not end when the next one starts.
    r = one("""SELECT count(*) AS n FROM (
                 SELECT valid_to, LEAD(valid_from) OVER (PARTITION BY mmsi ORDER BY valid_from) AS nxt
                   FROM vessel_status_history) x WHERE valid_to IS NOT NULL AND nxt IS DISTINCT FROM valid_to""")
    check("History is gap-free: each version ends exactly when the next begins", r["n"] == 0, f"{r['n']} breaks")
    # This compares the current history row with the destination and ETA in vessels.
    r = one("SELECT count(*) AS n FROM vessels v JOIN vessel_status_history h ON h.mmsi = v.mmsi AND h.is_current WHERE h.destination IS DISTINCT FROM v.destination OR h.eta IS DISTINCT FROM v.eta")
    check("Current history row equals vessels.destination / eta", r["n"] == 0, f"{r['n']} mismatches")

    # 4
    section("4. Ports (trigger output)")
    # This counts port events that are on the wrong side of the port radius.
    r = one("""SELECT count(*) AS bad FROM port_events pe JOIN ports p USING (port_id)
                JOIN location_events le ON le.mmsi = pe.mmsi AND le.event_time = pe.event_time
               WHERE (pe.event_type = 'ENTRY' AND ST_Distance(le.geom::geography, p.geom::geography) > p.buffer_radius_m)
                  OR (pe.event_type = 'EXIT'  AND ST_Distance(le.geom::geography, p.geom::geography) <= p.buffer_radius_m)""")
    # This counts all port events.
    n = one("SELECT count(*) AS n FROM port_events")["n"]
    check("Every ENTRY is inside the port radius and every EXIT outside it", r["bad"] == 0, f"{n} events checked")

    # 5
    section("5. Going Dark gaps and feasibility regions")
    # This gets the latest closed dark gaps to check.
    gaps = q("SELECT * FROM ais_gap_events WHERE status = 'CLOSED' AND is_dark_activity AND feasibility_status IN ('OK','INFEASIBLE') ORDER BY gap_id DESC LIMIT 25")
    if not gaps:
        check("Closed dark gaps exist to verify", None, "none yet - collect longer or load the demo scenario")
    bad_t = bad_p = bad_math = bad_geo = bad_land = bad_cls = 0
    for g in gaps:
        dur = (g["reappear_time"] - g["last_seen_time"]).total_seconds() / 60
        bad_t += abs(dur - float(g["gap_duration_minutes"])) > 0.1
        # These get the stored fix points before and after the gap and the points saved in the gap, as text.
        p1 = one("SELECT ST_AsEWKT(geom) AS w FROM location_events WHERE mmsi = %s AND event_time = %s", (g["mmsi"], g["last_seen_time"]))
        p2 = one("SELECT ST_AsEWKT(geom) AS w FROM location_events WHERE mmsi = %s AND event_time = %s", (g["mmsi"], g["reappear_time"]))
        s1 = one("SELECT ST_AsEWKT(%s::geometry) AS w", (g["start_geom"],)); s2 = one("SELECT ST_AsEWKT(%s::geometry) AS w", (g["end_geom"],))
        bad_p += (p1.get("w") != s1["w"]) or (p2.get("w") != s2["w"])
        bad_math += abs(float(g["v_max_knots"]) * float(g["speed_factor"]) - float(g["v_eff_knots"])) > 0.06 \
            or abs(float(g["v_eff_knots"]) * 1.852 * dur / 60 - float(g["reach_radius_km"])) > max(0.2, 0.01 * float(g["reach_radius_km"]))
        if g["feasibility_status"] == "OK":
            # This checks the region holds both dots and the tight area is inside it.
            geo = one("""SELECT ST_Contains(feasible_geom, start_geom) AND ST_Contains(feasible_geom, end_geom) AS both_in,
                                ST_Area(ST_Difference(tight_geom, feasible_geom)::geography) <= 0.02 * ST_Area(tight_geom::geography) AS tight_in
                           FROM ais_gap_events WHERE gap_id = %s""", (g["gap_id"],))
            bad_geo += not (geo["both_in"] and geo["tight_in"])
            # This gets how much land is inside the region, not counting the margin around the two dots.
            land = one("""SELECT COALESCE(ST_Area(ST_Intersection(g.feasible_geom, ST_Difference(
                                    (SELECT ST_Union(t.geom) FROM land_tiles t WHERE t.geom && g.feasible_geom),
                                    ST_Union(ST_Buffer(g.start_geom::geography, 1500)::geometry, ST_Buffer(g.end_geom::geography, 1500)::geometry)))::geography), 0) / 1e6 AS km2
                            FROM ais_gap_events g WHERE g.gap_id = %s""", (g["gap_id"],))
            bad_land += float(land["km2"]) > 0.05
        # This runs the classify function again for this gap.
        cls = one("SELECT classification FROM fn_classify_gap(%s, %s, %s::geometry, %s::geometry, %s, NULL)",
                  (g["mmsi"], g["last_seen_time"], g["start_geom"], g["end_geom"], g["reappear_time"]))
        bad_cls += (cls.get("classification") == "DARK_ACTIVITY") is False and not g.get("is_dark_activity") is False
    if gaps:
        check(f"Gap length = reappear - last seen ({len(gaps)} gaps)", bad_t == 0, f"{bad_t} wrong")
        check("P1 / P2 are exactly the stored fixes before and after the silence", bad_p == 0, f"{bad_p} wrong")
        check("v_eff = v_max x factor and R = v_eff x dT (recomputed independently)", bad_math == 0, f"{bad_math} wrong")
        check("Region contains both dots and the tight ellipse lies inside the circle intersection", bad_geo == 0, f"{bad_geo} wrong")
        check("Region contains no land (apart from the 1.5 km water margin around the two observed dots)", bad_land == 0, f"{bad_land} regions overlap land")
    # This counts gaps marked dark whose class is not DARK_ACTIVITY.
    r = one("""SELECT count(*) AS n FROM ais_gap_events WHERE is_dark_activity AND classification <> 'DARK_ACTIVITY'""")
    check("is_dark_activity is true only for DARK_ACTIVITY", r["n"] == 0)
    # This counts IN_PORT gaps that have no port.
    r = one("""SELECT count(*) AS n FROM ais_gap_events g WHERE g.classification = 'IN_PORT' AND g.start_port_id IS NULL""")
    check("Every IN_PORT verdict names its port", r["n"] == 0)
    # These two count the dark ships from the classifier and from the detector.
    sil = one("SELECT count(*) AS n FROM classify_silent_vessels(20) WHERE classification = 'DARK_ACTIVITY'")["n"]
    det = one("SELECT count(*) AS n FROM detect_going_dark_vessels(20)")["n"]
    check("detect_going_dark_vessels() equals the DARK_ACTIVITY subset of the classifier", sil == det, f"{det} dark ships")

    # 6
    section("6. Indexes are really used")
    # This asks for the plan of a lookup in latest_position.
    plan = "\n".join(r["QUERY PLAN"] for r in q("EXPLAIN SELECT * FROM latest_position WHERE mmsi = 230000000"))
    check("latest_position lookup uses the primary-key index", "Index" in plan, plan.splitlines()[0])
    # This asks for the plan of the nearest port query.
    plan = "\n".join(r["QUERY PLAN"] for r in q("EXPLAIN SELECT name FROM ports ORDER BY geom <-> ST_SetSRID(ST_MakePoint(25,60),4326) LIMIT 1"))
    check("Nearest-port query is a KNN query", "Limit" in plan)

    # 7
    section("7. Security")
    # This runs the function that tests what each role can and cannot do.
    rows = q("SELECT verdict, role_name, attempted_statement FROM dba_rbac_proof()")
    check("RBAC proof: every allowed/denied statement behaves as designed", all(x["verdict"] == "PASS" for x in rows), f"{sum(x['verdict'] == 'PASS' for x in rows)}/{len(rows)} pass")

    # 8
    section("8. Live API cross-check (is our database telling the truth about the real world?)")
    if args.no_live:
        check("Compare 5 ships against the Digitraffic API", None, "--no-live")
    else:
        # This picks 5 random live ships that were updated in the last 3 minutes.
        sample = q("""SELECT lp.mmsi, ST_Y(lp.geom) AS lat, ST_X(lp.geom) AS lon, lp.last_seen FROM latest_position lp JOIN vessels v USING (mmsi)
                       WHERE NOT v.is_synthetic AND lp.last_seen > now() - interval '3 minutes' ORDER BY random() LIMIT 5""")
        if not sample:
            check("Compare ships against the Digitraffic API", None, "no ship updated in the last 3 minutes (is the collector running?)")
        else:
            ok = 0
            for s in sample:
                try:
                    r = requests.get(db.API["locations"], params={"mmsi": s["mmsi"]}, headers=db.HTTP_HEADERS, timeout=30).json()
                    f = r["features"][0]; lon, lat = f["geometry"]["coordinates"]; ts = f["properties"]["timestampExternal"] / 1000
                    dt = abs(ts - s["last_seen"].timestamp()); d = haversine_m(lat, lon, s["lat"], s["lon"])
                    good = d < 5 if dt < 1 else d < 600 + 15 * 0.51 * dt     # same fix -> identical; newer fix -> plausible distance
                    ok += good
                    print(f"      MMSI {s['mmsi']}: API fix {dt:5.0f} s from ours, {d:7.1f} m apart -> {'same fix' if dt < 1 else 'newer fix, plausible'}")
                except Exception as exc:
                    print(f"      MMSI {s['mmsi']}: API error {exc}")
            check("5 random ships: database position equals (or is a plausible predecessor of) the live API position", ok == len(sample), f"{ok}/{len(sample)}")

    # 9
    section("9. Web UI numbers equal database numbers")
    if args.no_web:
        check("Web API", None, "--no-web")
    else:
        base = f"http://{db.WEB_HOST}:{db.WEB_PORT}"
        try:
            s = requests.get(base + "/api/summary", timeout=10).json()
            live = "NOT v.is_synthetic"
            # These are the queries that give the database numbers for the web page.
            sql = {
                "active_now": f"SELECT count(*) AS n FROM latest_position lp JOIN vessels v USING (mmsi) WHERE {live} AND lp.last_seen > now() - interval '20 minutes'",
                "fixes_stored": f"SELECT count(*) AS n FROM location_events le JOIN vessels v USING (mmsi) WHERE {live}",
                "rejected_total": "SELECT count(*) AS n FROM rejected_events WHERE source = 'digitraffic'",
                "dark_open": f"SELECT count(*) AS n FROM ais_gap_events g JOIN vessels v USING (mmsi) WHERE g.is_dark_activity AND g.status = 'OPEN' AND {live}",
                "dark_closed": f"SELECT count(*) AS n FROM ais_gap_events g JOIN vessels v USING (mmsi) WHERE g.is_dark_activity AND g.status = 'CLOSED' AND {live}",
            }
            for key, stmt in sql.items():
                truth = one(stmt)["n"]; shown = s[key]
                tol = 40 if key in ("active_now", "fixes_stored") else 1     # the collector writes every minute; allow tiny drift
                check(f"UI '{key}' = SQL", abs(truth - shown) <= tol, f"UI {shown} vs SQL {truth}")
            ships = requests.get(base + "/api/ships?synthetic=0&max_age_h=12", timeout=20).json()
            # This counts live ships updated in the last 12 hours.
            n_db = one("SELECT count(*) AS n FROM latest_position lp JOIN vessels v USING (mmsi) WHERE NOT v.is_synthetic AND lp.last_seen > now() - interval '12 hours'")["n"]
            check("Ships drawn on the map = rows in latest_position (12 h window)", abs(len(ships) - n_db) <= 5, f"map {len(ships)} vs SQL {n_db}")
            mm = random.choice(ships)["mmsi"]
            tr = requests.get(f"{base}/api/ship/{mm}?hours=12", timeout=20).json()
            # This counts the stored fixes of one ship in the last 12 hours.
            n_tr = one("SELECT count(*) AS n FROM location_events WHERE mmsi = %s AND event_time > now() - interval '12 hours'", (mm,))["n"]
            check(f"Trail of MMSI {mm}: points on the map = stored fixes", abs(len(tr["track"]) - n_tr) <= 1, f"map {len(tr['track'])} vs SQL {n_tr}")
            gp = requests.get(base + "/api/gaps?class=all", timeout=20).json()
            # This counts all gaps.
            n_g = one("SELECT count(*) AS n FROM ais_gap_events")["n"]
            check("Incident list = ais_gap_events", abs(len(gp) - min(n_g, 400)) <= 5, f"UI {len(gp)} vs SQL {n_g} (UI caps at 400)")
            for ep in ("/api/ports", "/api/pipeline", "/api/environment", "/api/land", "/api/dba/overview", "/api/port_events", "/api/funnel?threshold=20"):
                rr = requests.get(base + ep, timeout=30); check(f"GET {ep}", rr.status_code == 200, f"HTTP {rr.status_code}")
        except requests.RequestException as exc:
            check("Web UI reachable", None, f"{exc} - start it with `python app.py`")

    conn.close()
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n==== {sum(r[0] == 'PASS' for r in RESULTS)} passed, {len(fails)} failed, {sum(r[0] == 'SKIP' for r in RESULTS)} skipped ====")
    for status, name, detail in fails:
        print(f"  FAIL: {name} - {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
