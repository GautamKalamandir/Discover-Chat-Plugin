// Wire types of the backend API (docs/design/phase-9-chat-api.md).

export interface ModelOption {
    id: string;
    name: string;
    domain: string | null;
}

export interface SessionUser {
    object_id: string;
    tenant_id: string;
    username: string | null;
    display_name: string | null;
}

/** An answer the user can click (ADR 0014); the id is only valid for its clarification. */
export interface ClarificationOption {
    id: string;
    label: string;
}

/** A click on an option of the open clarification. */
export interface Choice {
    clarificationId: string;
    optionId: string;
}

export interface StoredMessage {
    id: string;
    role: "user" | "assistant";
    kind: "question" | "answer" | "clarification" | "notice";
    content: string;
    created_at: string;
    clarification_id?: string | null;
    options?: ClarificationOption[];
    /** Only the latest clarification can still be answered by a click. */
    open?: boolean;
}

export interface SessionDetail {
    session_id: string;
    primary_model_id: string | null;
    messages: StoredMessage[];
}

export interface ReportFilter {
    column: string;
    values: Array<string | number | boolean>;
}

export interface TableData {
    model_id: string;
    title: string;
    columns: string[];
    rows: Array<Record<string, unknown>>;
    truncated: boolean;
}

export type StreamEvent =
    | { type: "session"; session_id: string; correlation_id: string | null }
    | { type: "status"; stage: string }
    | ({ type: "table" } & TableData)
    | { type: "token"; text: string }
    | {
          type: "clarification";
          question: string;
          id?: string | null;
          options?: ClarificationOption[];
          allow_free_text?: boolean;
      }
    | { type: "error"; code: string; message: string }
    | { type: "done"; message_id: string | null; query_ids: string[] };

// --- reports (ADR 0015 and 0016, docs/design/phase-15-summary-reports.md §4) ---------------------

export type ReportStatus = "building" | "ready" | "failed";
export type Priority = "high" | "medium" | "low";

export interface SummaryStarted {
    report_id: string;
    status: "building" | "ready";
    /** True when today's report already existed and nothing was rebuilt. */
    reused: boolean;
}

export interface KpiComparison {
    fact_id: string;
    baseline: "prev_day" | "week_ago" | "mtd" | "target_day" | "target_mtd";
    label: string;
    current_value: number | null;
    baseline_value: number | null;
    change: number | null;
    percent_change: number | null;
}

export interface KpiRow {
    id: string;
    label: string;
    value: number | null;
    comparisons: KpiComparison[];
    // Schema 2 (ADR 0015 amendment 1); absent in older reports.
    higher_is_better?: boolean;
    achievement_day?: number | null;
    achievement_mtd?: number | null;
}

export interface DriverItem {
    item: string;
    baseline_value: number;
    current_value: number;
    change: number;
    percent_change: number | null;
    share_of_total_change: number | null;
}

export interface DriverBreakdown {
    by: string;
    biggest_decreases: DriverItem[];
    biggest_increases: DriverItem[];
}

export interface FocusAnalysis {
    id: string;
    kpi_id: string;
    kpi_label: string;
    baseline_period: string;
    current_period: string;
    baseline_value: number;
    current_value: number;
    change: number;
    percent_change: number | null;
    breakdowns: DriverBreakdown[];
    notes: string[];
}

export type Urgency = "today" | "this_week" | "this_month";

export interface Suggestion {
    priority: Priority;
    title: string;
    reason: string;
    notes: string;
    evidence: string[];
    model_name: string;
    where?: string;
    owner?: string;
    urgency?: Urgency;
}

export interface TrendPoint {
    day: string;
    value: number | null;
    earlier_value: number | null;
}

export interface Trend {
    kpi_label: string;
    higher_is_better: boolean;
    days: TrendPoint[];
}

export interface InactiveBreakdown {
    by: string;
    kpi_label: string;
    lookback_days: number;
    items: Array<{ item: string; baseline_value: number }>;
    total: number;
    truncated: boolean;
}

export interface OverviewRow {
    dataset_id: string;
    name: string;
    status: "ok" | "failed" | "no_data";
    data_date: string | null;
    kpi_label: string | null;
    value: number | null;
    change_percent: number | null;
    good: boolean | null;
    alert: string | null;
}

