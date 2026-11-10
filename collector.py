"""It runs all the time and brings live data into the database.

It asks four free sources for data. Ship positions go into the waiting room (staging_ais) and are checked straight away
by the database function validate_and_migrate_ais().

    positions   every 60 s       ship positions from Digitraffic
    vessels     every 10 min     ship name, size, destination and arrival time from Digitraffic
    weather     every 10 min     wind readings from the Finnish Meteorological Institute
    sea_state   every 30 min     sea state from Digitraffic and wave height from the Finnish Meteorological Institute
    engine      every 5 min      finds silent ships and works out their possible areas


If a source fails, the collector waits and tries again, and it reconnects to the database if the connection drops.
Every run of every feed writes one row to collector_heartbeat, even when it failed.
It logs in as the limited ingest user, which can add data but not read or delete it (see database.sql, part 3).
The second half of this file is the possible-area engine, which the website also uses.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests

import db

log = logging.getLogger("dsw.collector")
UTC = timezone.utc
FMI_NS = {"gml": "http://www.opengis.net/gml/3.2", "bs": "http://xml.fmi.fi/schema/wfs/2.0"}


# Helper functions that only convert data. They use no network and no database.
def ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def clean_float(value, bad: float | None = None):
    """Ships send special numbers for "not available" (speed 102.3, course 360). Turn those into None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or (bad is not None and v >= bad):
        return None
    return v


def decode_eta(packed: int | None, ref: datetime | None = None) -> datetime | None:
    """The arrival time comes as one packed number: month, day, hour and minute, but no year.

    The year is guessed: the date closest to now, between 45 days ago and 320 days ahead."""
    if not packed:
        return None
    minute, hour, day, month = packed & 0x3F, (packed >> 6) & 0x1F, (packed >> 11) & 0x1F, (packed >> 16) & 0x0F
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59):
        return None
    ref = ref or datetime.now(UTC)
    best = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = datetime(year, month, day, hour, minute, tzinfo=UTC)
        except ValueError:
            continue
        if ref - timedelta(days=45) <= cand <= ref + timedelta(days=320):
            if best is None or abs(cand - ref) < abs(best - ref):
                best = cand
    return best


def parse_position_features(features: list[dict]) -> list[tuple]:
    """Turn the position data from the source into rows for staging_ais."""
    rows = []
    for f in features:
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        ts = props.get("timestampExternal")
        if len(coords) < 2 or ts is None:
            continue
        heading = props.get("heading")
        rows.append((
            props.get("mmsi", f.get("mmsi")),
            coords[1], coords[0],
            clean_float(props.get("sog"), bad=102.3),
            clean_float(props.get("cog"), bad=360.0),
            heading if heading is not None and 0 <= heading <= 359 else None,
            props.get("navStat"),
            ms_to_dt(ts),
        ))
    return rows


def in_bbox(lat: float, lon: float) -> bool:
    b = db.BBOX
    return b["min_lat"] <= lat <= b["max_lat"] and b["min_lon"] <= lon <= b["max_lon"]


def parse_vessel(v: dict) -> tuple | None:
    mmsi = v.get("mmsi")
    if not mmsi:
        return None
    clean = lambda s: (s or "").replace("@", " ").strip() or None
    dest = clean(v.get("destination"))
    length = (v.get("referencePointA") or 0) + (v.get("referencePointB") or 0)
    width = (v.get("referencePointC") or 0) + (v.get("referencePointD") or 0)
    draught = (v.get("draught") or 0) / 10.0
    ship_type = v.get("shipType")
    status_ts = v.get("timestamp")
    return (
        mmsi,
        v.get("imo") or None,
        clean(v.get("callSign")),
        clean(v.get("name")),
        ship_type if ship_type is not None and 0 <= ship_type <= 99 else None,
        length if 0 < length <= 500 else None,
        width if 0 < width <= 80 else None,
        draught if 0 < draught <= 30 else None,
        dest.upper() if dest else None,
        decode_eta(v.get("eta")),
        ms_to_dt(status_ts) if status_ts else None,
    )


