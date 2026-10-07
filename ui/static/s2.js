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
