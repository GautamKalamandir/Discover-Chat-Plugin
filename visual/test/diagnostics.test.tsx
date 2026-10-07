import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api/client";
import { DiagnosticsSummary } from "../src/api/types";
import { EntraTokenProvider } from "../src/auth/tokenProvider";
import { dataViewSummary, filterSummary, streamedIncrementally } from "../src/diagnostics/report";
import { App, AppProps } from "../src/ui/App";
import { DiagnosticsContext } from "../src/ui/DiagnosticsPanel";

afterEach(cleanup);

const SUMMARY: DiagnosticsSummary = {
    bundle_id: "20261007T101500123456Z-abcdef0123",
    model_id: "sales-ds",
    sample_dax: "EVALUATE TOPN(5, 'Product')",
    steps: [
        { spike: "S1", name: "sign_in_claims", status: "ok", duration_ms: 1, detail: {}, error: null },
        { spike: "S3", name: "fabric_iq_schema", status: "failed", duration_ms: 40, detail: {}, error: "normalizer recognised no objects" },
    ],
};

function fakeClient() {
    return {
        session: vi.fn(async () => ({ user: { object_id: "u", tenant_id: "t", username: null, display_name: "User A" } })),
        accessibleModels: vi.fn(async () => []),
        getSession: vi.fn(),
        diagnosticsRun: vi.fn(async () => SUMMARY),
        diagnosticsStream: vi.fn(async function* () {
            for (const ms of [5, 305, 610, 2710]) yield ms;
        }),
        diagnosticsVisualReport: vi.fn(async (bundleId: string | null) => ({ bundle_id: bundleId ?? "new-bundle" })),
    };
}

const CONTEXT: DiagnosticsContext = {
    tokenStatus: () => ({ privilegeStatus: 0, expiresAt: null, error: null }),
    visualReport: { host: { hostEnv: null }, reportFilters: [{ column: "Product[LOB]", valueCount: 1 }] },
};

function renderApp(props: Partial<AppProps> = {}) {
    const client = fakeClient();
    render(
        <App
            client={client as unknown as ApiClient}
            conversations={{ load: async () => null, save: async () => undefined }}
            modelId="sales-ds"
            reportFilters={[]}
            title=""
            theme={{ foreground: "#000", background: "#fff", accent: "#00f", highContrast: false }}
            {...props}
        />,
    );
    return client;
}

describe("Diagnostics panel", () => {
    it("is absent from normal builds", async () => {
        renderApp();
        await screen.findByText("Signed in as User A");

        expect(screen.queryByText("Diagnostics (Phase 2)")).toBeNull();
    });

    it("runs the checks, the stream test and saves the visual report into the same bundle", async () => {
        const client = renderApp({ diagnostics: CONTEXT });
        expect(screen.getByText(/SSO: Allowed/)).toBeTruthy();

        fireEvent.change(screen.getByLabelText("Value to search (optional)"), { target: { value: " GOLD " } });
        fireEvent.click(screen.getByText("Run Phase 2 checks"));

        await screen.findByText(`Saved capture bundle: ${SUMMARY.bundle_id}`);
        expect(client.diagnosticsRun).toHaveBeenCalledWith("sales-ds", "GOLD");
        expect(screen.getByText("normalizer recognised no objects")).toBeTruthy();
        expect(screen.getByText(/4 events at 5, 305, 610, 2710 ms \(\s*incremental\)/)).toBeTruthy();
        expect(client.diagnosticsVisualReport).toHaveBeenCalledWith(SUMMARY.bundle_id, {
            ...CONTEXT.visualReport,
            sso: { privilegeStatus: 0, expiresAt: null, error: null },
            stream: { arrival_ms: [5, 305, 610, 2710], incremental: true },
        });
    });

    it("without a model it still runs the stream and visual checks", async () => {
        const client = renderApp({ diagnostics: CONTEXT, modelId: null });

        fireEvent.click(screen.getByText("Run Phase 2 checks"));

        await screen.findByText("Saved capture bundle: new-bundle");
        expect(client.diagnosticsRun).not.toHaveBeenCalled();
        expect(client.diagnosticsVisualReport).toHaveBeenCalledWith(null, expect.anything());
    });
});

describe("diagnostics report", () => {
    it("summarises the dataView without any values", () => {
        const dataView = {
            metadata: {
                columns: [{ queryName: "Product.LOB", roles: { contextFields: true }, type: { text: true } }],
            },
            table: { columns: [{}], rows: [["GOLD"], ["SILVER"], ["GOLD"]] },
        };

        const summary = dataViewSummary(dataView as never);

        expect(summary).toEqual({
            columns: [{ queryName: "Product.LOB", roles: ["contextFields"], type: ["text"] }],
            rowCount: 3,
            distinctValueCounts: [2],
        });
        expect(JSON.stringify(summary)).not.toContain("GOLD");
        expect(filterSummary([{ column: "Product[LOB]", values: ["GOLD", "SILVER"] }])).toEqual([
            { column: "Product[LOB]", valueCount: 2 },
        ]);
    });

    it("tells incremental streams from buffered ones", () => {
        expect(streamedIncrementally([5, 300, 600, 2700])).toBe(true);
        expect(streamedIncrementally([2700, 2701, 2702])).toBe(false);
    });

    it("token status never contains the token", async () => {
        const provider = new EntraTokenProvider({
            acquireAADTokenstatus: vi.fn(async () => 0),
            acquireAADToken: vi.fn(async () => ({ accessToken: "SECRET-TOKEN", expiresOn: 2_000_000_000 })),
        } as never);

        await provider.getToken();

        expect(provider.describe()).toEqual({ privilegeStatus: 0, expiresAt: 2_000_000_000_000, error: null });
        expect(JSON.stringify(provider.describe())).not.toContain("SECRET-TOKEN");
    });

    it("token status records why sign-in was refused", async () => {
        const provider = new EntraTokenProvider({
            acquireAADTokenstatus: vi.fn(async () => 3),
            acquireAADToken: vi.fn(),
        } as never);

        await expect(provider.getToken()).rejects.toThrow();

        expect(provider.describe()).toEqual({ privilegeStatus: 3, expiresAt: null, error: "disabled_by_admin" });
    });
});

describe("ApiClient diagnostics", () => {
    it("yields the arrival time of each tick", async () => {
        const body = "event: tick\ndata: {}\n\nevent: tick\ndata: {}\n\nevent: done\ndata: {}\n\n";
        const fetchImpl = vi.fn(async () => new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
        const client = new ApiClient("https://api.test", { getToken: async () => "tok" }, fetchImpl as never);
        const clock = [0, 10, 320];

        const times: number[] = [];
        for await (const ms of client.diagnosticsStream(() => clock.shift() ?? 999)) times.push(ms);

        expect(times).toEqual([10, 320]);
        expect(fetchImpl).toHaveBeenCalledWith("https://api.test/api/v1/diagnostics/stream", expect.anything());
    });
});