def parse_fmi_wfs(xml_text: str) -> dict[tuple[float, float, str], dict[str, float | None]]:
    """Turn the weather service answer into {(lat, lon, time): {measurement: value}}."""
    root = ET.fromstring(xml_text)
    if root.tag.endswith("ExceptionReport"):
        msg = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("ExceptionText"))
        raise RuntimeError(f"FMI WFS exception: {msg[:300]}")
    grouped: dict[tuple[float, float, str], dict[str, float | None]] = {}
    for el in root.iter(f"{{{FMI_NS['bs']}}}BsWfsElement"):
        pos = el.find(".//gml:pos", FMI_NS)
        when = el.find("bs:Time", FMI_NS)
        name = el.find("bs:ParameterName", FMI_NS)
        value = el.find("bs:ParameterValue", FMI_NS)
        if pos is None or when is None or name is None or value is None or not pos.text:
            continue
        lat, lon = (float(x) for x in pos.text.split()[:2])
        grouped.setdefault((lat, lon, when.text), {})[name.text] = clean_float(value.text)
    return grouped


# What one run of a feed reports back
@dataclass
class FeedResult:
    fetched: int = 0
    ingested: int = 0
    rejected: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors and self.ingested == 0 and self.fetched == 0:
            return "ERROR"
        return "PARTIAL" if self.errors else "OK"


