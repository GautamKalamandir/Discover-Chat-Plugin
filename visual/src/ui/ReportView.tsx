// A report in the chat: the header, Download PDF, Regenerate and the previous reports, around the
// report itself. /sales-summary reports (schema 3, ADR 0016) use SalesReportView; reports built
// before it (schema 2, ADR 0015 and amendment 1) keep the view below, in the same order as their
// PDF. All text is rendered as text (never HTML).

import * as React from "react";

import {
    DriverBreakdown,
    FocusAnalysis,
    isSalesReport,
    KpiComparison,
    KpiRow,
    ModelSection,
    OverviewRow,
    ReportDetail,
    ReportItem,
    SummaryReportContent,
} from "../api/types";
import { ReportState } from "../chat/store";
import { DownloadOutcome } from "../reports/download";
import { compactIndian, formatIndian, isGood, percentWords, tone } from "../reports/format";
import { Markdown } from "./components";
import { BarGroup, DivergingBars, TrendChart } from "./ReportCharts";
import { ActionCard, formatDay, formatTime, Inactive, PRIORITIES, PRIORITY_LABEL } from "./ReportParts";
import { SalesReport } from "./SalesReportView";

export { formatDay, formatTime } from "./ReportParts";

export interface ReportActions {
    download: (report: ReportDetail | ReportItem) => Promise<DownloadOutcome>;
    regenerate: () => void;
    open: (report: ReportItem) => void;
}

const DOWNLOAD_TEXT: Record<DownloadOutcome, string> = {
    downloaded: "Downloaded.",
    cancelled: "Download cancelled.",
    opened: "The PDF opened in your browser.",
};
/** The change shown under a value: "down 7.95%" (or the amount when there's no percent). */
export function changeText(c: Pick<KpiComparison, "change" | "percent_change">): string {
    if (c.percent_change !== null) return percentWords(c.percent_change);
    if (c.change === null) return "";
    return c.change === 0 ? "no change" : `${c.change > 0 ? "up" : "down"} ${formatIndian(Math.abs(c.change))}`;
}

