// Reports in the chat (ADR 0015, 0016): progress, the report (schema 2, and schema 3 for /sales-summary),
// Download PDF, Regenerate, previous reports.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api/client";
import { ReportDetail, ReportEvent, ReportItem, SalesReportContent, SummaryReportContent } from "../src/api/types";
import { SUMMARY_STOPPED } from "../src/chat/store";
import { ConversationStore } from "../src/storage";
import { App, AppProps } from "../src/ui/App";

afterEach(cleanup);

const THEME = { foreground: "#000", background: "#fff", accent: "#00f", highContrast: false };
const MEMORY: ConversationStore = { load: async () => null, save: async () => undefined };

const CONTENT: SummaryReportContent = {
    report_id: "r1",
    generated_at: "2026-10-13T03:05:00Z",
    timezone: "Asia/Kolkata",
    user_name: "User A",
    summary: "Net sales were **1,200.50** on 12 Oct 2026.",
    notes: [],
    sections: [
        {
            dataset_id: "hr-ds",
            name: "HR",
            status: "no_data",
            message: "This data source has no date and measures the daily report can use.",
            data_date: null,
            refreshed_at: null,
            kpis: [],
            focus: [],
            headline: null,
            narrative: null,
            suggestions: [],
            assumptions: [],
        },
        {
            dataset_id: "sales-ds",
            name: "Sales",
            status: "ok",
            message: null,
            data_date: "2026-10-12",
            refreshed_at: "2026-10-13T00:40:00Z",
            kpis: [
                {
                    id: "k1",
                    label: "Net sales",
                    value: 1200.5,
                    comparisons: [
                        {
                            fact_id: "k1.week_ago",
                            baseline: "week_ago",
                            label: "vs 05 Oct 2026",
                            current_value: 1200.5,
                            baseline_value: 1000,
                            change: 200.5,
                            percent_change: 20.05,
                        },
                    ],
                },
            ],
            focus: [
                {
                    id: "f1",
                    kpi_label: "Net sales",
                    baseline_period: "05 Oct 2026",
                    current_period: "12 Oct 2026",
                    baseline_value: 1000,
                    current_value: 1200.5,
                    change: 200.5,
                    percent_change: 20.05,
                    notes: [],
                    breakdowns: [
                        {
                            by: "Store",
                            biggest_decreases: [],
                            biggest_increases: [
                                {
                                    item: "Store B",
                                    baseline_value: 100,
                                    current_value: 300.5,
                                    change: 200.5,
                                    percent_change: 200.5,
                                    share_of_total_change: 100,
                                },
                            ],
                        },
                    ],
                },
            ],
            headline: "Net sales rose against last week.",
            narrative: "- Store B drove the rise.",
            suggestions: [
                {
                    priority: "high",
                    title: "Check stock at Store B",
                    reason: "Store B explains 100% of the rise.",
                    notes: "Confirm the stock levels for the coming week.",
                    evidence: ["f1"],
                    model_name: "Sales",
                },
                {
                    priority: "low",
                    title: "Keep an eye on <b>returns</b>",
                    reason: "Small change.",
                    notes: "",
                    evidence: ["k1.week_ago"],
                    model_name: "Sales",
                },
            ],
            assumptions: [],
        },
    ],
};

function item(id: string, date: string, extra: Partial<ReportItem> = {}): ReportItem {
    return {
        id,
        status: "ready",
        report_date: date,
        created_at: `${date}T03:05:00Z`,
        completed_at: `${date}T03:06:00Z`,
        headline: "Net sales were up.",
        models: [],
        suggestion_counts: { high: 1, medium: 0, low: 1 },
        error_message: null,
        ...extra,
    };
}

function detail(id = "r1", date = "2026-10-12", extra: Partial<ReportDetail> = {}): ReportDetail {
    return { ...item(id, date), content: { ...CONTENT, report_id: id }, ...extra };
}

async function* events(...list: ReportEvent[]): AsyncGenerator<ReportEvent> {
    for (const event of list) yield event;
}

function fakeClient(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): ApiClient {
    return {
        session: vi.fn(async () => ({ user: { object_id: "user-a", tenant_id: "t", username: null, display_name: "User A" } })),
        accessibleModels: vi.fn(async () => []),
        getSession: vi.fn(),
        ask: vi.fn(),
        startSummary: vi.fn(async () => ({ report_id: "r1", status: "building", reused: false })),
        reportEvents: vi.fn(() =>
            events(
                { type: "progress", stage: "querying", model_name: "Sales" },
                { type: "done", status: "ready", report_id: "r1" },
            ),
        ),
        getReport: vi.fn(async (id: string) => (id === "r0" ? detail("r0", "2026-10-11") : detail())),
        listReports: vi.fn(async () => [item("r1", "2026-10-12"), item("r0", "2026-10-11")]),
        ...overrides,
    } as unknown as ApiClient;
}

