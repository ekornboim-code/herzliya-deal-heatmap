/**
 * Shared color mapping for year-normalized scores (1-100).
 * Blue (cheap) → cyan → yellow → orange → red (expensive).
 */
export function scoreToColor(score, alpha = 1) {
  const t = Math.max(0, Math.min(1, (Number(score) - 1) / 99));
  const stops = [
    [0.0, [44, 123, 182]],
    [0.25, [0, 166, 166]],
    [0.5, [255, 255, 102]],
    [0.75, [253, 174, 97]],
    [1.0, [215, 25, 28]],
  ];

  let i = 0;
  while (i < stops.length - 2 && t > stops[i + 1][0]) i += 1;
  const [t0, c0] = stops[i];
  const [t1, c1] = stops[i + 1];
  const u = (t - t0) / (t1 - t0 || 1);
  const r = Math.round(c0[0] + (c1[0] - c0[0]) * u);
  const g = Math.round(c0[1] + (c1[1] - c0[1]) * u);
  const b = Math.round(c0[2] + (c1[2] - c0[2]) * u);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function activeScore(deal, metric) {
  return metric === "total" ? deal.scoreTotal : deal.scorePerSqm;
}

export function formatPrice(n) {
  return `₪${Number(n).toLocaleString("he-IL")}`;
}
