"""Flood status of critical facilities and event flood-extent overlays from OPERA DSWx-S1 classes.

For each event, every pass inside the event window is compared with the dry-season reference passes:

  event water   = open water or inundated vegetation on any event pass
  reference water = open water or inundated vegetation on any reference pass
  new water     = event water where the reference never saw water  (flood water)

Facility status per event, from the ~25 x 31 m grid cells around the OSM point:
  flooded      >= 2 new-water cells in the site window around the point: 3x3 cells (~75 x 95 m) for gas
               stations, hospitals and substations; 5x5 (~125 x 155 m) for water plants and fuel terminals
  near         not flooded, but new water within 4 cells (~110 m) of the site window
  water_edge   water at the site in the reference as well (river or pond edge); not counted as flooded
  dry          observed as not-water on at least one event pass
  masked       only HAND-masked (high above drainage) or layover/shadow pixels at the site
  unobserved   no event pass covered the site

Inputs: raw/opera[_earthdata]/<date>/<i>_<j>.tif, raw/facilities.geojson, raw/counties.geojson, pipeline/events.json
Outputs: docs/data/facilities.geojson, docs/data/flood_<event>.png (+ bounds in work/flood.json), work/flood_county.json
"""
import argparse
import json
import math
import pathlib
import warnings

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from fetch_flood import CHUNK, NORTH, NPX, RES, WEST, chunks

warnings.filterwarnings("ignore", category=DeprecationWarning)
ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT, WORK = ROOT / "docs" / "data", ROOT / "work"
DS = 4  # overlay downsampling (max-pool), ~100-120 m cells
NEAR = 4
MIN_CELLS = 2  # a single wet cell is too often radar speckle or a dark parking lot
SITE_K = {"water_plant": 2, "fuel_terminal": 2}  # half-width of the site window in cells (default 1)
OVERLAY_MIN = 3  # new-water cells (of DS*DS) for an overlay cell to show as flood water
CLUSTER_M = 250  # OSM fuel terminals are often mapped tank by tank; merge points within this distance
STATUS = ["flooded", "near", "water_edge", "dry", "masked", "unobserved"]
# overlay classes -> RGBA
OVERLAY = {0: (0, 0, 0, 0), 1: (120, 120, 120, 70), 2: (125, 150, 180, 170), 3: (11, 99, 206, 235)}


def load(folder, dates, i, j):
    out = []
    for d in dates:
        f = folder / d / f"{i}_{j}.tif"
        if f.exists():
            with rasterio.open(f) as s:
                out.append((d, s.read(1)))
    return out


def window(a, r, c, k):
    return a[max(r - k, 0): r + k + 1, max(c - k, 0): c + k + 1]


