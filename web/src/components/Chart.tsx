import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export type Series = { label: string; values: (number | null)[]; color: string; fill?: string; dash?: number[] };

export const PALETTE = ["--accent", "--c2", "--c3", "--c4", "--c5", "--c6", "--c7", "--c8"];

export function cssColor(name: string) {
  return cssVar(name);
}

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/** Time-series line chart. `dates` are YYYY-MM-DD strings. */
export function LineChart({
  dates,
  series,
  height = 280,
  format = (v: number) => v.toFixed(2),
  log = false,
}: {
  dates: string[];
  series: Series[];
  height?: number;
  format?: (v: number) => string;
  log?: boolean;
}) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const host = el.current;
    if (!host || dates.length === 0) return;
    const xs = dates.map((d) => Date.parse(d + "T00:00:00Z") / 1000);
    const axis = { stroke: cssVar("--muted"), grid: { stroke: cssVar("--grid"), width: 1 }, ticks: { stroke: cssVar("--grid") } };
    const opts: uPlot.Options = {
      width: host.clientWidth,
      height,
      scales: { x: { time: true }, y: log ? { distr: 3 } : {} },
      legend: { live: true },
      cursor: { drag: { x: true, y: false } },
      series: [
        { value: (_u, v) => (v == null ? "" : new Date(v * 1000).toISOString().slice(0, 10)) },
        ...series.map((s) => ({
          label: s.label,
          stroke: s.color,
          width: 1.75,
          fill: s.fill,
          dash: s.dash,
          value: (_u: uPlot, v: number | null) => (v == null ? "" : format(v)),
          points: { show: false },
        })),
      ],
      axes: [{ ...axis }, { ...axis, values: (_u, vals) => vals.map((v) => format(v)), size: 64 }],
    };
    const plot = new uPlot(opts, [xs, ...series.map((s) => s.values)] as uPlot.AlignedData, host);
    const ro = new ResizeObserver(() => plot.setSize({ width: host.clientWidth, height }));
    ro.observe(host);
    return () => {
      ro.disconnect();
      plot.destroy();
    };
  }, [dates, series, height, format, log]);
  return <div className="chart" ref={el} />;
}