class Collector:
    FEED_ORDER = ["positions", "vessels", "weather", "sea_state", "engine"]

    def __init__(self, feeds: list[str] | None = None):
        self.feeds = [f for f in self.FEED_ORDER if not feeds or f in feeds]
        self.session = requests.Session()
        self.session.headers.update(db.HTTP_HEADERS)
        self.conns: dict[str, psycopg2.extensions.connection | None] = {"ingest": None, "owner": None}
        self.last_ts_ms: dict[int, int] = {}          # for each ship: the newest position time already stored
        self.positions_from_ms: int | None = None
        self.cache_primed = False
        self.stop = False

    # connections and web requests
    def conn(self, role: str):
        c = self.conns.get(role)
        if not db.is_alive(c):
            if c is not None:
                log.warning("database connection (%s) lost - reconnecting", role)
            self.conns[role] = db.connect(role, app_name="collector")
        return self.conns[role]

    def http_get(self, url: str, params: dict | None = None) -> requests.Response:
        def _get():
            resp = self.session.get(url, params=params, timeout=db.HTTP_TIMEOUT_S)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code} from {url}", response=resp)
            resp.raise_for_status()
            return resp
        return db.retry(_get, db.HTTP_RETRIES, db.HTTP_BACKOFF_BASE_S,
                        (requests.RequestException,), what=f"GET {url.split('?')[0]}")

    def write_heartbeat(self, feed: str, started: datetime, res: FeedResult, message: str) -> None:
        finished = datetime.now(UTC)
        try:
            conn = self.conn("ingest")
            with conn.cursor() as cur:
                # Here we save one row in the heartbeat table to say how this feed run went.
                cur.execute(
                    "INSERT INTO collector_heartbeat (feed, started_at, finished_at, duration_ms, status, rows_fetched, "
                    "rows_ingested, rows_rejected, error_count, message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (feed, started, finished, int((finished - started).total_seconds() * 1000), res.status,
                     res.fetched, res.ingested, res.rejected, res.errors, message[:500]))
            conn.commit()
        except Exception as exc:  # a failed heartbeat must never stop the collector
            log.error("could not write heartbeat for %s: %s", feed, exc)

    # one method per feed
    def prime_cache(self) -> None:
        conn = self.conn("ingest")
        with conn.cursor() as cur:
            # This gets the last seen time of every ship we already know about.
            cur.execute("SELECT mmsi, last_seen FROM latest_position")
            self.last_ts_ms = {m: int(t.timestamp() * 1000) for m, t in cur.fetchall()}
        conn.rollback()
        self.cache_primed = True
        log.info("duplicate-skip cache primed with %d vessels", len(self.last_ts_ms))

    def run_positions(self) -> FeedResult:
        res = FeedResult()
        if not self.cache_primed:
            self.prime_cache()
        params = {}
        if self.positions_from_ms:
            params["from"] = self.positions_from_ms
        sent_at = int(time.time() * 1000)
        data = self.http_get(db.API["locations"], params).json()
        features = data.get("features", [])
        res.fetched = len(features)
        rows = parse_position_features(features)

        keep, outside, unchanged = [], 0, 0
        for r in rows:
            mmsi, lat, lon, ts = r[0], r[1], r[2], int(r[7].timestamp() * 1000)
            if not in_bbox(lat, lon):
                outside += 1
            elif mmsi is not None and self.last_ts_ms.get(mmsi, -1) >= ts:
                unchanged += 1                         # same position as last time, so skip it
            else:
                keep.append(r)
                if mmsi is not None:
                    self.last_ts_ms[mmsi] = ts
        res.notes.append(f"{outside} were outside the Gulf of Finland, {unchanged} had no new position")

        conn = self.conn("ingest")
        if keep:
            with conn.cursor() as cur:                 # 1) put the raw rows in the waiting room and save them
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO staging_ais (mmsi, lat, lon, sog, cog, heading, nav_stat, event_time) VALUES %s",
                    keep, page_size=1000)
            conn.commit()
        with conn.cursor() as cur:                     # 2) check everything waiting (this also retries leftovers)
            cur.execute("SELECT processed, accepted, rejected, duplicates FROM validate_and_migrate_ais()")
            processed, accepted, rejected, dups = cur.fetchone()
        conn.commit()
        res.ingested, res.rejected = accepted, rejected
        res.notes.append(f"checked {processed} new records: {accepted} stored, {rejected} refused")
        self.positions_from_ms = sent_at - 2 * 60 * 1000          # next time only ask for positions from the last 2 minutes
        return res

    def run_vessels(self) -> FeedResult:
        res = FeedResult()
        data = self.http_get(db.API["vessels"]).json()
        res.fetched = len(data)
        conn = self.conn("ingest")
        with conn.cursor() as cur:
            # This gets the mmsi of every ship we are tracking.
            cur.execute("SELECT mmsi FROM latest_position")
            tracked = {m for (m,) in cur.fetchall()}
        conn.rollback()

        rows = [p for p in (parse_vessel(v) for v in data) if p and p[0] in tracked]
        res.notes.append(f"{len(rows)} of {len(data)} ships are in the Gulf of Finland")
        changed = 0
        # This is the insert for the vessels table. If the ship is already there it only updates the row when the details changed.
        sql = (
            "INSERT INTO vessels (mmsi, imo, call_sign, name, ship_type, length_m, width_m, draught_m, destination, eta, status_ts) "
            "VALUES %s ON CONFLICT (mmsi) DO UPDATE SET imo = EXCLUDED.imo, call_sign = EXCLUDED.call_sign, "
            "name = EXCLUDED.name, ship_type = EXCLUDED.ship_type, length_m = EXCLUDED.length_m, width_m = EXCLUDED.width_m, "
            "draught_m = EXCLUDED.draught_m, destination = EXCLUDED.destination, eta = EXCLUDED.eta, "
            "status_ts = EXCLUDED.status_ts, updated_at = now() "
            "WHERE (vessels.status_ts IS NULL OR EXCLUDED.status_ts IS NULL OR EXCLUDED.status_ts >= vessels.status_ts) "
            "AND (vessels.imo, vessels.call_sign, vessels.name, vessels.ship_type, vessels.length_m, vessels.width_m, "
            "     vessels.draught_m, vessels.destination, vessels.eta) IS DISTINCT FROM "
            "    (EXCLUDED.imo, EXCLUDED.call_sign, EXCLUDED.name, EXCLUDED.ship_type, EXCLUDED.length_m, EXCLUDED.width_m, "
            "     EXCLUDED.draught_m, EXCLUDED.destination, EXCLUDED.eta)")
        with conn.cursor() as cur:
            for i in range(0, len(rows), 400):
                # Here we send the ships to the database in batches of 400.
                psycopg2.extras.execute_values(cur, sql, rows[i:i + 400], page_size=400)
                changed += cur.rowcount
        conn.commit()
        res.ingested = changed
        res.notes.append(f"{changed} ships had new details")
        return res

    def run_weather(self) -> FeedResult:
        res = FeedResult()
        b = db.BBOX
        start = (datetime.now(UTC) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"service": "WFS", "version": "2.0.0", "request": "getFeature",
                  "storedquery_id": db.FMI_WEATHER_QUERY,
                  "bbox": f"{b['min_lon'] - 1},{b['min_lat'] - 0.5},{b['max_lon']},{b['max_lat'] + 0.3}",
                  "parameters": "WindSpeedMS,WindGust,WindDirection", "starttime": start, "timestep": "10"}
        grouped = parse_fmi_wfs(self.http_get(db.API["fmi_wfs"], params).text)
        rows = []
        for (lat, lon, when), vals in grouped.items():
            if vals.get("WindSpeedMS") is None and vals.get("WindGust") is None:
                continue
            wd = vals.get("WindDirection")
            rows.append((f"{lat:.5f},{lon:.5f}", lon, lat, when, vals.get("WindSpeedMS"), vals.get("WindGust"),
                         int(round(wd)) % 361 if wd is not None else None))
        res.fetched = len(grouped)
        conn = self.conn("ingest")
        with conn.cursor() as cur:
            n = 0
            for i in range(0, len(rows), 500):
                # Here we save the wind readings and skip the ones we already have.
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO weather_obs (station_id, geom, observed_at, wind_speed_ms, wind_gust_ms, wind_dir_deg) VALUES %s "
                    "ON CONFLICT DO NOTHING", rows[i:i + 500],
                    template="(%s, ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s, %s, %s)", page_size=500)
                n += max(cur.rowcount, 0)
        conn.commit()
        res.ingested = n
        res.notes.append(f"{len(rows)} wind readings, {n} new")
        return res

    def run_sea_state(self) -> FeedResult:
        res = FeedResult()
        conn = self.conn("ingest")
        rows = []

        # (a) sea state estimates. Most sites report rarely, so old ones are skipped.
        try:
            feats = self.http_get(db.API["sea_state"]).json().get("features", [])
            res.fetched += len(feats)
            cutoff = datetime.now(UTC) - timedelta(days=7)
            stale = 0
            for f in feats:
                p, coords = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or []
                if len(coords) < 2 or not p.get("lastUpdate"):
                    continue
                seen = datetime.fromisoformat(p["lastUpdate"].replace("Z", "+00:00"))
                if seen < cutoff:
                    stale += 1
                    continue
                wd = p.get("windWaveDir")
                rows.append(("DIGITRAFFIC_SSE", str(p.get("siteNumber")), p.get("siteName"), coords[0], coords[1], seen,
                             p.get("seaState"), None, int(wd) % 361 if wd is not None else None, p.get("trend")))
            res.notes.append(f"{len(feats)} sea-state stations, {stale} ignored because their data is old")
        except Exception as exc:  # one source failing must not lose the other
            res.errors += 1
            res.notes.append(f"sea-state data failed: {exc}")
            log.error("sea_state (Digitraffic) failed: %s", exc)

        # (b) wave height from wave buoys
        try:
            b = db.BBOX
            start = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {"service": "WFS", "version": "2.0.0", "request": "getFeature", "storedquery_id": db.FMI_WAVE_QUERY,
                      "bbox": f"{b['min_lon'] - 1},{b['min_lat'] - 0.5},{b['max_lon']},{b['max_lat'] + 0.3}",
                      "parameters": "WaveHs,ModalWDi", "starttime": start}
            grouped = parse_fmi_wfs(self.http_get(db.API["fmi_wfs"], params).text)
            res.fetched += len(grouped)
            waves = 0
            for (lat, lon, when), vals in grouped.items():
                if vals.get("WaveHs") is None:
                    continue
                wd = vals.get("ModalWDi")
                rows.append(("FMI_WAVE", f"{lat:.5f},{lon:.5f}", None, lon, lat,
                             datetime.fromisoformat(when.replace("Z", "+00:00")), None, vals["WaveHs"],
                             int(round(wd)) % 361 if wd is not None else None, None))
                waves += 1
            res.notes.append(f"{waves} wave readings")
        except Exception as exc:
            res.errors += 1
            res.notes.append(f"wave data failed: {exc}")
            log.error("sea_state (FMI waves) failed: %s", exc)

        if rows:
            with conn.cursor() as cur:
                # Here we save the sea state and wave rows and skip the ones we already have.
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO sea_conditions (source, site_id, site_name, geom, observed_at, sea_state, wave_height_m, "
                    "wave_dir_deg, trend) VALUES %s ON CONFLICT DO NOTHING",
                    [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]) for r in rows],
                    template="(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s, %s, %s, %s)", page_size=500)
                res.ingested = max(cur.rowcount, 0)
            conn.commit()
        if res.errors == 2:
            raise RuntimeError("both sea-state sources failed")
        return res

    def run_engine(self) -> FeedResult:
        """Save ships that are silent right now as gaps, then work out the possible areas that are waiting."""
        res = FeedResult()
        conn = self.conn("ingest")
        with conn.cursor() as cur:
            # This asks the database to save the silent ships as gaps and update the old ones.
            cur.execute("SELECT opened, refreshed, expired FROM sync_open_gaps()")
            opened, refreshed, expired = cur.fetchone()
        conn.commit()
        res.ingested = opened
        res.notes.append(f"{opened} new silences found, {refreshed} re-checked, {expired} given up (ship left the area)")
        try:
            owner = self.conn("owner")
            done = process_pending(owner)
            res.fetched = len(done)
            res.notes.append(f"possible area drawn for {len(done)} ship(s)")
        except Exception as exc:
            res.errors += 1
            res.notes.append(f"area calculation failed: {exc}")
            log.error("feasibility engine failed: %s", exc)
            if self.conns.get("owner"):
                self.conns["owner"].rollback()
        return res

    # running the feeds
    def run_feed(self, feed: str) -> FeedResult:
        started = datetime.now(UTC)
        res = FeedResult()
        try:
            res = getattr(self, f"run_{feed}")()
        except Exception as exc:  # the daemon must survive any single failure
            res.errors += 1
            res.notes.append(f"{type(exc).__name__}: {exc}")
            log.error("%s failed: %s", feed, exc)
            c = self.conns.get("ingest")
            if c is not None and not c.closed:
                try:
                    c.rollback()
                except psycopg2.Error:
                    pass
        message = "; ".join(res.notes)
        self.write_heartbeat(feed, started, res, message)
        log.info("%-9s %-7s fetched=%-5d ingested=%-5d rejected=%-4d %s", feed, res.status, res.fetched, res.ingested,
                 res.rejected, message)
        return res

    def run_once(self) -> dict[str, FeedResult]:
        return {f: self.run_feed(f) for f in self.feeds}

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        next_run = {f: 0.0 for f in self.feeds}
        log.info("collector started: feeds=%s intervals=%s", self.feeds, {f: db.POLL_INTERVALS[f] for f in self.feeds})
        while not self.stop:
            now = time.monotonic()
            for feed in self.feeds:
                if self.stop:
                    break
                if next_run[feed] <= now:
                    self.run_feed(feed)
                    next_run[feed] = time.monotonic() + db.POLL_INTERVALS[feed]
            wait = max(0.5, min(next_run.values()) - time.monotonic())
            end = time.monotonic() + min(wait, 15)
            while not self.stop and time.monotonic() < end:
                time.sleep(0.25)
        log.info("collector stopped")
        for c in self.conns.values():
            if c is not None and not c.closed:
                c.close()

    def _on_signal(self, signum, _frame) -> None:
        log.info("signal %s received - finishing the current step, then stopping", signum)
        self.stop = True


