// /summary reports in the visual (ADR 0015): API client, chat state, Download PDF, formatting.

import { describe, expect, it, vi } from "vitest";

import { ApiClient, toBase64 } from "../src/api/client";
import { ReportDetail, ReportEvent } from "../src/api/types";
import { PRIVILEGE_ALLOWED, PRIVILEGE_DISABLED_BY_ADMIN } from "../src/auth/tokenProvider";
import { chatReducer, initialState, reportStageLabel, SUMMARY_STOPPED } from "../src/chat/store";
import { DownloadHost, downloadReport } from "../src/reports/download";
import { compactIndian, formatIndian, isGood, percentWords, tone } from "../src/reports/format";
import { changeText, formatDay } from "../src/ui/ReportView";

const API = "https://api.test";

function client(fetchMock: ReturnType<typeof vi.fn>): ApiClient {
    return new ApiClient(API, { getToken: async () => "t" }, fetchMock as unknown as typeof fetch);
}

function json(body: unknown): Response {
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

function call(fetchMock: ReturnType<typeof vi.fn>, index = 0): [string, RequestInit] {
    return fetchMock.mock.calls[index] as unknown as [string, RequestInit];
}

const DETAIL = {
    id: "r1",
    status: "ready",
    report_date: "2026-10-12",
    created_at: "2026-10-13T03:05:00Z",
    completed_at: "2026-10-13T03:06:00Z",
    headline: "All good.",
    models: [],
    suggestion_counts: { high: 0, medium: 0, low: 0 },
    error_message: null,
    content: null,
} as ReportDetail;

// --- API client --------------------------------------------------------------------------------------

describe("ApiClient reports", () => {
    it("starts or reuses today's report", async () => {
        const fetchMock = vi.fn(async () => json({ report_id: "r1", status: "building", reused: false }));

        const started = await client(fetchMock).startSummary(true);

        expect(started.report_id).toBe("r1");
        const [url, init] = call(fetchMock);
        expect(url).toBe(`${API}/api/v1/reports/summary`);
        expect(init.method).toBe("POST");
        expect(JSON.parse(String(init.body))).toEqual({ regenerate: true });
    });

    it("streams the build's progress as typed events", async () => {
        const body =
            'event: progress\ndata: {"stage":"querying","model_name":"Sales"}\n\n: ping\n\n' +
            'event: model_done\ndata: {"model_name":"Sales","status":"ok"}\n\n' +
            'event: done\ndata: {"status":"ready","report_id":"r1"}\n\n';
        const fetchMock = vi.fn(async () => new Response(body));

        const events: ReportEvent[] = [];
        for await (const event of client(fetchMock).reportEvents("r1")) events.push(event);

        expect(events.map((e) => e.type)).toEqual(["progress", "model_done", "done"]);
        expect(events[0]).toEqual({ type: "progress", stage: "querying", model_name: "Sales" });
        expect(call(fetchMock)[0]).toBe(`${API}/api/v1/reports/r1/events`);
    });

    it("reads the history, a report and a one-time link", async () => {
        const fetchMock = vi
            .fn()
            .mockResolvedValueOnce(json({ reports: [DETAIL] }))
            .mockResolvedValueOnce(json(DETAIL))
            .mockResolvedValueOnce(json({ url_path: "/api/v1/reports/download/tok", expires_in: 60 }));
        const api = client(fetchMock);

        expect(await api.listReports()).toEqual([DETAIL]);
        expect((await api.getReport("r1")).id).toBe("r1");
        const link = await api.downloadLink("r1");

        expect(api.url(link.url_path)).toBe(`${API}/api/v1/reports/download/tok`);
        expect(call(fetchMock, 2)[1].method).toBe("POST");
        expect(call(fetchMock, 2)[0]).toBe(`${API}/api/v1/reports/r1/download-link`);
    });

    it("returns the PDF as base64 for Power BI's download API", async () => {
        const pdf = new Uint8Array([37, 80, 68, 70, 45, 49, 46, 52]); // "%PDF-1.4"
        const fetchMock = vi.fn(async () => new Response(pdf));

        const base64 = await client(fetchMock).reportPdf("r 1");

        expect(atob(base64)).toBe("%PDF-1.4");
        expect(call(fetchMock)[0]).toBe(`${API}/api/v1/reports/r%201/pdf`); // ids are escaped
    });

    it("encodes large files without exceeding argument limits", () => {
        const bytes = new Uint8Array(200_000).map((_, i) => i % 256);

        const decoded = atob(toBase64(bytes));

        expect(decoded.length).toBe(bytes.length);
        expect(decoded.charCodeAt(199_999)).toBe(199_999 % 256);
    });
});

// --- chat state --------------------------------------------------------------------------------------

describe("chatReducer: /summary", () => {
    it("shows progress, then the report, and frees the composer", () => {
        let state = chatReducer(initialState, { type: "summary_asked", id: "1", text: "/summary" });
        expect(state.busy).toBe(true);
        expect(state.messages.map((m) => [m.role, m.kind])).toEqual([
            ["user", "question"],
            ["assistant", "report"],
        ]);
        expect(state.messages[1].report?.progress).toBe("Starting your report…");

        state = chatReducer(state, { type: "summary_progress", label: "Querying Sales…" });
        expect(state.messages[1].report?.progress).toBe("Querying Sales…");

        state = chatReducer(state, { type: "summary_ready", detail: DETAIL, history: [DETAIL] });
        expect(state.busy).toBe(false);
        expect(state.messages[1].pending).toBe(false);
        expect(state.messages[1].report).toEqual({ detail: DETAIL, history: [DETAIL], progress: null });
    });

    it("says the build continues when the user stops watching", () => {
        let state = chatReducer(initialState, { type: "summary_asked", id: "1", text: "/summary" });

        state = chatReducer(state, { type: "summary_stopped" });

        expect(state.messages[1]).toMatchObject({ kind: "notice", text: SUMMARY_STOPPED, pending: false });
        expect(state.messages[1].report).toBeUndefined();
    });

    it("turns a failed build into an error message", () => {
        let state = chatReducer(initialState, { type: "summary_asked", id: "1", text: "/summary" });

        state = chatReducer(state, { type: "failed", message: "I couldn't load any of your data." });

        expect(state.messages[1]).toMatchObject({ kind: "error", text: "I couldn't load any of your data." });
    });

    it("words each build stage for the user", () => {
        expect(reportStageLabel({ type: "progress", stage: "explaining", model_name: "Sales" })).toBe(
            "Looking at what changed in Sales…",
        );
        expect(reportStageLabel({ type: "progress", stage: "summarising" })).toBe("Writing the summary…");
        expect(reportStageLabel({ type: "progress", stage: "something_new" })).toBe("Preparing your report…");
        expect(reportStageLabel({ type: "progress", stage: "querying", model_name: "Sales", done: 12, total: 38 })).toBe(
            "Querying Sales… (12 of 38 done)",
        );
        expect(reportStageLabel({ type: "progress", stage: "querying", model_name: "Sales", done: 0, total: 1 })).toBe(
            "Querying Sales…", // a single data source needs no count
        );
        expect(reportStageLabel({ type: "model_done", model_name: "Sales", status: "ok" })).toBeNull();
    });
});

// --- Download PDF ------------------------------------------------------------------------------------

describe("downloadReport", () => {
    function setup(status: number | Error, completed = true) {
        const api = {
            reportPdf: vi.fn(async () => "JVBERi0="),
            downloadLink: vi.fn(async () => ({ url_path: "/api/v1/reports/download/tok", expires_in: 60 })),
            url: (path: string) => `${API}${path}`,
        } as unknown as ApiClient;
        const service = {
            exportStatus: vi.fn(async () => {
                if (status instanceof Error) throw status;
                return status as never;
            }),
            exportVisualsContentExtended: vi.fn(async () => ({ downloadCompleted: completed, fileName: "x" })),
        };
        const host: DownloadHost = { downloadService: service as never, launchUrl: vi.fn() };
        return { api, service, host };
    }

    it("uses Power BI's download API when the admin allows it", async () => {
        const { api, service, host } = setup(PRIVILEGE_ALLOWED);

        expect(await downloadReport(api, host, "r1", "2026-10-12")).toBe("downloaded");
        expect(service.exportVisualsContentExtended).toHaveBeenCalledWith(
            "JVBERi0=",
            "summary-2026-10-12.pdf",
            "base64",
            "Daily summary report",
        );
        expect(host.launchUrl).not.toHaveBeenCalled();
        expect(api.downloadLink).not.toHaveBeenCalled();
    });

    it("respects the user declining Power BI's prompt", async () => {
        const { api, host } = setup(PRIVILEGE_ALLOWED, false);

        expect(await downloadReport(api, host, "r1", "2026-10-12")).toBe("cancelled");
        expect(host.launchUrl).not.toHaveBeenCalled();
    });

    it.each([
        ["the admin switch is off", PRIVILEGE_DISABLED_BY_ADMIN],
        ["the status can't be read", new Error("not supported")],
    ])("opens a one-time link when %s", async (_, status) => {
        const { api, service, host } = setup(status);

        expect(await downloadReport(api, host, "r1", "2026-10-12")).toBe("opened");
        expect(service.exportVisualsContentExtended).not.toHaveBeenCalled();
        expect(host.launchUrl).toHaveBeenCalledWith(`${API}/api/v1/reports/download/tok`);
    });

    it("falls back to the link when the download API fails", async () => {
        const { api, service, host } = setup(PRIVILEGE_ALLOWED);
        service.exportVisualsContentExtended.mockRejectedValueOnce(new Error("host error"));

        expect(await downloadReport(api, host, "r1", "2026-10-12")).toBe("opened");
        expect(host.launchUrl).toHaveBeenCalledTimes(1);
    });

    it("opens the link in hosts without a download service", async () => {
        const { api } = setup(PRIVILEGE_ALLOWED);
        const launchUrl = vi.fn();

        expect(await downloadReport(api, { launchUrl }, "r1", "2026-10-12")).toBe("opened");
        expect(launchUrl).toHaveBeenCalledTimes(1);
    });
});

// --- formatting --------------------------------------------------------------------------------------

describe("report formatting", () => {
    it("uses the Indian grouping, like the PDF", () => {
        expect(formatIndian(147615304.7)).toBe("14,76,15,304.70");
        expect(formatIndian(1234567)).toBe("12,34,567");
        expect(formatIndian(100000)).toBe("1,00,000");
        expect(formatIndian(999)).toBe("999");
        expect(formatIndian(-194114359.36)).toBe("-19,41,14,359.36");
        expect(formatIndian(null)).toBe("n/a");
        expect(compactIndian(147615304.7)).toBe("14.76 Cr");
        expect(compactIndian(1476153)).toBe("14.76 lakh");
        expect(compactIndian(53200.4)).toBe("53,200.40");
    });

    it("words changes without signs or double negatives", () => {
        expect(percentWords(-7.95)).toBe("down 7.95%");
        expect(percentWords(2)).toBe("up 2.00%");
        expect(percentWords(0)).toBe("no change");
        expect(percentWords(null)).toBe("");
        expect(changeText({ change: -1500, percent_change: null })).toBe("down 1,500");
        expect(changeText({ change: 5, percent_change: 2 })).toBe("up 2.00%");
    });

    it("colours a change by whether it is good news for that KPI", () => {
        expect(tone(isGood(5, true))).toBe("good");
        expect(tone(isGood(-5, true))).toBe("bad");
        expect(tone(isGood(5, false))).toBe("bad"); // e.g. returns going up
        expect(tone(isGood(-5, false))).toBe("good");
        expect(tone(isGood(0, true))).toBe("neutral");
    });

    it("shows calendar days without a time-zone shift", () => {
        expect(formatDay("2026-10-12")).toBe("12 Oct 2026");
        expect(formatDay("2026-01-01T00:00:00")).toBe("01 Jan 2026");
        expect(formatDay(null)).toBe("");
    });
});
