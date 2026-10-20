"""Original 30 m OPERA DSWx-S1 B01_WTR GeoTIFFs from PO.DAAC (needs a free NASA Earthdata login).

Credentials are read by `earthaccess` from ~/.netrc (machine urs.earthdata.nasa.gov login <user> password <pw>)
or the EARTHDATA_USERNAME / EARTHDATA_PASSWORD environment variables. This script never asks for or stores them.

The granules are warped (nearest neighbour) onto the same lon/lat chunk grid as fetch_flood.py and written
to raw/opera_earthdata/<date>/<i>_<j>.tif. build_flood.py uses this folder instead of the GIBS one when present:

    python pipeline/fetch_opera_earthdata.py
    python pipeline/build_flood.py --source earthdata

Where granules of the same date overlap, water beats not-water beats masked beats no-data.
"""
import json
import pathlib

import earthaccess
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import box, shape
from shapely.ops import unary_union

from fetch_flood import CHUNK, NORTH, NPX, RES, SOUTH, WEST, EAST, chunks

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIF = ROOT / "raw" / "opera_tif"
OUT = ROOT / "raw" / "opera_earthdata"
RANK = np.zeros(256, np.uint8)  # merge priority per class value
RANK[[255, 250, 251, 0, 1, 3]] = [0, 1, 1, 2, 3, 3]


def main():
    earthaccess.login(strategy="netrc") or earthaccess.login(strategy="environment")
    ev = json.loads((ROOT / "pipeline" / "events.json").read_text())
    cty = json.loads((ROOT / "raw" / "counties.geojson").read_text())["features"]
    grid = chunks(unary_union([shape(f["geometry"]) for f in cty]).buffer(0.01))
    windows = ev["reference"] + ev["events"]
    for w in windows:
        res = earthaccess.search_data(short_name="OPERA_L3_DSWX-S1_V1", bounding_box=(WEST, SOUTH, EAST, NORTH),
                                      temporal=(w["start"], w["end"] + "T23:59:59"))
        urls = [u for g in res for u in g.data_links() if u.endswith("_B01_WTR.tif")]
        TIF.mkdir(parents=True, exist_ok=True)
        todo = [u for u in urls if not (TIF / u.rsplit("/", 1)[1]).exists()]
        print(f"{w['start']}..{w['end']}: {len(urls)} granules, {len(todo)} to download")
        if todo:
            earthaccess.download(todo, str(TIF))
        by_date = {}
        for u in urls:
            name = u.rsplit("/", 1)[1]
            t = name.split("_")[4]  # e.g. 20250406T234025Z
            by_date.setdefault(f"{t[:4]}-{t[4:6]}-{t[6:8]}", []).append(TIF / name)
        for date, files in sorted(by_date.items()):
            srcs = [rasterio.open(f) for f in files]
            for i, j, b in grid:
                w0, n0 = WEST + i * CHUNK, NORTH - j * CHUNK
                dst_t = from_origin(w0, n0, RES, RES)
                best = np.full((NPX, NPX), 255, np.uint8)
                for s in srcs:
                    tmp = np.full((NPX, NPX), 255, np.uint8)
                    reproject(rasterio.band(s, 1), tmp, dst_transform=dst_t, dst_crs="EPSG:4326",
                              resampling=Resampling.nearest, dst_nodata=255)
                    best = np.where(RANK[tmp] > RANK[best], tmp, best)
                if (best == 255).all():
                    continue
                dst = OUT / date / f"{i}_{j}.tif"
                dst.parent.mkdir(parents=True, exist_ok=True)
                with rasterio.open(dst, "w", driver="GTiff", width=NPX, height=NPX, count=1, dtype="uint8",
                                   crs="EPSG:4326", transform=dst_t, nodata=255, compress="deflate",
                                   tiled=True) as out:
                    out.write(best, 1)
            for s in srcs:
                s.close()
            print(f"  {date}: {len(files)} granules gridded")


if __name__ == "__main__":
    main()