# Possible-area 
# When a ship goes silent,where could it have been? steps for one gap:
#   1. top speed v_max   = the larger of the assumed speed for its ship type and 110 % of its own fastest recorded speed
#   2. usable speed      = v_max x weather slowdown (from wind and waves around the middle of the silence)
#   3. reach R           = usable speed x time silent
#   4. possible area     = circle(last position, R) overlap circle(next position, R), or just the first circle if the
#                          ship is still silent
#   5. tightest area     = an ellipse with the two positions as its focal points
#   6. land is cut out of both shapes (except a 1.5 km margin around the real positions, because the 10 m coastline is rough)
# All shapes are built in PostGIS on the earth's curved surface. This shows what was possible, not what happened.
# The slowdown table is my own assumption, not maritime science.

MAX_V_KNOTS = 45.0           # no merchant ship is assumed to be faster than this
OBSERVED_SPEED_MARGIN = 1.10  # allow 10 % above the fastest speed this ship was ever seen doing

# This gets one gap together with the type and name of its ship.
GAP_SQL = """
SELECT g.gap_id, g.mmsi, g.status, g.classification, g.last_seen_time, g.reappear_time,
       ST_X(g.start_geom) AS p1_lon, ST_Y(g.start_geom) AS p1_lat,
       v.ship_type, v.name
  FROM ais_gap_events g JOIN vessels v USING (mmsi)
 WHERE g.gap_id = %s
"""

