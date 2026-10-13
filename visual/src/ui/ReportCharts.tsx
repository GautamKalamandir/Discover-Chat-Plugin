// Charts for a /summary report in the chat (ADR 0015 amendment 1), drawn without a chart library:
// bars are plain HTML/CSS (they scale with the visual and follow the theme and high contrast), the
// trend is a small inline SVG. Colour is never the only signal: every bar has its value in words.

import * as React from "react";

import { Trend } from "../api/types";
import { compactIndian } from "../reports/format";

export type Tone = "good" | "bad" | "neutral";

export interface Bar {
    value: number;
    tone: Tone;
    /** The second series of a group (e.g. "vs last week"). */
    light?: boolean;
    text: string;
}

export interface BarGroup {
    label: string;
    bars: Bar[];
}

/** Horizontal bars around zero, one group per row. `marker` draws a reference line (e.g. 100%). */
export function DivergingBars({
    groups,
    label,
    marker,
}: {
    groups: BarGroup[];
    label: string;
    marker?: number;
}): React.JSX.Element | null {
    const values = groups.flatMap((g) => g.bars.map((b) => b.value));
    if (!values.length) return null;
    const low = Math.min(0, ...values, marker ?? 0);
    const high = Math.max(0, ...values, marker ?? 0);
    const span = high - low || 1;
    const at = (v: number): number => ((v - low) / span) * 100;
    const zero = at(0);
    return (
        <div className="bars" role="img" aria-label={label}>
            {groups.map((group) => (
                <div className="bar-row" key={group.label}>
                    <span className="bar-label" title={group.label}>
                        {group.label}
                    </span>
                    <span className="bar-track">
                        {group.bars.map((bar, i) => {
                            const start = Math.min(at(bar.value), zero);
                            const width = Math.abs(at(bar.value) - zero);
                            return (
                                <span className="bar-line" key={i}>
                                    <span
                                        className={`bar ${bar.tone}${bar.light ? " light" : ""}`}
                                        style={{ left: `${start}%`, width: `${Math.max(width, 0.5)}%` }}
                                    />
                                </span>
                            );
                        })}
                        <span className="bar-zero" style={{ left: `${zero}%` }} />
                        {marker !== undefined && <span className="bar-marker" style={{ left: `${at(marker)}%` }} />}
                    </span>
                    <span className="bar-value">
                        {group.bars.map((bar, i) => (
                            <span key={i} className={bar.tone}>
                                {bar.text}
                            </span>
                        ))}
                    </span>
                </div>
            ))}
        </div>
    );
}

const WIDTH = 300;
const HEIGHT = 80;

/** The main KPI per day (solid) and the same days a month earlier (dashed). */
export function TrendChart({ trend }: { trend: Trend }): React.JSX.Element | null {
    const current = trend.days.map((d) => d.value);
    const earlier = trend.days.map((d) => d.earlier_value);
    const known = [...current, ...earlier].filter((v): v is number => v !== null);
    if (current.filter((v) => v !== null).length < 2) return null;
    const low = Math.min(...known);
    const high = Math.max(...known);
    const span = high - low || Math.abs(high) || 1;
    const last = trend.days.length - 1;
    const x = (i: number): number => (last ? (i / last) * WIDTH : 0);
    const y = (v: number): number => HEIGHT - ((v - low) / span) * (HEIGHT - 8) - 4;
    const path = (series: Array<number | null>): string => {
        let d = "";
        let pen = false;
        series.forEach((v, i) => {
            if (v === null) {
                pen = false;
                return;
            }
            d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
            pen = true;
        });
        return d.trim();
    };
    const latest = trend.days[last];
    const first = trend.days[0];
    return (
        <figure className="trend">
            <svg
                viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
                preserveAspectRatio="none"
                role="img"
                aria-label={`${trend.kpi_label} over the last ${trend.days.length} days, latest ${compactIndian(latest.value)}`}
            >
                <path className="trend-earlier" d={path(earlier)} />
                <path className="trend-line" d={path(current)} />
                {latest.value !== null && <circle className="trend-dot" cx={x(last)} cy={y(latest.value)} r={2.5} />}
            </svg>
            <figcaption>
                <span>{shortDay(first.day)}</span>
                <span>
                    {shortDay(latest.day)}: <strong>{compactIndian(latest.value)}</strong>
                </span>
            </figcaption>
            <p className="note">
                Solid: the last {trend.days.length} days. Dashed: the same days a month earlier. Range{" "}
                {compactIndian(low)} to {compactIndian(high)}.
            </p>
        </figure>
    );
}

function shortDay(day: string): string {
    const [year, month, date] = day.slice(0, 10).split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, date)).toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        timeZone: "UTC",
    });
}
