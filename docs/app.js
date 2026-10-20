/* Kentucky Grid Risk Map */
(() => {
  const CATS = {
    gas_station: { label: "Gas stations", g: "G" },
    hospital: { label: "Hospitals", g: "H" },
    substation: { label: "Substations", g: "S" },
    water_plant: { label: "Water / wastewater plants", g: "W" },
    fuel_terminal: { label: "Fuel terminals", g: "F" },
  };
  const STATUS = {
    flooded: { label: "Flooded", r: 7, v: "--st-flooded" },
    near: { label: "Near flood water (~110 m)", r: 5, v: "--st-near" },
    water_edge: { label: "Normal water edge", r: 4.5, v: "--st-edge" },
    dry: { label: "Dry on the passes", r: 2.5, v: "--st-dry", plain: true },
    masked: { label: "Not observed", r: 3, v: "--st-unobs", hollow: true },
    unobserved: { label: "Not observed", r: 3, v: "--st-unobs", hollow: true },
  };
  const CANOPY_BREAKS = [10, 20, 30, 45, 60];          // % canopy in the corridor
  const FLOOD_BREAKS = [1, 3, 10, 30, 80];           // km² of flood water per county
  const HEAVY = 40;
  const MODES = {
    canopy: "Length-weighted mean tree canopy in a 90 m corridor along transmission lines.",
    flood: "Flood water (km²) seen by Sentinel-1 during the event that was dry in the reference seasons.",
    none: "County outlines only.",
  };

  const state = {
    event: "2025-04", mode: "canopy", lines: true, kvHigh: true, kv69: true, flood: true,
    cats: Object.fromEntries(Object.keys(CATS).map(k => [k, true])), onlyWet: false, selected: null,
  };

  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const cls = (v, br) => { let i = 0; while (i < br.length && v >= br[i]) i++; return i; };
  const green = v => (v == null ? css("--nodata") : css(`--g${cls(v, CANOPY_BREAKS)}`));
  const blue = v => (v == null ? css("--nodata") : css(`--b${cls(v, FLOOD_BREAKS)}`));
  const fmt = (v, d = 0) => (v == null ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const day = d => new Date(d + "T12:00:00Z").toLocaleDateString("en-US", { day: "numeric", month: "short", timeZone: "UTC" });
  const dayY = d => new Date(d + "T12:00:00Z").toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

  let D, map, countyLayer, lineCase, lineLayer, floodLayer, facLayer, lineRenderer, facRenderer;
  const ev = () => D.meta.events.find(e => e.id === state.event);
  const lineOn = p => (p.kv >= 100 ? state.kvHigh : state.kv69);
  const facStatus = p => p.st[state.event] || "unobserved";
  const isWet = s => s === "flooded" || s === "near";

  // ---------- map ----------
  function initMap() {
    map = L.map("map", { preferCanvas: true, zoomSnap: 0.25 }).setView([37.8, -85.7], 7);
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const esri = s => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${s}/MapServer/tile/{z}/{y}/{x}`;
    L.tileLayer(esri(dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base"), {
      attribution: "Basemap &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", maxZoom: 16,
    }).addTo(map);
    for (const [name, z] of [["flood", 405], ["lines", 420], ["fac", 430], ["labels", 450]]) {
      map.createPane(name).style.zIndex = z;
    }
    map.getPane("labels").style.pointerEvents = "none";
    map.getPane("flood").style.pointerEvents = "none";
    L.tileLayer(esri(dark ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference"), { pane: "labels", maxZoom: 16 }).addTo(map);
    lineRenderer = L.canvas({ pane: "lines", tolerance: 4 });
    facRenderer = L.canvas({ pane: "fac", tolerance: 3 });

    countyLayer = L.geoJSON(D.counties, {
      style: styleCounty,
      onEachFeature: (f, l) => {
        l.on({
          mouseover: () => l.setStyle({ weight: 2.5, color: css("--ink") }),
          mouseout: () => countyLayer.resetStyle(l),
          click: () => selectCounty(f.properties.name),
        });
        l.bindTooltip(() => countyTip(f.properties), { className: "tt", sticky: true, direction: "top", offset: [0, -8] });
      },
    }).addTo(map);
    map.fitBounds(countyLayer.getBounds(), { padding: [8, 8] });

    const weight = kv => (kv >= 300 ? 2.8 : kv >= 100 ? 1.9 : 1.2);
    lineCase = L.geoJSON(D.lines, {
      renderer: lineRenderer, interactive: false, filter: f => lineOn(f.properties),
      style: f => ({ color: css("--surface"), weight: weight(f.properties.kv) + 2, opacity: 0.9, lineCap: "butt" }),
    });
    lineLayer = L.geoJSON(D.lines, {
      renderer: lineRenderer, filter: f => lineOn(f.properties),
      style: f => ({ color: green(f.properties.canopy), weight: weight(f.properties.kv), opacity: 1, lineCap: "butt" }),
      onEachFeature: (f, l) => l.bindTooltip(() => lineTip(f.properties), { className: "tt", sticky: true }),
    });
    facLayer = L.layerGroup().addTo(map);
  }

  function styleCounty(f) {
    const p = f.properties, sel = state.selected === p.name;
    let fill = "transparent", op = 0;
    if (state.mode === "canopy") { fill = green(p.canopy); op = 0.42; }
    if (state.mode === "flood") { fill = blue(p.flood[state.event].new_water_km2); op = 0.6; }
    return { fillColor: fill, fillOpacity: op, color: sel ? css("--ink") : css("--axis"), weight: sel ? 3 : 0.8 };
  }

  function drawLines() {
    for (const l of [lineCase, lineLayer]) {
      l.clearLayers();
      if (state.lines) { l.addData(D.lines); if (!map.hasLayer(l)) l.addTo(map); }
      else map.removeLayer(l);
    }
  }

  function drawFlood() {
    if (floodLayer) map.removeLayer(floodLayer);
    floodLayer = null;
    if (!state.flood) return;
    floodLayer = L.imageOverlay(`data/flood_${state.event}.png`, ev().bounds, { pane: "flood", opacity: 0.9, interactive: false }).addTo(map);
  }

  function drawFacilities() {
    facLayer.clearLayers();
    const order = ["unobserved", "masked", "dry", "water_edge", "near", "flooded"];
    const fs = D.fac.features
      .filter(f => state.cats[f.properties.cat])
      .filter(f => !state.onlyWet || isWet(facStatus(f.properties)))
      .sort((a, b) => order.indexOf(facStatus(a.properties)) - order.indexOf(facStatus(b.properties)));
    for (const f of fs) {
      const p = f.properties, st = STATUS[facStatus(p)], [lon, lat] = f.geometry.coordinates;
      const c = css(st.v);
      const m = L.circleMarker([lat, lon], {
        renderer: facRenderer, radius: st.r, color: st.hollow ? c : css("--surface"), weight: st.plain ? 0 : 1.5,
        fillColor: c, fillOpacity: st.hollow ? 0 : 1,
      });
      m.bindTooltip(() => facTip(p), { className: "tt", direction: "top", offset: [0, -6] });
      m.on("click", () => selectCounty(p.county, false));
      facLayer.addLayer(m);
    }
  }

  // ---------- tooltips ----------
  function countyTip(p) {
    const fl = p.flood[state.event];
    return `<b>${esc(p.name)} County</b>
      <div class="m">Lines: ${fmt(p.line_km)} km · mean canopy ${fmt(p.canopy)} %</div>
      <div class="m">In heavy canopy (≥ ${HEAVY} %): ${fmt(p.hi_km)} km</div>
      <div class="m">${esc(ev().label)}: ${fmt(fl.new_water_km2, 1)} km² flood water · ${fmt(fl.flooded || 0)} flooded, ${fmt(fl.near || 0)} near</div>
      ${fl.observed_share < 0.5 ? `<div class="m">⚠ only ${fmt(fl.observed_share * 100)} % of the county was observed</div>` : ""}`;
  }
  function lineTip(p) {
    const info = D.info[p.l] || {};
    return `<b>${p.kv} kV line segment</b>
      <div class="m">Canopy in corridor: <b style="display:inline">${fmt(p.canopy)} %</b> · ${fmt(p.dense * 100)} % of cells ≥ 50 %</div>
      ${info.owner ? `<div class="m">Owner: ${esc(info.owner)}</div>` : ""}
      ${info.sub ? `<div class="m">${esc(info.sub)}</div>` : ""}
      <div class="m">${esc(p.county)} County · ${info.src === "OSM" ? "OpenStreetMap" : "HIFLD"} ${esc(info.id || "")}</div>`;
  }
  function facTip(p) {
    const s = facStatus(p), st = STATUS[s];
    const when = p.wd[state.event] ? ` (first seen ${day(p.wd[state.event])})` : "";
    return `<b>${esc(p.name || CATS[p.cat].label.replace(/s$/, ""))}</b>
      <div class="m">${CATS[p.cat].label.replace(/s$/, "")}${p.n > 1 ? ` · ${p.n} mapped tanks/parts` : ""}${p.kv ? ` · ${esc(p.kv)} V` : ""}</div>
      <div><span class="sw st-${s === "masked" ? "unobserved" : s}"></span>${st.label}${when}</div>
      <div class="m">${esc(p.county)} County · OSM ${esc(p.osm)}</div>`;
  }

  // ---------- panels ----------
  function renderEvents() {
    const box = document.getElementById("events");
    box.innerHTML = D.meta.events.map(e => `<button data-ev="${e.id}" class="${e.id === state.event ? "on" : ""}">${esc(e.label.replace(" flood", ""))}</button>`).join("");
    box.querySelectorAll("button").forEach(b => b.onclick = () => { state.event = b.dataset.ev; refresh(); });
    const e = ev();
    document.getElementById("evinfo").innerHTML = `
      <div><b>${esc(e.label)}</b> · window ${dayY(e.start)} – ${dayY(e.end)}</div>
      <div>Sentinel-1 passes with data:</div>
      <div class="passes">${e.dates.map(d => `<span>${day(d)}</span>`).join("")}</div>
      <div>${esc(e.note)}</div>
      <p class="hint">Compared with ${D.meta.reference_dates.length} dry-season passes (${D.meta.reference.map(r => r.label.replace("Dry season ", "")).join(", ")}).</p>`;
  }

  function kpis() {
    const segs = D.lines.features.filter(f => lineOn(f.properties));
    const km = segs.reduce((s, f) => s + f.properties.km, 0);
    const hi = segs.filter(f => f.properties.canopy >= HEAVY).reduce((s, f) => s + f.properties.km, 0);
    const mean = km ? segs.reduce((s, f) => s + f.properties.canopy * f.properties.km, 0) / km : null;
    const fs = D.fac.features.filter(f => state.cats[f.properties.cat]);
    const n = s => fs.filter(f => facStatus(f.properties) === s).length;
    const unobs = n("unobserved") + n("masked");
    document.getElementById("kpis").innerHTML = `
      <div class="kpi"><b>${fmt(km)} km</b><span>transmission lines shown</span></div>
      <div class="kpi"><b>${fmt(km ? hi / km * 100 : 0)} %</b><span>${fmt(hi)} km in heavy canopy (≥ ${HEAVY} %)</span></div>
      <div class="kpi"><b>${fmt(n("flooded"))}</b><span><span class="sw st-flooded"></span>facilities flooded</span></div>
      <div class="kpi"><b>${fmt(n("near"))}</b><span><span class="sw st-near"></span>near flood water</span></div>
      <div class="kpi"><b>${fmt(ev().new_water_km2)} km²</b><span>flood water seen in Kentucky</span></div>
      <div class="kpi"><b>${fmt(unobs)}</b><span>facilities not observed by any pass</span></div>`;
  }

  function renderCounty() {
    const box = document.getElementById("county");
    const f = D.counties.features.find(f => f.properties.name === state.selected);
    if (!f) { box.innerHTML = `<p class="empty">Click a county, line or facility for details.</p>`; return; }
    const p = f.properties, fl = p.flood[state.event];
    const fac = Object.entries(CATS).map(([k, c]) => `<div><span>${c.label}</span><b>${fmt(p.fac[k] || 0)}</b></div>`).join("");
    box.innerHTML = `<div class="cty">
      <h3>${esc(p.name)} County <button id="clearSel">clear</button></h3>
      <div class="score">Mean canopy along lines <b>${fmt(p.canopy)} %</b> · flood water ${fmt(fl.new_water_km2, 1)} km²</div>
      <div class="facts">
        <div class="h">Transmission lines</div>
        <div><span>Line length (all voltages)</span><b>${fmt(p.line_km)} km</b></div>
        <div><span>of which 138 kV and above</span><b>${fmt(p.hv_km)} km</b></div>
        <div><span>In heavy canopy (≥ ${HEAVY} %)</span><b>${fmt(p.hi_km)} km</b></div>
        <div><span>Corridor cells ≥ 50 % canopy</span><b>${fmt(p.line_km ? p.dense_km / p.line_km * 100 : 0)} %</b></div>
        <div class="h">${esc(ev().label)}</div>
        <div><span>Flood water</span><b>${fmt(fl.new_water_km2, 2)} km²</b></div>
        <div><span>County area observed</span><b>${fmt(fl.observed_share * 100)} %</b></div>
        <div><span><span class="sw st-flooded"></span>Facilities flooded</span><b>${fmt(fl.flooded || 0)}</b></div>
        <div><span><span class="sw st-near"></span>Near flood water</span><b>${fmt(fl.near || 0)}</b></div>
        <div><span><span class="sw st-unobserved"></span>Not observed</span><b>${fmt(fl.unobserved || 0)}</b></div>
        <div class="h">Facilities mapped in OSM</div>${fac}
      </div></div>`;
    document.getElementById("clearSel").onclick = () => selectCounty(null);
  }

  function renderRank() {
    const fs = D.counties.features.map(f => f.properties);
    let title, val, unit, d = 0;
    if (state.mode === "flood") { title = "Most flood water"; val = p => p.flood[state.event].new_water_km2; unit = " km²"; d = 1; }
    else { title = `Most line km in heavy canopy (≥ ${HEAVY} %)`; val = p => p.hi_km; unit = " km"; }
    document.getElementById("rankTitle").textContent = title;
    const top = fs.sort((a, b) => val(b) - val(a)).slice(0, 10);
    const ol = document.getElementById("rank");
    ol.innerHTML = top.map(p => `<li data-n="${esc(p.name)}">${esc(p.name)}<span class="s">${fmt(val(p), d)}${unit}</span></li>`).join("");
    ol.querySelectorAll("li").forEach(li => li.onclick = () => selectCounty(li.dataset.n));
  }

  function renderWet() {
    const fs = D.fac.features.filter(f => state.cats[f.properties.cat] && isWet(facStatus(f.properties)))
      .filter(f => !state.selected || f.properties.county === state.selected)
      .sort((a, b) => (facStatus(a.properties) === "flooded" ? 0 : 1) - (facStatus(b.properties) === "flooded" ? 0 : 1)
        || a.properties.cat.localeCompare(b.properties.cat));
    document.getElementById("wetHint").textContent = `${fs.length} of the selected categories${state.selected ? ` in ${state.selected} County` : ""} · ${esc(ev().label)}. Click to zoom.`;
    const ul = document.getElementById("wet");
    ul.innerHTML = fs.slice(0, 150).map((f, i) => {
      const p = f.properties, s = facStatus(p);
      return `<li data-i="${i}"><span class="sw st-${s}"></span><span class="nm">${esc(p.name || CATS[p.cat].label.replace(/s$/, ""))}<br><span class="m">${CATS[p.cat].label.replace(/s$/, "")} · ${esc(p.county)}</span></span><span class="m">${s === "flooded" ? (p.wd[state.event] ? day(p.wd[state.event]) : "flooded") : "near"}</span></li>`;
    }).join("") || `<li class="empty">None.</li>`;
    ul.querySelectorAll("li[data-i]").forEach(li => li.onclick = () => {
      const [lon, lat] = fs[+li.dataset.i].geometry.coordinates;
      map.setView([lat, lon], 14);
    });
  }

  function renderLegend() {
    const parts = [];
    if (state.lines || state.mode === "canopy") {
      parts.push(`<div class="blk"><div class="t">Tree canopy in line corridor</div>
        <div class="bar">${[0, 1, 2, 3, 4, 5].map(i => `<i style="background:var(--g${i})"></i>`).join("")}</div>
        <div class="ends"><span>0</span>${CANOPY_BREAKS.map(b => `<span>${b}</span>`).join("")}<span>%</span></div></div>`);
    }
    if (state.mode === "flood") {
      parts.push(`<div class="blk"><div class="t">County flood water (km²)</div>
        <div class="bar">${[0, 1, 2, 3, 4, 5].map(i => `<i style="background:var(--b${i})"></i>`).join("")}</div>
        <div class="ends"><span>0</span>${FLOOD_BREAKS.map(b => `<span>${b}</span>`).join("")}<span>km²</span></div></div>`);
    }
    if (state.flood) {
      parts.push(`<div class="blk"><div class="t">Sentinel-1 water</div>
        <div class="row"><span class="sw sq" style="background:rgb(11,99,206)"></span>Flood water (event only)</div>
        <div class="row"><span class="sw sq" style="background:rgb(125,150,180)"></span>Normal water</div>
        <div class="row"><span class="sw sq" style="background:rgba(120,120,120,.35)"></span>No pass in window</div></div>`);
    }
    parts.push(`<div class="blk"><div class="t">Facilities</div>
      ${["flooded", "near", "water_edge", "dry", "unobserved"].map(s => `<div class="row"><span class="sw st-${s}"></span>${STATUS[s].label}</div>`).join("")}</div>`);
    document.getElementById("legend").innerHTML = parts.join("");
  }

  function renderTable() {
    const cols = [
      ["County", p => p.name, 0], ["Line km", p => p.line_km, 0], ["Mean canopy %", p => p.canopy, 0],
      [`km ≥ ${HEAVY} % canopy`, p => p.hi_km, 0],
      ...Object.entries(CATS).map(([k, c]) => [c.label.split(" ")[0], p => p.fac[k] || 0, 0]),
      ["Flood water km²", p => p.flood[state.event].new_water_km2, 1],
      ["Observed %", p => p.flood[state.event].observed_share * 100, 0],
      ["Flooded", p => p.flood[state.event].flooded || 0, 0], ["Near", p => p.flood[state.event].near || 0, 0],
      ["Not observed", p => p.flood[state.event].unobserved || 0, 0],
    ];
    const tbl = document.getElementById("tbl");
    const sort = tbl._sort ?? { i: 3, dir: -1 };
    const rows = D.counties.features.map(f => f.properties).sort((a, b) => {
      const va = cols[sort.i][1](a), vb = cols[sort.i][1](b);
      return (typeof va === "string" ? va.localeCompare(vb) : (va ?? -1) - (vb ?? -1)) * sort.dir;
    });
    tbl.innerHTML = `<thead><tr>${cols.map((c, i) => `<th data-i="${i}">${c[0]}${i === sort.i ? (sort.dir < 0 ? " ↓" : " ↑") : ""}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(p => `<tr>${cols.map((c, i) => `<td${i === 0 ? ` data-n="${esc(p.name)}"` : ""}>${i === 0 ? esc(c[1](p)) : fmt(c[1](p), c[2])}</td>`).join("")}</tr>`).join("")}</tbody>`;
    tbl.querySelectorAll("th").forEach(th => th.onclick = () => {
      const i = +th.dataset.i;
      tbl._sort = { i, dir: sort.i === i ? -sort.dir : (i === 0 ? 1 : -1) };
      renderTable();
    });
    tbl.querySelectorAll("td[data-n]").forEach(td => td.onclick = () => { showView("map"); selectCounty(td.dataset.n); });
  }

  function renderCats() {
    const box = document.getElementById("cats");
    box.innerHTML = Object.entries(CATS).map(([k, c]) => {
      const n = D.fac.features.filter(f => f.properties.cat === k).length;
      return `<label class="chk"><input type="checkbox" data-c="${k}" ${state.cats[k] ? "checked" : ""}> <span class="glyph">${c.g}</span>${c.label}<span class="n">${fmt(n)}</span></label>`;
    }).join("");
    box.querySelectorAll("input").forEach(i => i.onchange = () => { state.cats[i.dataset.c] = i.checked; drawFacilities(); kpis(); renderWet(); });
  }

  // ---------- interaction ----------
  function selectCounty(name, zoom = true) {
    state.selected = state.selected === name && zoom ? null : name;
    countyLayer.setStyle(styleCounty);
    if (state.selected && zoom) {
      countyLayer.eachLayer(l => { if (l.feature.properties.name === state.selected) map.fitBounds(l.getBounds(), { padding: [30, 30], maxZoom: 11 }); });
    }
    renderCounty(); renderWet();
  }

  function showView(v) {
    document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", b.dataset.view === v));
    for (const id of ["map", "table", "method"]) document.getElementById(`view-${id}`).classList.toggle("hidden", id !== v);
    document.querySelector(".detail").classList.toggle("hidden", v !== "map");
    if (v === "table") renderTable();
    if (v === "map") map.invalidateSize();
  }

  function refresh() {
    renderEvents();
    countyLayer.setStyle(styleCounty);
    drawFlood(); drawFacilities(); kpis(); renderCounty(); renderRank(); renderWet(); renderLegend();
    if (!document.getElementById("view-table").classList.contains("hidden")) renderTable();
  }

  function wire() {
    document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => showView(b.dataset.view));
    document.querySelectorAll("#mode button").forEach(b => b.onclick = () => {
      state.mode = b.dataset.mode;
      document.querySelectorAll("#mode button").forEach(x => x.classList.toggle("on", x === b));
      document.getElementById("modeHint").textContent = MODES[state.mode];
      countyLayer.setStyle(styleCounty); renderRank(); renderLegend();
    });
    document.getElementById("modeHint").textContent = MODES[state.mode];
    const chk = (id, key, fn) => document.getElementById(id).onchange = e => { state[key] = e.target.checked; fn(); };
    chk("showLines", "lines", () => { drawLines(); renderLegend(); });
    chk("kvHigh", "kvHigh", () => { drawLines(); kpis(); });
    chk("kv69", "kv69", () => { drawLines(); kpis(); });
    chk("showFlood", "flood", () => { drawFlood(); renderLegend(); });
    chk("onlyWet", "onlyWet", drawFacilities);
  }

  function fillMethod() {
    const m = D.meta;
    document.getElementById("m-canopy").textContent = m.canopy;
    document.getElementById("m-hw").textContent = m.corridor_half_width_m;
    document.getElementById("m-ref").textContent = `${m.reference.map(r => `${dayY(r.start)} – ${dayY(r.end)}`).join(" and ")}; ${m.reference_dates.length} passes with data`;
    document.getElementById("m-src").textContent = m.flood_source === "earthdata"
      ? "the original 30 m B01_WTR GeoTIFFs from PO.DAAC (NASA Earthdata), regridded to ~25 × 31 m lon/lat cells with nearest neighbour."
      : "the DSWx-S1 water-class layer rendered by NASA GIBS (WMS, no login needed), requested at ~25 × 31 m and converted back to the product classes from its fixed colour table. Every chunk was checked to contain only DSWx-S1 palette colours. The pipeline can instead read the original 30 m GeoTIFFs with a NASA Earthdata login (pipeline/fetch_opera_earthdata.py).";
    document.querySelectorAll(".built").forEach(e => e.textContent = m.built);
  }

  Promise.all(["meta.json", "counties.geojson", "lines.geojson", "line_info.json", "facilities.geojson"]
    .map(f => fetch(`data/${f}`).then(r => r.json())))
    .then(([meta, counties, lines, info, fac]) => {
      D = { meta, counties, lines, info, fac };
      initMap(); renderCats(); wire(); fillMethod(); drawLines(); refresh();
    });
})();
