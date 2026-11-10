"""Dark Ship Watch - the terminal demo, the demo ships and the stress test in one file.

    python simulate_demo.py                  run every section (about 1-2 minutes)
    python simulate_demo.py --skip-live      skip the live API poll (works offline)
    python simulate_demo.py --skip-bench     skip the 400k-row index benchmark
    python simulate_demo.py --stress         instead, measure what 10,000 new records cost (--rows, --ships, --bad-percent, --keep)

Sections of the demo, each one prints the SQL it ran and the real output:
  0  environment: PostGIS and the tables
  1  live data: one real poll of all four feeds, plus the collector heartbeat
  2  demo ships sent through the same staging and validation path as live data
  3  validation log: accepted and rejected records, every rejection reason
  4  latest position and movement history of one ship (Track B)
  5  destination history with valid time and recorded time (Track A)
  6  port entries and exits, plus spatial queries (Track C)
  7  going dark detector: ports, outages and parked ships are ignored
  8  possible areas: circle overlap, land removed, weather adjusted (Track C)
  9  DBA: EXPLAIN before and after indexes, locking test, permissions, monitoring

The demo ships are clearly labelled (MMSI 799xxxxxx, is_synthetic = true). The real evidence comes from the live feeds.
"""
from __future__ import annotations

import argparse
import math
import random
import re
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import psycopg2
import psycopg2.errors
import psycopg2.extras

import db
import collector

UTC = timezone.utc


# Demo ships. They are NOT real observations: each one has an MMSI between 799000000 and 799999999
# (799 is not a real country code), is flagged vessels.is_synthetic = true and is named "SYN ...".
# They travel the same road as live data (staging_ais -> validate_and_migrate_ais() -> triggers), which is
# what makes them a fair test. The point is to show every rule on demand: rejection reasons, port events and
# gap classes. The cast, with minutes counted from T0 = now minus 200 minutes:
#   799000001 SYN HARBOUR RUNNER   sails into Helsinki port (entry), stays, sails out (exit)
#   799000002 SYN GLITCH           a clean track plus every kind of bad record
#   799000003 SYN DARK STAR        goes silent for 95 minutes while two witnesses nearby keep transmitting,
#                                  then reappears: a closed gap, dark activity, possible area
#   799000004/5 SYN WITNESS 1/2    keep transmitting next to Dark Star
#   799000006/7 SYN OUTAGE A/B     two ships in a quiet corner go silent together: receiver outage
#   799000008 SYN VANISHER         goes silent and does not come back: an open gap
#   799000010 SYN WITNESS 3        keeps transmitting next to Vanisher
#   799000009 SYN HARBOUR SLEEPER  goes silent inside Turku port: in port
#   799000011 SYN ANCHORED         goes silent while anchored at 0 knots: stationary
T0_MINUTES_AGO = 200

SYN = {
    "runner": 799000001, "glitch": 799000002, "dark": 799000003, "wit1": 799000004, "wit2": 799000005,
    "outage_a": 799000006, "outage_b": 799000007, "vanisher": 799000008, "sleeper": 799000009,
    "wit3": 799000010, "anchored": 799000011,
}

VESSELS = [  # mmsi, name, ship_type, length, width, draught
    (SYN["runner"],   "SYN HARBOUR RUNNER",  60, 180.0, 28.0, 6.5),
    (SYN["glitch"],   "SYN GLITCH",          70, 120.0, 18.0, 7.0),
    (SYN["dark"],     "SYN DARK STAR",       70, 150.0, 22.0, 8.2),
    (SYN["wit1"],     "SYN WITNESS 1",       70, 110.0, 17.0, 6.0),
    (SYN["wit2"],     "SYN WITNESS 2",       80, 130.0, 20.0, 7.5),
    (SYN["outage_a"], "SYN OUTAGE A",        70, 100.0, 16.0, 5.5),
    (SYN["outage_b"], "SYN OUTAGE B",        70,  95.0, 15.0, 5.0),
    (SYN["vanisher"], "SYN VANISHER",        80, 170.0, 26.0, 9.5),
    (SYN["sleeper"],  "SYN HARBOUR SLEEPER", 70, 105.0, 16.0, 5.5),
    (SYN["wit3"],     "SYN WITNESS 3",       70, 115.0, 17.5, 6.2),
    (SYN["anchored"], "SYN ANCHORED",        80, 140.0, 21.0, 8.0),
]


