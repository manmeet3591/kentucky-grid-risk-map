# Kentucky Grid Risk Map

An interactive screening map for Kentucky's power grid and lifeline facilities:

1. **Vegetation exposure of transmission lines.** Every line segment is scored by the tree canopy in a 90 m corridor around it, and by how tall those trees are: the 95th-percentile tree height and the share of the line within **fall-in reach** (trees taller than their distance to the line), from the Meta/WRI 1 m global canopy height map. Lines through dense, tall trees are more exposed to tree-contact outages in wind, ice and storms.
2. **Critical facilities in flood water.** Gas stations, hospitals, substations, water/wastewater plants and fuel terminals are checked against NASA OPERA DSWx-S1 Sentinel-1 water maps for the **February 2025** and **April 2025** Kentucky floods.

**Live dashboard:** https://manmeet3591.github.io/kentucky-grid-risk-map/ (once GitHub Pages is enabled)

Prototype by the Disaster Science Operations Center (DSOC), for Kentucky utilities (EKPC, LG&E/KU, Big Rivers, TVA distributors) and emergency managers.

## Headline numbers (build of 2026-10-28)

| | |
|---|---|
| Transmission lines scored | 20,247 km: 10,458 km HIFLD (138–765 kV) + 9,789 km OSM (69 kV) |
| Length-weighted mean canopy in corridor | 27.7 % |
| Line km in heavy canopy (≥ 40 %) | 6,286 km (31 %), concentrated in eastern Kentucky: Harlan, Pike, Bell, Floyd, Knox |
| Tree height (Meta/WRI 1 m, imagery 2015–2020) | length-weighted p95 tree height in corridor 16.3 m; 14 % of line length within fall-in reach of trees; 4,086 km (20 %) of segments ≥ 25 % fall-in. Most fall-in km: Harlan 132, Pike 127, Jefferson 85, Knox 74, Floyd 74. 1,272 km in heavy canopy has < 5 % fall-in (cleared right-of-way, trees out of reach) |
| Flood water seen (vs dry season) | Feb 2025: 968 km² on 4 passes · Apr 2025: 1,264 km² on 5 passes |
| Facilities flagged **flooded** | Feb: 23 · Apr: 21. Mostly water/wastewater plants and Ohio River fuel terminals (Owensboro, Henderson), plus one substation in Pike County. **No gas stations or hospitals** showed flood water on site at pass time. |
| Facilities **near** flood water (~110 m) | Feb: 102 (29 gas stations) · Apr: 91 (22 gas stations) |
| Not observed | Feb: 515 facilities, mostly in 19 eastern counties, had no pass with data, including much of the 15–16 Feb flash-flood area |

## Method

**Lines.** HIFLD Open *Electric Power Transmission Lines* (overhead, ≥ 69 kV). HIFLD has no 69 kV lines in Kentucky, so 69 kV lines come from OpenStreetMap (`power=line`, `voltage=69000`; ways shared with higher-voltage circuits are dropped). Lines are clipped to Kentucky, cut into ~1 km segments and buffered 45 m each side (flat ends).

**Canopy.** USFS/NLCD Tree Canopy Cover 2021 (v2021-4, 30 m), read from the MRLC WCS on its native EPSG:5070 grid. Segment score = mean % canopy of the corridor cells; we also store the share of cells ≥ 50 %. County values are length-weighted.

