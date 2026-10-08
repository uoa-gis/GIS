/** Sentinel-2 U-Net tab. Isolated from Sentinel-1 (`app.js`) map and form state. */

const S2_DEFAULT_CENTER = [-39.471, 176.869];

let s2Map = null;
let s2Drawn = null;
let s2Mode = "none";
let s2ClickCenter = null;
let s2ClickRect = null;
let s2DrawnBounds = null;
let s2Drawing = false;

function s2KmRect(lat, lon, halfKm) {
  const dlat = halfKm / 111.32;
  const dlon = halfKm / (111.32 * Math.cos((lat * Math.PI) / 180));
  return L.latLngBounds([lat - dlat, lon - dlon], [lat + dlat, lon + dlon]);
}

function s2SetAoiText() {
  const el = document.getElementById("s2AoiText");
  if (!el) return;
  if (s2Mode === "draw" && s2DrawnBounds) {
    el.textContent =
      `Drawn rectangle: west ${s2DrawnBounds.getWest().toFixed(4)}, south ${s2DrawnBounds.getSouth().toFixed(4)}, east ${s2DrawnBounds.getEast().toFixed(4)}, north ${s2DrawnBounds.getNorth().toFixed(4)}`;
    return;
  }
  if (s2Mode === "click" && s2ClickCenter) {
    const half = document.getElementById("s2HalfKm")?.value || "5";
    el.textContent = `Click centre: ${s2ClickCenter.lat.toFixed(4)}, ${s2ClickCenter.lng.toFixed(4)} · half-width ${half} km`;
    return;
  }
  el.textContent = "No AOI yet.";
}

function s2RedrawClickRect() {
  if (!s2Map || !s2ClickCenter) return;
  const half = Number(document.getElementById("s2HalfKm").value) || 5;
  const b = s2KmRect(s2ClickCenter.lat, s2ClickCenter.lng, half);
  if (s2ClickRect) s2Map.removeLayer(s2ClickRect);
  s2ClickRect = L.rectangle(b, { color: "#d4a017", weight: 2 }).addTo(s2Map);
  s2SetAoiText();
}

function initS2Map() {
  const el = document.getElementById("s2-map");
  if (!el) return;
  if (s2Map) {
    s2Map.invalidateSize();
    return;
  }
  s2Map = L.map("s2-map").setView(S2_DEFAULT_CENTER, 11);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap",
  }).addTo(s2Map);
  s2Drawn = new L.FeatureGroup();
  s2Map.addLayer(s2Drawn);
  const drawControl = new L.Control.Draw({
    draw: {
      polygon: false,
      polyline: false,
      circle: false,
      circlemarker: false,
      marker: false,
      rectangle: { shapeOptions: { color: "#d4a017" } },
    },
    edit: { featureGroup: s2Drawn },
  });
  s2Map.addControl(drawControl);
  s2Map.on("draw:drawstart", () => {
    s2Drawing = true;
  });
  s2Map.on("draw:drawstop", () => {
    s2Drawing = false;
  });
  s2Map.on("click", (e) => {
    if (s2Drawing || s2Mode === "draw") return;
    s2Mode = "click";
    s2ClickCenter = e.latlng;
    s2Drawn.clearLayers();
    s2DrawnBounds = null;
    s2RedrawClickRect();
  });
  s2Map.on(L.Draw.Event.CREATED, (e) => {
    s2Mode = "draw";
    s2ClickCenter = null;
    if (s2ClickRect) {
      s2Map.removeLayer(s2ClickRect);
      s2ClickRect = null;
    }
    s2Drawn.clearLayers();
    s2Drawn.addLayer(e.layer);
    s2DrawnBounds = e.layer.getBounds();
    s2SetAoiText();
  });
  document.getElementById("s2HalfKm")?.addEventListener("input", () => {
    if (s2Mode === "click") s2RedrawClickRect();
  });
  s2SetAoiText();
  setTimeout(() => s2Map.invalidateSize(), 0);
}

window.initS2Map = initS2Map;
window.invalidateS2Map = function invalidateS2Map() {
  if (s2Map) s2Map.invalidateSize();
};

function s2Payload() {
  const body = {
    start_date: document.getElementById("s2StartDate").value,
    end_date: document.getElementById("s2EndDate").value,
    peak_date: document.getElementById("s2PeakDate").value || null,
    half_km: Number(document.getElementById("s2HalfKm").value) || 5,
    slope_max_deg: Number(document.getElementById("s2SlopeMaxDeg").value),
    elevation_max_m: Number(document.getElementById("s2ElevationMaxM").value),
  };
  if (!Number.isFinite(body.slope_max_deg) || body.slope_max_deg < 0) {
    throw new Error("Max slope (deg) must be a number ≥ 0.");
  }
  if (!Number.isFinite(body.elevation_max_m) || body.elevation_max_m < 0) {
    throw new Error("Max elevation (m) must be a number ≥ 0.");
  }
  if (s2Mode === "draw" && s2DrawnBounds) {
    body.west = s2DrawnBounds.getWest();
    body.south = s2DrawnBounds.getSouth();
    body.east = s2DrawnBounds.getEast();
    body.north = s2DrawnBounds.getNorth();
  } else if (s2Mode === "click" && s2ClickCenter) {
    body.center_lat = s2ClickCenter.lat;
    body.center_lon = s2ClickCenter.lng;
  } else {
    throw new Error("Select an AOI: click the map or draw a rectangle.");
  }
  return body;
}

