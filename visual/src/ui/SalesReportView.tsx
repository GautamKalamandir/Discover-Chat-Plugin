// A /sales-summary report in the chat (ADR 0016, design §10), in the same order as the PDF: the MIS
// summary bullets (▲ green / ▼ red / ● neutral, colour set by the server from the facts), the
// data sources used, one collapsible part per data source with its four tables (coloured changes,
// findings, action points, the agent's drill-downs), the trend and "no activity now", then the
// combined action list. All text is rendered as text (never HTML).

import * as React from "react";

import {
    Bullet,
    Lens,
    LensTable,
    ReportCell,
    ReportTable,
    SalesAction,
    SalesReportContent,
    SalesSection,
    Suggestion,
    Tone,
} from "../api/types";
import { Markdown } from "./components";
import { TrendChart } from "./ReportCharts";
import { ActionCard, formatDay, formatTime, Inactive, PRIORITIES, PRIORITY_LABEL } from "./ReportParts";

/** The four tables (backend app/reports/sales/lenses.py TITLES). */
export const LENS_TITLES: Record<Lens, string> = {
    lob_growth: "LOB-wise growth and degrowth",
    subcategory_loss: "Sub-category loss by store and month",
    stock_turn: "Stock turn",
    price_range: "Sales by price range",
};

const MARKS: Record<Tone, string> = { good: "▲", bad: "▼", neutral: "●" };
const MARK_LABELS: Record<Tone, string> = { good: "better", bad: "needs attention", neutral: "" };

