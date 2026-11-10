
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps

import math

import psycopg2
import requests
from psycopg2 import sql as pgsql
from flask import Flask, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

import db
import collector
import simulate_demo

log = logging.getLogger("dsw.web")
SYN_MIN = db.SYNTHETIC_MMSI_MIN


class Provider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, timedelta):
            return o.total_seconds()
        return DefaultJSONProvider.default(o)


app = Flask(__name__, static_folder=str(db.BASE_DIR / "web"), static_url_path="/static")
app.json = Provider(app)


# A small pool of read-only database connections, plus a short-lived cache
class ReadPool:
    def __init__(self, size: int = 6):
        self.size = size
        self.q: queue.LifoQueue = queue.LifoQueue()
        self.created = 0
        self.lock = threading.Lock()

    def _new(self):
        conn = db.connect("analyst", autocommit=True, app_name="webui")
        conn.set_session(readonly=True, autocommit=True)
        return conn

    @contextmanager
    def get(self):
        conn = None
        try:
            conn = self.q.get_nowait()
        except queue.Empty:
            with self.lock:
                can_create = self.created < self.size
                if can_create:
                    self.created += 1
            if can_create:
                try:
                    conn = self._new()
                except Exception:
                    with self.lock:
                        self.created -= 1
                    raise
            else:
                conn = self.q.get(timeout=15)
        if conn.closed or not db.is_alive(conn):
            try:
                conn.close()
            except psycopg2.Error:
                pass
            conn = self._new()
        try:
            yield conn
        finally:
            self.q.put(conn)


pool = ReadPool()
_cache: dict[str, tuple[float, object]] = {}


def cached(ttl: float):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            key = fn.__name__ + repr((a, sorted(kw.items()), sorted(request.args.items())))
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = fn(*a, **kw)
            _cache[key] = (time.monotonic(), value)
            return value
        return wrapper
    return deco


def q(sql: str, params=None) -> list[dict]:
    with pool.get() as conn:
        # This runs the query and gives back the rows as a list of dicts.
        return db.query(conn, sql, params)


def one(sql: str, params=None) -> dict:
    rows = q(sql, params)
    return rows[0] if rows else {}


def include_syn() -> bool:
    return request.args.get("synthetic", "1") not in ("0", "false", "no")


@app.errorhandler(Exception)
def on_error(exc):
    if isinstance(exc, HTTPException):          # errors like "not found" keep their own status
        return exc
    log.exception("request failed")
    return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


# web pages
@app.get("/")
def index():
    """Send index.html with the change time added to every file link, so the browser always loads the newest files."""
    import re
    from pathlib import Path
    root = Path(app.static_folder)
    html = (root / "index.html").read_text(encoding="utf-8")

    def stamp(m):
        f = root / m.group(1)
        return f'/static/{m.group(1)}?v={int(f.stat().st_mtime)}' if f.exists() else m.group(0)

    html = re.sub(r"/static/([\w./-]+\.(?:css|js))", stamp, html)
    return app.response_class(html, mimetype="text/html")


@app.after_request
def no_stale_assets(resp):
    """Ask the browser to re-check pages and files every time, so changes show on the next reload."""
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/api/health")
def health():
    # This asks the database for the time, the login name and the version.
    return jsonify(one("SELECT now() AS server_time, current_user AS db_user, version() AS version"))


# the number strip and the ships on the map
@app.get("/api/summary")
@cached(8)
def summary():
    live = "NOT v.is_synthetic"
    # Here we count everything for the number strip, all in one query.
    # The real ships only are counted, the demo ones are left out.
    row = one(f"""
        SELECT
          (SELECT count(*) FROM latest_position lp JOIN vessels v USING (mmsi)
            WHERE {live} AND lp.last_seen > now() - interval '20 minutes')                       AS active_now,
          (SELECT count(*) FROM latest_position lp JOIN vessels v USING (mmsi)
            WHERE {live} AND lp.last_seen > now() - interval '6 hours')                          AS tracked_6h,
          (SELECT count(*) FROM vessels v WHERE {live})                                          AS vessels_known,
          (SELECT count(*) FROM location_events le JOIN vessels v USING (mmsi) WHERE {live})     AS fixes_stored,
          (SELECT count(*) FROM location_events le JOIN vessels v USING (mmsi)
            WHERE {live} AND le.ingested_at > now() - interval '24 hours')                       AS fixes_24h,
          (SELECT count(*) FROM rejected_events WHERE source = 'digitraffic')                    AS rejected_total,
          (SELECT count(*) FROM rejected_events WHERE source = 'digitraffic'
              AND rejected_at > now() - interval '24 hours')                                     AS rejected_24h,
          (SELECT count(*) FROM staging_ais)                                                     AS staging_backlog,
          (SELECT count(*) FROM ais_gap_events g JOIN vessels v USING (mmsi)
            WHERE g.is_dark_activity AND g.status = 'OPEN' AND {live})                           AS dark_open,
          (SELECT count(*) FROM ais_gap_events g JOIN vessels v USING (mmsi)
            WHERE g.is_dark_activity AND g.status = 'CLOSED' AND {live})                         AS dark_closed,
          (SELECT count(*) FROM ais_gap_events g JOIN vessels v USING (mmsi)
            WHERE NOT g.is_dark_activity AND {live})                                             AS gaps_ignored,
          (SELECT count(*) FROM port_events pe JOIN vessels v USING (mmsi)
            WHERE {live} AND pe.event_time > now() - interval '24 hours')                        AS port_events_24h,
          (SELECT count(*) FROM vessels WHERE is_synthetic)                                      AS synthetic_vessels,
          (SELECT round(EXTRACT(EPOCH FROM now() - max(lp.last_seen)))
             FROM latest_position lp JOIN vessels v USING (mmsi) WHERE {live})                   AS newest_fix_age_s,
          (SELECT count(*) FROM weather_obs WHERE observed_at > now() - interval '2 hours')      AS weather_obs_2h,
          pg_size_pretty(pg_database_size(current_database()))                                   AS db_size,
          now()                                                                                  AS server_time""")
    return jsonify(row)


@app.get("/api/ships")
def ships():
    max_age = float(request.args.get("max_age_h", 12))
    # This gets every recent ship with its latest position, its open gap and the port it is in.
    # The CASE at the end gives each ship a state for the map colour.
    rows = q("""
        SELECT v.mmsi, v.name, fn_ship_class(v.ship_type) AS ship_class, v.destination, v.eta, v.length_m, v.is_synthetic,
               lp.speed_knots, lp.course, lp.heading, lp.last_seen,
               round((EXTRACT(EPOCH FROM now() - lp.last_seen) / 60.0)::numeric, 1) AS silent_min,
               ST_X(lp.geom) AS lon, ST_Y(lp.geom) AS lat,
               g.gap_id, g.classification AS gap_class,
               pst.port_id AS in_port_id,
               CASE
                 WHEN g.classification = 'DARK_ACTIVITY' THEN 'dark'
                 WHEN g.gap_id IS NOT NULL                THEN 'silent'
                 WHEN pst.port_id IS NOT NULL             THEN 'inport'
                 WHEN lp.last_seen < now() - interval '20 minutes' THEN 'stale'
                 WHEN COALESCE(lp.speed_knots, 0) < 0.5   THEN 'still'
                 ELSE 'underway'
               END AS state
          FROM latest_position lp
          JOIN vessels v USING (mmsi)
          LEFT JOIN ais_gap_events g ON g.mmsi = lp.mmsi AND g.status = 'OPEN'
          LEFT JOIN vessel_port_state pst ON pst.mmsi = lp.mmsi
         WHERE lp.last_seen > now() - (%s::float8 * interval '1 hour') AND (%s OR NOT v.is_synthetic)""",
             (max_age, include_syn()))
    return jsonify(rows)