export function ReportView({
    state,
    actions,
    busy,
}: {
    state: ReportState;
    actions: ReportActions;
    busy: boolean;
}): React.JSX.Element | null {
    const [download, setDownload] = React.useState<string | null>(null);
    const detail = state.detail;
    const content = detail?.content;
    if (!detail || !content) return null;

    const save = async (report: ReportDetail | ReportItem): Promise<void> => {
        setDownload("Preparing the download…");
        try {
            setDownload(DOWNLOAD_TEXT[await actions.download(report)]);
        } catch (e) {
            setDownload(e instanceof Error && e.message ? e.message : "The PDF couldn't be downloaded.");
        }
    };
    const sales = isSalesReport(content);
    const earlier = state.history.filter((r) => r.id !== detail.id && r.status === "ready");

    return (
        <section className="report" aria-label={sales ? "Daily sales report" : "Daily summary report"}>
            <h3>{sales ? "Daily sales MIS summary" : "Daily MIS summary"}</h3>
            <p className="report-meta">
                Generated {formatTime(content.generated_at, content.timezone)}
                {content.notes.map((note) => (
                    <span key={note}> · {note}</span>
                ))}
            </p>

            {sales ? <SalesReport content={content} /> : <SummaryReport content={content} />}

            <div className="report-actions">
                <button type="button" onClick={() => void save(detail)}>
                    Download PDF
                </button>
                <button type="button" onClick={actions.regenerate} disabled={busy}>
                    Regenerate
                </button>
            </div>
            {download && (
                <p className="note" role="status">
                    {download}
                </p>
            )}

            {earlier.length > 0 && (
                <details className="history">
                    <summary>Previous reports ({earlier.length})</summary>
                    <ul>
                        {earlier.map((r) => (
                            <li key={r.id}>
                                <span>
                                    {formatDay(r.report_date)}, {formatTime(r.created_at, content.timezone).split(", ").pop()}
                                    {r.headline ? ` — ${r.headline}` : ""}
                                </span>
                                <button type="button" onClick={() => actions.open(r)} disabled={busy}>
                                    Open
                                </button>
                                <button type="button" onClick={() => void save(r)}>
                                    PDF
                                </button>
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </section>
    );
}

/** A report built before /sales-summary (schema 2, ADR 0015): kept for the 7-day history. */
function SummaryReport({ content }: { content: SummaryReportContent }): React.JSX.Element {
    const withFigures = content.sections.filter((s) => s.status === "ok");
    const without = content.sections.filter((s) => s.status !== "ok");
    return (
        <>
            <Overview content={content} />

            <h4>Executive summary</h4>
            <Markdown text={content.summary} />

            <ActionPoints sections={content.sections} />

            {withFigures.map((section) => (
                <SectionView key={section.dataset_id} section={section} timeZone={content.timezone} />
            ))}
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

// --- at a glance and action points ------------------------------------------------------------------

function Overview({ content }: { content: SummaryReportContent }): React.JSX.Element | null {
    // Reports from before the at-a-glance table have an empty list: build it from the sections.
    const rows: OverviewRow[] = content.overview?.length
        ? content.overview
        : content.sections.map((s) => ({
            dataset_id: s.dataset_id,
            name: s.name,
            status: s.status,
            data_date: s.data_date,
            kpi_label: s.kpis[0]?.label ?? null,
            value: s.kpis[0]?.value ?? null,
            change_percent: null,
            good: null,
            alert: s.status === "ok" ? null : s.message,
        }));
    if (!rows.length) return null;
    return (
        <>
            <h4>All data sources at a glance</h4>
            <div className="result-scroll">
                <table className="overview">
                    <thead>
                        <tr>
                            <th scope="col">Data source</th>
                            <th scope="col">Main KPI</th>
                            <th scope="col" className="num">
                                Value
                            </th>
                            <th scope="col" className="num">
                                vs previous
                            </th>
                            <th scope="col">Needs attention</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row) => (
                            <tr key={row.dataset_id} className={row.status}>
                                <td>{row.name}</td>
                                <td>{row.status === "ok" ? row.kpi_label : row.status === "no_data" ? "No data" : "Not loaded"}</td>
                                <td className="num">{row.status === "ok" ? compactIndian(row.value) : ""}</td>
                                <td className={`num ${tone(row.good)}`}>{percentWords(row.change_percent)}</td>
                                <td>{row.alert}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </>
    );
}

function ActionPoints({ sections }: { sections: ModelSection[] }): React.JSX.Element {
    const all = sections.flatMap((s) => s.suggestions);
    return (
        <>
            <h4>Action points</h4>
            {all.length ? (
                PRIORITIES.map((priority) => {
                    const chosen = all.filter((s) => s.priority === priority);
                    return chosen.length ? (
                        <div key={priority} className={`priority ${priority}`}>
                            {chosen.map((s, i) => (
                                <ActionCard key={i} suggestion={s} />
                            ))}
                        </div>
                    ) : null;
                })
            ) : (
                <p className="note">No action points for today.</p>
            )}
        </>
    );
}

// --- one data source ---------------------------------------------------------------------------------

function SectionView({ section, timeZone }: { section: ModelSection; timeZone: string }): React.JSX.Element {
    const when = [
        section.data_date ? `data for ${formatDay(section.data_date)}` : "",
        section.refreshed_at ? `refreshed ${formatTime(section.refreshed_at, timeZone)}` : "",
    ].filter(Boolean);
    const main = section.kpis[0];
    const better = main?.higher_is_better ?? true;
    return (
        <details className={`report-section ${section.status}`}>
            <summary>
                {section.name}
                {when.length > 0 && <span className="source"> · {when.join(", ")}</span>}
            </summary>
            {section.headline && (
                <p>
                    <strong>{section.headline}</strong>
                </p>
            )}
            {section.kpis.length > 0 && <KpiTable rows={section.kpis} dataDate={section.data_date} />}
            {(section.missing_kpis ?? []).length > 0 && (
                <p className="note">No value on this day: {(section.missing_kpis ?? []).join(", ")}.</p>
            )}
            <KpiChanges rows={section.kpis} />
            {section.narrative && <Markdown text={section.narrative} />}
            {section.trend && (
                <>
                    <h5>Daily trend: {section.trend.kpi_label}</h5>
                    <TrendChart trend={section.trend} />
                </>
            )}
            {(section.performers ?? []).map((b) => (
                <Performers
                    key={b.by}
                    breakdown={b}
                    title={`Top and bottom by ${b.by}: ${main?.label ?? ""}${section.performers_period ? `, ${section.performers_period}` : ""}`}
                    higherIsBetter={better}
                />
            ))}
            <Targets rows={section.kpis} />
            {(section.inactive ?? []).length > 0 && <Inactive blocks={section.inactive ?? []} />}
            {section.focus.map((f) => (
                <FocusView
                    key={f.id}
                    analysis={f}
                    higherIsBetter={section.kpis.find((k) => k.id === f.kpi_id)?.higher_is_better ?? true}
                />
            ))}
            {section.assumptions.length > 0 && <p className="note">Assumptions: {section.assumptions.join("; ")}</p>}
        </details>
    );
}

function KpiTable({ rows, dataDate }: { rows: KpiRow[]; dataDate: string | null }): React.JSX.Element {
    const has = (b: KpiComparison["baseline"]): boolean => rows.some((r) => r.comparisons.some((c) => c.baseline === b));
    const label = (b: KpiComparison["baseline"]): string =>
        rows.flatMap((r) => r.comparisons).find((c) => c.baseline === b)?.label.replace(/^vs /, "") ?? "";
    const targets = rows.some((r) => r.achievement_day != null || r.achievement_mtd != null);
    const cell = (row: KpiRow, b: KpiComparison["baseline"], show: "baseline" | "current"): React.JSX.Element => {
        const c = row.comparisons.find((x) => x.baseline === b);
        if (!c) return <td className="num" />;
        const good = isGood(c.change, row.higher_is_better ?? true);
        return (
            <td className="num">
                {formatIndian(show === "baseline" ? c.baseline_value : c.current_value)}
                <br />
                <span className={tone(good)}>{changeText(c)}</span>
            </td>
        );
    };
    return (
        <div className="result-scroll">
            <table className="kpis">
                <thead>
                    <tr>
                        <th scope="col">KPI</th>
                        <th scope="col" className="num">
                            {dataDate ? formatDay(dataDate) : "Latest day"}
                        </th>
                        {has("prev_day") && (
                            <th scope="col" className="num">
                                Previous day ({label("prev_day")})
                            </th>
                        )}
                        {has("week_ago") && (
                            <th scope="col" className="num">
                                Same day last week ({label("week_ago")})
                            </th>
                        )}
                        {has("mtd") && (
                            <th scope="col" className="num">
                                Month to date (vs same days last month)
                            </th>
                        )}
                        {targets && (
                            <th scope="col" className="num">
                                Target (day / month)
                            </th>
                        )}
                    </tr>
                </thead>
                <tbody>
                    {rows.map((row) => (
                        <tr key={row.id}>
                            <td>{row.label}</td>
                            <td className="num">{formatIndian(row.value)}</td>
                            {has("prev_day") && cell(row, "prev_day", "baseline")}
                            {has("week_ago") && cell(row, "week_ago", "baseline")}
                            {has("mtd") && cell(row, "mtd", "current")}
                            {targets && (
                                <td className="num">
                                    <Achievement value={row.achievement_day} better={row.higher_is_better ?? true} /> /{" "}
                                    <Achievement value={row.achievement_mtd} better={row.higher_is_better ?? true} />
                                </td>
                            )}
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function Achievement({ value, better }: { value: number | null | undefined; better: boolean }): React.JSX.Element {
    if (value === null || value === undefined) return <span>n/a</span>;
    return <span className={(better ? value >= 100 : value <= 100) ? "good" : "bad"}>{value.toFixed(1)}%</span>;
}

function KpiChanges({ rows }: { rows: KpiRow[] }): React.JSX.Element | null {
    const groups: BarGroup[] = [];
    for (const row of rows) {
        const better = row.higher_is_better ?? true;
        const bars = (["prev_day", "week_ago"] as const)
            .map((b, i) => {
                const c = row.comparisons.find((x) => x.baseline === b);
                if (!c || c.percent_change === null) return null;
                const prefix = i === 0 ? "vs prev. day" : "vs last week";
                return {
                    value: c.percent_change,
                    tone: tone(isGood(c.change, better)),
                    light: i > 0,
                    text: `${prefix}: ${percentWords(c.percent_change)}`,
                };
            })
            .filter((b): b is NonNullable<typeof b> => b !== null);
        if (bars.length) groups.push({ label: row.label, bars });
    }
    if (!groups.length) return null;
    return (
        <>
            <h5>How each KPI changed</h5>
            <DivergingBars groups={groups} label="Percent change of each KPI" />
            <p className="note">Dark: vs the previous day with data. Light: vs the same day last week.</p>
        </>
    );
}

function Performers({
    breakdown,
    title,
    higherIsBetter,
}: {
    breakdown: DriverBreakdown;
    title: string;
    higherIsBetter: boolean;
}): React.JSX.Element | null {
    const items = [...breakdown.biggest_increases, ...[...breakdown.biggest_decreases].reverse()];
    if (!items.length) return null;
    const groups: BarGroup[] = items.map((item) => ({
        label: item.item,
        bars: [
            {
                value: item.change,
                tone: tone(isGood(item.change, higherIsBetter)),
                text:
                    `${item.change > 0 ? "+" : ""}${compactIndian(item.change)}` +
                    (item.share_of_total_change !== null ? ` (${item.share_of_total_change.toFixed(0)}%)` : ""),
            },
        ],
    }));
    return (
        <>
            <h5>{title}</h5>
            <DivergingBars groups={groups} label={title} />
            <p className="note">In brackets: share of the total change.</p>
        </>
    );
}

function Targets({ rows }: { rows: KpiRow[] }): React.JSX.Element | null {
    const picked = rows.filter((r) => r.achievement_day != null || r.achievement_mtd != null);
    if (!picked.length) return null;
    const groups: BarGroup[] = picked.map((row) => {
        const better = row.higher_is_better ?? true;
        const met = (v: number): boolean => (better ? v >= 100 : v <= 100);
        return {
            label: row.label,
            bars: [row.achievement_day, row.achievement_mtd]
                .map((v, i) =>
                    v == null
                        ? null
                        : {
                              value: v,
                              tone: met(v) ? ("good" as const) : ("bad" as const),
                              light: i > 0,
                              text: `${i === 0 ? "day" : "month"}: ${v.toFixed(1)}%`,
                          },
                )
                .filter((b): b is NonNullable<typeof b> => b !== null),
        };
    });
    return (
        <>
            <h5>Target achievement</h5>
            <DivergingBars groups={groups} label="Target achievement" marker={100} />
            <p className="note">Dark: the day. Light: month to date. The line marks 100% of target.</p>
        </>
    );
}

function FocusView({ analysis, higherIsBetter }: { analysis: FocusAnalysis; higherIsBetter: boolean }): React.JSX.Element {
    const good = isGood(analysis.change, higherIsBetter);
    return (
        <div className="focus">
            <h5>
                What changed: {analysis.kpi_label}, {analysis.current_period} vs {analysis.baseline_period}{" "}
                <span className={tone(good)}>{changeText(analysis)}</span>
            </h5>
            {analysis.breakdowns.map((b) => (
                <Performers key={b.by} breakdown={b} title={`By ${b.by}`} higherIsBetter={higherIsBetter} />
            ))}
        </div>
    );
}
