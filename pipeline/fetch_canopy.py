"""USFS / NLCD Tree Canopy Cover (CONUS, 30 m, 2021 v2021-4) over Kentucky from the MRLC WCS.

Tiles are requested on the native EPSG:5070 grid (no resampling), stored as compressed GeoTIFFs and
wrapped in a VRT. Only tiles touching Kentucky (+5 km) are fetched. Values: 0-100 % canopy; 254/255 = no data.
"""
import json
import pathlib
import subprocess
import sys
import tempfile

import requests
import rasterio
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "raw" / "canopy"
WCS = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
COVERAGE = "nlcd_tcc_conus_2021_v2021-4"
X0, Y0 = -2493045.0, 177285.0  # coverage lower-left corner; tiles snap to its 30 m grid
TILE = 60000  # m (2000 x 2000 px)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cty = json.loads((ROOT / "raw" / "counties.geojson").read_text())
    to5070 = Transformer.from_crs(4326, 5070, always_xy=True).transform
    ky = transform(to5070, unary_union([shape(f["geometry"]) for f in cty["features"]])).buffer(5000)
    minx, miny, maxx, maxy = ky.bounds
    i0, i1 = int((minx - X0) // TILE), int((maxx - X0) // TILE)
    j0, j1 = int((miny - Y0) // TILE), int((maxy - Y0) // TILE)
    tiles = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            x, y = X0 + i * TILE, Y0 + j * TILE
            if not ky.intersects(box(x, y, x + TILE, y + TILE)):
                continue
            dst = OUT / f"tcc2021_{i}_{j}.tif"
            tiles.append(dst)
            if dst.exists():
                continue
            url = (f"{WCS}?service=WCS&version=2.0.1&request=GetCoverage&coverageId={COVERAGE}"
                   f"&subset=X({x},{x + TILE})&subset=Y({y},{y + TILE})&format=image/geotiff")
            r = requests.get(url, timeout=300)
            r.raise_for_status()
            if not r.content.startswith((b"II", b"MM")):
                raise RuntimeError(f"not a GeoTIFF for tile {i},{j}: {r.content[:200]!r}")
            with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
                tmp.write(r.content)
                tmp.flush()
                with rasterio.open(tmp.name) as src:
                    prof = src.profile | {"compress": "deflate", "tiled": True, "blockxsize": 512,
                                          "blockysize": 512, "nodata": 255, "photometric": "MINISBLACK"}
                    prof.pop("photometric", None)
                    a = src.read(1)
                    with rasterio.open(dst, "w", **prof) as out:
                        out.write(a, 1)
            print(f"  tile {i},{j} ok ({len(tiles)})")
    vrt = OUT / "tcc2021.vrt"
    subprocess.run([str(pathlib.Path(sys.executable).parent / "gdalbuildvrt"), "-q", str(vrt)] + [str(t) for t in tiles], check=True)
    print(f"canopy tiles: {len(tiles)} -> {vrt}")


if __name__ == "__main__":
    main()