@app.get("/api/ship/<int:mmsi>")
def ship(mmsi: int):
    hours = float(request.args.get("hours", 12))
    # This gets the details of one ship and its newest position.
    vessel = one("""SELECT v.mmsi, v.name, v.imo, v.call_sign, v.ship_type, fn_ship_class(v.ship_type) AS ship_class,
                           v.length_m, v.width_m, v.draught_m, v.destination, v.eta, v.is_synthetic, v.first_seen,
                           lp.speed_knots, lp.course, lp.heading, lp.nav_stat, lp.last_seen,
                           ST_X(lp.geom) AS lon, ST_Y(lp.geom) AS lat
                      FROM vessels v LEFT JOIN latest_position lp USING (mmsi) WHERE v.mmsi = %s""", (mmsi,))
    if not vessel:
        return jsonify({"error": "unknown vessel"}), 404
    # This gets the recent track points of the ship.
    track = q("""SELECT event_time, ST_X(geom) AS lon, ST_Y(geom) AS lat, speed_knots, course
                   FROM location_events WHERE mmsi = %s AND event_time > now() - (%s::float8 * interval '1 hour')
                  ORDER BY event_time LIMIT 3000""", (mmsi, hours))
    # These next queries get the destination history, the silences, the port visits and the refused records of the ship.
    history = q("""SELECT history_id, destination, eta, valid_from, valid_to, recorded_at, superseded_at, is_current
                     FROM vessel_status_history WHERE mmsi = %s ORDER BY valid_from""", (mmsi,))
    gaps = q("""SELECT gap_id, status, classification, is_dark_activity, last_seen_time, reappear_time, gap_duration_minutes,
                       feasibility_status FROM ais_gap_events WHERE mmsi = %s ORDER BY last_seen_time DESC LIMIT 20""", (mmsi,))
    port_events = q("""SELECT pe.event_type, pe.event_time, p.name AS port, pe.distance_m
                         FROM port_events pe JOIN ports p USING (port_id) WHERE pe.mmsi = %s ORDER BY pe.event_time DESC LIMIT 20""", (mmsi,))
    rejected = q("""SELECT rejection_reason, detail, event_time FROM rejected_events WHERE mmsi = %s ORDER BY rejected_at DESC LIMIT 10""", (mmsi,))
    return jsonify({"vessel": vessel, "track": track, "history": history, "gaps": gaps, "port_events": port_events, "rejected": rejected})


@app.get("/api/vessels/search")
def vessel_search():
    term = (request.args.get("q") or "").strip()
    if not term:
        # No search text, so this gets the ships with the most destination versions.
        return jsonify(q("""SELECT v.mmsi, v.name, v.destination, v.is_synthetic,
                                   (SELECT count(*) FROM vessel_status_history h WHERE h.mmsi = v.mmsi) AS versions
                              FROM vessels v WHERE (%s OR NOT v.is_synthetic)
                             ORDER BY (SELECT count(*) FROM vessel_status_history h WHERE h.mmsi = v.mmsi) DESC, v.is_synthetic DESC, v.name
                             LIMIT 15""", (include_syn(),)))
    like = f"%{term}%"
    # This looks for ships whose name or MMSI contains the search text.
    return jsonify(q("""SELECT v.mmsi, v.name, v.destination, v.is_synthetic,
                               (SELECT count(*) FROM vessel_status_history h WHERE h.mmsi = v.mmsi) AS versions
                          FROM vessels v
                         WHERE (v.name ILIKE %s OR v.mmsi::text LIKE %s) AND (%s OR NOT v.is_synthetic)
                         ORDER BY v.name LIMIT 15""", (like, like, include_syn())))


# going dark
# This is the base query for the list of silences. The endpoints add their own WHERE part.
GAP_LIST_SQL = """
    SELECT g.gap_id, g.mmsi, v.name, v.is_synthetic, fn_ship_class(v.ship_type) AS ship_class, g.status, g.classification,
           g.is_dark_activity, g.last_seen_time, g.reappear_time, g.gap_duration_minutes,
           g.neighbours_before, g.neighbours_silent, g.feasibility_status,
           g.v_max_knots, g.speed_factor, g.v_eff_knots, g.reach_radius_km, g.observed_distance_km, g.implied_speed_knots,
           g.feasible_area_km2, g.tight_area_km2, g.land_removed_km2, g.visited_area_km2,
           ST_X(g.start_geom) AS p1_lon, ST_Y(g.start_geom) AS p1_lat,
           ST_X(g.end_geom) AS p2_lon, ST_Y(g.end_geom) AS p2_lat,
           (g.feasible_geom IS NOT NULL) AS has_region
      FROM ais_gap_events g JOIN vessels v USING (mmsi)"""


@app.get("/api/gaps")
def gaps():
    which = request.args.get("class", "dark")
    where = "g.is_dark_activity" if which == "dark" else "true"
    # This gets the list of silences, newest first.
    rows = q(GAP_LIST_SQL + f" WHERE {where} AND (%s OR NOT v.is_synthetic) ORDER BY g.last_seen_time DESC LIMIT 400", (include_syn(),))
    return jsonify(rows)


@app.get("/api/gap/<int:gap_id>")
def gap(gap_id: int):
    # This gets one silence by its id.
    row = one(GAP_LIST_SQL + " WHERE g.gap_id = %s", (gap_id,))
    if not row:
        return jsonify({"error": "unknown gap"}), 404
    # This gets the possible area shapes and the two reach circles for the silence.
    geo = one("""SELECT ST_AsGeoJSON(ST_Simplify(feasible_geom, 0.0004))::json AS lens,
                        ST_AsGeoJSON(ST_Simplify(tight_geom,    0.0004))::json AS tight,
                        CASE WHEN reach_radius_km IS NULL THEN NULL
                             ELSE ST_AsGeoJSON(ST_Buffer(start_geom::geography, reach_radius_km * 1000, 'quad_segs=24')::geometry)::json END AS circle1,
                        CASE WHEN reach_radius_km IS NULL OR end_geom IS NULL THEN NULL
                             ELSE ST_AsGeoJSON(ST_Buffer(end_geom::geography, reach_radius_km * 1000, 'quad_segs=24')::geometry)::json END AS circle2,
                        conditions
                   FROM ais_gap_events WHERE gap_id = %s""", (gap_id,))
    # This gets the track points of the ship from before the silence till after it.
    track = q("""SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, event_time
                   FROM location_events WHERE mmsi = %s
                    AND event_time BETWEEN %s - interval '90 minutes' AND COALESCE(%s, now()) + interval '45 minutes'
                  ORDER BY event_time LIMIT 800""", (row["mmsi"], row["last_seen_time"], row["reappear_time"]))
    return jsonify({**row, **geo, "track": track})


