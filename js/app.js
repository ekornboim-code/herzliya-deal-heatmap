import { activeScore, formatPrice, scoreToColor } from "./normalize.js";
import { ScoreHeatLayer } from "./heatmap-grid.js";

const CITIES = {
  herzliya: { label: "Herzliya" },
  haifa: { label: "Haifa" },
  "beer-sheva": { label: "Beer Sheva" },
  raanana: { label: "Ra'anana" },
  "tel-aviv": { label: "Tel Aviv-Yafo" },
};
const DEFAULT_CITY = "herzliya";

const state = {
  city: DEFAULT_CITY,
  deals: [],
  metric: "perSqm", // perSqm | total
  mode: "dots", // dots | heat
  map: null,
  dotsLayer: null,
  heatLayer: null,
  loadSeq: 0, // guards against a slow, superseded fetch overwriting a newer city switch
};

function $(sel) {
  return document.querySelector(sel);
}

function setStatus(msg, isError = false) {
  const el = $("#status");
  if (!msg) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

function tooltipHtml(deal) {
  const score =
    state.metric === "total" ? deal.scoreTotal : deal.scorePerSqm;
  const scoreLabel = state.metric === "total" ? "Year score (total)" : "Year score (₪/m²)";
  const rooms = deal.rooms != null ? deal.rooms : "—";
  const floor = deal.floor || "—";
  const houseNumberRow = deal.houseNumberApprox
    ? `<div class="row"><span class="k">Nearest # (approx.)</span><span>${escapeHtml(deal.houseNumberApprox)}</span></div>`
    : "";
  return `
    <strong>${escapeHtml(deal.address)}</strong>
    ${houseNumberRow}
    <div class="row"><span class="k">Date</span><span>${deal.date}</span></div>
    <div class="row"><span class="k">Price</span><span>${formatPrice(deal.price)}</span></div>
    <div class="row"><span class="k">Area</span><span>${deal.areaSqm} m²</span></div>
    <div class="row"><span class="k">₪/m²</span><span>${formatPrice(deal.pricePerSqm)}</span></div>
    <div class="row"><span class="k">${scoreLabel}</span><span>${score}</span></div>
    <div class="row"><span class="k">Rooms / floor</span><span>${rooms} / ${escapeHtml(String(floor))}</span></div>
    <div class="row"><span class="k">Type</span><span>${escapeHtml(deal.propertyType || "—")}</span></div>
  `;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function buildDots() {
  if (state.dotsLayer) {
    state.map.removeLayer(state.dotsLayer);
    state.dotsLayer = null;
  }
  const layer = L.layerGroup();
  for (const deal of state.deals) {
    const score = activeScore(deal, state.metric);
    const marker = L.circleMarker([deal.lat, deal.lon], {
      radius: 5,
      weight: 1,
      color: "rgba(20,30,40,0.35)",
      fillColor: scoreToColor(score, 1),
      fillOpacity: 0.85,
    });
    marker.bindTooltip(tooltipHtml(deal), {
      className: "deal-tip",
      sticky: true,
      opacity: 1,
      direction: "top",
    });
    layer.addLayer(marker);
  }
  state.dotsLayer = layer;
  if (state.mode === "dots") layer.addTo(state.map);
}

function ensureHeat() {
  if (!state.heatLayer) {
    state.heatLayer = new ScoreHeatLayer(state.deals, { metric: state.metric });
  } else {
    state.heatLayer.setDeals(state.deals);
    state.heatLayer.setMetric(state.metric);
  }
}

function applyMode() {
  ensureHeat();
  if (state.mode === "dots") {
    if (state.map.hasLayer(state.heatLayer)) state.map.removeLayer(state.heatLayer);
    if (state.dotsLayer && !state.map.hasLayer(state.dotsLayer)) {
      state.dotsLayer.addTo(state.map);
    }
  } else {
    if (state.dotsLayer && state.map.hasLayer(state.dotsLayer)) {
      state.map.removeLayer(state.dotsLayer);
    }
    if (!state.map.hasLayer(state.heatLayer)) state.heatLayer.addTo(state.map);
    else state.heatLayer.redraw();
  }
}

function setMetric(metric) {
  state.metric = metric;
  document.querySelectorAll("[data-metric]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.metric === metric);
  });
  buildDots();
  applyMode();
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  applyMode();
}

async function setCity(city) {
  if (!CITIES[city]) return;
  state.city = city;
  const select = $("#city-select");
  if (select && select.value !== city) select.value = city;
  try {
    await loadData(city);
  } catch (err) {
    console.error(err);
    setStatus(
      `${err.message}. Run: python3 scripts/fetch_deals.py ${city} then serve this folder.`,
      true
    );
  }
}

function initMap() {
  state.map = L.map("map", { zoomControl: true }).setView([32.165, 34.845], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
  }).addTo(state.map);
}

function wireControls() {
  document.querySelectorAll("[data-metric]").forEach((btn) => {
    btn.addEventListener("click", () => setMetric(btn.dataset.metric));
  });
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  const citySelect = $("#city-select");
  if (citySelect) {
    citySelect.addEventListener("change", () => setCity(citySelect.value));
  }
}

async function loadData(city) {
  const seq = ++state.loadSeq;
  setStatus("Loading deals…");
  $("#title").textContent = `${CITIES[city].label} residential deals`;
  $("#meta").textContent = "Loading…";

  const res = await fetch(`data/${city}-deals.json`);
  if (!res.ok) throw new Error(`Failed to load data (${res.status})`);
  const payload = await res.json();
  if (seq !== state.loadSeq) return; // a newer city switch started while this fetch was in flight

  state.deals = payload.deals || [];
  const meta = payload.meta || {};
  const years = meta.years ? `${meta.years[0]}–${meta.years[1]}` : "";
  $("#meta").textContent = `${meta.dealCount ?? state.deals.length} residential deals · ${years}`;
  if (!state.deals.length) throw new Error("Dump contains no deals");

  const bounds = L.latLngBounds(state.deals.map((d) => [d.lat, d.lon]));
  state.map.flyToBounds(bounds.pad(0.08), { duration: 1.1 });
  buildDots();
  applyMode();
  setStatus("");
}

function applyUrlParams() {
  const params = new URLSearchParams(location.search);
  if (params.get("metric") === "total" || params.get("metric") === "perSqm") {
    setMetric(params.get("metric"));
  }
  if (params.get("mode") === "heat" || params.get("mode") === "dots") {
    setMode(params.get("mode"));
  }
}

async function main() {
  try {
    if (typeof L === "undefined") {
      throw new Error("Leaflet failed to load (check vendor/leaflet/leaflet.js)");
    }
    initMap();
    wireControls();
    const params = new URLSearchParams(location.search);
    const initialCity = CITIES[params.get("city")] ? params.get("city") : DEFAULT_CITY;
    state.city = initialCity;
    const citySelect = $("#city-select");
    if (citySelect) citySelect.value = initialCity;
    await loadData(initialCity);
    applyUrlParams();
  } catch (err) {
    console.error(err);
    setStatus(
      `${err.message}. Run: python3 scripts/fetch_deals.py then serve this folder.`,
      true
    );
    const meta = $("#meta");
    if (meta && meta.textContent === "Loading…") {
      meta.textContent = "Failed to load";
    }
  }
}

main();