function s2ShowProgress(progress, startedAt) {
  const box = document.getElementById("s2Progress");
  const fill = document.getElementById("s2ProgressFill");
  const text = document.getElementById("s2ProgressText");
  const elapsed = document.getElementById("s2ProgressElapsed");
  box.hidden = false;
  if (progress && progress.total > 0) {
    fill.style.width = `${Math.round((100 * progress.step) / progress.total)}%`;
    text.textContent = `Step ${progress.step}/${progress.total}: ${progress.label}…`;
  } else {
    fill.style.width = "0%";
    text.textContent = progress ? `${progress.label}…` : "Queued…";
  }
  const secs = Math.round((Date.now() - startedAt) / 1000);
  elapsed.textContent = `Elapsed ${Math.floor(secs / 60)}m ${secs % 60}s · Earth Engine and U-Net can take a few minutes.`;
}

function s2FinishProgress(ok) {
  const fill = document.getElementById("s2ProgressFill");
  const text = document.getElementById("s2ProgressText");
  if (ok) {
    fill.style.width = "100%";
    text.textContent = "Completed.";
  } else {
    text.textContent = "Failed.";
  }
}

async function s2Poll(jobId) {
  const startedAt = Date.now();
  while (true) {
    const res = await fetch(`/api/job/${jobId}`);
    const data = await res.json();
    s2ShowProgress(data.progress, startedAt);
    if (data.status === "done" || data.status === "error") {
      s2FinishProgress(data.status === "done");
      return data.result;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

const S2_KEY_STATS = [
  ["Final flood extent", "flood_final_km2", "km²"],
  ["Flood share of AOI", "flood_pct_of_aoi", "%"],
  ["AOI area", "aoi_km2", "km²"],
  ["U-Net water (event)", "event_unet_water_km2", "km²"],
  ["Permanent water", "permanent_water_km2", "km²"],
  ["Event scene", "event_scene", ""],
  ["Event cloud cover", "event_cloud_pct", "%"],
];

function s2FormatStat(value, unit) {
  if (typeof value === "string") return value;
  if (typeof value !== "number" || Number.isNaN(value)) return String(value);
  const abs = Math.abs(value);
  let text;
  if (Number.isInteger(value) || abs >= 100) text = value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  else if (abs >= 1) text = value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  else text = value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return unit ? `${text} ${unit}` : text;
}

function s2RenderKeyStats(stats) {
  const table = document.getElementById("s2KeyStats");
  const tbody = table.querySelector("tbody");
  tbody.replaceChildren();
  if (!stats) {
    table.hidden = true;
    return;
  }
  let any = false;
  for (const [label, key, unit] of S2_KEY_STATS) {
    if (!(key in stats) || stats[key] == null) continue;
    any = true;
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = label;
    const td = document.createElement("td");
    td.textContent = s2FormatStat(stats[key], unit);
    tr.append(th, td);
    tbody.append(tr);
  }
  table.hidden = !any;
}

document.getElementById("s2RunBtn")?.addEventListener("click", async () => {
  const btn = document.getElementById("s2RunBtn");
  const statusEl = document.getElementById("s2Status");
  const fig = document.getElementById("s2Figure");
  try {
    const body = s2Payload();
    btn.disabled = true;
    statusEl.textContent = "";
    s2ShowProgress(null, Date.now());
    fig.hidden = true;
    s2RenderKeyStats(null);
    const start = await fetch("/api/run-s2", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!start.ok) throw new Error(await start.text());
    const { job_id } = await start.json();
    const result = await s2Poll(job_id);
    if (!result.ok) {
      statusEl.textContent = result.error || "Run failed.";
      return;
    }
    statusEl.textContent = "Done.";
    fig.src = `data:image/png;base64,${result.figure_png_base64}`;
    fig.hidden = false;
    s2RenderKeyStats(result.stats);
  } catch (err) {
    statusEl.textContent = err.message || String(err);
  } finally {
    btn.disabled = false;
  }
});

fetch("/api/defaults")
  .then((res) => (res.ok ? res.json() : null))
  .then((defaults) => {
    if (!defaults) return;
    const slope = document.getElementById("s2SlopeMaxDeg");
    const elev = document.getElementById("s2ElevationMaxM");
    if (slope) slope.value = defaults.slope_max_deg;
    if (elev) elev.value = defaults.elevation_max_m;
  })
  .catch(() => {});