# One statement builds both shapes, removes the land, measures the areas and stores them. %(r)s is the reach in metres.
REGION_SQL = """
WITH g AS (
    SELECT gap_id, start_geom AS p1, end_geom AS p2 FROM ais_gap_events WHERE gap_id = %(gap_id)s
), base AS (
    SELECT g.*,
           CASE WHEN p2 IS NULL THEN NULL ELSE ST_Distance(p1::geography, p2::geography) END AS d_m,
           CASE WHEN p2 IS NULL THEN NULL ELSE ST_Azimuth(p1::geography, p2::geography) END AS az
      FROM g
), circles AS (                          -- reach circles on the earth
    SELECT b.*,
           ST_Buffer(b.p1::geography, %(r)s, %(quad)s)::geometry AS c1,
           CASE WHEN b.p2 IS NULL THEN NULL ELSE ST_Buffer(b.p2::geography, %(r)s, %(quad)s)::geometry END AS c2
      FROM base b
), lens AS (                             -- possible area = circle(P1) overlap circle(P2)
    SELECT c.*, CASE WHEN c.p2 IS NULL THEN c.c1 ELSE ST_Intersection(c.c1, c.c2) END AS lens_geom
      FROM circles c
), shapes AS (                           -- tightest area = ellipse (distance to P1 plus distance to P2 is at most R)
    SELECT l.gap_id, l.lens_geom, l.d_m,
           CASE
             WHEN l.p2 IS NULL THEN l.lens_geom
             WHEN l.d_m >= %(r)s THEN NULL
             ELSE (
                 SELECT ST_MakePolygon(ST_AddPoint(ring.line, ST_StartPoint(ring.line)))
                   FROM (
                        SELECT ST_MakeLine(pt.geom ORDER BY pt.k) AS line
                          FROM (
                               SELECT k,
                                      ST_Project(ST_Project(l.p1::geography, l.d_m / 2.0, l.az),
                                                 sqrt(e.x * e.x + e.y * e.y),
                                                 l.az + atan2(e.y, e.x))::geometry AS geom
                                 FROM (SELECT k,
                                              (%(r)s / 2.0) * cos(2 * pi() * k / %(n)s) AS x,
                                              sqrt(power(%(r)s / 2.0, 2) - power(l.d_m / 2.0, 2)) * sin(2 * pi() * k / %(n)s) AS y
                                         FROM generate_series(0, %(n)s - 1) k) e
                               ) pt
                        ) ring
             )
           END AS tight_geom
      FROM lens l
), keep AS (                             -- the real positions are water, but the coastline is rough, so
    SELECT ST_Union(ST_Buffer(p1::geography, 1500)::geometry,           -- keep a 1.5 km margin around them
                    COALESCE(ST_Buffer(p2::geography, 1500)::geometry, ST_Buffer(p1::geography, 1500)::geometry)) AS geom
      FROM base
), land AS (                             -- only the land tiles that touch the area (uses the index)
    SELECT ST_Difference(ST_UnaryUnion(ST_Collect(t.geom)), (SELECT geom FROM keep)) AS geom
      FROM land_tiles t JOIN shapes s ON t.geom && s.lens_geom AND ST_Intersects(t.geom, s.lens_geom)
), water AS (
    SELECT s.gap_id, s.lens_geom, s.tight_geom,
           fn_clean_polygons(ST_Difference(s.lens_geom,  COALESCE(land.geom, ST_SetSRID('GEOMETRYCOLLECTION EMPTY'::geometry, 4326)))) AS lens_water,
           fn_clean_polygons(ST_Difference(s.tight_geom, COALESCE(land.geom, ST_SetSRID('GEOMETRYCOLLECTION EMPTY'::geometry, 4326)))) AS tight_water
      FROM shapes s CROSS JOIN land
)
UPDATE ais_gap_events a
   SET feasible_geom     = w.lens_water,
       tight_geom        = w.tight_water,
       feasible_area_km2 = round((ST_Area(w.lens_water::geography)  / 1e6)::numeric, 1),
       tight_area_km2    = round((ST_Area(w.tight_water::geography) / 1e6)::numeric, 1),
       land_removed_km2  = round(((ST_Area(w.lens_geom::geography) - COALESCE(ST_Area(w.lens_water::geography), 0)) / 1e6)::numeric, 1)
  FROM water w
 WHERE a.gap_id = w.gap_id
RETURNING a.feasible_area_km2, a.tight_area_km2, a.land_removed_km2, (a.feasible_geom IS NOT NULL) AS has_region
"""

