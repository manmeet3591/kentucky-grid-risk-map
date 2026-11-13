/* Multi-hazard risk tab: county hazard x exposure for Kentucky critical infrastructure */
window.RiskView = (() => {
  const HAZARDS = [
    { key: "wind", label: "Damaging wind" },
    { key: "rain", label: "Extreme rain" },
    { key: "heat", label: "Extreme heat" },
    { key: "ice", label: "Ice & cold" },
  ];
  const MODES = {
    risk: "√(hazard × exposure). High where severe hazards meet many critical assets.",
    hazard: "Weighted multi-hazard score from the sliders, independent of assets.",
    exposure: "Weighted count of critical assets, line km and customers, ranked across counties.",
  };
  const SPARKS = [
    ["wind_annmax", "Annual max wind (m/s)", 1],
    ["rain_annmax", "Annual max 1-day rain (mm)", 0],
    ["heat_days", "Days ≥ HEAT °C", 0],
    ["ice_days", "Wintry-precip days", 0],
  ];
  const ASSET_LABEL = {
    power_plant: "Power plants", substation: "Substations", fuel_terminal: "Fuel terminals",
    hospital: "Hospitals", water_plant: "Water / wastewater plants", gas_station: "Gas stations",
  };

  const state = { weights: Object.fromEntries(HAZARDS.map(h => [h.key, 1])), mode: "risk", selected: null, view: "map" };
  let R, geo, map, layer, ready = false;

  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const color = v => (v == null ? css("--nodata") : css(`--r${Math.min(5, Math.floor(v * 6))}`));
  const pct = v => (v == null ? "–" : Math.round(v * 100));
  const fmt = (v, d = 0) => (v == null ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const $ = id => document.getElementById(id);

  function score() {
    for (const c of Object.values(R.counties)) {
      let s = 0, w = 0;
      for (const { key } of HAZARDS) { s += state.weights[key] * c.h[key]; w += state.weights[key]; }
      c._hazard = w ? s / w : 0;
      c._risk = Math.sqrt(c._hazard * c.exposure);
    }
  }
  const val = c => (state.mode === "risk" ? c._risk : state.mode === "hazard" ? c._hazard : c.exposure);

  // ---------- map ----------
  function initMap() {
    map = L.map("rmap", { zoomSnap: 0.25 });
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const esri = s => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${s}/MapServer/tile/{z}/{y}/{x}`;
    L.tileLayer(esri(dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base"), {
      attribution: "Basemap &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", maxZoom: 16,
    }).addTo(map);
    map.createPane("rlabels").style.zIndex = 450;
    map.getPane("rlabels").style.pointerEvents = "none";
    L.tileLayer(esri(dark ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference"), { pane: "rlabels", maxZoom: 16 }).addTo(map);
    layer = L.geoJSON(geo, {
      style,
      onEachFeature: (f, l) => {
        l.on({
          mouseover: () => { l.setStyle({ weight: 2.5, color: css("--ink") }); l.bringToFront(); },
          mouseout: () => layer.resetStyle(l),
          click: () => select(f.properties.fips),
        });
        l.bindTooltip(() => tip(R.counties[f.properties.fips]), { className: "tt", sticky: true, direction: "top", offset: [0, -8] });
      },
    }).addTo(map);
    map.fitBounds(layer.getBounds(), { padding: [8, 8] });
  }

  function style(f) {
    const c = R.counties[f.properties.fips], sel = state.selected === f.properties.fips;
    return { fillColor: color(val(c)), fillOpacity: 0.82, color: sel ? css("--ink") : css("--surface"), weight: sel ? 3 : 0.8 };
  }

  function tip(c) {
    return `<b>${esc(c.name)} County</b>
      <div class="m">Risk ${pct(c._risk)} · hazard ${pct(c._hazard)} · exposure ${pct(c.exposure)}</div>
      <div class="m">${HAZARDS.map(h => `${h.label.split(" ").pop()} ${pct(c.h[h.key])}`).join(" · ")}</div>`;
  }

  // ---------- panels ----------
  function renderWeights() {
    $("rweights").innerHTML = HAZARDS.map(h => `<div class="w"><label for="rw-${h.key}">${h.label}<output id="rwo-${h.key}">${state.weights[h.key].toFixed(1)}</output></label>
      <input type="range" id="rw-${h.key}" min="0" max="3" step="0.5" value="${state.weights[h.key]}"></div>`).join("");
    for (const h of HAZARDS) {
      $(`rw-${h.key}`).oninput = e => { state.weights[h.key] = +e.target.value; $(`rwo-${h.key}`).textContent = state.weights[h.key].toFixed(1); update(); };
    }
  }

  function renderLegend() {
    const t = { risk: "Risk", hazard: "Hazard", exposure: "Exposure" }[state.mode];
    $("rlegend").innerHTML = `<div class="blk"><div class="t">${t} (percentile, 0–100)</div>
      <div class="bar">${[0, 1, 2, 3, 4, 5].map(i => `<i style="background:var(--r${i})"></i>`).join("")}</div>
      <div class="ends"><span>low</span><span>high</span></div></div>`;
  }

  function spark(series, label, d) {
    const ys = series, n = ys.length, lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const W = 280, H = 40, x = i => (i / (n - 1)) * W, y = v => H - 4 - ((v - lo) / span) * (H - 8);
    const pts = ys.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const mean = ys.reduce((a, b) => a + b, 0) / n;
    const [y0, y1] = R.meta.years;
    return `<div class="spark"><div class="t"><span>${label}</span><span>mean ${fmt(mean, d)}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}, ${y0} to ${y1}">
        <line class="bl" x1="0" x2="${W}" y1="${y(mean)}" y2="${y(mean)}"></line>
        <polyline class="ln" points="${pts}"></polyline>
        ${ys.map((v, i) => `<circle class="hit" cx="${x(i)}" cy="${y(v)}" r="5"><title>${y0 + i}: ${fmt(v, d)}</title></circle>`).join("")}
        <circle class="dot" cx="${x(n - 1)}" cy="${y(ys[n - 1])}" r="3"></circle>
      </svg><div class="t"><span>${y0}</span><span>${y1}</span></div></div>`;
  }

  function renderDetail() {
    const box = $("rcounty");
    const c = R.counties[state.selected];
    if (!c) { box.innerHTML = `<p class="empty">Click a county for its hazard profile, ${R.meta.years.join("–")} climate series and exposed assets.</p>`; return; }
    const th = R.meta.thresholds;
    const bars = HAZARDS.map(h => `<div class="hbar"><span>${h.label}</span><span class="track"><span class="fill" style="width:${pct(c.h[h.key])}%"></span></span><span class="v">${pct(c.h[h.key])}</span></div>`).join("");
    const m = c.m;
    const assets = Object.entries(ASSET_LABEL).map(([k, l]) => `<div><span>${l}</span><b>${fmt(c.assets[k] || 0)}</b></div>`).join("");
    box.innerHTML = `<div class="cty">
      <h3>${esc(c.name)} County <button id="rclear">clear</button></h3>
      <div class="score">Risk <b>${pct(c._risk)}</b> · hazard ${pct(c._hazard)} · exposure ${pct(c.exposure)}</div>
      ${bars}
      <div class="facts">
        <div class="h">Climate ${R.meta.years.join("–")} (NASA POWER)</div>
        <div><span>Annual max wind (median)</span><b>${fmt(m.wind_annmax, 1)} m/s</b></div>
        <div><span>Days/yr wind ≥ ${th.wind_ms} m/s</span><b>${fmt(m.wind_days, 1)}</b></div>
        <div><span>Annual max 1-day rain (median)</span><b>${fmt(m.rain_annmax)} mm</b></div>
        <div><span>Days/yr rain ≥ ${th.rain_mm} mm</span><b>${fmt(m.rain_days, 2)}</b></div>
        <div><span>Days/yr Tmax ≥ ${th.heat_c} °C</span><b>${fmt(m.heat_days, 1)}</b></div>
        <div><span>Wintry-precip days/yr</span><b>${fmt(m.ice_days, 1)}</b></div>
        <div><span>Days/yr Tmin ≤ ${th.cold_c} °C</span><b>${fmt(m.cold_days, 1)}</b></div>
      </div>
      ${SPARKS.map(([k, l, d]) => spark(c.series[k], l.replace("HEAT", th.heat_c), d)).join("")}
      <div class="facts">
        <div class="h">Exposure</div>
        <div><span>Electricity customers</span><b>${fmt(c.customers)}</b></div>
        <div><span>Lines ≥ 138 kV / 69 kV</span><b>${fmt(c.hv_km)} / ${fmt(c.kv69_km)} km</b></div>
        ${assets}
      </div></div>`;
    $("rclear").onclick = () => select(null);
  }

  function renderRank() {
    const t = { risk: "Highest-risk counties", hazard: "Highest-hazard counties", exposure: "Most exposed counties" }[state.mode];
    $("rrankTitle").textContent = t;
    const top = Object.entries(R.counties).sort((a, b) => val(b[1]) - val(a[1])).slice(0, 12);
    $("rrank").innerHTML = top.map(([f, c]) => `<li data-f="${f}">${esc(c.name)}<span class="s">${pct(val(c))}</span></li>`).join("");
    $("rrank").querySelectorAll("li").forEach(li => li.onclick = () => select(li.dataset.f));
  }

  function renderKpis() {
    const cs = Object.values(R.counties);
    const top = cs.reduce((a, b) => (b._risk > a._risk ? b : a));
    $("rkpis").innerHTML = `
      <div class="kpi"><b>${esc(top.name)}</b><span>highest risk (${pct(top._risk)})</span></div>
      <div class="kpi"><b>${cs.filter(c => c._risk >= 0.7).length}</b><span>counties with risk ≥ 70</span></div>`;
  }

  function renderTable() {
    const cols = [
      ["County", c => c.name, 0], ["Risk", c => c._risk * 100, 0], ["Hazard", c => c._hazard * 100, 0], ["Exposure", c => c.exposure * 100, 0],
      ...HAZARDS.map(h => [h.label, c => c.h[h.key] * 100, 0]),
      ["Customers", c => c.customers, 0], ["Lines km", c => c.hv_km + c.kv69_km, 0],
      ["Substations", c => c.assets.substation || 0, 0], ["Hospitals", c => c.assets.hospital || 0, 0],
      ["Plants", c => c.assets.power_plant || 0, 0], ["Fuel term.", c => c.assets.fuel_terminal || 0, 0],
      ["Water", c => c.assets.water_plant || 0, 0], ["Gas stations", c => c.assets.gas_station || 0, 0],
    ];
    const tbl = $("rtbl");
    const sort = tbl._sort ?? { i: 1, dir: -1 };
    const rows = Object.entries(R.counties).sort((a, b) => {
      const va = cols[sort.i][1](a[1]), vb = cols[sort.i][1](b[1]);
      return (typeof va === "string" ? va.localeCompare(vb) : (va ?? -1) - (vb ?? -1)) * sort.dir;
    });
    tbl.innerHTML = `<thead><tr><th>#</th>${cols.map((c, i) => `<th data-i="${i}">${c[0]}${i === sort.i ? (sort.dir < 0 ? " ↓" : " ↑") : ""}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(([f, c], r) => `<tr><td>${r + 1}</td>${cols.map((col, i) => `<td${i === 0 ? ` data-f="${f}" class="lnk"` : ""}>${i === 0 ? esc(col[1](c)) : fmt(col[1](c), col[2])}</td>`).join("")}</tr>`).join("")}</tbody>`;
    tbl.querySelectorAll("th[data-i]").forEach(th => th.onclick = () => {
      const i = +th.dataset.i;
      tbl._sort = { i, dir: sort.i === i ? -sort.dir : (i === 0 ? 1 : -1) };
      renderTable();
    });
    tbl.querySelectorAll("td[data-f]").forEach(td => td.onclick = () => { setView("map"); select(td.dataset.f); });
  }

  function select(f) {
    state.selected = state.selected === f ? null : f;
    layer.setStyle(style);
    if (state.selected) layer.eachLayer(l => { if (l.feature.properties.fips === state.selected) map.fitBounds(l.getBounds(), { padding: [40, 40], maxZoom: 9 }); });
    renderDetail();
  }

  function setView(v) {
    state.view = v;
    document.querySelectorAll("#rview button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
    $("rmapwrap").classList.toggle("hidden", v !== "map");
    $("rtablewrap").classList.toggle("hidden", v !== "table");
    if (v === "table") renderTable(); else map.invalidateSize();
  }

  function update() {
    score();
    layer.setStyle(style);
    renderKpis(); renderRank(); renderDetail(); renderLegend();
    if (state.view === "table") renderTable();
  }

  function fillMethod() {
    const t = R.meta.thresholds, w = R.meta.exposure_weights;
    $("rm-years").textContent = R.meta.years.join("–");
    $("rm-cells").textContent = R.meta.n_grid_cells;
    for (const [k, v] of Object.entries({ wind: t.wind_ms, rain: t.rain_mm, heat: t.heat_c, icep: t.ice_p_mm, icetx: t.ice_tmax_c, cold: t.cold_c, qc: t.rain_qc_mm })) {
      document.querySelectorAll(`.rt-${k}`).forEach(e => e.textContent = v);
    }
    $("rm-expw").textContent = `power plant ${w.power_plant}, fuel terminal ${w.fuel_terminal}, substation ${w.substation}, hospital ${w.hospital}, water/wastewater plant ${w.water_plant}, gas station ${w.gas_station}; plus 1 per ${w.hv_km_per_unit} km of ≥ 138 kV line, 1 per ${w.kv69_km_per_unit} km of 69 kV line and 1 per ${fmt(w.customers_per_unit)} electricity customers`;
  }

  async function show() {
    if (!ready) {
      const [risk, counties] = await Promise.all(["risk.json", "counties.geojson"].map(f => fetch(`data/${f}`).then(r => r.json())));
      R = risk;
      geo = { type: "FeatureCollection", features: counties.features.map(f => ({ type: "Feature", geometry: f.geometry, properties: { fips: f.properties.fips, name: f.properties.name } })) };
      score(); initMap(); renderWeights(); fillMethod();
      document.querySelectorAll("#rmode button").forEach(b => b.onclick = () => {
        state.mode = b.dataset.mode;
        document.querySelectorAll("#rmode button").forEach(x => x.classList.toggle("on", x === b));
        $("rmodeHint").textContent = MODES[state.mode];
        update();
      });
      $("rmodeHint").textContent = MODES[state.mode];
      $("rreset").onclick = () => { for (const h of HAZARDS) state.weights[h.key] = 1; renderWeights(); update(); };
      document.querySelectorAll("#rview button").forEach(b => b.onclick = () => setView(b.dataset.v));
      ready = true;
      update();
    }
    map.invalidateSize();
  }

  return { show };
})();
