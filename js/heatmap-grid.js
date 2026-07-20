import { scoreToColor } from "./normalize.js";

const CELL_METERS = 50;
const METERS_PER_DEG_LAT = 111_320;

// Outlier rejection: a deal is dismissed from the heat aggregation (not from
// the dots view) if it deviates too far from its local 3x3-cell neighborhood,
// using a MAD-based robust z-score so a single very-different dot in an
// otherwise consistent area doesn't drag the local heat color toward it.
const OUTLIER_MIN_NEIGHBORHOOD = 4;
const OUTLIER_Z_THRESHOLD = 3;
const OUTLIER_MAD_FLOOR = 5;

function median(sortedOrUnsorted) {
  const arr = [...sortedOrUnsorted].sort((a, b) => a - b);
  const n = arr.length;
  if (n === 0) return 0;
  const mid = n >> 1;
  return n % 2 ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2;
}

function filterOutliers(cellScores) {
  const filtered = new Map();
  for (const [key, scores] of cellScores) {
    const [ix, iy] = key.split(",").map(Number);
    const neighborhood = [];
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        const nk = `${ix + dx},${iy + dy}`;
        const nScores = cellScores.get(nk);
        if (nScores) neighborhood.push(...nScores);
      }
    }
    if (neighborhood.length < OUTLIER_MIN_NEIGHBORHOOD) {
      filtered.set(key, scores);
      continue;
    }
    const med = median(neighborhood);
    const mad = median(neighborhood.map((s) => Math.abs(s - med))) * 1.4826;
    const scale = Math.max(mad, OUTLIER_MAD_FLOOR);
    const kept = scores.filter((s) => Math.abs(s - med) / scale <= OUTLIER_Z_THRESHOLD);
    filtered.set(key, kept.length ? kept : scores);
  }
  return filtered;
}

/**
 * Leaflet canvas overlay: 50m grid mean scores (outlier-filtered) + 3x3 blur
 * → soft heat blobs.
 */
export class ScoreHeatLayer extends L.Layer {
  constructor(deals, options = {}) {
    super();
    this.deals = deals;
    this.metric = options.metric || "perSqm";
    this.opacity = options.opacity ?? 0.65;
    this._canvas = null;
  }

  setDeals(deals) {
    this.deals = deals;
    this.redraw();
  }

  setMetric(metric) {
    this.metric = metric;
    this.redraw();
  }

  onAdd(map) {
    this._map = map;
    this._canvas = L.DomUtil.create("canvas", "score-heat-layer");
    map.getPanes().overlayPane.appendChild(this._canvas);
    this._canvas.style.position = "absolute";
    this._canvas.style.pointerEvents = "none";
    map.on("moveend zoomend resize", this._reset, this);
    this._reset();
  }

  onRemove(map) {
    map.off("moveend zoomend resize", this._reset, this);
    if (this._canvas?.parentNode) this._canvas.parentNode.removeChild(this._canvas);
    this._canvas = null;
  }

  redraw() {
    if (this._map) this._reset();
  }

  _reset() {
    if (!this._map || !this._canvas) return;
    const map = this._map;
    const size = map.getSize();
    const topLeft = map.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(this._canvas, topLeft);
    this._canvas.width = size.x;
    this._canvas.height = size.y;
    this._canvas.style.width = `${size.x}px`;
    this._canvas.style.height = `${size.y}px`;
    this._draw();
  }

  _scoreOf(deal) {
    return this.metric === "total" ? deal.scoreTotal : deal.scorePerSqm;
  }

  _draw() {
    const map = this._map;
    const size = map.getSize();
    const ctx = this._canvas.getContext("2d");
    ctx.clearRect(0, 0, size.x, size.y);
    if (!this.deals?.length) return;

    const bounds = map.getBounds();
    const centerLat = bounds.getCenter().lat;
    const metersPerDegLon = METERS_PER_DEG_LAT * Math.cos((centerLat * Math.PI) / 180);
    const cellLat = CELL_METERS / METERS_PER_DEG_LAT;
    const cellLon = CELL_METERS / metersPerDegLon;

    const south = bounds.getSouth() - cellLat;
    const north = bounds.getNorth() + cellLat;
    const west = bounds.getWest() - cellLon;
    const east = bounds.getEast() + cellLon;

    const cellScores = new Map();
    for (const deal of this.deals) {
      if (deal.lat < south || deal.lat > north || deal.lon < west || deal.lon > east) continue;
      const iy = Math.floor((deal.lat - south) / cellLat);
      const ix = Math.floor((deal.lon - west) / cellLon);
      const key = `${ix},${iy}`;
      if (!cellScores.has(key)) cellScores.set(key, []);
      cellScores.get(key).push(this._scoreOf(deal));
    }
    if (!cellScores.size) return;

    const filteredCellScores = filterOutliers(cellScores);
    const means = new Map();
    for (const [key, scores] of filteredCellScores) {
      means.set(key, scores.reduce((a, b) => a + b, 0) / scores.length);
    }

    const blurred = new Map();
    for (const key of means.keys()) {
      const [ix, iy] = key.split(",").map(Number);
      let acc = 0;
      let n = 0;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const nk = `${ix + dx},${iy + dy}`;
          if (means.has(nk)) {
            acc += means.get(nk);
            n += 1;
          }
        }
      }
      if (n) blurred.set(key, acc / n);
    }

    const off = document.createElement("canvas");
    off.width = size.x;
    off.height = size.y;
    const octx = off.getContext("2d");
    octx.globalAlpha = this.opacity;

    for (const [key, score] of blurred) {
      const [ix, iy] = key.split(",").map(Number);
      const lat0 = south + iy * cellLat;
      const lon0 = west + ix * cellLon;
      const p0 = map.latLngToContainerPoint([lat0, lon0]);
      const p1 = map.latLngToContainerPoint([lat0 + cellLat, lon0 + cellLon]);
      const x = Math.min(p0.x, p1.x);
      const y = Math.min(p0.y, p1.y);
      const w = Math.max(1, Math.abs(p1.x - p0.x) + 1);
      const h = Math.max(1, Math.abs(p1.y - p0.y) + 1);
      octx.fillStyle = scoreToColor(score, 0.9);
      octx.fillRect(x, y, w, h);
    }

    ctx.filter = "blur(7px)";
    ctx.drawImage(off, 0, 0);
    ctx.filter = "none";
  }
}
