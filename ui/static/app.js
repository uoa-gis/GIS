const DEFAULT_CENTER = [-39.471, 176.869];

const map = L.map("map").setView(DEFAULT_CENTER, 11);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

const drawn = new L.FeatureGroup();
map.addLayer(drawn);

const drawControl = new L.Control.Draw({
  draw: {
    polygon: false,
    polyline: false,
    circle: false,
    circlemarker: false,
    marker: false,
    rectangle: { shapeOptions: { color: "#d4a017" } },
  },
  edit: { featureGroup: drawn },
});
map.addControl(drawControl);

let mode = "none"; // "click" | "draw"
let clickCenter = null;
let clickRect = null;
let drawnBounds = null;

function kmRect(lat, lon, halfKm) {
  const dlat = halfKm / 111.32;
  const dlon = halfKm / (111.32 * Math.cos((lat * Math.PI) / 180));
  return L.latLngBounds(
    [lat - dlat, lon - dlon],
    [lat + dlat, lon + dlon]
  );
}

function setAoiText() {
  const el = document.getElementById("aoiText");
  if (mode === "draw" && drawnBounds) {
    el.textContent = `Drawn rectangle: west ${drawnBounds.getWest().toFixed(4)}, south ${drawnBounds.getSouth().toFixed(4)}, east ${drawnBounds.getEast().toFixed(4)}, north ${drawnBounds.getNorth().toFixed(4)}`;
    return;
  }
  if (mode === "click" && clickCenter) {
    el.textContent = `Click centre: ${clickCenter.lat.toFixed(4)}, ${clickCenter.lng.toFixed(4)} · half-width ${document.getElementById("halfKm").value} km`;
    return;
  }
  el.textContent = "No AOI yet.";
}

function redrawClickRect() {
  if (!clickCenter) return;
  const half = Number(document.getElementById("halfKm").value) || 5;
  const b = kmRect(clickCenter.lat, clickCenter.lng, half);
  if (clickRect) map.removeLayer(clickRect);
  clickRect = L.rectangle(b, { color: "#d4a017", weight: 2 }).addTo(map);
  setAoiText();
}

let drawing = false;
map.on("draw:drawstart", () => {
  drawing = true;
});
map.on("draw:drawstop", () => {
  drawing = false;
});
map.on("click", (e) => {
  if (drawing || mode === "draw") return;
  mode = "click";
  clickCenter = e.latlng;
  drawn.clearLayers();
  drawnBounds = null;
  redrawClickRect();
});

map.on(L.Draw.Event.CREATED, (e) => {
  mode = "draw";
  clickCenter = null;
  if (clickRect) {
    map.removeLayer(clickRect);
    clickRect = null;
  }
  drawn.clearLayers();
  drawn.addLayer(e.layer);
  drawnBounds = e.layer.getBounds();
  setAoiText();
});

document.getElementById("halfKm").addEventListener("input", () => {
  if (mode === "click") redrawClickRect();
});

function payload() {
  const body = {
    start_date: document.getElementById("startDate").value,
    end_date: document.getElementById("endDate").value,
    peak_date: document.getElementById("peakDate").value || null,
    half_km: Number(document.getElementById("halfKm").value) || 5,
    slope_max_deg: Number(document.getElementById("slopeMaxDeg").value),
    elevation_max_m: Number(document.getElementById("elevationMaxM").value),
  };
  if (!Number.isFinite(body.slope_max_deg) || body.slope_max_deg < 0) {
    throw new Error("Max slope (deg) must be a number ≥ 0.");
  }
  if (!Number.isFinite(body.elevation_max_m) || body.elevation_max_m < 0) {
    throw new Error("Max elevation (m) must be a number ≥ 0.");
  }
  if (mode === "draw" && drawnBounds) {
    body.west = drawnBounds.getWest();
    body.south = drawnBounds.getSouth();
    body.east = drawnBounds.getEast();
    body.north = drawnBounds.getNorth();
  } else if (mode === "click" && clickCenter) {
    body.center_lat = clickCenter.lat;
    body.center_lon = clickCenter.lng;
  } else {
    throw new Error("Select an AOI: click the map or draw a rectangle.");
  }
  return body;
}

function showProgress(progress, startedAt) {
  const box = document.getElementById("progress");
  const fill = document.getElementById("progressFill");
  const text = document.getElementById("progressText");
  const elapsed = document.getElementById("progressElapsed");
  box.hidden = false;
  if (progress && progress.total > 0) {
    fill.style.width = `${Math.round((100 * progress.step) / progress.total)}%`;
    text.textContent = `Step ${progress.step}/${progress.total}: ${progress.label}…`;
  } else {
    fill.style.width = "0%";
    text.textContent = progress ? `${progress.label}…` : "Queued…";
  }
  const secs = Math.round((Date.now() - startedAt) / 1000);
  elapsed.textContent = `Elapsed ${Math.floor(secs / 60)}m ${secs % 60}s · Earth Engine can take a few minutes.`;
}

function finishProgress(ok) {
  const fill = document.getElementById("progressFill");
  const text = document.getElementById("progressText");
  if (ok) {
    fill.style.width = "100%";
    text.textContent = "Completed.";
  } else {
    text.textContent = "Failed.";
  }
}

async function poll(jobId) {
  const startedAt = Date.now();
  while (true) {
    const res = await fetch(`/api/job/${jobId}`);
    const data = await res.json();
    showProgress(data.progress, startedAt);
    if (data.status === "done" || data.status === "error") {
      finishProgress(data.status === "done");
      return data.result;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

document.getElementById("runBtn").addEventListener("click", async () => {
  const btn = document.getElementById("runBtn");
  const statusEl = document.getElementById("status");
  const fig = document.getElementById("figure");
  const statsEl = document.getElementById("stats");
  try {
    const body = payload();
    btn.disabled = true;
    statusEl.textContent = "";
    showProgress(null, Date.now());
    fig.hidden = true;
    statsEl.textContent = "";
    const start = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!start.ok) throw new Error(await start.text());
    const { job_id } = await start.json();
    const result = await poll(job_id);
    if (!result.ok) {
      statusEl.textContent = result.error || "Run failed.";
      return;
    }
    statusEl.textContent = "Done.";
    fig.src = `data:image/png;base64,${result.figure_png_base64}`;
    fig.hidden = false;
    statsEl.textContent = JSON.stringify(
      { stats: result.stats, meta: result.meta, report: result.report },
      null,
      2
    );
  } catch (err) {
    statusEl.textContent = err.message || String(err);
  } finally {
    btn.disabled = false;
  }
});

setAoiText();

fetch("/api/defaults")
  .then((res) => (res.ok ? res.json() : null))
  .then((defaults) => {
    if (!defaults) return;
    document.getElementById("slopeMaxDeg").value = defaults.slope_max_deg;
    document.getElementById("elevationMaxM").value = defaults.elevation_max_m;
  })
  .catch(() => {});
