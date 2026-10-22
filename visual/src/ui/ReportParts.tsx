// Pieces shared by the report views (schema 2: ReportView, schema 3: SalesReportView).

import * as React from "react";

import { InactiveBreakdown, Priority, Suggestion, Urgency } from "../api/types";
import { formatIndian } from "../reports/format";

export const PRIORITIES: Priority[] = ["high", "medium", "low"];
export const PRIORITY_LABEL: Record<Priority, string> = { high: "High", medium: "Medium", low: "Low" };
const URGENCY_LABEL: Record<Urgency, string> = { today: "Today", this_week: "This week", this_month: "This month" };
const MAX_INACTIVE = 10;


/** "2026-10-12" -> "12 Oct 2026" (dates are calendar days; no time-zone shift). */
export function formatDay(day: string | null): string {
    if (!day) return "";
    const [year, month, date] = day.slice(0, 10).split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, date)).toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
    });
}

export function formatTime(moment: string | null, timeZone?: string): string {
    if (!moment) return "";
    const options: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" };
    try {
        return new Date(moment).toLocaleString("en-GB", { ...options, timeZone });
    } catch {
        return new Date(moment).toLocaleString("en-GB", options); // unknown time zone
    }
}

/** One action point: what, where, who, by when, and why. `table`: which report table it comes from. */
export function ActionCard({ suggestion, table }: { suggestion: Suggestion; table?: string }): React.JSX.Element {
    const meta = [
        suggestion.where ? `Where: ${suggestion.where}` : "",
        suggestion.owner ? `Who: ${suggestion.owner}` : "",
        suggestion.urgency ? `By: ${URGENCY_LABEL[suggestion.urgency]}` : "",
    ].filter(Boolean);
    return (
        <article className="suggestion">
            <p>
                <span className={`badge ${suggestion.priority}`}>{PRIORITY_LABEL[suggestion.priority]}</span>{" "}
                <strong>{suggestion.title}</strong>{" "}
                <span className="source">({[suggestion.model_name, table].filter(Boolean).join(" · ")})</span>
            </p>
            {meta.length > 0 && <p className="source">{meta.join(" · ")}</p>}
            <p>{suggestion.reason}</p>
            {suggestion.notes && <p className="note">{suggestion.notes}</p>}
        </article>
    );
}

export function Inactive({ blocks }: { blocks: InactiveBreakdown[] }): React.JSX.Element {
    return (
        <>
            <h5>No activity now (had a value in the previous {blocks[0].lookback_days} days)</h5>
            {blocks.map((block) => (
                <div key={`${block.by}|${block.kpi_label}`} className="inactive">
                    <p className="note">
                        {block.kpi_label} by {block.by}: {block.total} item(s)
                    </p>
                    <ul>
                        {block.items.slice(0, MAX_INACTIVE).map((item) => (
                            <li key={item.item}>
                                {item.item}: {formatIndian(item.baseline_value)} before, none on the latest day
                            </li>
                        ))}
                        {block.total > Math.min(block.items.length, MAX_INACTIVE) && (
                            <li className="note">and {block.total - Math.min(block.items.length, MAX_INACTIVE)} more</li>
                        )}
                    </ul>
                </div>
            ))}
        </>
    );
}