def advance(lat: float, lon: float, course_deg: float, knots: float, seconds: float) -> tuple[float, float]:
    """Move a position along a course for some seconds. A flat-earth estimate is good enough for short legs."""
    dist_m = knots * 1852.0 / 3600.0 * seconds
    c = math.radians(course_deg)
    dlat = dist_m * math.cos(c) / 111_132.0
    dlon = dist_m * math.sin(c) / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def track(mmsi: int, start: tuple[float, float], t_start: datetime, minutes: float, step_min: float,
          course: float, knots: float, nav_stat: int = 0) -> list[tuple]:
    """A position every step_min minutes along a straight course at constant speed. Returns rows for staging_ais."""
    rows, lat, lon = [], start[0], start[1]
    n = int(minutes // step_min)
    for i in range(n + 1):
        rows.append((mmsi, round(lat, 6), round(lon, 6), knots, course % 360, int(course) % 360, nav_stat,
                     t_start + timedelta(minutes=i * step_min), "synthetic"))
        lat, lon = advance(lat, lon, course, knots, step_min * 60)
    return rows


def build_rows(now: datetime | None = None) -> tuple[list[tuple], list[tuple], datetime]:
    """Builds all demo positions. Returns the good rows, the extra bad rows and T0 (the start time)."""
    now = now or datetime.now(UTC)
    t0 = now - timedelta(minutes=T0_MINUTES_AGO)
    at = lambda m: t0 + timedelta(minutes=m)
    rows: list[tuple] = []

    # A. HARBOUR RUNNER: Helsinki port centre is (60.1610 N, 24.9360 E)
    rows += track(SYN["runner"], (60.0400, 24.9360), at(0), 44, 2, 0, 10)                    # inbound, due north
    port_lat, port_lon = advance(60.0400, 24.9360, 0, 10, 44 * 60)
    rows += [(SYN["runner"], round(port_lat, 6), round(port_lon, 6), 0.2, 0.0, 0, 5, at(46 + 2 * i), "synthetic") for i in range(3)]
    start_out = advance(port_lat, port_lon, 160, 10, 60)
    rows += track(SYN["runner"], start_out, at(54), 141, 3, 160, 10)                         # outbound, SSE, keeps transmitting

    # B. GLITCH: clean track + bad records
    good = track(SYN["glitch"], (60.0000, 26.0000), at(0), 195, 2, 90, 12)
    rows += good[:6]                                                                          # t = 0..10
    rows.append((SYN["glitch"], 60.4500, 28.0000, 12.0, 90.0, 90, 0, at(12), "synthetic"))   # teleport ~100 km in 2 min
    rows += good[7:]                                                                          # carries on from the true position

    # C. DARK STAR + witnesses
    dark_start = (59.8200, 24.2000)
    dark_leg1 = track(SYN["dark"], dark_start, at(0), 30, 2, 80, 14)                          # t = 0..30, then silence
    rows += dark_leg1
    p1_lat, p1_lon, _ = dark_leg1[-1][1], dark_leg1[-1][2], None
    reappear = advance(p1_lat, p1_lon, 70, 11.4, 95 * 60)                                     # ~18 nm in 95 min
    rows += track(SYN["dark"], reappear, at(125), 70, 2, 75, 13)                              # t = 125..195, back on air
    rows += track(SYN["wit1"], (59.7800, 24.2500), at(-10), 195, 3, 85, 12)
    rows += track(SYN["wit2"], (59.8700, 24.3000), at(-10), 195, 3, 85, 11)

    # D. RECEIVER OUTAGE: two ships in a quiet corner both go silent
    rows += track(SYN["outage_a"], (59.6200, 26.6000), at(0), 40, 2, 90, 10)
    rows += track(SYN["outage_b"], (59.6350, 26.5800), at(0), 40, 2, 90, 10)
    oa = advance(59.6200, 26.6000, 90, 10, 40 * 60)
    ob = advance(59.6350, 26.5800, 90, 10, 40 * 60)
    rows += track(SYN["outage_a"], advance(oa[0], oa[1], 90, 10, 90 * 60), at(130), 65, 2, 90, 10)
    rows += track(SYN["outage_b"], advance(ob[0], ob[1], 90, 10, 90 * 60), at(130), 65, 2, 90, 10)

    # E. VANISHER (open gap) + witness
    rows += track(SYN["vanisher"], (59.9500, 25.6000), at(40), 40, 2, 70, 11)                 # last fix t = 80
    rows += track(SYN["wit3"], (59.9400, 25.5500), at(20), 175, 3, 72, 10)

    # F. SLEEPER in Turku port; G. ANCHORED
    rows += [(SYN["sleeper"], 60.4350, 22.2200, 0.1, 0.0, 0, 5, at(2 * i), "synthetic") for i in range(31)]   # t = 0..60
    rows += [(SYN["anchored"], 59.9000, 25.3000, 0.0, 0.0, 0, 1, at(2 * i), "synthetic") for i in range(31)]  # t = 0..60

    # bad records for the rejection log
    bad = [
        (None,        60.0, 25.0, 10.0, 90.0, 90, 0, at(150), "synthetic"),                         # NULL_MMSI
        (12345,       60.0, 25.0, 10.0, 90.0, 90, 0, at(150), "synthetic"),                         # INVALID_MMSI
        (SYN["glitch"], 95.2, 25.0, 10.0, 90.0, 90, 0, at(151), "synthetic"),                       # INVALID_COORDINATES
        (SYN["glitch"], 57.5, 18.0, 10.0, 90.0, 90, 0, at(152), "synthetic"),                       # OUT_OF_AREA (Baltic proper)
        (SYN["glitch"], 60.0, 26.9, 75.0, 90.0, 90, 0, at(153), "synthetic"),                       # UNREALISTIC_SPEED
        (SYN["glitch"], 60.0, 26.9, 10.0, 90.0, 90, 0, now + timedelta(hours=2), "synthetic"),      # FUTURE_TIMESTAMP
    ]
    return rows, bad, t0


# This is the insert for the staging table. The %s gets filled with many rows at once.
STAGING_SQL = ("INSERT INTO staging_ais (mmsi, lat, lon, sog, cog, heading, nav_stat, event_time, source) VALUES %s")


def reset(conn_owner) -> dict:
    """Delete every demo ship. Its positions, gaps, port events and history go with it."""
    with conn_owner.cursor() as cur:
        # Here we delete all the demo data. Staging and rejected rows go first, then the demo vessels.
        cur.execute("DELETE FROM staging_ais WHERE source = 'synthetic'")
        cur.execute("DELETE FROM rejected_events WHERE source = 'synthetic' OR source = 'dba-demo'")
        cur.execute("DELETE FROM vessels WHERE is_synthetic")
        removed = cur.rowcount
    conn_owner.commit()
    return {"synthetic_vessels_removed": removed}


def seed(conn_ingest, conn_owner) -> dict:
    """Create the demo ships and send them through the real checking path. Returns counts for each step."""
    reset(conn_owner)
    now = datetime.now(UTC)
    rows, bad, t0 = build_rows(now)

    summary: dict = {"t0": t0}
    with conn_ingest.cursor() as cur:
        # static data first (flagged synthetic) - destination/ETA history for DARK STAR is built from real UPDATEs
        for mmsi, name, ship_type, length, width, draught in VESSELS:
            cur.execute(
                "INSERT INTO vessels (mmsi, name, ship_type, length_m, width_m, draught_m, is_synthetic) "
                "VALUES (%s,%s,%s,%s,%s,%s,true) ON CONFLICT (mmsi) DO NOTHING",
                (mmsi, name, ship_type, length, width, draught))
        # DARK STAR: a destination at the start (valid from T0-5 min) ...
        cur.execute("UPDATE vessels SET destination = 'HELSINKI', eta = %s, status_ts = %s WHERE mmsi = %s",
                    (t0 + timedelta(hours=6), t0 - timedelta(minutes=5), SYN["dark"]))
        # ... and a change reported while the ship was dark (valid from T0+100 min)
        cur.execute("UPDATE vessels SET destination = 'TALLINN', eta = %s, status_ts = %s WHERE mmsi = %s",
                    (t0 + timedelta(hours=8), t0 + timedelta(minutes=100), SYN["dark"]))
        # Same for the SLEEPER ship, it gets a destination of TURKU.
        cur.execute("UPDATE vessels SET destination = 'TURKU', eta = %s, status_ts = %s WHERE mmsi = %s",
                    (t0 + timedelta(hours=1), t0 - timedelta(minutes=5), SYN["sleeper"]))
    conn_ingest.commit()

    def stage_and_validate(batch: list[tuple]) -> dict:
        with conn_ingest.cursor() as cur:
            # This puts the batch of rows into staging_ais in big chunks.
            psycopg2.extras.execute_values(cur, STAGING_SQL, batch, page_size=1000)
        conn_ingest.commit()
        with conn_ingest.cursor() as cur:
            # This runs the checking function that moves the good rows to the real tables and gives the counts.
            cur.execute("SELECT processed, accepted, rejected, duplicates FROM validate_and_migrate_ais()")
            processed, accepted, rejected, dups = cur.fetchone()
        conn_ingest.commit()
        return {"processed": processed, "accepted": accepted, "rejected": rejected, "duplicates": dups}

    summary["main_batch"] = stage_and_validate(rows + bad)

    # a DUPLICATE: re-send a fix that is already stored (same vessel, same instant)
    dup = [r for r in rows if r[0] == SYN["glitch"]][2]
    summary["duplicate_batch"] = stage_and_validate([dup])

    with conn_ingest.cursor() as cur:
        # This opens, refreshes or expires the gaps for ships that went silent.
        cur.execute("SELECT opened, refreshed, expired FROM sync_open_gaps()")
        summary["gap_sync"] = dict(zip(("opened", "refreshed", "expired"), cur.fetchone()))
    conn_ingest.commit()
    return summary


# Stress test: what happens when 10,000 new records arrive? Measured, not claimed. It makes labelled
# fake ships (MMSI 7991xxxxx, source = 'stress'), stages the rows, runs validate_and_migrate_ais() once,
# prints the speed and the rejection mix, and then deletes everything again (unless --keep).
# Live data is never touched.

BASE_MMSI = 799100000


def build_stress_rows(n_rows: int, n_ships: int, bad_pct: float, rng: random.Random) -> list[tuple]:
    per_ship = max(1, n_rows // n_ships)
    start = datetime.now(timezone.utc) - timedelta(minutes=per_ship + 5)
    rows = []
    for s in range(n_ships):
        mmsi = BASE_MMSI + s
        lat, lon = rng.uniform(59.70, 60.00), rng.uniform(23.0, 28.0)
        course, knots = rng.uniform(0, 360), rng.uniform(6, 16)
        for i in range(per_ship):
            lat, lon = advance(lat, lon, course, knots, 60)
            lat = min(max(lat, 59.2), 60.6); lon = min(max(lon, 21.5), 30.0)
            rows.append([mmsi, lat, lon, round(knots, 1), round(course, 1), int(course) % 360, 0, start + timedelta(minutes=i), "stress"])
    rng.shuffle(rows)                                              # arrive out of order, like a real backlog
    n_bad = int(len(rows) * bad_pct / 100)
    for k in range(n_bad):                                         # corrupt a share of the rows in five different ways
        r, kind = rows[k], k % 5
        if kind == 0: r[0] = None                                  # NULL_MMSI
        elif kind == 1: r[1] = 95.0 + rng.random()                 # INVALID_COORDINATES
        elif kind == 2: r[1], r[2] = 57.0, 17.0                    # OUT_OF_AREA
        elif kind == 3: r[3] = 80.0                                # UNREALISTIC_SPEED
        else: r[7] = datetime.now(timezone.utc) + timedelta(hours=3)   # FUTURE_TIMESTAMP
    return [tuple(r) for r in rows]


def run_stress(args) -> int:
    rows = build_stress_rows(args.rows, args.ships, args.bad_percent, random.Random(42))
    ingest, owner = db.connect("ingest", app_name="stress"), db.connect("owner", app_name="stress")
    try:
        with ingest.cursor() as cur:
            for s in range(args.ships):                            # flag the fixtures as synthetic before any fix arrives
                cur.execute("INSERT INTO vessels (mmsi, name, is_synthetic) VALUES (%s, %s, true) ON CONFLICT DO NOTHING",
                            (BASE_MMSI + s, f"STRESS {s:04d}"))
        ingest.commit()

        t0 = time.perf_counter()
        with ingest.cursor() as cur:
            # Here we put all the stress rows into staging in big chunks.
            psycopg2.extras.execute_values(
                cur, "INSERT INTO staging_ais (mmsi, lat, lon, sog, cog, heading, nav_stat, event_time, source) VALUES %s",
                rows, page_size=2000)
        ingest.commit()
        t_stage = time.perf_counter() - t0

        t0 = time.perf_counter()
        with ingest.cursor() as cur:
            # Here the checking function runs once over all the staged stress rows.
            cur.execute("SELECT processed, accepted, rejected, duplicates FROM validate_and_migrate_ais(%s)", (len(rows) + 10,))
            processed, accepted, rejected, dups = cur.fetchone()
        ingest.commit()
        t_val = time.perf_counter() - t0

        # These three queries get the rejection reasons, how many rows are left in staging
        # and how many fixes were stored for the stress ships.
        reasons = db.query(owner, "SELECT rejection_reason, count(*) AS rows FROM rejected_events WHERE source = 'stress' GROUP BY 1 ORDER BY 2 DESC")
        left = db.scalar(owner, "SELECT count(*) FROM staging_ais WHERE source = 'stress'")
        events = db.scalar(owner, "SELECT count(*) FROM location_events le JOIN vessels v USING (mmsi) WHERE v.mmsi >= %s AND v.mmsi < %s", (BASE_MMSI, BASE_MMSI + 1_000_000))
        owner.rollback()

        print(f"rows staged            : {len(rows):,}   ({t_stage:.2f} s, {len(rows) / t_stage:,.0f} rows/s)")
        print(f"validation + migration : {processed:,} processed in {t_val:.2f} s  ->  {processed / t_val:,.0f} rows/s   (one atomic transaction)")
        print(f"accepted               : {accepted:,}   (location_events now holds {events:,} stress fixes)")
        print(f"rejected               : {rejected:,}   duplicates: {dups}")
        db.print_table(reasons)
        print(f"left in staging        : {left}  (must be 0)")
        print(f"\n=> 10,000 rows take about {10_000 * t_val / max(processed, 1):.1f} s to validate; the live feed delivers ~300-600 fixes per minute.")
        ok = left == 0 and accepted + rejected == processed
        print("CHECK accepted + rejected == processed and staging empty:", "PASS" if ok else "FAIL")
    finally:
        if not args.keep:
            with owner.cursor() as cur:
                # Here we delete the stress ships and any rows they left behind.
                cur.execute("DELETE FROM vessels WHERE mmsi >= %s AND mmsi < %s", (BASE_MMSI, BASE_MMSI + 1_000_000))
                cur.execute("DELETE FROM rejected_events WHERE source = 'stress'")
                cur.execute("DELETE FROM staging_ais WHERE source = 'stress'")
            owner.commit()
            print("stress fixtures removed")
        ingest.close(); owner.close()
    return 0


# DBA lab: the locking experiment and the index experiment. The terminal demo and the web page both use them.
# All timings are measured on the running database, nothing here is made up.
LOCK_TEST_MMSI = 799000099
Emit = Callable[[str, list | None], None]


# Locking experiment
def run_concurrency(emit: Emit | None = None) -> dict:
    """A locks one ship's row. B tries without waiting, C skips locked rows, D has to wait. Then A finishes.

    It uses its own test ship (MMSI 799000099), so no real ship is touched."""
    events: list[dict] = []
    t_start = time.monotonic()

    def say(msg: str, rows: list | None = None) -> None:
        events.append({"t": round(time.monotonic() - t_start, 2), "message": msg, "rows": rows})
        if emit:
            emit(msg, rows)

    a, b, c, d, mon, admin = (db.connect("owner", app_name=n) for n in
                              ("session-A", "session-B", "session-C", "session-D", "monitor", "admin"))
    mon.autocommit = True
    try:
        with admin.cursor() as cur:
            # Here we make the test ship and give it a start position so there is a row to lock.
            cur.execute("INSERT INTO vessels (mmsi, name, is_synthetic) VALUES (%s, 'SYN LOCK TEST', true) ON CONFLICT DO NOTHING",
                        (LOCK_TEST_MMSI,))
            cur.execute("DELETE FROM latest_position WHERE mmsi = %s", (LOCK_TEST_MMSI,))
            cur.execute("INSERT INTO latest_position (mmsi, geom, speed_knots, last_seen) "
                        "VALUES (%s, ST_SetSRID(ST_MakePoint(25.0, 60.0), 4326), 13.0, now())", (LOCK_TEST_MMSI,))
        admin.commit()

        with a.cursor() as cur:
            # Person A locks the row with FOR UPDATE so nobody else can change it.
            cur.execute("SELECT speed_knots FROM latest_position WHERE mmsi = %s FOR UPDATE", (LOCK_TEST_MMSI,))
            say(f"Person A opens the test ship and locks it so nobody else can change it (its speed is {cur.fetchone()[0]} knots).")

        try:
            with b.cursor() as cur:
                # Person B asks for the lock with NOWAIT, so it fails at once if the row is locked.
                cur.execute("SELECT 1 FROM latest_position WHERE mmsi = %s FOR UPDATE NOWAIT", (LOCK_TEST_MMSI,))
            say("Person B got the lock although A holds it (this should not happen).")
        except psycopg2.errors.LockNotAvailable as exc:
            say("Person B tries to take the same ship without waiting. Refused straight away: the ship is locked by A.")
        b.rollback()

        with c.cursor() as cur:
            # Person C asks with SKIP LOCKED, so locked rows are just left out.
            cur.execute("SELECT 1 FROM latest_position WHERE mmsi = %s FOR UPDATE SKIP LOCKED", (LOCK_TEST_MMSI,))
            say(f"Person C asks for ships but is told to skip locked ones. C gets {len(cur.fetchall())} ships. This is how several workers avoid colliding.")
        c.rollback()

        result: dict = {}

        def writer() -> None:
            t = time.monotonic()
            with d.cursor() as cur:
                # Person D tries to change the speed, so it has to wait for A.
                cur.execute("UPDATE latest_position SET speed_knots = 9.0 WHERE mmsi = %s", (LOCK_TEST_MMSI,))
            d.commit()
            result["waited"] = time.monotonic() - t

        th = threading.Thread(target=writer)
        th.start()
        time.sleep(1.2)
        with mon.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # This reads the view that shows which connections are waiting and who is blocking them.
            cur.execute("""SELECT blocked_pid AS "waiting connection", blocking_pid AS "held up by", waiting_s AS "seconds waiting",
                                  CASE WHEN blocked_query ILIKE 'UPDATE%' THEN 'change the ship speed' ELSE 'read or change data' END AS "wants to do"
                             FROM v_mon_blocking""")
            blocked = [dict(r) for r in cur.fetchall()]
        say("Person D tries to change the speed to 9. D has to wait because A still holds the lock. This is who is waiting:", blocked)

        with a.cursor() as cur:
            # Person A changes the speed to 11 and then commits.
            cur.execute("UPDATE latest_position SET speed_knots = 11.0 WHERE mmsi = %s", (LOCK_TEST_MMSI,))
        a.commit()
        say("Person A changes the speed to 11 and finishes. The lock is released.")
        th.join(10)
        say(f"Person D now gets its turn after waiting {result.get('waited', float('nan')):.2f} seconds. Both changes were applied, one after the other. Nothing was lost.")
        with mon.cursor() as cur:
            # This reads the final speed after everything is done.
            cur.execute("SELECT speed_knots FROM latest_position WHERE mmsi = %s", (LOCK_TEST_MMSI,))
            final = float(cur.fetchone()[0])
        say(f"The final speed is {final} knots: A wrote 11, then D wrote 9, in that order.")
    finally:
        for conn in (a, b, c, d, mon):
            try:
                conn.rollback()
            except psycopg2.Error:
                pass
            conn.close()
        try:
            with admin.cursor() as cur:
                cur.execute("DELETE FROM vessels WHERE mmsi = %s", (LOCK_TEST_MMSI,))     # cascades to latest_position
            admin.commit()
        finally:
            admin.close()
    return {"events": events, "final_speed": final, "waited_s": result.get("waited")}


# Index experiment
# These are the two queries we time with and without indexes.
BENCH_QUERIES = {
    "history": ("Find one ship’s movements",
                "SELECT event_id, event_time, speed_knots FROM bench_location_events "
                "WHERE mmsi = 230000042 AND event_time > now() - interval '3 days' ORDER BY event_time DESC"),
    "spatial": ("Find ships within 10 km of Helsinki",
                "SELECT count(*) FROM bench_location_events WHERE ST_DWithin(geom::geography, "
                "ST_SetSRID(ST_MakePoint(24.94, 60.17), 4326)::geography, 10000)"),
}


def bench_state(conn) -> dict:
    # These queries check if the benchmark table exists, how many rows it has and which indexes are on it.
    exists = db.scalar(conn, "SELECT to_regclass('public.bench_location_events') IS NOT NULL")
    if not exists:
        return {"exists": False, "rows": 0, "indexes": []}
    rows = db.scalar(conn, "SELECT count(*) FROM bench_location_events")
    idx = [r["indexname"] for r in db.query(conn, "SELECT indexname FROM pg_indexes WHERE tablename = 'bench_location_events'")]
    return {"exists": True, "rows": rows, "indexes": idx}


def bench_build(owner, rows: int = 400_000) -> dict:
    owner.autocommit = True
    try:
        # This calls the database function that builds the benchmark table with this many rows.
        n = db.scalar(owner, "SELECT dba_build_benchmark(%s)", (rows,))
    finally:
        owner.autocommit = False
    return {"rows": n}


def bench_indexes(owner, create: bool) -> dict:
    owner.autocommit = True
    try:
        # Here we create the two indexes (or drop them) and then run ANALYZE so the planner has fresh numbers.
        with owner.cursor() as cur:
            if create:
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bench_mmsi_time ON bench_location_events (mmsi, event_time DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS gist_bench_geom ON bench_location_events USING gist ((geom::geography))")
            else:
                cur.execute("DROP INDEX IF EXISTS idx_bench_mmsi_time")
                cur.execute("DROP INDEX IF EXISTS gist_bench_geom")
            cur.execute("ANALYZE bench_location_events")
    finally:
        owner.autocommit = False
    return bench_state(owner)


def explain_sql(conn, sql: str, force_seq: bool = False, params=None) -> dict:
    """Run EXPLAIN ANALYZE twice (the second run has a warm cache) and return the plan and the time."""
    plan: list[str] = []
    # Here we run EXPLAIN ANALYZE twice and keep the last plan.
    # With force_seq we turn off index scans so the slow plan shows up.
    with conn.cursor() as cur:
        if conn.autocommit:
            cur.execute("BEGIN")
        if force_seq:
            cur.execute("SET LOCAL enable_indexscan = off; SET LOCAL enable_indexonlyscan = off; SET LOCAL enable_bitmapscan = off")
        for _ in range(2):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
            plan = [r[0] for r in cur.fetchall()]
        cur.execute("ROLLBACK")
    text = "\n".join(plan)
    ms = float(re.search(r"Execution Time: ([\d.]+) ms", text).group(1))
    nodes = re.findall(r"(Seq Scan|Index Only Scan|Index Scan|Bitmap Index Scan|Bitmap Heap Scan|Parallel Seq Scan)", text)
    return {"plan": plan, "execution_ms": ms, "scan_types": list(dict.fromkeys(nodes))}


# Printing helpers for the terminal demo
def banner(title: str) -> None:
    print("\n" + "=" * 110)
    print(f"  {title}")
    print("=" * 110)


def sub(title: str) -> None:
    print(f"\n--- {title}")


def show(conn, sql: str, params=None, columns=None, max_width: int = 38, show_sql: bool = True) -> list[dict]:
    if show_sql:
        with conn.cursor() as cur:
            text = cur.mogrify(sql, params).decode() if params is not None else sql
        lines = textwrap.wrap(" ".join(text.split()), width=104, break_long_words=False)
        for i, ln in enumerate(lines):
            print(("  SQL> " if i == 0 else "       ") + ln)
    # This runs the query and gets the rows back.
    rows = db.query(conn, sql, params)
    conn.rollback()
    db.print_table(rows, columns, max_width=max_width, indent="  ")
    return rows


def explain(conn, sql: str, label: str) -> tuple[float, list[str]]:
    """Run EXPLAIN ANALYZE twice (the second run has a warm cache), print the plan and return the time."""
    # This runs EXPLAIN ANALYZE two times and keeps the plan from the last run.
    for _ in range(2):
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
            plan = [r[0] for r in cur.fetchall()]
    conn.rollback()
    print(f"  [{label}]")
    for ln in plan:
        print("    " + ln)
    ms = float(re.search(r"Execution Time: ([\d.]+) ms", "\n".join(plan)).group(1))
    return ms, plan


# The sections of the demo, in order
def section_environment(owner) -> None:
    banner("0. ENVIRONMENT CHECK")
    # This lists the installed extensions we need.
    show(owner, "SELECT extname, extversion FROM pg_extension WHERE extname IN ('postgis','btree_gist') ORDER BY 1")
    # This gets the PostGIS version text.
    show(owner, "SELECT postgis_full_version() AS postgis", max_width=100, show_sql=False)
    # This lists every table in the public schema with a rough row count.
    tables = db.query(owner, """
        SELECT c.relname AS table_name, c.reltuples::bigint AS approx_rows
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1""")
    print(f"\n  {len(tables)} tables in schema public")
    # This counts the land parts and vertices for each land source.
    show(owner, "SELECT source, count(*) AS parts, sum(ST_NPoints(geom)) AS vertices FROM land_polygons GROUP BY source")


def section_live(skip: bool) -> None:
    banner("1. LIVE DATA: one real poll of Digitraffic (positions, vessels, sea state) and FMI (wind, waves)")
    if skip:
        print("  (skipped with --skip-live)")
        return
    logging_ok = True
    try:
        results = collector.Collector(["positions", "vessels", "weather", "sea_state"]).run_once()
    except Exception as exc:  # the demo must continue offline
        logging_ok = False
        print(f"  live poll failed ({exc}); continuing with stored data")
    if logging_ok:
        owner = db.connect("owner")
        sub("collector_heartbeat - proof the collection ran")
        # This shows the newest heartbeat rows from the collector.
        show(owner, "SELECT feed, status, rows_fetched, rows_ingested, rows_rejected, duration_ms, message FROM collector_heartbeat ORDER BY heartbeat_id DESC LIMIT 10",
             max_width=70)
        sub("sample of REAL ships stored right now (latest_position)")
        # This shows a few real ships and where they were last seen.
        show(owner, """SELECT v.mmsi, v.name, v.destination, lp.speed_knots, lp.last_seen
                         FROM latest_position lp JOIN vessels v USING (mmsi)
                        WHERE NOT v.is_synthetic AND v.name IS NOT NULL ORDER BY lp.last_seen DESC LIMIT 6""")
        owner.close()


def section_seed(ingest, owner) -> dict:
    banner("2. SYNTHETIC FIXTURES through the real pipeline (staging_ais -> validate_and_migrate_ais -> triggers)")
    summary = seed(ingest, owner)
    print(f"  T0 = {summary['t0']:%Y-%m-%d %H:%M:%S} UTC  (fixtures cover the last 200 minutes)")
    print(f"  main batch      : {summary['main_batch']}")
    print(f"  duplicate batch : {summary['duplicate_batch']}")
    print(f"  open-gap sync   : {summary['gap_sync']}")
    return summary


def section_validation(owner) -> None:
    banner("3. VALIDATION LOG: what the database accepted and why it rejected the rest  (Track B)")
    sub("rejected_events grouped by reason (fixtures only)")
    # This counts the rejected demo rows for each reason.
    show(owner, """SELECT rejection_reason, count(*) AS rows, min(detail) AS example_detail
                     FROM rejected_events WHERE source = 'synthetic' GROUP BY rejection_reason ORDER BY 2 DESC""", max_width=80)
    sub("every rejected fixture row with the exact reason")
    # This lists every rejected demo row with its reason.
    show(owner, """SELECT rejected_id, mmsi, lat, lon, sog, rejection_reason, detail
                     FROM rejected_events WHERE source = 'synthetic' ORDER BY rejected_id""", max_width=60)
    sub("the impossible jump, proven: the teleporting fix was rejected, the next real fix was accepted")
    # This shows the stored positions of the glitch ship, so we can see the jump was left out.
    show(owner, """SELECT event_time, round(ST_Y(geom)::numeric, 4) AS lat, round(ST_X(geom)::numeric, 4) AS lon, speed_knots
                     FROM location_events WHERE mmsi = %s ORDER BY event_time LIMIT 9""", (SYN["glitch"],))
    sub("pipeline totals - LIVE data (real feeds)")
    # This counts the live fixes, live rejections, rows still in staging and live vessels.
    show(owner, """SELECT (SELECT count(*) FROM location_events le JOIN vessels v USING (mmsi) WHERE NOT v.is_synthetic) AS live_fixes_stored,
                          (SELECT count(*) FROM rejected_events WHERE source = 'digitraffic') AS live_rejected,
                          (SELECT count(*) FROM staging_ais) AS still_in_staging,
                          (SELECT count(*) FROM vessels WHERE NOT is_synthetic) AS live_vessels""")


def section_mobile(owner) -> None:
    banner("4. MOBILE QUERIES: latest position and movement history of one ship  (Track B - primary)")
    sub("latest position (one row per ship, primary-key lookup)")
    # This gets the latest position of the dark ship.
    show(owner, """SELECT mmsi, round(ST_Y(geom)::numeric, 4) AS lat, round(ST_X(geom)::numeric, 4) AS lon, speed_knots, course, last_seen
                     FROM latest_position WHERE mmsi = %s""", (SYN["dark"],))
    sub("movement history: the trajectory with distance and implied speed between consecutive fixes")
    # This gets the last fixes of the ship with distance and time since the fix before.
    show(owner, """SELECT event_time, round(ST_Y(geom)::numeric, 4) AS lat, round(ST_X(geom)::numeric, 4) AS lon, speed_knots AS sog,
                          round((ST_Distance(geom::geography, LAG(geom) OVER w::geography) / 1852.0)::numeric, 2) AS nm_from_prev,
                          round((EXTRACT(EPOCH FROM event_time - LAG(event_time) OVER w) / 60.0)::numeric, 1) AS min_since_prev
                     FROM location_events WHERE mmsi = %s WINDOW w AS (ORDER BY event_time)
                    ORDER BY event_time DESC LIMIT 8""", (SYN["dark"],))
    sub("the same ship's whole track as a line, with its length")
    # This joins all fixes of the ship into one line and gets its length.
    show(owner, """SELECT count(*) AS fixes, round((ST_Length(ST_MakeLine(geom ORDER BY event_time)::geography) / 1852.0)::numeric, 1) AS track_nm,
                          min(event_time) AS first_fix, max(event_time) AS last_fix
                     FROM location_events WHERE mmsi = %s""", (SYN["dark"],))


def section_temporal(owner, ingest) -> None:
    banner("5. TEMPORAL (SCD-2): destination / ETA history with valid time and recorded time  (Track A)")
    mmsi = SYN["dark"]
    # This query is used twice to show the destination and ETA history of the ship.
    hist_sql = """SELECT history_id, destination, to_char(eta, 'MM-DD HH24:MI') AS eta, to_char(valid_from, 'HH24:MI:SS') AS valid_from,
                         to_char(valid_to, 'HH24:MI:SS') AS valid_to, to_char(recorded_at, 'HH24:MI:SS') AS recorded_at,
                         to_char(superseded_at, 'HH24:MI:SS') AS superseded_at, is_current
                    FROM vessel_status_history WHERE mmsi = %s ORDER BY valid_from"""
    sub("BEFORE: two versions exist (HELSINKI -> TALLINN, the change was reported while the ship was dark)")
    show(owner, hist_sql, (mmsi,))
    sub("EVENT: a new static message arrives - the ETA slips by 45 minutes (UPDATE vessels, as the ingest role)")
    with ingest.cursor() as cur:
        # This moves the ETA of the dark ship by 45 minutes so the history trigger fires.
        cur.execute("UPDATE vessels SET eta = eta + interval '45 minutes', status_ts = now() WHERE mmsi = %s", (mmsi,))
    ingest.commit()
    print("  SQL> UPDATE vessels SET eta = eta + interval '45 minutes', status_ts = now() WHERE mmsi = 799000003;")
    print("       (trigger trg_vessel_status_upd -> track_vessel_status_scd2(): close old row, insert new row)")
    sub("AFTER: old row closed (valid_to set, is_current = no), new current row inserted, nothing deleted")
    show(owner, hist_sql, (mmsi,))
    sub("current value  /  value as of a past moment  /  audit of what the database knew when")
    # This gets the current destination and ETA of the ship.
    show(owner, "SELECT destination, eta, valid_from FROM vessel_status_history WHERE mmsi = %s AND is_current", (mmsi,))
    t0 = owner_t0(owner)
    # This asks what the destination was at one past moment.
    show(owner, "SELECT destination, valid_from, valid_to FROM fn_vessel_status_as_of(%s, %s)", (mmsi, t0 + timedelta(minutes=60)))
    print("       ^ at T0+60 min the plan said HELSINKI;  below, T0+110 min (after the change made while dark):")
    # Same function again, for a later moment.
    show(owner, "SELECT destination, valid_from, valid_to FROM fn_vessel_status_as_of(%s, %s)", (mmsi, t0 + timedelta(minutes=110)),
         show_sql=False)
    sub("integrity: the database itself refuses overlapping versions (EXCLUDE constraint)")
    try:
        with owner.cursor() as cur:
            # This tries to insert an overlapping history row. The database should refuse it.
            cur.execute("INSERT INTO vessel_status_history (mmsi, destination, valid_from, valid_to, is_current) "
                        "SELECT mmsi, 'OVERLAP', valid_from + interval '1 minute', valid_from + interval '5 minutes', false "
                        "FROM vessel_status_history WHERE mmsi = %s AND NOT is_current LIMIT 1", (mmsi,))
        print("  UNEXPECTED: overlap accepted")
    except psycopg2.errors.ExclusionViolation as exc:
        print(f"  rejected as expected -> {str(exc).splitlines()[0]}")
    owner.rollback()


def owner_t0(owner) -> datetime:
    # This gets the time of the first fix of the dark ship.
    row = db.query(owner, "SELECT min(event_time) AS t FROM location_events WHERE mmsi = %s", (SYN["dark"],))
    owner.rollback()
    return row[0]["t"]


def section_ports(owner) -> None:
    banner("6. PORT EVENTS from the trigger check_port_proximity()  +  spatial queries  (Track C)")
    sub("port ENTRY / EXIT generated automatically while the fixtures were inserted")
    # This lists the port entry and exit events of the demo ships.
    show(owner, """SELECT pe.event_id, v.name, p.name AS port, pe.event_type, pe.event_time, pe.distance_m
                     FROM port_events pe JOIN vessels v USING (mmsi) JOIN ports p USING (port_id)
                    WHERE v.is_synthetic ORDER BY pe.event_time""")
    sub("nearest port to the DARK STAR's last fix before it went silent (KNN operator <-> on the GiST index)")
    # This finds the 3 ports closest to where the dark ship was last seen.
    show(owner, """SELECT p.name, round((ST_Distance(p.geom::geography, g.start_geom::geography) / 1000)::numeric, 1) AS km
                     FROM ports p, (SELECT start_geom FROM ais_gap_events WHERE mmsi = %s ORDER BY gap_id DESC LIMIT 1) g
                    ORDER BY p.geom <-> g.start_geom LIMIT 3""", (SYN["dark"],))
    sub("ships within 8 km of Helsinki port right now (ST_DWithin on geography, GiST-indexed)")
    # This finds ships within 8 km of the Helsinki port.
    show(owner, """SELECT v.name, round((ST_Distance(lp.geom::geography, p.geom::geography) / 1000)::numeric, 1) AS km, lp.last_seen
                     FROM latest_position lp JOIN vessels v USING (mmsi), ports p
                    WHERE p.locode = 'FIHEL' AND ST_DWithin(lp.geom::geography, p.geom::geography, 8000)
                    ORDER BY km LIMIT 6""")


def section_dark(owner) -> None:
    banner("7. GOING DARK DETECTOR: silent ships, with ports / receiver outages / berthed ships excluded")
    sub("classify_silent_vessels(20) - every ship silent > 20 min and the verdict + evidence (fixtures)")
    # This runs the classifier for ships silent over 20 minutes (demo ships only).
    show(owner, """SELECT mmsi, vessel_name, silent_minutes, last_speed_knots AS kn, classification, port_name,
                          neighbours_before AS nb, neighbours_silent AS nb_silent
                     FROM classify_silent_vessels(20) WHERE mmsi >= 799000000 ORDER BY classification""")
    sub("detect_going_dark_vessels(20) - only the ones that survive every exclusion (live + fixtures)")
    # This gets only the ships the detector marks as going dark.
    rows = show(owner, """SELECT mmsi, vessel_name, silent_minutes, last_speed_knots AS kn, neighbours_before AS nb,
                                 neighbours_silent AS nb_silent, round(ST_Y(last_geom)::numeric, 3) AS lat, round(ST_X(last_geom)::numeric, 3) AS lon
                            FROM detect_going_dark_vessels(20) ORDER BY silent_minutes""")
    live = [r for r in rows if r["mmsi"] < 799000000]
    print(f"  -> {len(rows)} dark ship(s): {len(rows) - len(live)} synthetic fixture(s) + {len(live)} real vessel(s) from the live feed")
    sub("breakdown of ALL silent ships by verdict - live data")
    # This counts the live silent ships for each verdict.
    show(owner, "SELECT classification, count(*) AS ships FROM classify_silent_vessels(20) WHERE mmsi < 799000000 GROUP BY 1 ORDER BY 2 DESC")
    sub("gap book-keeping: closed gaps (ship reappeared) and open gaps (still silent) for the fixtures")
    # This lists the demo gaps with their status and class.
    show(owner, """SELECT g.gap_id, v.name, g.status, g.classification, g.gap_duration_minutes AS minutes,
                          to_char(g.last_seen_time, 'HH24:MI') AS last_seen, to_char(g.reappear_time, 'HH24:MI') AS reappeared,
                          g.neighbours_before AS nb, g.neighbours_silent AS nb_silent, g.is_dark_activity AS dark
                     FROM ais_gap_events g JOIN vessels v USING (mmsi) WHERE v.is_synthetic ORDER BY g.gap_id""")


def section_feasibility(owner) -> None:
    banner("8. FEASIBILITY REGION: where could the ship have been?  (circle ∩ circle, land removed, conditions-adjusted)")
    # This gets the ids of the demo gaps that are dark activity.
    ids = [r["gap_id"] for r in db.query(owner, """
        SELECT g.gap_id FROM ais_gap_events g JOIN vessels v USING (mmsi)
         WHERE v.is_synthetic AND g.is_dark_activity ORDER BY g.gap_id""")]
    owner.rollback()
    results = [collector.compute_gap(owner, i) for i in ids]
    sub("engine output per dark gap")
    db.print_table(results, indent="  ")
    sub("how the numbers were built (v_eff = v_max x speed_factor,  R = v_eff x dT)")
    # This shows the speed and radius numbers for each gap.
    show(owner, """SELECT gap_id, status, gap_duration_minutes AS minutes, v_max_knots, speed_factor, v_eff_knots, reach_radius_km AS r_km,
                          observed_distance_km AS p1_p2_km, implied_speed_knots AS implied_kn, feasibility_status
                     FROM ais_gap_events WHERE gap_id = ANY(%s) ORDER BY gap_id""", (ids,))
    sub("the conditions that were applied (wind / waves / basis) - the factor table is an ASSUMPTION, labelled as such")
    # This shows the wind and wave values used for each gap.
    show(owner, """SELECT gap_id, conditions->>'ship_class' AS class, conditions->>'wind_ms' AS wind_ms, conditions->>'wave_m' AS wave_m,
                          conditions->>'wind_factor' AS wind_f, conditions->>'wave_factor' AS wave_f, conditions->>'basis' AS basis
                     FROM ais_gap_events WHERE gap_id = ANY(%s) ORDER BY gap_id""", (ids,), max_width=70)
    sub("geometry checks: the water-only region contains the observed dots, contains no land, and is smaller than the raw circles")
    # These are the geometry checks for each gap: dots inside the region, land overlap and areas.
    show(owner, """SELECT g.gap_id,
                          ST_Contains(g.tight_geom, g.start_geom) AS p1_in_tight,
                          CASE WHEN g.end_geom IS NULL THEN NULL ELSE ST_Contains(g.tight_geom, g.end_geom) END AS p2_in_tight,
                          round((COALESCE(ST_Area(ST_Intersection(g.feasible_geom, (SELECT ST_Union(t.geom) FROM land_tiles t WHERE t.geom && g.feasible_geom))::geography), 0) / 1e6)::numeric, 3) AS land_overlap_km2,
                          g.feasible_area_km2 AS lens_km2, g.tight_area_km2 AS tight_km2, g.land_removed_km2
                     FROM ais_gap_events g WHERE g.gap_id = ANY(%s) ORDER BY g.gap_id""", (ids,))
    print("\n  Honest reading: this is FEASIBILITY, not prediction. The ellipse (tight) is the exact straight-line envelope;")
    print("  the circle intersection is the looser region requested by the brief. pgRouting would shrink both further.")


def section_dba(owner, skip_bench: bool) -> None:
    banner("9. DBA LAYER")
    sub("9a  Indexing: EXPLAIN (ANALYZE, BUFFERS) on a 400,000-row copy - measured here, not illustrative")
    if skip_bench:
        print("  (skipped with --skip-bench)")
    else:
        owner.autocommit = True
        # This calls the function that builds the 400,000 row benchmark table.
        n = db.scalar(owner, "SELECT dba_build_benchmark(400000)")
        print(f"  benchmark table: {n:,} rows, no indexes yet")
        # These are the two test queries for the before and after timing.
        q1 = ("SELECT event_id, event_time, speed_knots FROM bench_location_events "
              "WHERE mmsi = 230000042 AND event_time > now() - interval '3 days' ORDER BY event_time DESC")
        q2 = ("SELECT count(*) FROM bench_location_events WHERE ST_DWithin(geom::geography, "
              "ST_SetSRID(ST_MakePoint(24.94, 60.17), 4326)::geography, 10000)")
        print("\n  Q1 = movement history of one ship;  Q2 = ships within 10 km of Helsinki")
        b1, _ = explain(owner, q1, "Q1 BEFORE - no index")
        b2, _ = explain(owner, q2, "Q2 BEFORE - no index")
        # Here we create the two indexes and run ANALYZE.
        with owner.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_bench_mmsi_time ON bench_location_events (mmsi, event_time DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS gist_bench_geom ON bench_location_events USING gist ((geom::geography))")
            cur.execute("ANALYZE bench_location_events")
        print("\n  CREATE INDEX idx_bench_mmsi_time ON bench_location_events (mmsi, event_time DESC);")
        print("  CREATE INDEX gist_bench_geom ON bench_location_events USING gist ((geom::geography));\n")
        a1, _ = explain(owner, q1, "Q1 AFTER  - B-tree")
        a2, _ = explain(owner, q2, "Q2 AFTER  - GiST")
        owner.autocommit = False
        print(f"\n  measured: Q1 {b1:.2f} ms -> {a1:.2f} ms  ({b1 / max(a1, 0.001):.0f}x faster)    "
              f"Q2 {b2:.2f} ms -> {a2:.2f} ms  ({b2 / max(a2, 0.001):.0f}x faster)")

    sub("9b  Concurrency: two sessions fight over one ship's row (SELECT ... FOR UPDATE)")
    concurrency_demo()

    sub("9c  RBAC: each role attempts allowed and forbidden statements (rolled back afterwards)")
    # This runs the function that tries allowed and forbidden statements for each role.
    show(owner, "SELECT role_name, left(attempted_statement, 62) AS attempted, expected, outcome, verdict FROM dba_rbac_proof()",
         max_width=64, show_sql=False)

    sub("9d  Monitoring (read through the read-only analyst login): sessions, cache hit ratio, table health")
    analyst = db.connect("analyst", app_name="monitor")
    # These four queries read the monitoring views as the analyst login:
    # sessions, cache hits, table health and collector health.
    show(analyst, "SELECT pid, usename, application_name, state, wait_event_type FROM v_mon_activity ORDER BY pid", max_width=30)
    show(analyst, "SELECT object AS database, blks_hit, blks_read, hit_pct FROM v_mon_cache_hit WHERE scope = 'database'")
    show(analyst, "SELECT table_name, live_rows, dead_rows, hot_updates, total_size FROM v_mon_table_health ORDER BY total_bytes DESC LIMIT 6")
    show(analyst, "SELECT feed, status, minutes_ago, rows_fetched, rows_ingested FROM v_collector_health")
    analyst.close()

    sub("9e  Backup / recovery: run  bash backup_restore.sh  (pg_dump -Fc, pg_restore --list, restore to a scratch DB, row-count diff)")


def concurrency_demo() -> None:
    def emit(message: str, rows) -> None:
        print(f"  {message}")
        if rows:
            db.print_table(rows, indent="             ")
    run_concurrency(emit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-live", action="store_true", help="do not call the live APIs")
    parser.add_argument("--skip-bench", action="store_true", help="skip the 400k-row index benchmark")
    parser.add_argument("--stress", action="store_true", help="run only the stress test")
    parser.add_argument("--rows", type=int, default=10_000, help="with --stress: how many rows")
    parser.add_argument("--ships", type=int, default=500, help="with --stress: how many ships")
    parser.add_argument("--bad-percent", type=float, default=2.0, help="with --stress: share of bad records")
    parser.add_argument("--keep", action="store_true", help="with --stress: keep the generated rows")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.stress:
        return run_stress(args)

    started = time.monotonic()
    owner = db.connect("owner", app_name="simulate_demo")
    ingest = db.connect("ingest", app_name="simulate_demo")
    try:
        section_environment(owner)
        section_live(args.skip_live)
        section_seed(ingest, owner)
        section_validation(owner)
        section_mobile(owner)
        section_temporal(owner, ingest)
        section_ports(owner)
        section_dark(owner)
        section_feasibility(owner)
        section_dba(owner, args.skip_bench)
    finally:
        owner.close()
        ingest.close()
    banner(f"DONE in {time.monotonic() - started:.0f}s - synthetic fixtures stay in the database so the web UI can show them "
           f"(python app.py).  Remove them with:  python -c \"import db, simulate_demo as d; print(d.reset(db.connect()))\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
