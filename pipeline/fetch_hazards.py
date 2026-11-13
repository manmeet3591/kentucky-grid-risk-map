"""Download daily NASA POWER (MERRA-2 based, ~0.5 x 0.625 deg) grids over Kentucky, 2001-2024.

Same approach as the DSOC Kenya prototype: the regional daily endpoint, one request per parameter-year,
saved as slim JSON in raw/power/<PARAM>_<year>.json.
"""
import concurrent.futures as cf
import json
import pathlib
import time

import requests

RAW = pathlib.Path(__file__).resolve().parent.parent / "raw" / "power"
PARAMS = ["WS10M_MAX", "PRECTOTCORR", "T2M_MAX", "T2M_MIN"]
BBOX = dict(lonmin=-89.75, lonmax=-81.75, latmin=36.25, latmax=39.25)  # regional API max 10 x 10 deg
YEARS = range(2001, 2025)
UA = {"User-Agent": "kentucky-grid-risk-map/0.1 (research; github.com/manmeet3591)"}


def fetch(job):
    param, year = job
    out = RAW / f"{param}_{year}.json"
    if out.exists():
        return
    url = ("https://power.larc.nasa.gov/api/temporal/daily/regional"
           f"?parameters={param}&community=RE&longitude-min={BBOX['lonmin']}&longitude-max={BBOX['lonmax']}"
           f"&latitude-min={BBOX['latmin']}&latitude-max={BBOX['latmax']}&start={year}0101&end={year}1231&format=JSON")
    for i in range(5):
        try:
            r = requests.get(url, timeout=900, headers=UA)
            if r.status_code == 200:
                data = r.json()
                slim = [{"lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1],
                         "v": list(f["properties"]["parameter"][param].values())} for f in data["features"]]
                out.write_text(json.dumps(slim))
                print(f"{param} {year}: {len(slim)} cells", flush=True)
                return
            print(f"{param} {year}: HTTP {r.status_code}", flush=True)
        except requests.RequestException as e:
            print(f"{param} {year}: {e}", flush=True)
        time.sleep(20 * (i + 1))
    raise RuntimeError(f"failed {param} {year}")


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    jobs = [(p, y) for p in PARAMS for y in YEARS]
    with cf.ThreadPoolExecutor(3) as ex:
        list(ex.map(fetch, jobs))


if __name__ == "__main__":
    main()