async function summary(props: Partial<AppProps> = {}, typed = "/summary") {
    const client = (props.client as ApiClient | undefined) ?? fakeClient();
    const all: AppProps = {
        client,
        conversations: MEMORY,
        modelId: "sales-ds",
        reportFilters: [],
        title: "",
        theme: THEME,
        ...props,
    };
    render(<App {...all} />);
    await screen.findByText("Signed in as User A");
    fireEvent.change(screen.getByLabelText("Ask a question about your data"), { target: { value: typed } });
    fireEvent.click(screen.getByLabelText("Send question"));
    return client;
}

describe("/summary", () => {
    it("is answered with a report, never by the chat agent", async () => {
        const client = await summary({}, "  /SUMMARY ");

        await screen.findByText("Check stock at Store B");
        expect(client.ask).not.toHaveBeenCalled();
        expect(client.startSummary).toHaveBeenCalledWith(false);
        expect(client.reportEvents).toHaveBeenCalledWith("r1", expect.any(AbortSignal));
        expect(screen.getAllByText("1,200.50").some((e) => e.tagName === "STRONG")).toBe(true); // summary
        expect(screen.getByText("High").className).toBe("badge high");
        expect(screen.getByText("Low").className).toBe("badge low");
        // Text from the report is shown as text, never as markup.
        expect(screen.getByText("Keep an eye on <b>returns</b>")).toBeTruthy();
        expect(screen.getAllByText("Sales").some((e) => e.tagName === "SUMMARY")).toBe(true);
        // In the at-a-glance table and in "Data sources without figures today".
        const noData = screen.getAllByText(/This data source has no date and measures/);
        expect(noData.map((e) => e.tagName).sort()).toEqual(["LI", "TD"]);
        // KPI table: the comparison's own value, and the change in words coloured by direction.
        expect(screen.getAllByText("1,000").some((e) => e.tagName === "TD")).toBe(true);
        expect(screen.getAllByText("up 20.05%").some((e) => e.className === "good")).toBe(true);
        expect(screen.getAllByText("+200.50 (100%)").length).toBeGreaterThan(0); // Store B's bar
    });

    it("also starts from /sales-summary (ADR 0016), never the chat agent", async () => {
        const client = await summary({}, " /Sales-Summary ");

        await screen.findByText("Check stock at Store B");
        expect(client.ask).not.toHaveBeenCalled();
        expect(client.startSummary).toHaveBeenCalledWith(false);
    });

    it("shows what the build is doing while it runs", async () => {
        let finish: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (finish = resolve));
        const client = fakeClient({
            reportEvents: vi.fn(async function* (): AsyncGenerator<ReportEvent> {
                yield { type: "progress", stage: "explaining", model_name: "Sales" };
                await held;
                yield { type: "done", status: "ready", report_id: "r1" };
            }),
        });

        await summary({ client });

        expect((await screen.findByRole("status")).textContent).toBe("Looking at what changed in Sales…");
        expect(screen.getByLabelText("Stop answering")).toBeTruthy();
        finish();
        await screen.findByText("Check stock at Store B");
    });

    it("shows today's ready report at once", async () => {
        const client = fakeClient({
            startSummary: vi.fn(async () => ({ report_id: "r1", status: "ready", reused: true })),
        });

        await summary({ client });

        await screen.findByText("Check stock at Store B");
        expect(client.reportEvents).not.toHaveBeenCalled();
    });

    it("downloads the PDF through the visual and says how it went", async () => {
        const download = vi.fn(async () => "downloaded" as const);

        await summary({ download });
        fireEvent.click(await screen.findByText("Download PDF"));

        expect(await screen.findByText("Downloaded.")).toBeTruthy();
        expect(download).toHaveBeenCalledWith("r1", "2026-10-12");
    });

    it("shows a download problem in the report", async () => {
        const download = vi.fn(async () => Promise.reject(new Error("This report was not found or has expired.")));

        await summary({ download });
        fireEvent.click(await screen.findByText("Download PDF"));

        expect(await screen.findByText("This report was not found or has expired.")).toBeTruthy();
    });

    it("regenerates on request", async () => {
        const client = await summary();

        fireEvent.click(await screen.findByText("Regenerate"));

        await waitFor(() => expect(client.startSummary).toHaveBeenLastCalledWith(true));
        expect(await screen.findByText("Regenerate the report")).toBeTruthy();
    });

    it("opens and downloads previous reports", async () => {
        const download = vi.fn(async () => "opened" as const);
        const client = await summary({ download });

        fireEvent.click(await screen.findByText("Previous reports (1)"));
        const buttons = screen.getAllByRole("button", { name: "PDF" });
        fireEvent.click(buttons[0]);
        await waitFor(() => expect(download).toHaveBeenCalledWith("r0", "2026-10-11"));
        fireEvent.click(screen.getByRole("button", { name: "Open" }));

        expect(await screen.findByText("Open the report from 11 Oct 2026")).toBeTruthy();
        expect(client.getReport).toHaveBeenLastCalledWith("r0");
    });

    it("says why a build failed", async () => {
        const client = fakeClient({
            reportEvents: vi.fn(() =>
                events(
                    { type: "error", code: "all_models_failed", message: "I couldn't load any of your data right now." },
                    { type: "done", status: "failed", report_id: "r1" },
                ),
            ),
            getReport: vi.fn(async () =>
                detail("r1", "2026-10-12", {
                    status: "failed",
                    content: null,
                    error_message: "I couldn't load any of your data right now.",
                }),
            ),
        });

        await summary({ client });

        expect(await screen.findByText("I couldn't load any of your data right now.")).toBeTruthy();
        expect(screen.queryByText("Download PDF")).toBeNull();
    });

    it("stops watching without cancelling the build", async () => {
        const client = fakeClient({
            reportEvents: vi.fn(async function* (_id: string, signal: AbortSignal): AsyncGenerator<ReportEvent> {
                yield { type: "progress", stage: "querying", model_name: "Sales" };
                await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new Error("aborted"))));
            }),
        });

        await summary({ client });
        fireEvent.click(await screen.findByLabelText("Stop answering"));

        expect(await screen.findByText(SUMMARY_STOPPED)).toBeTruthy();
        expect(client.getReport).not.toHaveBeenCalled();
    });

    it("tells new users about /sales-summary", async () => {
        render(<App client={fakeClient()} conversations={MEMORY} modelId="sales-ds" reportFilters={[]} title="" theme={THEME} />);

        expect(await screen.findByText(/type \/sales-summary for today's sales report/)).toBeTruthy();
    });
});

