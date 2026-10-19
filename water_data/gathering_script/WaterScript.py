import json
import mysql.connector
import requests
from mysql.connector import Error
from shapely.geometry import Point, shape

DB_CONFIG = {
    'host': 'localhost',
    'port': 3306,
    'database': 'water_data',
    'user': 'USER HERE (changed to generics for personal safety)',
    'password': 'PASSWORD HERE (changed to generics for personal safety)',
}

"""Loads Florida county boundaries from the local GeoJSON file to make it easier to setup counties."""
def load_county():
    try:
        with open("geojson-fl-counties-fips.json", "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            counties = []
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                county_name = props.get("NAME") or props.get("NAMEFL") or "Unknown"
                poly = shape(feature.get("geometry"))
                counties.append((county_name, poly))
            print(f"Loaded {len(counties)} county polygons successfully.")
            return counties
    except FileNotFoundError:
        print("Error: geojson-fl-counties-fips.json can't be found in the project directory.")
        return []

COUNTY_POLYGONS = load_county()

"""Gets the county from the given coordinates, picking the nearest one if unknown."""
def get_county_from_coords(lat, lon):
    if lat is None or lon is None or not COUNTY_POLYGONS:
        return "Unknown"
    point = Point(lon, lat)
    for county_name, poly in COUNTY_POLYGONS:
        if poly.contains(point):
            return county_name
    min_distance = float('inf')
    closest_county = "Unknown"

    for county_name, poly in COUNTY_POLYGONS:
        centroid = poly.centroid
        dist = point.distance(centroid)
        if dist < min_distance:
            min_distance = dist
            closest_county = county_name
    return closest_county

"""Makes sure that the county column actually exists in the data."""
def county_column_check(conn):
    cursor = conn.cursor()
    try:
        cursor.execute("""
                       SELECT COLUMN_NAME
                       FROM INFORMATION_SCHEMA.COLUMNS
                       WHERE TABLE_SCHEMA = 'water_data'
                         AND TABLE_NAME = 'stations'
                         AND COLUMN_NAME = 'county_code';
                       """)
        result = cursor.fetchone()
        if not result:
            print("Adding county_code column to stations table...")
            cursor.execute("ALTER TABLE stations ADD COLUMN county_code VARCHAR(100) NULL;")
            conn.commit()
    except Error as e:
        print(f"Error checking county column: {e}")
    finally:
        cursor.close()

"""Fetches data from the USGS API, specifically the gauge height, location, time and discharge rate."""
def get_water_data():
    url = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-continuous/items"
    florida_bbox = "-87.63,24.39,-79.97,31.01"
    params = {
        "f": "json",
        "bbox": florida_bbox,
        "parameter_code": "00060,00065",
        "limit": 1000
    }
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return None

"""Saves OGC telementry data to the database"""
def save_ogc_data(data):
    if not data:
        print("No data received.")
        return

    features = data.get("features", [])
    if not features:
        print("No features returned in the payload.")
        return

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        county_column_check(conn)
        cursor = conn.cursor()
    except Error as e:
        print(f"Database connection error: {e}")
        return

    inserted_count = 0
    for feature in features:
        props = feature.get("properties", {})
        geometry = feature.get("geometry", {})

        site_code = props.get("monitoring_location_id")
        var_code = props.get("parameter_code")
        timestamp = props.get("time")
        val_str = props.get("value")

        coords = geometry.get("coordinates", [])
        lon = coords[0] if len(coords) > 0 else None
        lat = coords[1] if len(coords) > 1 else None

        if val_str is None or not site_code:
            continue

        try:
            val_float = float(val_str)
        except ValueError:
            continue

        station_name = props.get("station_name", site_code)
        county_name = get_county_from_coords(lat, lon)
        cursor.execute('''
                       INSERT INTO stations (site_id, station_name, latitude, longitude, county_code)
                       VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY
                       UPDATE
                           station_name = COALESCE (VALUES (station_name), station_name),
                           latitude = COALESCE (VALUES (latitude), latitude),
                           longitude = COALESCE (VALUES (longitude), longitude),
                           county_code = COALESCE (VALUES (county_code), county_code)
                       ''', (site_code, station_name, lat, lon, county_name))

        if var_code == "00065":
            cursor.execute('''
                           INSERT INTO telemetry_logs (site_id, timestamp, gage_height)
                           VALUES (%s, %s, %s)
                           ''', (site_code, timestamp, val_float))
            inserted_count += 1

        elif var_code == "00060":
            cursor.execute('''
                           INSERT INTO telemetry_logs (site_id, timestamp, discharge)
                           VALUES (%s, %s, %s)
                           ''', (site_code, timestamp, val_float))
            inserted_count += 1
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Success! Saved snapshot of {inserted_count} logs with automated county mapping.")


if __name__ == "__main__":
    raw_payload = get_water_data()
    save_ogc_data(raw_payload)