export function SalesReport({ content }: { content: SalesReportContent }): React.JSX.Element {
    const withFigures = content.sales_sections.filter((s) => s.status === "ok");
    const without = content.sales_sections.filter((s) => s.status !== "ok");
    return (
        <>
            {content.models_used.length > 0 && (
                <p className="report-meta">
                    Data sources used:{" "}
                    {content.models_used
                        .map((m) => `${m.name} (${m.lenses.map((l) => LENS_TITLES[l]).join(", ")})`)
                        .join("; ")}
                </p>
            )}

            <h4>MIS summary</h4>
            {content.bullets.length ? (
                <ul className="bullets">
                    {content.bullets.map((b, i) => (
                        <SummaryBullet key={i} bullet={b} />
                    ))}
                </ul>
            ) : (
                <p className="note">No summary could be written today; see the tables below.</p>
            )}

            {withFigures.map((section) => (
                <SectionView key={section.dataset_id} section={section} timeZone={content.timezone} />
            ))}

            <h4>Action points</h4>
            <Actions actions={content.actions} />

            {without.length > 0 && (
                <details className="report-section without">
                    <summary>Data sources without figures today ({without.length})</summary>
                    <ul>
                        {without.map((s) => (
                            <li key={s.dataset_id}>
                                <strong>{s.name}</strong>: {s.message}
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </>
    );
}

/** One bullet: its mark and colour say whether it is good news; key phrases in bold. */
export function SummaryBullet({ bullet }: { bullet: Bullet }): React.JSX.Element {
    const label = MARK_LABELS[bullet.tone];
    return (
        <li className={`bullet ${bullet.tone}`}>
            <span className="mark" aria-label={label || undefined} aria-hidden={label ? undefined : true}>
                {MARKS[bullet.tone]}
            </span>{" "}
            <span>{emphasised(bullet.text, bullet.emphasis)}</span>
        </li>
    );
}

/** The text with each emphasised phrase (first occurrence, when present) in <strong>. */
export function emphasised(text: string, phrases: string[]): React.ReactNode[] {
    let parts: Array<string | React.JSX.Element> = [text];
    phrases.forEach((phrase, n) => {
        if (!phrase) return;
        let done = false;
        parts = parts.flatMap((part) => {
            if (done || typeof part !== "string") return [part];
            const at = part.indexOf(phrase);
            if (at < 0) return [part];
            done = true;
            return [part.slice(0, at), <strong key={`e${n}`}>{phrase}</strong>, part.slice(at + phrase.length)];
        });
    });
    return parts.filter((p) => p !== "");
}

function SectionView({ section, timeZone }: { section: SalesSection; timeZone: string }): React.JSX.Element {
    const when = [
        section.data_date ? `figures up to ${formatDay(section.data_date)}` : "",
        section.refreshed_at ? `refreshed ${formatTime(section.refreshed_at, timeZone)}` : "",
    ].filter(Boolean);
    return (
        <details className="report-section ok" open>
            <summary>
                {section.name}
                {when.length > 0 && <span className="source"> · {when.join(", ")}</span>}
            </summary>
            {section.tables.map((table) => (
                <LensView key={table.lens} table={table} />
            ))}
            {section.trend && (
                <>
                    <h5>Daily trend: {section.trend.kpi_label}</h5>
                    <TrendChart trend={section.trend} />
                </>
            )}
            {section.inactive.length > 0 && <Inactive blocks={section.inactive} />}
            {section.notes.map((note) => (
                <p key={note} className="note">
                    {note}
                </p>
            ))}
        </details>
    );
}

function LensView({ table }: { table: LensTable }): React.JSX.Element {
    if (table.status !== "ok") {
        return (
            <div className="lens">
                <h5>{table.title}</h5>
                <p className="note">{table.message}</p>
            </div>
        );
    }
    return (
        <div className="lens">
            <h5>{table.title}</h5>
            {table.main && <Grid table={table.main} />}
            {table.findings && <Markdown text={table.findings} />}
            {table.suggestions.map((s, i) => (
                <ActionCard key={i} suggestion={s} />
            ))}
            {table.drilldowns.length > 0 && (
                <details className="drilldowns">
                    <summary>How the agent looked into it ({table.drilldowns.length})</summary>
                    {table.drilldowns.map((d, i) => (
                        <div key={i}>
                            <p className="drill-title">{d.title}</p>
                            <Grid table={d} />
                        </div>
                    ))}
                </details>
            )}
            {table.notes.map((note) => (
                <p key={note} className="note">
                    {note}
                </p>
            ))}
            {table.cut_short && (
                <p className="note">The analysis of this table was cut short: its query limit was reached.</p>
            )}
        </div>
    );
}

/** A table from the report: numbers right-aligned, each change coloured by the server's tone. */
export function Grid({ table }: { table: ReportTable }): React.JSX.Element {
    return (
        <div className="result-scroll">
            <table className="grid">
                <thead>
                    <tr>
                        {table.columns.map((c, i) => (
                            <th key={i} scope="col" className={i ? "num" : undefined}>
                                {c}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {table.rows.map((row, r) => (
                        <tr key={r}>
                            {row.map((cell, i) => (
                                <GridCell key={i} cell={cell} first={i === 0} />
                            ))}
                        </tr>
                    ))}
                </tbody>
            </table>
            {table.note && <p className="note">{table.note}</p>}
        </div>
    );
}

function GridCell({ cell, first }: { cell: ReportCell; first: boolean }): React.JSX.Element {
    const classes = [first ? "" : "num", cell.tone && cell.tone !== "neutral" ? cell.tone : ""].filter(Boolean);
    return <td className={classes.join(" ") || undefined}>{cell.text}</td>;
}

function Actions({ actions }: { actions: SalesAction[] }): React.JSX.Element {
    if (!actions.length) return <p className="note">No action points for today.</p>;
    return (
        <>
            {PRIORITIES.map((priority) => {
                const chosen = actions.filter((a) => a.priority === priority);
                return chosen.length ? (
                    <div key={priority} className={`priority ${priority}`}>
                        <p className="source">
                            {PRIORITY_LABEL[priority]} priority ({chosen.length})
                        </p>
                        {chosen.map((a, i) => (
                            <ActionCard key={i} suggestion={a as Suggestion} table={LENS_TITLES[a.lens]} />
                        ))}
                    </div>
                ) : null;
            })}
        </>
    );
}
