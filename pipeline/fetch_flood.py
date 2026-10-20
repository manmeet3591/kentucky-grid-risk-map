"""NASA OPERA DSWx-S1 water classes over Kentucky for each flood event, without credentials.

Source: NASA GIBS WMS (EPSG:4326), layer OPERA_L3_Dynamic_Surface_Water_Extent-Sentinel-1. GIBS renders
the DSWx-S1 B01_WTR band with a fixed palette, which we invert back to the product classes:

    0 not water   1 open water   3 inundated vegetation   250 HAND-masked   251 layover/shadow   255 no data

Each acquisition date is requested in 0.5 deg chunks at RES deg (~25 x 31 m, close to the 30 m product),
and saved as uint8 GeoTIFFs on a fixed grid, one folder per date: raw/opera/<date>/<i>_<j>.tif

The WMS was chosen over the WMTS tiles: on some dates the WMTS endpoint returned tiles from other
layers (RTC backscatter, true colour), while the WMS returned only DSWx palette colours.
Acquisition dates come from CMR (only dates with a DSWx-S1 granule over Kentucky are requested).

fetch_opera_earthdata.py writes the same grid from the original 30 m GeoTIFFs (needs Earthdata login).
"""
import concurrent.futures as cf
import io
import json
import pathlib

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.transform import from_origin
from shapely.geometry import box, shape
from shapely.ops import unary_union

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "raw" / "opera"
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
WMS = "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
LAYER = "OPERA_L3_Dynamic_Surface_Water_Extent-Sentinel-1"
UA = {"User-Agent": "kentucky-grid-risk-map/0.1 (research; github.com/manmeet3591)"}

# Analysis grid shared by every source: lon/lat, origin at the NW corner of the Kentucky box.
WEST, NORTH, EAST, SOUTH = -89.6, 39.2, -81.9, 36.45
RES = 0.00028
CHUNK = 0.5                      # degrees
NPX = round(CHUNK / RES)         # pixels per chunk side (1786)
PALETTE = {(255, 255, 255): 0, (0, 0, 255): 1, (0, 255, 0): 3, (200, 200, 200): 250, (128, 128, 128): 251}


def chunks(ky):
    out = []
    for i in range(int(np.ceil((EAST - WEST) / CHUNK))):
        for j in range(int(np.ceil((NORTH - SOUTH) / CHUNK))):
            w, n = WEST + i * CHUNK, NORTH - j * CHUNK
            b = box(w, n - NPX * RES, w + NPX * RES, n)
            if ky.intersects(b):
                out.append((i, j, b))
    return out


def granules(start, end):
    """{utc_date: footprint union} for DSWx-S1 granules over Kentucky."""
    out, page = {}, 1
    while True:
        r = requests.get(CMR, params={"short_name": "OPERA_L3_DSWX-S1_V1",
                                      "bounding_box": f"{WEST},{SOUTH},{EAST},{NORTH}",
                                      "temporal": f"{start}T00:00:00Z,{end}T23:59:59Z", "page_size": 2000,
                                      "page_num": page}, timeout=120, headers=UA)
        r.raise_for_status()
        es = r.json()["feed"]["entry"]
        for e in es:
            s, w, n, ea = map(float, e["boxes"][0].split())
            out.setdefault(e["time_start"][:10], []).append(box(w, s, ea, n))
        if len(es) < 2000:
            break
        page += 1
    return {d: unary_union(v) for d, v in sorted(out.items())}


def decode(png):
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    cls = np.full(a.shape[:2], 255, np.uint8)
    opaque = a[..., 3] > 0
    known = np.zeros_like(opaque)
    for rgb, v in PALETTE.items():
        m = opaque & (a[..., 0] == rgb[0]) & (a[..., 1] == rgb[1]) & (a[..., 2] == rgb[2])
        cls[m] = v
        known |= m
    bad = int((opaque & ~known).sum())
    return cls, bad, int(opaque.sum())


def fetch(job):
    date, i, j, b = job
    dst = OUT / date / f"{i}_{j}.tif"
    if dst.exists():
        return "cached"
    w, s, e, n = b.bounds
    params = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": LAYER, "CRS": "EPSG:4326",
              "BBOX": f"{s},{w},{n},{e}", "WIDTH": NPX, "HEIGHT": NPX, "FORMAT": "image/png",
              "TRANSPARENT": "true", "TIME": date}
    for _ in range(4):
        try:
            r = requests.get(WMS, params=params, timeout=180, headers=UA)
            if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                break
        except requests.RequestException:
            pass
    else:
        return "failed"
    cls, bad, opaque = decode(r.content)
    if opaque and bad / opaque > 0.001:
        return f"rejected ({bad}/{opaque} off-palette px)"
    if not opaque:
        return "empty"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst, "w", driver="GTiff", width=NPX, height=NPX, count=1, dtype="uint8", crs="EPSG:4326",
                       transform=from_origin(w, n, RES, RES), nodata=255, compress="deflate", tiled=True) as out:
        out.write(cls, 1)
    return "ok"


def main():
    ev = json.loads((ROOT / "pipeline" / "events.json").read_text())
    cty = json.loads((ROOT / "raw" / "counties.geojson").read_text())["features"]
    ky = unary_union([shape(f["geometry"]) for f in cty]).buffer(0.01)
    grid = chunks(ky)
    windows = [(f"reference{k}", r) for k, r in enumerate(ev["reference"])] + [(e["id"], e) for e in ev["events"]]
    manifest = {"res": RES, "west": WEST, "north": NORTH, "chunk_px": NPX, "source": "GIBS WMS", "windows": {}}
    for wid, w in windows:
        dates = granules(w["start"], w["end"])
        jobs = [(d, i, j, b) for d, foot in dates.items() for i, j, b in grid if foot.intersects(b)]
        with cf.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(fetch, jobs))
        stats = {}
        for (d, *_), r in zip(jobs, res):
            stats.setdefault(d, {}).setdefault(r.split(" ")[0], 0)
            stats[d][r.split(" ")[0]] += 1
        for (d, i, j, _), r in zip(jobs, res):
            if r.startswith(("rejected", "failed")):
                print(f"  {d} {i}_{j}: {r}")
        manifest["windows"][wid] = {d: stats[d] for d in dates}
        print(f"{wid}: {len(dates)} dates, {len(jobs)} chunks; {stats}")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