// --- report quality (ADR 0015 amendment 1) ------------------------------------------------------

const SALES = CONTENT.sections[1];
const RICH: SummaryReportContent = {
    ...CONTENT,
    overview: [
        {
            dataset_id: "sales-ds", name: "Sales", status: "ok", data_date: "2026-10-12", kpi_label: "Net sales",
            value: 147615304.7, change_percent: -7.95, good: false, alert: "Check stock at Store B",
        },
        {
            dataset_id: "hr-ds", name: "HR", status: "no_data", data_date: null, kpi_label: null, value: null,
            change_percent: null, good: null, alert: "No usable fields.",
        },
    ],
    sections: [
        CONTENT.sections[0],
        {
            ...SALES,
            kpis: [{ ...SALES.kpis[0], higher_is_better: true, achievement_day: 86.6, achievement_mtd: 104.2 }],
            missing_kpis: ["GV Conversion %"],
            trend: {
                kpi_label: "Net sales",
                higher_is_better: true,
                days: [10, 11, 12].map((d) => ({ day: `2026-10-${d}`, value: 1000 * d, earlier_value: 900 * d })),
            },
            performers: SALES.focus[0].breakdowns,
            performers_period: "12 Oct 2026 vs 05 Oct 2026",
            inactive: [
                {
                    by: "Store", kpi_label: "Net sales", lookback_days: 7, total: 3, truncated: false,
                    items: [{ item: "STR - UDHNA", baseline_value: 1250000 }],
                },
            ],
            suggestions: [
                {
                    ...SALES.suggestions[0], where: "Store B", owner: "Store manager", urgency: "today",
                },
            ],
        },
    ],
};