# How much of the tight area ships have really sailed through, using the traffic grid.
VISITED_SQL = """
UPDATE ais_gap_events g
   SET visited_area_km2 = (
        SELECT round((ST_Area(ST_Intersection(g.tight_geom, u.geom)::geography) / 1e6)::numeric, 1)
          FROM (SELECT ST_UnaryUnion(ST_Collect(c.geom)) AS geom
                  FROM traffic_grid c WHERE c.visits > 0 AND c.geom && g.tight_geom) u
         WHERE u.geom IS NOT NULL)
 WHERE g.gap_id = %s AND g.tight_geom IS NOT NULL
"""


def compute_gap(conn, gap_id: int) -> dict:
    """Work out and store the possible area of one gap. Returns a short summary."""
    gap_rows = db.query(conn, GAP_SQL, (gap_id,))
    if not gap_rows:
        raise ValueError(f"gap {gap_id} not found")
    gap = gap_rows[0]
    is_open = gap["reappear_time"] is None
    t1 = gap["last_seen_time"]
    t2 = datetime.now(timezone.utc) if is_open else gap["reappear_time"]
    dt_s = (t2 - t1).total_seconds()
    dt_h = dt_s / 3600.0

    # top speed: the assumption for the ship type, or 110 % of the fastest speed this ship was seen doing
    class_label = db.scalar(conn, "SELECT fn_ship_class(%s::smallint)", (gap["ship_type"],))
    class_vmax = float(db.scalar(conn, "SELECT fn_ship_vmax(%s::smallint)", (gap["ship_type"],)))
    observed = db.scalar(conn, "SELECT max(speed_knots) FROM location_events WHERE mmsi = %s AND event_time <= %s",
                         (gap["mmsi"], t1))
    observed = float(observed) if observed is not None else None
    v_max = min(MAX_V_KNOTS, max(class_vmax, OBSERVED_SPEED_MARGIN * observed if observed else 0.0))

    # weather around the middle of the silence
    cond = db.query(conn, """
        SELECT c.* FROM get_speed_factor(
              (SELECT ST_LineInterpolatePoint(ST_MakeLine(start_geom, COALESCE(end_geom, start_geom)), 0.5)
                 FROM ais_gap_events WHERE gap_id = %s),
              %s::timestamptz) c""", (gap_id, t1 + (t2 - t1) / 2)) [0]
    factor = float(cond["speed_factor"])
    v_eff = v_max * factor
    reach_m = v_eff * db.KNOT_M_PER_S * dt_s

    # build the shapes in PostGIS
    out = db.query(conn, REGION_SQL, {"gap_id": gap_id, "r": reach_m, "n": db.ELLIPSE_POINTS,
                                      "quad": f"quad_segs={db.CIRCLE_SEGMENTS_PER_QUARTER}"})[0]
    # Here we work out how much of the tight area was visited before.
    db.execute(conn, VISITED_SQL, (gap_id,))

    # This gets the distance in metres between the two positions of the gap.
    dist_m = db.scalar(conn, "SELECT CASE WHEN end_geom IS NULL THEN NULL ELSE ST_Distance(start_geom::geography, end_geom::geography) END "
                             "FROM ais_gap_events WHERE gap_id = %s", (gap_id,))
    dist_km = float(dist_m) / 1000 if dist_m is not None else None
    implied = (float(dist_m) / 1852.0) / dt_h if dist_m is not None and dt_h > 0 else None

    if is_open:
        status = "PROJECTED"
    elif dist_m is not None and float(dist_m) > reach_m:
        status = "INFEASIBLE"          # even at full usable speed it could not have covered the distance between the two positions
    else:
        status = "OK"

    conditions = {
        "assumption_notice": "speed_factor_lookup and ship_class_speed are engineering assumptions, not maritime science",
        "ship_class": class_label, "class_default_knots": class_vmax, "observed_max_knots": observed,
        "v_max_rule": f"min({MAX_V_KNOTS}, max(class default, {OBSERVED_SPEED_MARGIN} x observed max))",
        "gap_hours": round(dt_h, 2),
        **{k: (v.isoformat() if hasattr(v, "isoformat") else (float(v) if v is not None and k not in ("sea_state", "basis") else v))
           for k, v in cond.items()},
    }
    # Here we save the result of the calculation (status, speeds, reach) on the gap row.
    db.execute(conn, """
        UPDATE ais_gap_events
           SET feasibility_status = %s, v_max_knots = %s, speed_factor = %s, v_eff_knots = %s,
               reach_radius_km = %s, observed_distance_km = %s, implied_speed_knots = %s,
               conditions = %s::jsonb, feasibility_computed_at = now()
         WHERE gap_id = %s""",
               (status, round(v_max, 1), round(factor, 2), round(v_eff, 1), round(reach_m / 1000.0, 2),
                round(dist_km, 2) if dist_km is not None else None,
                round(implied, 1) if implied is not None else None, json.dumps(conditions), gap_id))
    conn.commit()
    return {"gap_id": gap_id, "mmsi": gap["mmsi"], "vessel": gap["name"], "status": status, "gap_h": round(dt_h, 2),
            "v_max": round(v_max, 1), "factor": round(factor, 2), "v_eff": round(v_eff, 1),
            "reach_km": round(reach_m / 1000.0, 1), "dist_km": round(dist_km, 1) if dist_km is not None else None,
            "lens_km2": out["feasible_area_km2"], "tight_km2": out["tight_area_km2"], "land_km2": out["land_removed_km2"]}


