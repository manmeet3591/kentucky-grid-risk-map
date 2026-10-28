"""Meta / WRI global 1 m canopy height (Tolan et al. 2024) cut to the Kentucky line corridors.

Source: AWS Open Data, s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/ (no login).
  chm/<quadkey>.tif       zoom-9 quadkey tiles, 65536 x 65536 px, EPSG:3857 (~1.19 m, ~0.95 m on the ground
                          in Kentucky), Byte, canopy height in m. Stored in full-width strips (65536 x 1 blocks),
                          not internal tiles, so a window read still pulls whole rows: we download each tile
                          (~0.6 GB), keep only the corridors and delete it.
  msk/<quadkey>.tif.msk   cloud mask of the source imagery, 255 = cloud (small patches), 0 = clear;
                          missing for some tiles, which are then used unmasked
  CHM_acquisition_date.tif  global 0.01 deg map of the imagery date (decimal years since 2000)

Corridors are every scored line (same set as build_lines.py) clipped to Kentucky and buffered MARGIN m.
Output: raw/canopy_height/<quadkey>.tif (Byte m, 255 = outside corridor or cloud), raw/canopy_height/chm.vrt,
        raw/canopy_height/acq_date.tif (Kentucky window of the acquisition-date map).
About 26 GB is downloaded in total; what is kept is a few hundred MB.
"""
import json
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import requests
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_lines import load_lines, pieces  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "raw" / "canopy_height"
TMP = OUT / "tmp"
S3 = "https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float"
MARGIN = 60   # m each side; build_lines uses 45 m, the rest absorbs pixel alignment
ROWS = 2048   # rows processed at a time
WORKERS = 3


def corridors():
    to5070 = Transformer.from_crs(4326, 5070, always_xy=True).transform
    to3857 = Transformer.from_crs(5070, 3857, always_xy=True).transform
    cty = json.loads((ROOT / "raw" / "counties.geojson").read_text())["features"]
    ky = unary_union([transform(to5070, shape(c["geometry"])) for c in cty])
    out = []
    for ln in load_lines():
        for part in pieces(transform(to5070, ln["geom"]).intersection(ky)):
            if part.length >= 50:
                out.append(transform(to3857, part.buffer(MARGIN)))
    return out


def get(url, dst, optional=False):
    with requests.get(url, stream=True, timeout=600) as r:
        if optional and r.status_code == 404:
            return False
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)
    return True


def do_tile(qk, geoms):
    dst = OUT / f"{qk}.tif"
    if dst.exists():
        return qk, "cached"
    chm, msk = TMP / f"{qk}.tif", TMP / f"{qk}.tif.msk"
    get(f"{S3}/chm/{qk}.tif", chm)
    has_msk = get(f"{S3}/msk/{qk}.tif.msk", msk, optional=True)
    kept = 0
    with rasterio.open(chm) as src:
        m = rasterio.open(msk) if has_msk else None
        prof = src.profile | {"compress": "deflate", "predictor": 2, "tiled": True, "blockxsize": 512,
                              "blockysize": 512, "nodata": 255, "sparse_ok": True, "BIGTIFF": "IF_SAFER"}
        tree = STRtree(geoms)
        part = dst.with_suffix(".part.tif")
        with rasterio.open(part, "w", **prof) as out:
            for r0 in range(0, src.height, ROWS):
                w = Window(0, r0, src.width, min(ROWS, src.height - r0))
                wt = src.window_transform(w)
                bb = box(*rasterio.windows.bounds(w, src.transform))
                hit = [geoms[i] for i in tree.query(bb, predicate="intersects")]
                if not hit:
                    continue
                inside = ~geometry_mask(hit, out_shape=(w.height, w.width), transform=wt)
                # only the column range the corridors touch, to keep the writes sparse
                cols = np.nonzero(inside.any(axis=0))[0]
                c0, c1 = int(cols[0]), int(cols[-1]) + 1
                sub = Window(c0, r0, c1 - c0, w.height)
                a = src.read(1, window=sub)
                ok = inside[:, c0:c1]
                if m is not None:
                    ok &= m.read(1, window=sub) != 255
                a[~ok] = 255
                a[(a == 255) & ok] = 254  # keep 255 free for nodata (heights never reach this)
                out.write(a, 1, window=sub)
                kept += int(ok.sum())
        if m is not None:
            m.close()
        part.rename(dst)
    chm.unlink()
    msk.unlink(missing_ok=True)
    return qk, f"{kept / 1e6:.0f} M px" + ("" if has_msk else " (no cloud mask)")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(exist_ok=True)
    geoms = corridors()
    allc = unary_union(geoms)
    tiles_p = OUT / "tiles.geojson"
    if not tiles_p.exists():
        get(f"{S3}/tiles.geojson", tiles_p)
    to3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform
    todo = []
    for f in json.loads(tiles_p.read_text())["features"]:
        t = transform(to3857, shape(f["geometry"]))
        if t.intersects(allc):
            todo.append((f["properties"]["tile"], [g for g in geoms if g.intersects(t)]))
    print(f"{len(geoms)} corridor pieces, {len(todo)} tiles")
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (qk, msg) in enumerate(ex.map(lambda t: do_tile(*t), todo), 1):
            print(f"  {i}/{len(todo)} {qk}: {msg}", flush=True)
    subprocess.run([str(pathlib.Path(sys.executable).parent / "gdalbuildvrt"), "-q", "-srcnodata", "255",
                    str(OUT / "chm.vrt")] + [str(OUT / f"{qk}.tif") for qk, _ in todo], check=True)
    # imagery date, Kentucky window of the global 0.01 deg map
    with rasterio.open(f"/vsicurl/{S3}/CHM_acquisition_date.tif") as s:
        w = from_bounds(-89.7, 36.4, -81.8, 39.3, s.transform).round_offsets().round_lengths()
        a = s.read(1, window=w)
        with rasterio.open(OUT / "acq_date.tif", "w", **(s.profile | {
                "width": w.width, "height": w.height, "transform": s.window_transform(w), "tiled": False,
                "compress": "deflate", "blockysize": 1})) as d:
            d.write(a, 1)
    print("done ->", OUT / "chm.vrt")


if __name__ == "__main__":
    main()
