"""Create or update the Dark Ship Watch database, and load the land map.

    python setup_db.py                 create the database if missing, apply database.sql, sync thresholds
    python setup_db.py --check         only report what exists
    python setup_db.py --reset --yes   drop everything in the public schema and rebuild (deletes all data)
    python setup_db.py --land          load Natural Earth 10m land (downloaded once into data/natural_earth/)
    python setup_db.py --land --zip path/to/ne_10m_land.zip

It is safe to run again and again because database.sql can be repeated without harm.

The land is cut to a box around the Gulf of Finland, stored in land_polygons and split into small indexed
tiles (land_tiles) so that cutting land out of a possible area stays fast. It replaces the rough fallback
coastline that database.sql creates. 10m data is still only an approximation of the Finnish archipelago.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import psycopg2
from psycopg2 import sql

import db

SQL_FILE = "database.sql"
DEMO_MARKER = "-- @@DEMO"
EXPECTED_TABLES = [
    "collector_heartbeat", "vessels", "staging_ais", "rejected_events", "location_events", "latest_position",
    "vessel_status_history", "ports", "port_events", "ais_gap_events", "sea_conditions", "weather_obs",
    "land_polygons", "land_tiles", "traffic_grid", "speed_factor_lookup", "ship_class_speed", "pipeline_config",
    "vessel_port_state", "rejection_reasons", "location_events_archive",
]
LAND_DIR = db.BASE_DIR / "data" / "natural_earth"


def ensure_database() -> None:
    admin = psycopg2.connect(host=db.DB_HOST, port=db.DB_PORT, dbname="postgres",
                             user=db.DB_ROLES["owner"]["user"],
                             password=db.DB_ROLES["owner"]["password"] or None, connect_timeout=10)
    admin.autocommit = True
    with admin.cursor() as cur:
        # This checks if the database already exists.
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db.DB_NAME,))
        if cur.fetchone():
            print(f"[db] database '{db.DB_NAME}' exists")
        else:
            # This creates the database.
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db.DB_NAME)))
            print(f"[db] created database '{db.DB_NAME}'")
    admin.close()


def apply_sql_file(conn) -> None:
    text = (db.BASE_DIR / SQL_FILE).read_text(encoding="utf-8")
    text = text.split(DEMO_MARKER, 1)[0]                  # the part after the marker is a demo for psql only
    with conn.cursor() as cur:
        # This runs the whole database.sql text.
        cur.execute(text)
    conn.commit()
    print(f"[sql] applied {SQL_FILE}")


def sync_thresholds(conn) -> None:
    with conn.cursor() as cur:
        for key, value in db.THRESHOLDS.items():
            # This updates the config value for each threshold if it is different.
            cur.execute(
                "UPDATE pipeline_config SET cfg_value = %s WHERE cfg_key = %s AND cfg_value IS DISTINCT FROM %s",
                (value, key, value))
    conn.commit()
    print(f"[cfg] pipeline_config synchronised with db.THRESHOLDS ({len(db.THRESHOLDS)} keys)")


def report(conn) -> bool:
    ok = True
    # This gets the installed extensions and their versions.
    ext = {r["extname"]: r["extversion"] for r in db.query(conn, "SELECT extname, extversion FROM pg_extension")}
    print("[check] extensions:", ", ".join(f"{k} {v}" for k, v in sorted(ext.items())))
    for needed in ("postgis", "btree_gist"):
        if needed not in ext:
            print(f"[check] MISSING extension {needed}")
            ok = False
    # This gets the names of the tables in the public schema.
    present = {r["table_name"] for r in db.query(
        conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    missing = [t for t in EXPECTED_TABLES if t not in present]
    print(f"[check] tables present: {len(EXPECTED_TABLES) - len(missing)}/{len(EXPECTED_TABLES)}")
    if missing:
        print("[check] MISSING tables:", ", ".join(missing))
        ok = False
    # This counts the land polygons for each source.
    land = db.query(conn, "SELECT source, count(*) AS n FROM land_polygons GROUP BY source")
    print("[check] land polygons:", ", ".join(f"{r['source']} x{r['n']}" for r in land) or "none")
    if any(r["source"] == "FALLBACK_COARSE" for r in land):
        print("        -> rough fallback coastline in use; run `python setup_db.py --land` for Natural Earth 10m land")
    return ok


def download_land(dest_zip: Path) -> None:
    import requests
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    print(f"[land] downloading {db.NATURAL_EARTH_LAND_URL}")
    with requests.get(db.NATURAL_EARTH_LAND_URL, headers=db.HTTP_HEADERS, stream=True, timeout=(10, 120)) as r:
        r.raise_for_status()
        with open(dest_zip, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    print(f"[land] saved {dest_zip} ({dest_zip.stat().st_size / 1e6:.1f} MB)")


def polygons_only(geom) -> list:
    from shapely.geometry import MultiPolygon, Polygon
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [p for g in getattr(geom, "geoms", []) for p in polygons_only(g)]


def read_clipped(zip_path: Path) -> list:
    """Unzip the shapefile and keep only the land inside the clip box."""
    import shapefile                      # the pyshp package
    from shapely.geometry import MultiPolygon, box, shape
    from shapely.validation import make_valid

    extract_dir = zip_path.parent / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():                       # use only the file name, never the stored folder path
            target = extract_dir / Path(member).name
            if member.endswith("/") or not Path(member).name:
                continue
            target.write_bytes(zf.read(member))
    shp_files = list(extract_dir.glob("*.shp"))
    if not shp_files:
        raise SystemExit("no .shp file found in the archive")
    clip = box(*db.LAND_CLIP_BOX)
    out = []
    reader = shapefile.Reader(str(shp_files[0]))
    for sh in reader.shapes():
        geom = make_valid(shape(sh.__geo_interface__))
        if not geom.intersects(clip):
            continue
        parts = polygons_only(geom.intersection(clip))
        if parts:
            out.append(MultiPolygon(parts))
    return out


def load_land(zip_arg: Path | None) -> int:
    zip_path = zip_arg or (LAND_DIR / "ne_10m_land.zip")
    if not zip_path.exists():
        download_land(zip_path)
    else:
        print(f"[land] using {zip_path}")

    pieces = read_clipped(zip_path)
    print(f"[land] {len(pieces)} land mass(es) intersect the Baltic clip box {db.LAND_CLIP_BOX}")
    if not pieces:
        raise SystemExit("nothing to load")

    conn = db.connect("owner")
    try:
        with conn.cursor() as cur:
            # First remove any old Natural Earth land from the table.
            cur.execute("DELETE FROM land_polygons WHERE source = 'NATURAL_EARTH_10M'")
            for i, mp in enumerate(pieces, 1):
                # Here we insert each land piece as a polygon.
                cur.execute(
                    "INSERT INTO land_polygons (name, source, geom) "
                    "VALUES (%s, 'NATURAL_EARTH_10M', ST_Multi(ST_SetSRID(ST_GeomFromWKB(%s), 4326)))",
                    (f"ne_10m_land part {i}", mp.wkb))
            # Then remove the rough fallback land and rebuild the land tiles.
            cur.execute("DELETE FROM land_polygons WHERE source = 'FALLBACK_COARSE'")
            cur.execute("SELECT rebuild_land_tiles()")
            tiles = cur.fetchone()[0]
            # This counts the land vertices and the land area.
            cur.execute("SELECT ST_NPoints(ST_Collect(geom)), round((sum(ST_Area(geom::geography)) / 1e6)::numeric) FROM land_polygons")
            npts, area = cur.fetchone()
        conn.commit()
        print(f"[land] loaded: {npts:,} vertices, {area:,} km2 of land -> {tiles:,} subdivided tiles")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only report the current state")
    parser.add_argument("--reset", action="store_true", help="drop the public schema first (needs --yes)")
    parser.add_argument("--yes", action="store_true", help="confirm --reset")
    parser.add_argument("--land", action="store_true", help="load the Natural Earth land map instead of setting up")
    parser.add_argument("--zip", type=Path, help="with --land: use an already downloaded ne_10m_land.zip")
    args = parser.parse_args()
    if args.reset and not args.yes:
        parser.error("--reset destroys all data; add --yes to confirm")
    sys.stdout.reconfigure(encoding="utf-8")

    if args.land:
        return load_land(args.zip)

    ensure_database()
    conn = db.connect("owner")
    try:
        if args.reset:
            with conn.cursor() as cur:
                # This drops the whole public schema and makes it again.
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            conn.commit()
            print("[db] public schema dropped and recreated")
        if not args.check:
            apply_sql_file(conn)
            sync_thresholds(conn)
        ok = report(conn)
    finally:
        conn.close()
    print("[done]" if ok else "[done with problems]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
