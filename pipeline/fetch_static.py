"""Kentucky counties (Census TIGERweb), transmission lines (HIFLD Open) and critical facilities (OSM / Overpass)."""
import json
import pathlib
import time

import requests

RAW = pathlib.Path(__file__).resolve().parent.parent / "raw"
UA = {"User-Agent": "kentucky-grid-risk-map/0.1 (research; github.com/manmeet3591)"}
KY_BBOX = (-89.6, 36.45, -81.9, 39.2)  # lon/lat, a little wider than the state

TIGER = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query"
         "?where=STATE%3D%2721%27&outFields=NAME,GEOID&f=geojson&geometryPrecision=5"
         "&maxAllowableOffset=0.001&returnGeometry=true")
HIFLD = ("https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
         "Electric_Power_Transmission_Lines/FeatureServer/0/query")
OVERPASS = "https://overpass-api.de/api/interpreter"

# Critical facilities. Fuel terminals are tagged inconsistently in OSM, so several tags are combined.
FACILITIES = {
    "gas_station": 'nwr["amenity"="fuel"](area.k);',
    "hospital": 'nwr["amenity"="hospital"](area.k);nwr["healthcare"="hospital"](area.k);',
    "substation": 'nwr["power"="substation"](area.k);',
    "water_plant": ('nwr["man_made"="water_works"](area.k);nwr["man_made"="wastewater_plant"](area.k);'
                    'nwr["water_works"](area.k);'),
    "fuel_terminal": ('nwr["industrial"~"^(oil|fuel|fuel_depot|oil_depot|petroleum_terminal|terminal)$"](area.k);'
                      'nwr["man_made"="storage_tank"]["content"~"oil|fuel|petrol|diesel|gasoline"](area.k);'
                      'nwr["landuse"="industrial"]["product"~"oil|fuel|petrol"](area.k);'),
}


def counties():
    g = requests.get(TIGER, timeout=120, headers=UA).json()
    for f in g["features"]:
        p = f["properties"]
        f["properties"] = {"name": p["NAME"].replace(" County", ""), "fips": p["GEOID"]}
    print(f"counties: {len(g['features'])}")
    (RAW / "counties.geojson").write_text(json.dumps(g))


def lines():
    """All HIFLD lines whose envelope touches Kentucky. Voltage filtering happens in build step."""
    feats, offset = [], 0
    while True:
        params = {
            "where": "1=1", "geometry": ",".join(map(str, KY_BBOX)), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "f": "geojson",
            "outFields": "ID,TYPE,STATUS,OWNER,VOLTAGE,VOLT_CLASS,SUB_1,SUB_2,SOURCEDATE,VAL_DATE",
            "resultOffset": offset, "resultRecordCount": 1000, "geometryPrecision": 5,
        }
        r = requests.get(HIFLD, params=params, timeout=180, headers=UA)
        r.raise_for_status()
        page = r.json()["features"]
        feats += page
        print(f"  lines: {len(feats)}")
        if len(page) < 1000:
            break
        offset += len(page)
    (RAW / "hifld_lines.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def osm_lines():
    """69 kV lines from OSM: HIFLD Open has no sub-100 kV lines in Kentucky, so these fill that gap."""
    q = ('[out:json][timeout:300];area["ISO3166-2"="US-KY"]->.k;'
         'way["power"="line"]["voltage"~"(^|;)69000(;|$)"](area.k);out geom tags;')
    r = requests.post(OVERPASS, data={"data": q}, timeout=360, headers=UA)
    r.raise_for_status()
    feats = []
    for e in r.json()["elements"]:
        t = e.get("tags", {})
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in e["geometry"]]},
                      "properties": {"osm": f"way/{e['id']}", "voltage": t.get("voltage", ""),
                                     "operator": t.get("operator", ""), "name": t.get("name", "")}})
    print(f"  osm 69 kV ways: {len(feats)}")
    (RAW / "osm_lines_69kv.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def facilities():
    feats = []
    for cat, sel in FACILITIES.items():
        q = f'[out:json][timeout:300];area["ISO3166-2"="US-KY"]->.k;({sel});out center tags;'
        for attempt in range(4):
            try:
                r = requests.post(OVERPASS, data={"data": q}, timeout=360, headers=UA)
                r.raise_for_status()
                els = r.json()["elements"]
                break
            except Exception as e:  # Overpass rate-limits; back off and retry
                print(f"  {cat}: retry after {e}")
                time.sleep(30 * (attempt + 1))
        else:
            raise RuntimeError(f"Overpass failed for {cat}")
        seen = set()
        for e in els:
            lat, lon = (e["lat"], e["lon"]) if "lat" in e else (e["center"]["lat"], e["center"]["lon"])
            key = (e["type"], e["id"])
            if key in seen:
                continue
            seen.add(key)
            t = e.get("tags", {})
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                          "properties": {"cat": cat, "osm": f"{e['type']}/{e['id']}",
                                         "name": t.get("name") or t.get("brand") or t.get("operator") or "",
                                         "operator": t.get("operator", ""),
                                         "voltage": t.get("voltage", "") if cat == "substation" else ""}})
        print(f"  {cat}: {len(seen)}")
        time.sleep(5)
    (RAW / "facilities.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


if __name__ == "__main__":
    import sys
    RAW.mkdir(exist_ok=True)
    if len(sys.argv) > 1:  # run selected steps, e.g. `fetch_static.py osm_lines`
        for step in sys.argv[1:]:
            globals()[step]()
        raise SystemExit
    counties()
    lines()
    facilities()
    osm_lines()