@app.get("/api/funnel")
def funnel():
    thr = int(request.args.get("threshold", 20))
    # This counts the silent ships for each verdict using the classify function.
    rows = q("""SELECT classification, count(*) AS ships FROM classify_silent_vessels(%s, 6)
                 WHERE (%s OR mmsi < %s) GROUP BY 1""", (thr, include_syn(), SYN_MIN))
    # This counts the ships seen in the last 6 hours.
    active = one("""SELECT count(*) AS n FROM latest_position lp JOIN vessels v USING (mmsi)
                     WHERE lp.last_seen > now() - interval '6 hours' AND (%s OR NOT v.is_synthetic)""", (include_syn(),))
    return jsonify({"threshold": thr, "tracked_6h": active["n"], "by_class": {r["classification"]: r["ships"] for r in rows}})


@app.get("/api/detect")
def detect():
    thr = int(request.args.get("threshold", 20))
    # This gets the ships that went silent using the detect function.
    rows = q("""SELECT mmsi, vessel_name, silent_minutes, last_speed_knots, neighbours_before, neighbours_silent,
                       ST_X(last_geom) AS lon, ST_Y(last_geom) AS lat, last_seen
                  FROM detect_going_dark_vessels(%s) WHERE (%s OR mmsi < %s) ORDER BY silent_minutes""",
             (thr, include_syn(), SYN_MIN))
    return jsonify(rows)


# pipeline
@app.get("/api/pipeline")
def pipeline():
    # This gets the health of each data feed.
    feeds = q("SELECT * FROM v_collector_health ORDER BY feed")
    # This gets the last 25 runs of the collector.
    recent = q("""SELECT heartbeat_id, feed, status, started_at, duration_ms, rows_fetched, rows_ingested, rows_rejected, error_count, message
                    FROM collector_heartbeat ORDER BY heartbeat_id DESC LIMIT 25""")
    # This counts the rows of the main tables.
    counts = one("""SELECT (SELECT count(*) FROM staging_ais) AS staging,
                           (SELECT count(*) FROM location_events) AS location_events,
                           (SELECT count(*) FROM latest_position) AS latest_position,
                           (SELECT count(*) FROM rejected_events) AS rejected_events,
                           (SELECT count(*) FROM vessels) AS vessels,
                           (SELECT count(*) FROM vessel_status_history) AS status_history,
                           (SELECT count(*) FROM port_events) AS port_events,
                           (SELECT count(*) FROM ais_gap_events) AS gap_events,
                           (SELECT count(*) FROM weather_obs) AS weather_obs,
                           (SELECT count(*) FROM sea_conditions) AS sea_conditions""")
    # This counts the fixes stored per minute for the last hour.
    throughput = q("""SELECT date_trunc('minute', le.ingested_at) AS minute, count(*) AS fixes
                        FROM location_events le JOIN vessels v USING (mmsi)
                       WHERE NOT v.is_synthetic AND le.ingested_at > now() - interval '60 minutes'
                       GROUP BY 1 ORDER BY 1""")
    # This counts the refused records for each reason.
    reasons = q("""SELECT r.code AS reason, r.description, count(e.rejected_id) FILTER (WHERE e.source = 'digitraffic') AS live,
                          count(e.rejected_id) FILTER (WHERE e.source <> 'digitraffic') AS fixtures
                     FROM rejection_reasons r LEFT JOIN rejected_events e ON e.rejection_reason = r.code
                    GROUP BY r.code, r.description ORDER BY 3 DESC, 4 DESC, 1""")
    return jsonify({"feeds": feeds, "recent": recent, "counts": counts, "throughput": throughput, "reasons": reasons})


@app.get("/api/rejections")
def rejections():
    # This gets the latest 60 refused records.
    rows = q("""SELECT rejected_id, mmsi, lat, lon, sog, rejection_reason, detail, event_time, rejected_at, source
                  FROM rejected_events WHERE (%s OR source = 'digitraffic') ORDER BY rejected_id DESC LIMIT 60""", (include_syn(),))
    return jsonify(rows)


# temporal / ports / environment
@app.get("/api/temporal/<int:mmsi>")
def temporal(mmsi: int):
    # This gets all the destination versions of the ship.
    hist = q("""SELECT history_id, destination, eta, valid_from, valid_to, recorded_at, superseded_at, is_current
                  FROM vessel_status_history WHERE mmsi = %s ORDER BY valid_from""", (mmsi,))
    # This gets the basic info of the ship.
    v = one("SELECT mmsi, name, is_synthetic FROM vessels WHERE mmsi = %s", (mmsi,))
    return jsonify({"vessel": v, "history": hist})


@app.get("/api/asof")
def asof():
    mmsi = int(request.args["mmsi"])
    valid_at = request.args["valid_at"]
    known_at = request.args.get("known_at") or None
    # This asks the as of function what the destination was at the chosen times.
    rows = q("""SELECT destination, eta, valid_from, valid_to, recorded_at, superseded_at
                  FROM fn_vessel_status_as_of(%s, %s::timestamptz, COALESCE(%s::timestamptz, now()))""", (mmsi, valid_at, known_at))
    return jsonify(rows)


@app.get("/api/ports")
@cached(20)
def ports():
    # This gets each port with its zone and counts the ships inside and the entries in 24 hours.
    rows = q("""SELECT p.port_id, p.locode, p.name, p.country, ST_X(p.geom) AS lon, ST_Y(p.geom) AS lat, p.buffer_radius_m,
                       ST_AsGeoJSON(ST_Buffer(p.geom::geography, p.buffer_radius_m, 'quad_segs=24')::geometry)::json AS zone,
                       (SELECT count(*) FROM vessel_port_state s JOIN vessels v USING (mmsi)
                         WHERE s.port_id = p.port_id AND (%s OR NOT v.is_synthetic)) AS ships_inside,
                       (SELECT count(*) FROM port_events e JOIN vessels v USING (mmsi)
                         WHERE e.port_id = p.port_id AND e.event_type = 'ENTRY' AND e.event_time > now() - interval '24 hours'
                           AND (%s OR NOT v.is_synthetic)) AS entries_24h
                  FROM ports p ORDER BY p.name""", (include_syn(), include_syn()))
    return jsonify(rows)


@app.get("/api/port_events")
def port_events():
    # This gets the latest 80 arrivals and departures with the ship and port names.
    rows = q("""SELECT pe.event_id, pe.mmsi, v.name, v.is_synthetic, p.name AS port, pe.event_type, pe.event_time, pe.distance_m
                  FROM port_events pe JOIN vessels v USING (mmsi) JOIN ports p USING (port_id)
                 WHERE (%s OR NOT v.is_synthetic) ORDER BY pe.event_time DESC LIMIT 80""", (include_syn(),))
    return jsonify(rows)