export interface ModelSection {
    dataset_id: string;
    name: string;
    status: "ok" | "failed" | "no_data";
    message: string | null;
    data_date: string | null;
    refreshed_at: string | null;
    kpis: KpiRow[];
    focus: FocusAnalysis[];
    headline: string | null;
    narrative: string | null;
    suggestions: Suggestion[];
    assumptions: string[];
    trend?: Trend | null;
    performers?: DriverBreakdown[];
    performers_period?: string | null;
    inactive?: InactiveBreakdown[];
    missing_kpis?: string[];
}

export interface SummaryReportContent {
    report_id: string;
    generated_at: string;
    timezone: string;
    user_name: string | null;
    summary: string;
    sections: ModelSection[];
    notes: string[];
    overview?: OverviewRow[];
}

// --- /sales-summary reports: report.json schema 3 (ADR 0016, docs/design/phase-16-sales-summary.md §8) ---

export type Lens = "lob_growth" | "subcategory_loss" | "stock_turn" | "price_range";
/** Set by the server from the facts behind it: good news, needs attention, or neither. */
export type Tone = "good" | "bad" | "neutral";

export interface ReportCell {
    text: string;
    tone?: Tone | null;
    fact?: string | null;
}

export interface ReportTable {
    title: string;
    columns: string[];
    rows: ReportCell[][];
    note?: string | null;
}

export interface LensTable {
    lens: Lens;
    title: string;
    status: "ok" | "failed" | "no_data";
    message: string | null;
    main: ReportTable | null;
    drilldowns: ReportTable[];
    findings: string;
    suggestions: Suggestion[];
    cut_short: boolean;
    queries_used: number;
    notes: string[];
}

export interface Bullet {
    text: string;
    tone: Tone;
    emphasis: string[];
    evidence: string[];
}

export interface SalesAction extends Suggestion {
    lens: Lens;
}

export interface SalesSection {
    dataset_id: string;
    name: string;
    status: "ok" | "failed" | "no_data";
    message: string | null;
    data_date: string | null;
    refreshed_at: string | null;
    tables: LensTable[];
    trend: Trend | null;
    inactive: InactiveBreakdown[];
    notes: string[];
}

export interface SalesReportContent {
    schema_version: number;
    kind: "sales";
    report_id: string;
    generated_at: string;
    timezone: string;
    user_name: string | null;
    bullets: Bullet[];
    summary: string;
    models_used: Array<{ dataset_id: string; name: string; lenses: Lens[]; reason: string }>;
    sales_sections: SalesSection[];
    actions: SalesAction[];
    notes: string[];
}

export type ReportContent = SummaryReportContent | SalesReportContent;

/** A /sales-summary report (schema 3), as opposed to one built before it (schema 2). */
export function isSalesReport(content: ReportContent): content is SalesReportContent {
    return (content as SalesReportContent).kind === "sales";
}

export interface ReportItem {
    id: string;
    status: ReportStatus;
    report_date: string;
    created_at: string;
    completed_at: string | null;
    headline: string | null;
    models: Array<{ dataset_id: string; name: string; status: string }>;
    suggestion_counts: Record<Priority, number> | null;
    error_message: string | null;
}

export interface ReportDetail extends ReportItem {
    content: ReportContent | null;
}

/** GET /api/v1/reports/{id}/events */
export type ReportEvent =
    | { type: "progress"; stage: string; model_name?: string; done?: number; total?: number }
    | { type: "model_done"; model_name: string; status: string; done?: number; total?: number }
    | { type: "error"; code: string; message: string }
    | { type: "done"; status: ReportStatus; report_id: string };

export interface DownloadLink {
    url_path: string;
    expires_in: number;
}

/** A refused request (the backend's uniform error envelope). Messages are always user-safe. */
export class ApiError extends Error {
    constructor(
        readonly status: number,
        readonly code: string,
        message: string,
    ) {
        super(message);
        this.name = "ApiError";
    }
}

/** POST /api/v1/diagnostics/run (Phase 2 spikes). */
export interface DiagnosticsStep {
    spike: string;
    name: string;
    status: "ok" | "failed" | "skipped";
    duration_ms: number;
    detail: Record<string, unknown>;
    error: string | null;
}

export interface DiagnosticsSummary {
    bundle_id: string;
    model_id: string;
    sample_dax: string;
    steps: DiagnosticsStep[];
}