def process_pending(conn, limit: int = 100, only_dark: bool = True, refresh_open_after_min: int = 5) -> list[dict]:
    """Work out every gap that needs it: new closed gaps, and open gaps whose area is out of date."""
    where = "is_dark_activity AND " if only_dark else ""
    # This gets the ids of the gaps that are new or have an old area.
    ids = [r["gap_id"] for r in db.query(conn, f"""
        SELECT gap_id FROM ais_gap_events
         WHERE {where}((status = 'CLOSED' AND feasibility_status = 'PENDING')
                    OR (status = 'OPEN' AND (feasibility_computed_at IS NULL
                                             OR feasibility_computed_at < now() - make_interval(mins => %s))))
         ORDER BY gap_id LIMIT %s""", (refresh_open_after_min, limit))]
    conn.rollback()
    done = []
    for gap_id in ids:
        try:
            done.append(compute_gap(conn, gap_id))
        except Exception:
            conn.rollback()
            raise
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run each feed once and exit")
    parser.add_argument("--feeds", help="comma separated subset, e.g. positions,weather")
    parser.add_argument("--gap", type=int, help="only work out the possible area of this gap id, then exit")
    parser.add_argument("--areas-all", action="store_true", help="only redo every possible area, then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.gap or args.areas_all:
        conn = db.connect("owner", app_name="feasibility")
        if args.gap:
            results = [compute_gap(conn, args.gap)]
        else:
            # Here we mark every dark gap as pending so all the areas get redone.
            db.execute(conn, "UPDATE ais_gap_events SET feasibility_status = 'PENDING', feasibility_computed_at = NULL WHERE is_dark_activity")
            conn.commit()
            results = process_pending(conn)
        db.print_table(results)
        conn.close()
        return 0

    feeds = [f.strip() for f in args.feeds.split(",")] if args.feeds else None
    collector = Collector(feeds)
    if args.once:
        results = collector.run_once()
        return 0 if all(r.status != "ERROR" for r in results.values()) else 1
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