**Tree height.** Meta / World Resources Institute global canopy height map v1 (Tolan et al. 2024, *Remote Sensing of Environment* 300, 113888), 1 m, from Maxar imagery, on AWS Open Data (`s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/`, no login, CC BY 4.0). Tiles are zoom-9 quadkeys in EPSG:3857 (65,536² px, Byte metres). They are stored in full-width strips, not internal tiles, so `fetch_canopy_height.py` downloads each of the ~44 Kentucky tiles (~0.6 GB each, ~26 GB in total), keeps only the line corridors (±60 m) minus the product's cloud mask, and deletes the original. Per segment (45 m each side, as for canopy):
- `hp95`: 95th percentile height of tree pixels (≥ 3 m); 0 if < 100 m² of trees
- `fall`: share of the segment's length (10 m steps) where, on either side, ≥ 5 m² of trees are taller than their horizontal distance to the centreline
- `yr`: imagery year at the segment midpoint (from the product's acquisition-date map)

County values are km-weighted; `fall_km` = Σ length × fall share; `fall_hi_km` counts segments with fall ≥ 25 %.

**Flood water.** OPERA DSWx-S1 classes (not water / open water / inundated vegetation / HAND-masked / layover). For each event, every Sentinel-1 pass in the window is compared with 15 dry-season passes (15 Sep – 5 Oct 2024 and 25 Aug – 5 Oct 2025). *Flood water* = water on any event pass where no reference pass saw water.

**Facility status** (OSM points; grid cells ~25 × 31 m):
- *flooded*: ≥ 2 flood-water cells in the site window (3 × 3 cells; 5 × 5 for water plants and fuel terminals)
- *near*: flood water within ~110 m of the site window
- *normal water edge*: water at the site in the dry season too (not counted)
- *dry*: seen as not-water on at least one event pass
- *not observed*: no event pass covered the site

OSM fuel-storage tanks within 250 m are merged into one terminal site (130 features → 42 sites).

### How the OPERA data are read

By default the pipeline needs **no login**. It reads the DSWx-S1 water-class layer that NASA GIBS renders (WMS, EPSG:4326) at ~25 × 31 m, and inverts the fixed colour table back to product classes. Every 0.5° chunk is rejected unless > 99.9 % of its pixels are exact DSWx palette colours. (The GIBS WMTS tile endpoint was tried first and rejected: on some dates it returned tiles from other layers.)

For the original 30 m GeoTIFFs, use your own free NASA Earthdata login:

```bash
# put your credentials in ~/.netrc yourself (never commit it):
#   machine urs.earthdata.nasa.gov login <user> password <password>
chmod 600 ~/.netrc
python pipeline/fetch_opera_earthdata.py           # downloads B01_WTR, regrids to the same chunks
python pipeline/build_flood.py --source earthdata  # then rebuild counties
python pipeline/build_counties.py
```

## Rebuild

```bash
conda create -n ky-grid-risk -c conda-forge python=3.12 rasterio geopandas shapely pyproj requests numpy pillow earthaccess
conda activate ky-grid-risk
cd pipeline
python fetch_static.py      # counties (TIGERweb), HIFLD lines, OSM facilities + 69 kV lines (Overpass, a few min)
python fetch_canopy.py      # NLCD/USFS canopy tiles over Kentucky (~95 MB)
python fetch_canopy_height.py  # Meta/WRI 1 m canopy height, line corridors only (~26 GB streamed, 30-60 min)
python build_lines.py       # -> docs/data/lines.geojson, line_info.json (uses all CPU cores)
python fetch_flood.py       # OPERA DSWx-S1 via GIBS WMS (~20 MB)
python build_flood.py       # -> docs/data/facilities.geojson, flood_<event>.png
python build_counties.py    # -> docs/data/counties.geojson, meta.json
```

Flood events and reference windows live in `pipeline/events.json`; add an event there and rerun the last three steps. Preview: `python -m http.server -d docs 8000`.

## Limitations and data gaps

- **OPERA revisit gaps.** In early 2025 Sentinel-1A alone covered Kentucky, so any one spot was imaged only every few days. In the February window **no pass with data covered 19 eastern counties** (Mason/Lewis/Fleming south to Breathitt, Perry, Knott, Leslie, Letcher, Harlan), the core of the 15–16 Feb flash floods. Pike County was imaged only on 17 and 19 Feb, after the peak. Short flash floods in narrow valleys are largely invisible to this method. "Not observed" is not "dry".
- **Radar confusions.** Smooth surfaces (parking lots, flat roofs, tank tops) and wet snow (19–20 Feb) can look like water. Flooding under trees or among buildings is often missed. Treatment-plant basins and lagoons that the dry-season passes missed can show as flood water. Flagged sites should be checked against local reports.
- **GIBS rendering.** The default path reads a rendered product, not the original GeoTIFFs. The palette check guards against wrong layers. The Earthdata path above is the authoritative version. Several 2025 dry-season dates that CMR lists returned no data from GIBS.
- **OSM coverage.** Facility mapping is incomplete and uneven: rural gas stations, small substations and water plants are often missing, and points are centroids. Utility, KYEM and KY Division of Water asset lists would improve this.
- **Line data.** HIFLD geometry is generalised and partly dated, and has no 69 kV lines in Kentucky. OSM 69 kV coverage is incomplete. Distribution lines, where most tree-caused outages happen, are not included.
- **Canopy vintage.** 2021 canopy predates recent right-of-way clearing and storm damage. 30 m cells blur cleared-corridor edges. Species and health aren't in the data.
- **Tree height.** The Meta/WRI map is a model estimate from single satellite images (MAE roughly 3 m against airborne lidar in the paper; tall closed forest tends to be underestimated, crown and clearing edges are blurred). Imagery behind Kentucky's corridors is mostly 2015–2020, older than the canopy data. Fall-in is geometric only: it measures to the centreline and ignores conductor height and sag, phase spacing, slope, lean and wind. Line geometry errors of tens of metres move trees in and out of reach. Use it as a relative ranking.
- Screening tool only, not an engineering or vegetation-management assessment.

## Data and licences

- HIFLD Open transmission lines (public domain); OpenStreetMap contributors (ODbL) for 69 kV lines and facilities.
- USFS / NLCD Tree Canopy Cover 2021 via MRLC (public domain).
- NASA/JPL OPERA DSWx-S1 (PO.DAAC `OPERA_L3_DSWX-S1_V1`) via NASA GIBS; contains modified Copernicus Sentinel-1 data.
- US Census TIGERweb county boundaries. Basemap © Esri.