@app.get("/api/environment")
def environment():
    # These next queries get the newest wind readings, the newest wave readings and the slowdown table.
    wind = q("""SELECT DISTINCT ON (station_id) station_id, ST_X(geom) AS lon, ST_Y(geom) AS lat, observed_at,
                       wind_speed_ms, wind_gust_ms, wind_dir_deg
                  FROM weather_obs WHERE observed_at > now() - interval '3 hours' AND wind_speed_ms IS NOT NULL
                 ORDER BY station_id, observed_at DESC""")
    waves = q("""SELECT DISTINCT ON (source, site_id) source, site_id, site_name, ST_X(geom) AS lon, ST_Y(geom) AS lat, observed_at,
                        wave_height_m, sea_state
                   FROM sea_conditions WHERE observed_at > now() - interval '12 hours'
                  ORDER BY source, site_id, observed_at DESC""")
    factors = q("SELECT condition_type, label, lower_bound, upper_bound, speed_factor FROM speed_factor_lookup ORDER BY condition_type, lower_bound NULLS LAST, speed_factor DESC")
    return jsonify({"wind": wind, "waves": waves, "speed_factors": factors})


@app.get("/api/land")
@cached(3600)
def land():
    """Simplified land outline of the Gulf of Finland - used by the illustrated map and the tracker preview on the intro page."""
    # This joins all the land shapes into one simple outline of the area.
    row = one("""SELECT ST_AsGeoJSON(ST_Simplify(ST_CollectionExtract(ST_Collect(
                        ST_MakeValid(ST_Intersection(geom, ST_MakeEnvelope(21.0, 58.8, 31.0, 61.2, 4326)))), 3), 0.003), 4)::json AS geometry
                 FROM land_polygons""")
    return jsonify(row["geometry"])


@app.get("/api/traffic")
@cached(60)
def traffic():
    # This counts the grid squares and how many were visited or never visited.
    meta = one("SELECT count(*) AS cells, count(*) FILTER (WHERE visits > 0) AS visited, count(*) FILTER (WHERE never_visited) AS never, "
               "max(refreshed_at) AS refreshed FROM traffic_grid")
    if not meta.get("cells"):
        return jsonify({"built": False})
    # This gets the size of one grid square.
    size = one("SELECT round((ST_XMax(geom) - ST_XMin(geom))::numeric, 4) AS s FROM traffic_grid LIMIT 1")["s"]
    # These get the visited squares and the never visited squares.
    visited = q("SELECT round(ST_XMin(geom)::numeric, 4) AS lon, round(ST_YMin(geom)::numeric, 4) AS lat, visits FROM traffic_grid WHERE visits > 0")
    never = q("SELECT round(ST_XMin(geom)::numeric, 4) AS lon, round(ST_YMin(geom)::numeric, 4) AS lat FROM traffic_grid WHERE never_visited")
    return jsonify({"built": True, "cell": size, "meta": meta, "visited": visited, "never": never})


# DBA console
@app.get("/api/dba/overview")
def dba_overview():
    return jsonify({
        # The queries below read the monitoring views for the DBA page.
        "activity": q("SELECT pid, usename, application_name, state, wait_event_type, wait_event, xact_age_s, query_age_s, query FROM v_mon_activity ORDER BY pid"),
        "blocking": q("SELECT * FROM v_mon_blocking"),
        "cache": q("SELECT scope, object, blks_hit, blks_read, hit_pct FROM v_mon_cache_hit WHERE scope = 'database' OR hit_pct IS NOT NULL ORDER BY scope ASC, blks_hit DESC LIMIT 10"),
        "tables": q("SELECT table_name, live_rows, dead_rows, dead_pct, seq_scan, idx_scan, hot_updates, total_size, total_bytes, last_vacuum FROM v_mon_table_health ORDER BY total_bytes DESC LIMIT 12"),
        "indexes": q("SELECT table_name, index_name, index_type, idx_scan, index_size, index_bytes FROM v_mon_index_usage ORDER BY index_bytes DESC LIMIT 14"),
        # This gets the roles of the project and which groups they belong to.
        "roles": q("""SELECT r.rolname, r.rolcanlogin AS can_login,
                             COALESCE((SELECT string_agg(g.rolname, ', ') FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                                        WHERE m.member = r.oid), '') AS member_of
                        FROM pg_roles r WHERE r.rolname IN ('ingest_role','analyst_role','dsw_ingest','dsw_analyst') ORDER BY 1"""),
        "backups": list_backups(),
        "bench": bench_state(),
    })


def bench_state() -> dict:
    with pool.get() as conn:
        return simulate_demo.bench_state(conn)


def list_backups() -> list[dict]:
    folder = db.BASE_DIR / "backups"
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    return [{"file": f.name, "mb": round(f.stat().st_size / 1e6, 2),
             "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")} for f in files]


@app.get("/api/dba/explain")
def dba_explain():
    preset = request.args.get("preset", "latest")
    force_seq = request.args.get("force_seq") in ("1", "true")
    with pool.get() as conn:
        # This finds the ship that has the most stored positions.
        pick = db.query(conn, "SELECT mmsi FROM location_events GROUP BY mmsi ORDER BY count(*) DESC LIMIT 1")
        mmsi = pick[0]["mmsi"] if pick else 0
        # These are the fixed queries that can be explained. None of them take text from the user.
        presets = {
            "latest": ("Find the newest position of one ship",
                       "SELECT * FROM latest_position WHERE mmsi = %s", (mmsi,)),
            "trajectory": ("Find one ship’s recent movements, newest first",
                           "SELECT event_time, ST_X(geom), ST_Y(geom), speed_knots FROM location_events WHERE mmsi = %s "
                           "AND event_time > now() - interval '24 hours' ORDER BY event_time DESC", (mmsi,)),
            "near_helsinki": ("Find ships within 15 km of Helsinki",
                              "SELECT count(*) FROM latest_position WHERE ST_DWithin(geom::geography, "
                              "ST_SetSRID(ST_MakePoint(24.94, 60.17), 4326)::geography, 15000)", None),
            "nearest_port": ("Find the nearest port to a point",
                             "SELECT name FROM ports ORDER BY geom <-> ST_SetSRID(ST_MakePoint(25.2, 59.9), 4326) LIMIT 1", None),
            "rejections": ("Count refused records by reason, last 24 hours",
                           "SELECT rejection_reason, count(*) FROM rejected_events WHERE rejected_at > now() - interval '1 day' GROUP BY 1", None),
        }
        bench = simulate_demo.bench_state(conn)
        if bench["exists"]:
            for key, (label, sql) in simulate_demo.BENCH_QUERIES.items():
                presets["bench_" + key] = (f"[Test table] {label}", sql, None)
        if preset not in presets:
            return jsonify({"error": "unknown preset", "presets": list(presets)}), 400
        label, sql, params = presets[preset]
        # This runs EXPLAIN on the chosen query.
        result = simulate_demo.explain_sql(conn, sql, force_seq, params)
    return jsonify({"preset": preset, "label": label, "sql": sql if params is None else sql % tuple(params),
                    "force_seq": force_seq, "presets": {k: v[0] for k, v in presets.items()}, **result})