def cluster_terminals(fac):
    """Merge fuel-terminal points (often one per storage tank) within CLUSTER_M into one site."""
    other = [f for f in fac if f["properties"]["cat"] != "fuel_terminal"]
    pts = [f for f in fac if f["properties"]["cat"] == "fuel_terminal"]
    groups = []
    for f in pts:
        lon, lat = f["geometry"]["coordinates"]
        for g in groups:
            glon, glat = g["c"]
            dx = (lon - glon) * 111320 * math.cos(math.radians(lat))
            if math.hypot(dx, (lat - glat) * 110540) < CLUSTER_M:
                g["m"].append(f)
                n = len(g["m"])
                g["c"] = (glon + (lon - glon) / n, glat + (lat - glat) / n)
                break
        else:
            groups.append({"c": (lon, lat), "m": [f]})
    merged = []
    for g in groups:
        named = [m for m in g["m"] if m["properties"]["name"]]
        p = dict((named or g["m"])[0]["properties"])
        p["n"] = len(g["m"])
        merged.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(g["c"])}, "properties": p})
    print(f"fuel terminals: {len(pts)} OSM features -> {len(merged)} sites")
    return other + merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["gibs", "earthdata"], default="gibs")
    args = ap.parse_args()
    folder = ROOT / "raw" / ("opera_earthdata" if args.source == "earthdata" else "opera")
    WORK.mkdir(exist_ok=True)
    ev = json.loads((ROOT / "pipeline" / "events.json").read_text())
    events = ev["events"]
    have = sorted(p.name for p in folder.iterdir() if p.is_dir())
    in_win = lambda w: [d for d in have if w["start"] <= d <= w["end"]]
    ref_dates = [d for r in ev["reference"] for d in in_win(r)]
    ev_dates = {e["id"]: in_win(e) for e in events}
    print("reference:", ref_dates)
    print("events:", ev_dates)

    cty = json.loads((ROOT / "raw" / "counties.geojson").read_text())["features"]
    names = [c["properties"]["name"] for c in cty]
    cgeo = [shape(c["geometry"]) for c in cty]
    ky = unary_union(cgeo)
    tree = STRtree(cgeo)
    grid = chunks(ky.buffer(0.01))

    fac = cluster_terminals(json.loads((ROOT / "raw" / "facilities.geojson").read_text())["features"])
    for f in fac:
        p = Point(f["geometry"]["coordinates"])
        hits = [names[k] for k in tree.query(p, predicate="intersects")]
        f["properties"]["county"] = hits[0] if hits else None
    fac = [f for f in fac if f["properties"]["county"]]
    for f in fac:
        f["properties"]["st"], f["properties"]["wd"] = {}, {}

    ni = int(math.ceil((max(i for i, _, _ in grid) + 1)))
    nj = int(math.ceil((max(j for _, j, _ in grid) + 1)))
    m = NPX // DS
    over = {e["id"]: np.zeros((nj * m, ni * m), np.uint8) for e in events}
    county_km2 = {e["id"]: np.zeros(len(cty)) for e in events}
    county_obs = {e["id"]: np.zeros(len(cty)) for e in events}
    county_px = np.zeros(len(cty))

    for i, j, b in grid:
        w0, n0 = WEST + i * CHUNK, NORTH - j * CHUNK
        tr = from_origin(w0, n0, RES, RES)
        lat = n0 - CHUNK / 2
        px_km2 = (RES * 111.32) ** 2 * math.cos(math.radians(lat))
        cid = rasterize([(g, k + 1) for k, g in enumerate(cgeo) if g.intersects(b)], out_shape=(NPX, NPX),
                        transform=tr, fill=0, dtype="int16")
        county_px += np.bincount(cid.ravel(), minlength=len(cty) + 1)[1:] * px_km2
        ref = load(folder, ref_dates, i, j)
        ref_w = np.zeros((NPX, NPX), bool)
        for _, a in ref:
            ref_w |= (a == 1) | (a == 3)
        pts = [(f, *~tr * tuple(f["geometry"]["coordinates"])) for f in fac
               if b.contains(Point(f["geometry"]["coordinates"]))]
        for e in events:
            eid = e["id"]
            passes = load(folder, ev_dates[eid], i, j)
            ev_w = np.zeros((NPX, NPX), bool)
            ev_obs = np.zeros((NPX, NPX), bool)
            ev_mask = np.zeros((NPX, NPX), bool)
            first = {}
            for d, a in passes:
                wtr = (a == 1) | (a == 3)
                ev_w |= wtr
                ev_obs |= a <= 3
                ev_mask |= (a == 250) | (a == 251)
            new = ev_w & ~ref_w
            # per-county flood area and observed share
            county_km2[eid] += np.bincount(cid[new].ravel(), minlength=len(cty) + 1)[1:] * px_km2
            county_obs[eid] += np.bincount(cid[ev_obs].ravel(), minlength=len(cty) + 1)[1:] * px_km2
            # overlay: max-pool classes
            cls = np.zeros((NPX, NPX), np.uint8)
            cls[ev_obs | ev_mask] = 0
            cls[~(ev_obs | ev_mask)] = 1
            cls[ev_w & ref_w] = 2
            pooled = cls[: m * DS, : m * DS].reshape(m, DS, m, DS).max(axis=(1, 3))
            n_new = new[: m * DS, : m * DS].reshape(m, DS, m, DS).sum(axis=(1, 3))
            pooled[n_new >= OVERLAY_MIN] = 3
            over[eid][j * m:(j + 1) * m, i * m:(i + 1) * m] = pooled
            for f, cx, cy in pts:
                c, r = int(cx), int(cy)
                p = f["properties"]
                k = SITE_K.get(p["cat"], 1)
                if window(new, r, c, k).sum() >= MIN_CELLS:
                    st = "flooded"
                    for d, a in passes:  # first pass that saw new water at the site
                        if (window((a == 1) | (a == 3), r, c, k) & window(new, r, c, k)).sum() >= MIN_CELLS:
                            p["wd"][eid] = d
                            break
                elif window(ev_w & ref_w, r, c, k).sum() >= MIN_CELLS:
                    st = "water_edge"
                elif window(new, r, c, k + NEAR).sum() >= MIN_CELLS:
                    st = "near"
                elif window(ev_obs, r, c, 0).any():
                    st = "dry"
                elif window(ev_mask, r, c, 0).any():
                    st = "masked"
                else:
                    st = "unobserved"
                p["st"][eid] = st
        print(f"  chunk {i}_{j}: {len(pts)} facilities")

    # overlays -> EPSG:3857 PNG
    flood_meta = {}
    src_t = from_origin(WEST, NORTH, RES * DS, RES * DS)
    H, W = over[events[0]["id"]].shape
    bounds = (WEST, NORTH - H * RES * DS, WEST + W * RES * DS, NORTH)
    dst_t, dw, dh = calculate_default_transform("EPSG:4326", "EPSG:3857", W, H, *bounds)
    # clip overlays to Kentucky (+~3 km so river flooding along the border stays visible)
    inside = rasterize([(ky.buffer(0.03), 1)], out_shape=(H, W), transform=src_t, fill=0, dtype="uint8")
    for e in events:
        eid = e["id"]
        over[eid][inside == 0] = 0
        dst = np.zeros((dh, dw), np.uint8)
        reproject(over[eid], dst, src_transform=src_t, src_crs="EPSG:4326", dst_transform=dst_t,
                  dst_crs="EPSG:3857", resampling=Resampling.nearest)
        rgba = np.zeros((dh, dw, 4), np.uint8)
        for k, col in OVERLAY.items():
            rgba[dst == k] = col
        Image.fromarray(rgba, "RGBA").save(OUT / f"flood_{eid}.png", optimize=True)
        mb = (dst_t.c, dst_t.f + dh * dst_t.e, dst_t.c + dw * dst_t.a, dst_t.f)
        w, s, ea, n = transform_bounds("EPSG:3857", "EPSG:4326", *mb)
        flood_meta[eid] = {"bounds": [[s, w], [n, ea]], "ref_dates": ref_dates, "dates": ev_dates[eid],
                           "new_water_km2": round(float(county_km2[eid].sum()), 1)}

    # facilities output
    feats = []
    for f in fac:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                      "properties": {"cat": p["cat"], "name": p["name"], "n": p.get("n", 1), "op": p["operator"], "kv": p["voltage"],
                                     "osm": p["osm"], "county": p["county"], "st": p["st"], "wd": p["wd"]}})
    (OUT / "facilities.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                       separators=(",", ":")))
    per_county = {}
    for e in events:
        eid = e["id"]
        for k, nm in enumerate(names):
            per_county.setdefault(nm, {})[eid] = {
                "new_water_km2": round(float(county_km2[eid][k]), 2),
                "observed_share": round(float(county_obs[eid][k] / county_px[k]), 3) if county_px[k] else 0,
            }
    (WORK / "flood_county.json").write_text(json.dumps(per_county))
    (WORK / "flood.json").write_text(json.dumps({"source": args.source, "events": flood_meta}))
    for e in events:
        eid = e["id"]
        cnt = {}
        for f in feats:
            s = f["properties"]["st"].get(eid, "unobserved")
            cnt.setdefault(f["properties"]["cat"], {}).setdefault(s, 0)
            cnt[f["properties"]["cat"]][s] += 1
        print(eid, f"new water {flood_meta[eid]['new_water_km2']} km2", json.dumps(cnt))


if __name__ == "__main__":
    main()