describe("/summary report view", () => {
    async function rich() {
        const client = fakeClient({ getReport: vi.fn(async () => ({ ...detail(), content: RICH })) });
        await summary({ client });
        await screen.findByText("Check stock at Store B", { selector: "strong" });
        return client;
    }

    it("starts with every data source at a glance, coloured by good or bad news", async () => {
        await rich();

        expect(screen.getByText("All data sources at a glance")).toBeTruthy();
        expect(screen.getByText("14.76 Cr").tagName).toBe("TD");
        expect(screen.getByText("down 7.95%").className).toContain("bad");
        expect(screen.getByText("No usable fields.").tagName).toBe("TD");
    });

    it("says where, who and by when for each action point", async () => {
        await rich();

        expect(screen.getByText("Where: Store B · Who: Store manager · By: Today")).toBeTruthy();
    });

    it("shows the charts, target achievement and alerts of each data source", async () => {
        await rich();

        expect(screen.getByRole("img", { name: /Net sales over the last 3 days, latest 12,000/ })).toBeTruthy();
        expect(screen.getByText("Top and bottom by Store: Net sales, 12 Oct 2026 vs 05 Oct 2026")).toBeTruthy();
        expect(screen.getByRole("img", { name: "Target achievement" })).toBeTruthy();
        expect(screen.getByText("day: 86.6%").className).toBe("bad"); // below target
        expect(screen.getByText("month: 104.2%").className).toBe("good");
        expect(screen.getByText("STR - UDHNA: 12,50,000 before, none on the latest day")).toBeTruthy();
        expect(screen.getByText("and 2 more")).toBeTruthy();
        expect(screen.getByText("No value on this day: GV Conversion %.")).toBeTruthy();
        expect(screen.getByRole("img", { name: "Percent change of each KPI" })).toBeTruthy();
    });

    it("counts the data sources done while a report builds", async () => {
        let finish: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (finish = resolve));
        const client = fakeClient({
            reportEvents: vi.fn(async function* (): AsyncGenerator<ReportEvent> {
                yield { type: "progress", stage: "querying", model_name: "Sales", done: 12, total: 38 };
                await held;
                yield { type: "done", status: "ready", report_id: "r1" };
            }),
        });

        await summary({ client });

        expect((await screen.findByRole("status")).textContent).toBe("Querying Sales… (12 of 38 done)");
        finish();
        await screen.findByText("Check stock at Store B");
    });
});

// --- /sales-summary (schema 3, ADR 0016) -------------------------------------------------------------

const SALES_REPORT: SalesReportContent = {
    schema_version: 3,
    kind: "sales",
    report_id: "r1",
    generated_at: "2026-10-13T03:05:00Z",
    timezone: "Asia/Kolkata",
    user_name: "User A",
    bullets: [
        { text: "GOLD fell -20.00% against last year.", tone: "bad", emphasis: ["GOLD", "SILVER"], evidence: ["f1"] },
        { text: "SILVER rose +25.00%.", tone: "good", emphasis: [], evidence: ["f2"] },
        { text: "Stock turn was 1.50.", tone: "neutral", emphasis: [], evidence: ["f3"] },
    ],
    summary: "GOLD fell -20.00% against last year.",
    models_used: [{ dataset_id: "sales-ds", name: "Sales", lenses: ["lob_growth", "stock_turn"], reason: "" }],
    sales_sections: [
        {
            dataset_id: "sales-ds",
            name: "Sales",
            status: "ok",
            message: null,
            data_date: "2026-10-12",
            refreshed_at: null,
            tables: [
                {
                    lens: "lob_growth",
                    title: "LOB-wise growth and degrowth",
                    status: "ok",
                    message: null,
                    main: {
                        title: "t",
                        columns: ["LOB", "MTD", "MTD vs LY", "Status"],
                        rows: [
                            [
                                { text: "GOLD" },
                                { text: "12,34,567.89", fact: "f1" },
                                { text: "-20.00%", tone: "bad", fact: "f1" },
                                { text: "Degrowth", tone: "bad", fact: "f1" },
                            ],
                            [
                                { text: "<b>SILVER</b>" },
                                { text: "500" },
                                { text: "+25.00%", tone: "good" },
                                { text: "Growth", tone: "good" },
                            ],
                        ],
                        note: null,
                    },
                    drilldowns: [
                        {
                            title: "Stores behind GOLD's fall",
                            columns: ["Store", "MTD vs LY"],
                            rows: [[{ text: "Store B" }, { text: "-50.00%", tone: "bad" }]],
                        },
                    ],
                    findings: "- GOLD fell most at Store B.",
                    suggestions: [
                        {
                            priority: "high",
                            title: "Move GOLD stock to Store B",
                            where: "Store B",
                            owner: "Merchandising",
                            urgency: "today",
                            reason: "GOLD fell -20.00%.",
                            notes: "",
                            evidence: ["f1"],
                            model_name: "Sales",
                        },
                    ],
                    cut_short: true,
                    queries_used: 8,
                    notes: ["Not found in the data of Stock: GOLD."],
                },
                {
                    lens: "stock_turn",
                    title: "Stock turn",
                    status: "failed",
                    message: "Couldn't load Sales data right now.",
                    main: null,
                    drilldowns: [],
                    findings: "",
                    suggestions: [],
                    cut_short: false,
                    queries_used: 1,
                    notes: [],
                },
            ],
            trend: null,
            inactive: [],
            notes: [],
        },
        {
            dataset_id: "east-ds",
            name: "Retail East",
            status: "no_data",
            message: "No data was found up to today.",
            data_date: null,
            refreshed_at: null,
            tables: [],
            trend: null,
            inactive: [],
            notes: [],
        },
    ],
    actions: [
        {
            priority: "high",
            title: "Move GOLD stock to Store B",
            where: "Store B",
            owner: "Merchandising",
            urgency: "today",
            reason: "GOLD fell -20.00%.",
            notes: "",
            evidence: ["f1"],
            model_name: "Sales",
            lens: "lob_growth",
        },
    ],
    notes: ["Development data, not real figures."],
};