# "How it works" page: table descriptions, collection timetable, and a live check of one real record
def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.get("/api/dictionary")
@cached(20)
def dictionary():
    names = list(TABLES)
    # This gets the columns of our tables from the database catalog.
    cols = q("""SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position""", (names,))
    # This gets the primary keys and foreign keys of the tables.
    keys = q("""SELECT c.conrelid::regclass::text AS tbl, c.contype, a.attname AS col, c.confrelid::regclass::text AS ref
                  FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                 WHERE c.contype IN ('p', 'f') AND c.connamespace = 'public'::regnamespace""")
    pk = {(k["tbl"], k["col"]) for k in keys if k["contype"] == "p"}
    fk = {(k["tbl"], k["col"]): k["ref"] for k in keys if k["contype"] == "f"}
    out = []
    for t in names:
        meta = TABLES[t]
        with pool.get() as conn:
            with conn.cursor() as cur:
                # This counts the rows of the table.
                cur.execute(pgsql.SQL("SELECT count(*) FROM public.{}").format(pgsql.Identifier(t)))
                n = cur.fetchone()[0]
        columns = []
        for c in (x for x in cols if x["table_name"] == t):
            kind = c["udt_name"] if c["data_type"] == "USER-DEFINED" else c["data_type"]
            columns.append({"name": c["column_name"], "type": PLAIN_TYPES.get(kind, kind), "raw_type": kind,
                            "key": "PK" if (t, c["column_name"]) in pk else ("FK" if (t, c["column_name"]) in fk else ""),
                            "links_to": fk.get((t, c["column_name"])), "meaning": meta["columns"].get(c["column_name"], "")})
        out.append({"table": t, "title": meta["title"], "what": meta["what"], "used_by": meta["used_by"], "rows": n, "columns": columns})
    return jsonify(out)


@app.get("/api/dictionary/sample")
def dictionary_sample():
    t = request.args.get("table", "")
    if t not in TABLES:
        return jsonify({"error": "unknown table"}), 404
    # This gets the column names and types of the chosen table.
    cols = q("""SELECT column_name, udt_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (t,))
    # This finds the primary key column of the table.
    pkc = q("""SELECT a.attname AS col FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.contype = 'p' AND c.conrelid = %s::regclass LIMIT 1""", (f"public.{t}",))
    # Here we build a query for the sample rows. Map columns are cut to text so they are short.
    select = pgsql.SQL(", ").join(
        pgsql.SQL("left(ST_AsText({c}), 60) AS {c}").format(c=pgsql.Identifier(c["column_name"])) if c["udt_name"] in ("geometry", "geography")
        else pgsql.Identifier(c["column_name"]) for c in cols)
    query = pgsql.SQL("SELECT {} FROM public.{}").format(select, pgsql.Identifier(t))
    if pkc:
        query += pgsql.SQL(" ORDER BY {} DESC").format(pgsql.Identifier(pkc[0]["col"]))
    query += pgsql.SQL(" LIMIT 3")
    # This runs the sample query and reads the rows.
    with pool.get() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
    return jsonify({"table": t, "rows": rows})


FEED_INFO = [
    ("positions", "Ship positions", "Digitraffic (Finnish Transport Infrastructure Agency), fed by radio receivers along the coast", "https://meri.digitraffic.fi/api/ais/v1/locations"),
    ("vessels", "Ship details", "Digitraffic, the same service. Crews type the destination and arrival time into the transmitter", "https://meri.digitraffic.fi/api/ais/v1/vessels"),
    ("weather", "Wind", "Finnish Meteorological Institute (FMI) weather stations", "https://opendata.fmi.fi/wfs"),
    ("sea_state", "Waves and sea state", "FMI wave buoys and Digitraffic sea-state stations", "https://meri.digitraffic.fi/api/sse/v1/measurements"),
    ("engine", "Silence check and possible areas", "Worked out by our own database from the data collected above. Nothing is fetched from outside.", ""),
]


@app.get("/api/guide/schedule")
def guide_schedule():
    # This gets the health row of each feed.
    beats = {r["feed"]: r for r in q("SELECT * FROM v_collector_health")}
    # This gets the time of the database.
    now = one("SELECT now() AS now")["now"]
    out = []
    for feed, title, source, url in FEED_INFO:
        h = beats.get(feed)
        interval = db.POLL_INTERVALS.get(feed, 60)
        since = (now - h["started_at"]).total_seconds() if h else None
        out.append({"feed": feed, "title": title, "source": source, "url": url, "interval_s": interval,
                    "last_run": h["started_at"] if h else None, "seconds_since": since,
                    "next_in_s": max(0, interval - since) if since is not None else None,
                    "status": h["status"] if h else None, "received": h["rows_fetched"] if h else None,
                    "stored": h["rows_ingested"] if h else None, "refused": h["rows_rejected"] if h else None,
                    "message": h["message"] if h else None})
    return jsonify({"now": now, "feeds": out})


@app.get("/api/guide/trace")
def guide_trace():
    """Follow one real ship: what the official source says right now, and what our database holds for it."""
    mmsi = request.args.get("mmsi", type=int)
    if not mmsi:
        # This picks one real ship that moved lately, or the newest real ship if none did.
        pick = q("""SELECT lp.mmsi FROM latest_position lp JOIN vessels v USING (mmsi)
                     WHERE NOT v.is_synthetic AND v.name IS NOT NULL AND lp.last_seen > now() - interval '3 minutes' AND COALESCE(lp.speed_knots, 0) > 1
                     ORDER BY random() LIMIT 1""") or q("""SELECT lp.mmsi FROM latest_position lp JOIN vessels v USING (mmsi)
                     WHERE NOT v.is_synthetic ORDER BY lp.last_seen DESC LIMIT 1""")
        if not pick:
            return jsonify({"error": "no ships stored yet"}), 404
        mmsi = pick[0]["mmsi"]
    # This gets the details and newest position of the ship.
    vessel = one("""SELECT v.mmsi, v.name, fn_ship_class(v.ship_type) AS ship_class, v.destination, v.eta, v.length_m, v.width_m, v.is_synthetic,
                           lp.speed_knots, lp.course, lp.heading, lp.nav_stat, lp.last_seen, ST_Y(lp.geom) AS lat, ST_X(lp.geom) AS lon,
                           round((EXTRACT(EPOCH FROM now() - lp.last_seen) / 60.0)::numeric, 1) AS silent_min
                      FROM vessels v JOIN latest_position lp USING (mmsi) WHERE v.mmsi = %s""", (mmsi,))
    if not vessel:
        return jsonify({"error": "unknown ship"}), 404
    # This gets the last 3 stored positions of the ship.
    recent = q("""SELECT event_time, ST_Y(geom) AS lat, ST_X(geom) AS lon, speed_knots, course, ingested_at FROM location_events
                   WHERE mmsi = %s ORDER BY event_time DESC LIMIT 3""", (mmsi,))
    # This counts the records connected to the ship.
    counts = one("""SELECT (SELECT count(*) FROM rejected_events WHERE source = 'digitraffic' AND rejected_at > now() - interval '24 hours') AS refused_24h,
                           (SELECT count(*) FROM port_events WHERE mmsi = %s) AS port_visits,
                           (SELECT count(*) FROM ais_gap_events WHERE mmsi = %s) AS silences,
                           (SELECT count(*) FROM vessel_status_history WHERE mmsi = %s) AS destination_versions,
                           (SELECT count(*) FROM location_events WHERE mmsi = %s) AS stored_positions""", (mmsi, mmsi, mmsi, mmsi))
    source = {"url": f"{db.API['locations']}?mmsi={mmsi}", "raw": None, "error": None}
    compare = None
    if not vessel["is_synthetic"]:
        try:
            r = requests.get(db.API["locations"], params={"mmsi": mmsi}, headers=db.HTTP_HEADERS, timeout=20)
            r.raise_for_status()
            feats = r.json().get("features", [])
            if feats:
                f = feats[0]
                source["raw"] = {"mmsi": f.get("mmsi"), "coordinates": f["geometry"]["coordinates"], "properties": f["properties"]}
                pr = f["properties"]
                src_time = pr.get("timestampExternal")
                ours = vessel["last_seen"].timestamp() * 1000
                dt = (src_time - ours) / 1000.0
                dist = _haversine_m(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], vessel["lat"], vessel["lon"])
                def same(a, b, tol=0.15):
                    return a is None and b is None or (a is not None and b is not None and abs(float(a) - float(b)) <= tol)
                clean = lambda v, bad: None if v is None or v >= bad else v
                if abs(dt) < 1:
                    verdict = "Identical: the official source and our database hold the same report."
                    ok = dist < 5
                elif dt > 0:
                    verdict = f"The official source has a newer report ({dt:.0f} s newer). Our next collection will pick it up; the ship moved {dist:.0f} m since our copy."
                    ok = dist < 1200
                else:
                    verdict = f"Our copy is newer than the one the source shows ({-dt:.0f} s)."
                    ok = True
                compare = {"verdict": verdict, "ok": ok, "time_diff_s": dt, "distance_m": dist,
                           "rows": [
                               {"field": "Time of report (UTC)", "source": datetime.fromtimestamp(src_time / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "ours": vessel["last_seen"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "match": True if abs(dt) < 1 else None},
                               {"field": "Latitude (north)", "source": round(f["geometry"]["coordinates"][1], 5), "ours": round(vessel["lat"], 5), "match": None if abs(dt) >= 1 else same(f["geometry"]["coordinates"][1], vessel["lat"], 0.00005)},
                               {"field": "Longitude (east)", "source": round(f["geometry"]["coordinates"][0], 5), "ours": round(vessel["lon"], 5), "match": None if abs(dt) >= 1 else same(f["geometry"]["coordinates"][0], vessel["lon"], 0.00005)},
                               {"field": "Speed (knots)", "source": clean(pr.get("sog"), 102.2), "ours": float(vessel["speed_knots"]) if vessel["speed_knots"] is not None else None, "match": None if abs(dt) >= 1 else same(clean(pr.get("sog"), 102.2), vessel["speed_knots"])},
                               {"field": "Direction of travel (degrees)", "source": clean(pr.get("cog"), 360), "ours": float(vessel["course"]) if vessel["course"] is not None else None, "match": None if abs(dt) >= 1 else same(clean(pr.get("cog"), 360), vessel["course"])},
                               {"field": "Navigation status code", "source": pr.get("navStat"), "ours": vessel["nav_stat"], "match": None if abs(dt) >= 1 else pr.get("navStat") == vessel["nav_stat"]},
                           ]}
            else:
                source["error"] = "The official source has no current report for this ship."
        except Exception as exc:
            source["error"] = f"Could not reach the official source: {exc}"
    return jsonify({"ship": vessel, "recent": recent, "counts": counts, "source": source, "compare": compare, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


# admin buttons (these use the owner login)
def action(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if request.headers.get("X-DSW") != "1":          # a custom header stops other websites from triggering these actions
            return jsonify({"error": "missing X-DSW header"}), 403
        t = time.monotonic()
        result = fn(*a, **kw)
        result = result if isinstance(result, dict) else {"result": result}
        result["elapsed_s"] = round(time.monotonic() - t, 2)
        _cache.clear()
        return jsonify(result)
    return wrapper


@app.post("/api/actions/seed_demo")
@action
def act_seed():
    owner, ingest = db.connect("owner", app_name="webui-admin"), db.connect("ingest", app_name="webui-admin")
    try:
        summary = simulate_demo.seed(ingest, owner)
        regions = collector.process_pending(owner)
        summary["t0"] = summary["t0"].isoformat()
        summary["regions_computed"] = len(regions)
        return summary
    finally:
        owner.close()
        ingest.close()


@app.post("/api/actions/reset_demo")
@action
def act_reset():
    owner = db.connect("owner", app_name="webui-admin")
    try:
        return simulate_demo.reset(owner)
    finally:
        owner.close()


@app.post("/api/actions/collect")
@action
def act_collect():
    feeds = (request.json or {}).get("feeds") or ["positions", "weather"]
    results = collector.Collector(feeds).run_once()
    return {f: {"status": r.status, "fetched": r.fetched, "ingested": r.ingested, "rejected": r.rejected, "notes": r.notes}
            for f, r in results.items()}


@app.post("/api/actions/detect")
@action
def act_detect():
    ingest, owner = db.connect("ingest", app_name="webui-admin"), db.connect("owner", app_name="webui-admin")
    try:
        with ingest.cursor() as cur:
            # This runs the function that opens, refreshes and expires the silences.
            cur.execute("SELECT opened, refreshed, expired FROM sync_open_gaps()")
            opened, refreshed, expired = cur.fetchone()
        ingest.commit()
        regions = collector.process_pending(owner)
        return {"opened": opened, "refreshed": refreshed, "expired": expired, "regions_computed": len(regions)}
    finally:
        ingest.close()
        owner.close()


@app.post("/api/actions/traffic_grid")
@action
def act_grid():
    owner = db.connect("owner", app_name="webui-admin")
    try:
        with owner.cursor() as cur:
            # This rebuilds the traffic grid and gives back the number of cells.
            cur.execute("SELECT rebuild_traffic_grid(0.05)")
            n = cur.fetchone()[0]
        owner.commit()
        return {"cells": n}
    finally:
        owner.close()


@app.post("/api/actions/rbac")
@action
def act_rbac():
    owner = db.connect("owner", app_name="webui-admin")
    try:
        # This runs the function that tests what each role is allowed to do.
        rows = db.query(owner, "SELECT role_name, attempted_statement, expected, outcome, verdict FROM dba_rbac_proof()")
        owner.rollback()
        return {"rows": rows}
    finally:
        owner.close()


@app.post("/api/actions/concurrency")
@action
def act_concurrency():
    return simulate_demo.run_concurrency()


@app.post("/api/actions/bench")
@action
def act_bench():
    step = (request.json or {}).get("step")
    owner = db.connect("owner", app_name="webui-admin")
    try:
        if step == "build":
            simulate_demo.bench_build(owner)
            return simulate_demo.bench_state(owner)
        if step == "create_indexes":
            return simulate_demo.bench_indexes(owner, True)
        if step == "drop_indexes":
            return simulate_demo.bench_indexes(owner, False)
        return {"error": "unknown step"}
    finally:
        owner.close()


@app.post("/api/actions/backup")
@action
def act_backup():
    bash = shutil.which("bash") or next((p for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe") if os.path.exists(p)), None)
    if not bash:
        return {"ok": False, "output": "bash not found - run backup_restore.sh from Git Bash, WSL or Linux."}
    env = {**os.environ, "PGHOST": db.DB_HOST, "PGPORT": str(db.DB_PORT), "DSW_DB": db.DB_NAME,
           "PGUSER": db.DB_ROLES["owner"]["user"], "PGPASSWORD": db.DB_ROLES["owner"]["password"]}
    proc = subprocess.run([bash, "backup_restore.sh"], cwd=db.BASE_DIR, env=env, capture_output=True, text=True, timeout=300)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-6000:]}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"Dark Ship Watch UI  ->  http://{db.WEB_HOST}:{db.WEB_PORT}   (Ctrl-C to stop)")
    app.run(host=db.WEB_HOST, port=db.WEB_PORT, threaded=True, debug=False)


# Plain-language descriptions of every table and column. The "How it works" page mixes this text with the live
# database structure (types, keys, row counts), so a column added later still shows up, just without a description.

LOCATION_COLUMNS = {
    "event_id": "Row number.",
    "mmsi": "The ship’s ID number (MMSI). Links to the ships table.",
    "geom": "The ship’s position as a point on the map.",
    "speed_knots": "Speed over ground in knots (1 knot = 1.85 km/h).",
    "course": "Direction of travel in degrees (0 = north, 90 = east).",
    "heading": "Direction the bow points, in degrees.",
    "nav_stat": "Navigation status code from the ship (0 = under way, 1 = at anchor, 5 = moored).",
    "event_time": "When the ship reported this position.",
    "ingested_at": "When our system stored it.",
}

TABLES = {
    "vessels": {
        "title": "Ships",
        "what": "One row per ship: its name, size and current destination.",
        "used_by": ["Ship panel on the map", "Search", "Temporal page", "Ship type decides the top speed in the possible-area calculation"],
        "columns": {
            "mmsi": "The ship’s unique ID number (MMSI), sent by its transmitter. The main key used everywhere.",
            "imo": "Official IMO ship number (larger ships only).",
            "call_sign": "The ship’s radio call sign.",
            "name": "Ship name.",
            "ship_type": "Ship type code (60 to 69 = passenger, 70 to 79 = cargo, 80 to 89 = tanker, 30 = fishing, 52 = tug...).",
            "length_m": "Length in metres.",
            "width_m": "Width in metres.",
            "draught_m": "How deep the ship sits in the water, in metres.",
            "destination": "Destination as typed in by the crew (free text, so it can look messy).",
            "eta": "Estimated arrival time, typed in by the crew.",
            "status_ts": "When the ship sent that destination and arrival information.",
            "is_synthetic": "Yes for the made-up demo ships, no for real ships.",
            "first_seen": "When our system first saw this ship.",
            "updated_at": "When this row last changed.",
        },
    },
    "staging_ais": {
        "title": "Waiting room",
        "what": "New position records wait here to be checked. It is normally empty because records are checked straight away.",
        "used_by": ["Pipeline page (“Waiting room” count)"],
        "columns": {
            "staging_id": "Row number.", "mmsi": "Ship ID.", "lat": "Latitude: how far north.", "lon": "Longitude: how far east.",
            "sog": "Speed over ground in knots.", "cog": "Direction of travel in degrees.", "heading": "Direction the bow points.",
            "nav_stat": "Navigation status code.", "event_time": "When the ship reported this position.",
            "source": "Where the record came from.", "ingested_at": "When it arrived in the waiting room.",
        },
    },
    "location_events": {
        "title": "Movement history",
        "what": "Every checked position of every ship, one row per report. This is each ship’s trail.",
        "used_by": ["Ship trail and Replay on the map", "Latest reports table", "Silence detection (what came before and after)", "Busy sea lanes"],
        "columns": LOCATION_COLUMNS,
    },
    "latest_position": {
        "title": "Latest positions",
        "what": "Only the newest position of each ship, so “where is it now?” is answered instantly.",
        "used_by": ["Ships drawn on the map", "Ships active now", "Silence detection"],
        "columns": {
            "mmsi": "Ship ID.", "geom": "Newest position as a map point.", "speed_knots": "Speed in knots.", "course": "Direction of travel in degrees.",
            "heading": "Direction the bow points.", "nav_stat": "Navigation status code.",
            "last_seen": "Time of the newest report. A ship quiet for long has an old time here.", "updated_at": "When this row last changed.",
        },
    },
    "rejected_events": {
        "title": "Refused records",
        "what": "Records the checks refused, kept together with the reason. Nothing is thrown away silently.",
        "used_by": ["Pipeline page (reasons and latest refused records)", "Refused records counter"],
        "columns": {
            "rejected_id": "Row number.", "mmsi": "Ship ID, if there was one.", "lat": "Latitude as received.", "lon": "Longitude as received.",
            "sog": "Speed as received.", "cog": "Direction as received.", "heading": "Heading as received.", "nav_stat": "Status code as received.",
            "event_time": "Time as received.", "source": "Where the record came from.", "rejection_reason": "Why it was refused (a code, see Reasons list).",
            "detail": "Extra detail such as the impossible speed.", "rejected_at": "When it was refused.",
        },
    },
    "rejection_reasons": {
        "title": "Reasons list",
        "what": "The fixed list of reasons a record can be refused.",
        "used_by": ["Pipeline page"],
        "columns": {"code": "Short code of the reason.", "description": "What the reason means."},
    },
    "vessel_status_history": {
        "title": "Destination history",
        "what": "Every version of a ship’s destination and arrival time. A change adds a new row and closes the old one; nothing is overwritten.",
        "used_by": ["Temporal page", "Destination history in the ship panel"],
        "columns": {
            "history_id": "Row number.", "mmsi": "Ship ID.", "destination": "Destination in this version.", "eta": "Arrival time in this version.",
            "valid_from": "From when this destination was true (as reported by the ship).",
            "valid_to": "Until when it was true. Empty means it is still true.",
            "recorded_at": "When our system learned about this version.", "superseded_at": "When a newer version replaced it.",
            "is_current": "Yes for the version that is true now.",
        },
    },
    "ports": {
        "title": "Ports",
        "what": "The eight ports we watch, each with a centre point and a zone around it.",
        "used_by": ["Ports page", "Port rings on the map", "Silence detection (silent in port is normal)"],
        "columns": {
            "port_id": "Row number.", "locode": "Short port code.", "name": "Port name.", "country": "Country.", "geom": "Centre of the port as a map point.",
            "buffer_radius_m": "Size of the port zone in metres (3000 = 3 km).", "source": "How the port data was obtained (typed in by hand).",
        },
    },
    "port_events": {
        "title": "Port visits",
        "what": "Arrivals and departures, found automatically from ship positions.",
        "used_by": ["Ports page (arrivals and departures list)", "Port visits counter"],
        "columns": {
            "event_id": "Row number.", "mmsi": "Ship ID.", "port_id": "Which port.", "event_type": "ENTRY = arrived, EXIT = left.",
            "event_time": "When it happened.", "distance_m": "Distance from the port centre at that moment, in metres.", "created_at": "When the row was written.",
        },
    },
    "vessel_port_state": {
        "title": "Which port each ship is in",
        "what": "Remembers whether each ship is currently inside a port zone, so arrivals and departures can be spotted.",
        "used_by": ["Ports page (ships in port)", "Port visit detection"],
        "columns": {"mmsi": "Ship ID.", "port_id": "Port the ship is in now (empty = not in a port).", "since": "Since when.", "last_event_time": "Time of the last position used."},
    },
    "ais_gap_events": {
        "title": "Silences",
        "what": "Every time a ship stopped sending, with the verdict, the evidence and the possible area.",
        "used_by": ["Ships going dark list", "Going Dark page", "Red markers and possible areas on the map"],
        "columns": {
            "gap_id": "Row number.", "mmsi": "Ship ID.", "last_seen_time": "Time of the last report before the silence.",
            "reappear_time": "Time of the first report after it. Empty means still silent.", "gap_duration_minutes": "How long it was (or has been) silent.",
            "start_geom": "Position of the last report before the silence.", "end_geom": "Position of the first report after it.",
            "status": "OPEN = still silent, CLOSED = came back.",
            "classification": "The system’s verdict: going dark, in port, parked, left the area, or our receiver was down.",
            "is_dark_activity": "Yes only when nothing explains the silence.", "start_port_id": "The port, if it went quiet inside one.",
            "neighbours_before": "Ships within 15 km shortly before it went quiet.", "neighbours_silent": "How many of those also went quiet.",
            "detected_at": "When the silence was first noticed.", "closed_at": "When the ship was seen again.",
            "feasibility_status": "State of the possible-area calculation (OK, PROJECTED for ongoing, INFEASIBLE if impossible).",
            "v_max_knots": "Top speed assumed for this type of ship.", "speed_factor": "Weather slowdown (1 = none, 0.5 = half speed).",
            "v_eff_knots": "Usable speed after the weather slowdown.", "reach_radius_km": "The farthest it could have travelled while silent.",
            "observed_distance_km": "Distance between the two positions.", "implied_speed_knots": "Average speed needed to cover that distance.",
            "feasible_geom": "Possible area: the overlap of the two reach circles, without land.", "tight_geom": "The tightest possible area (an oval shape), without land.",
            "feasible_area_km2": "Size of the possible area in square kilometres.", "tight_area_km2": "Size of the tight area.",
            "land_removed_km2": "How much land was cut out.", "visited_area_km2": "Part of the area where ships have sailed before.",
            "conditions": "The weather and speed details used (stored as structured text).", "feasibility_computed_at": "When the area was calculated.",
        },
    },
    "land_polygons": {
        "title": "Land map",
        "what": "The coastline from Natural Earth, clipped to the Baltic.",
        "used_by": ["Cutting land out of possible areas", "Illustrated map on the opening page"],
        "columns": {"land_id": "Row number.", "name": "Name of the land piece.", "source": "Where the shape came from.", "geom": "The land shape."},
    },
    "land_tiles": {
        "title": "Land map pieces",
        "what": "The land map cut into small pieces so that cutting land out of an area is fast.",
        "used_by": ["Possible-area calculation"],
        "columns": {"tile_id": "Row number.", "geom": "One small piece of land."},
    },
    "sea_conditions": {
        "title": "Waves and sea state",
        "what": "Wave heights from weather buoys and sea-state estimates from Digitraffic.",
        "used_by": ["Waves layer on the map", "Weather slowdown in the possible-area calculation"],
        "columns": {
            "obs_id": "Row number.", "source": "Which service the reading came from.", "site_id": "Station or buoy ID.", "site_name": "Station name.",
            "geom": "Where the station is.", "observed_at": "When it was measured.", "sea_state": "Calm, light, moderate, rough...",
            "wave_height_m": "Wave height in metres.", "wave_dir_deg": "Wave direction in degrees.", "trend": "Getting better or worse.", "collected_at": "When we stored it.",
        },
    },
    "weather_obs": {
        "title": "Wind readings",
        "what": "Wind measurements from Finnish Meteorological Institute stations.",
        "used_by": ["Wind layer on the map", "Weather slowdown in the possible-area calculation", "Wind readings counter"],
        "columns": {
            "obs_id": "Row number.", "station_id": "Station identifier (its position).", "geom": "Where the station is.", "observed_at": "When it was measured.",
            "wind_speed_ms": "Wind speed in metres per second.", "wind_gust_ms": "Strongest gust in metres per second.",
            "wind_dir_deg": "Direction the wind comes from, in degrees.", "collected_at": "When we stored it.",
        },
    },
    "speed_factor_lookup": {
        "title": "Weather slowdown table",
        "what": "My own rough guess of how much wind and waves slow a ship. It is an assumption, not marine science.",
        "used_by": ["Possible-area calculation"],
        "columns": {
            "band_id": "Row number.", "condition_type": "What is measured: wind, wave height or sea state.", "label": "Name of the band (calm, breeze, storm...).",
            "lower_bound": "Band starts at this value.", "upper_bound": "Band ends before this value.", "speed_factor": "Multiply the ship’s speed by this number.",
            "is_assumption": "Yes: this is my assumption.",
        },
    },
    "ship_class_speed": {
        "title": "Top speed by ship type",
        "what": "Assumed top speed for each kind of ship (another rough, editable assumption).",
        "used_by": ["Possible-area calculation"],
        "columns": {"class_id": "Row number.", "label": "Kind of ship.", "type_min": "First ship-type code in this group.", "type_max": "Last ship-type code in this group.", "v_max_knots": "Assumed top speed in knots."},
    },
    "traffic_grid": {
        "title": "Busy-lanes grid",
        "what": "The sea cut into squares, counting how many positions fell in each. Squares no ship ever used are “never visited”.",
        "used_by": ["Busy sea lanes and Water no ship has used layers"],
        "columns": {
            "cell_id": "Row number.", "geom": "The square.", "visits": "Positions that fell in this square.", "distinct_ships": "How many different ships.",
            "is_land": "Yes if the square is land.", "never_visited": "Yes for water no ship has used yet.", "refreshed_at": "When the counts were last rebuilt.",
        },
    },
    "collector_heartbeat": {
        "title": "Collector log",
        "what": "One row every time the collector asks a data source: proof that collection is continuous.",
        "used_by": ["Pipeline page (data source cards)", "How it works page (timetable)", "Live pill at the top"],
        "columns": {
            "heartbeat_id": "Row number.", "feed": "Which data source.", "started_at": "When the run started.", "finished_at": "When it finished.",
            "duration_ms": "How long it took, in milliseconds.", "status": "OK, PARTIAL (part failed) or ERROR.", "rows_fetched": "Records received.",
            "rows_ingested": "Records stored.", "rows_rejected": "Records refused.", "error_count": "How many parts failed.", "message": "Short summary of the run.",
        },
    },
    "pipeline_config": {
        "title": "Settings",
        "what": "The limits the checks use, such as the maximum believable speed.",
        "used_by": ["Record checks", "Silence detection"],
        "columns": {"cfg_key": "Name of the setting.", "cfg_value": "Its value.", "description": "What it controls."},
    },
    "location_events_archive": {
        "title": "Old movement history",
        "what": "Positions older than the retention period are moved here to keep the main table fast. Same columns as Movement history.",
        "used_by": ["Not shown on screen (long-term storage)"],
        "columns": LOCATION_COLUMNS,
    },
}

PLAIN_TYPES = {
    "smallint": "whole number", "integer": "whole number", "bigint": "whole number", "numeric": "number", "double precision": "number", "real": "number",
    "text": "text", "character varying": "text", "boolean": "yes / no", "timestamp with time zone": "date and time", "timestamp without time zone": "date and time",
    "geometry": "map shape", "geography": "map shape", "jsonb": "structured text", "json": "structured text", "uuid": "ID",
}


if __name__ == "__main__":
    main()