describe("/sales-summary report view", () => {
    async function sales() {
        const client = fakeClient({ getReport: vi.fn(async () => ({ ...detail(), content: SALES_REPORT })) });
        await summary({ client }, "/sales-summary");
        await screen.findByText("Daily sales MIS summary");
        return client;
    }

    it("opens with the MIS summary, each bullet marked and coloured by its news", async () => {
        await sales();

        const bullets = screen.getAllByRole("listitem").filter((li) => li.className.startsWith("bullet"));
        const [fell, rose, neutral] = bullets;
        expect(fell.className).toBe("bullet bad");
        expect(fell.textContent).toBe("▼ GOLD fell -20.00% against last year.");
        expect(fell.querySelector("[aria-label='needs attention']")).toBeTruthy();
        expect(screen.getAllByText("GOLD").some((e) => e.tagName === "STRONG")).toBe(true); // emphasis
        expect(rose.className).toBe("bullet good");
        expect(rose.textContent?.startsWith("▲")).toBe(true);
        expect(neutral.textContent?.startsWith("●")).toBe(true);
        expect(screen.getByText(/Data sources used: Sales \(LOB-wise growth and degrowth, Stock turn\)/)).toBeTruthy();
    });

    it("shows each table with its changes coloured, findings, action points and drill-downs", async () => {
        await sales();

        expect(screen.getByText("LOB-wise growth and degrowth")).toBeTruthy();
        expect(screen.getByText("-20.00%", { selector: "td" }).className).toBe("num bad");
        expect(screen.getByText("Growth").className).toBe("num good");
        expect(screen.getByText("<b>SILVER</b>").tagName).toBe("TD"); // text, never markup
        expect(screen.getByText("GOLD fell most at Store B.")).toBeTruthy();
        expect(screen.getByText("How the agent looked into it (1)")).toBeTruthy();
        expect(screen.getByText("Stores behind GOLD's fall")).toBeTruthy();
        expect(screen.getByText("Not found in the data of Stock: GOLD.")).toBeTruthy();
        expect(screen.getByText(/was cut short: its query limit was reached/)).toBeTruthy();
        expect(screen.getByText("Couldn't load Sales data right now.")).toBeTruthy(); // a failed table
    });

    it("ends with every action point, naming its data source and table", async () => {
        await sales();

        expect(screen.getByText("High priority (1)")).toBeTruthy();
        expect(screen.getByText("(Sales · LOB-wise growth and degrowth)")).toBeTruthy();
        expect(screen.getAllByText("Where: Store B · Who: Merchandising · By: Today")).toHaveLength(2);
        expect(screen.getByText("Data sources without figures today (1)")).toBeTruthy();
        expect(screen.getByText(/No data was found up to today/)).toBeTruthy();
    });

    it("names the table being worked on while the report builds", async () => {
        let finish: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (finish = resolve));
        const client = fakeClient({
            reportEvents: vi.fn(async function* (): AsyncGenerator<ReportEvent> {
                yield { type: "progress", stage: "stock_turn", model_name: "Sales", done: 3, total: 12 };
                await held;
                yield { type: "done", status: "ready", report_id: "r1" };
            }),
        });

        await summary({ client }, "/sales-summary");

        expect((await screen.findByRole("status")).textContent).toBe("Stock turn: Sales… (3 of 12 done)");
        finish();
        await screen.findByText("Check stock at Store B");
    });
});
