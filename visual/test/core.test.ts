import { describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api/client";
import { SseParser } from "../src/api/sse";
import { ApiError, StreamEvent } from "../src/api/types";
import {
    AuthUnavailableError,
    DevTokenProvider,
    EntraTokenProvider,
    toEpochMs,
} from "../src/auth/tokenProvider";
import { chatReducer, initialState } from "../src/chat/store";
import { columnRef, reportFilters } from "../src/context/reportFilters";
import { parseMarkdown } from "../src/ui/markdown";
import { VisualConversationStore } from "../src/storage";

// --- SSE ---------------------------------------------------------------------------------------------

describe("SseParser", () => {
    it("handles frames split across chunks, CRLF, comments and multi-line data", () => {
        const parser = new SseParser();
        const frames = [
            ...parser.push('id: 1\r\nevent: status\r\ndata: {"stage":'),
            ...parser.push('"planning"}\r\n\r\n: ping\r\n\r\n'),
            ...parser.push("event: token\ndata: line one\ndata: line two\n\n"),
        ];

        expect(frames).toEqual([
            { event: "status", data: '{"stage":"planning"}', id: "1" },
            { event: "token", data: "line one\nline two", id: undefined },
        ]);
    });
});

// --- chat state ----------------------------------------------------------------------------------------

function run(events: StreamEvent[]) {
    let state = chatReducer(initialState, { type: "asked", id: "1", question: "GOLD sales?" });
    for (const event of events) state = chatReducer(state, { type: "event", event });
    return state;
}

describe("chatReducer", () => {
    it("builds an answer from a streamed turn", () => {
        const state = run([
            { type: "session", session_id: "s1", correlation_id: null },
            { type: "status", stage: "querying" },
            { type: "table", model_id: "m", title: "t", columns: ["[x]"], rows: [{ "[x]": 1 }], truncated: false },
            { type: "token", text: "GOLD sales " },
            { type: "token", text: "were 1." },
            { type: "done", message_id: "m1", query_ids: [] },
        ]);

        expect(state.sessionId).toBe("s1");
        expect(state.busy).toBe(false);
        const answer = state.messages[1];
        expect(answer).toMatchObject({ kind: "answer", text: "GOLD sales were 1.", pending: false });
        expect(answer.tables).toHaveLength(1);
    });

    it("shows clarifications and errors as such", () => {
        expect(run([{ type: "clarification", question: "Net or gross?" }]).messages[1]).toMatchObject({
            kind: "clarification",
            text: "Net or gross?",
        });
        expect(run([{ type: "error", code: "x", message: "Busy." }]).messages[1].kind).toBe("error");
    });

    it("marks an empty stopped answer and keeps a partial one", () => {
        const stopped = chatReducer(run([]), { type: "stopped" });
        expect(stopped.messages[1]).toMatchObject({ kind: "notice", text: "Stopped.", pending: false });

        const partial = chatReducer(run([{ type: "token", text: "Partial" }]), { type: "stopped" });
        expect(partial.messages[1].text).toBe("Partial");
    });

    it("restores a stored conversation", () => {
        const state = chatReducer(initialState, {
            type: "restored",
            session: {
                session_id: "s9",
                primary_model_id: "sales-ds",
                messages: [{ id: "a", role: "user", kind: "question", content: "hi", created_at: "" }],
            },
        });
        expect(state).toMatchObject({ sessionId: "s9", busy: false });
        expect(state.messages[0].text).toBe("hi");
    });
});

// --- report filters ------------------------------------------------------------------------------------

describe("reportFilters", () => {
    it("maps query names to Table[Column] and skips measures", () => {
        expect(columnRef("Product.LOB")).toBe("Product[LOB]");
        expect(columnRef("Sales.Invoice Date")).toBe("Sales[Invoice Date]");
        expect(columnRef("Sum(Sales.Amount)")).toBeNull();
        expect(columnRef("NoTable")).toBeNull();
    });

    it("sends distinct selected values and skips effectively unfiltered fields", () => {
        const many = Array.from({ length: 60 }, (_, i) => [`G${i}`, "x"]);
        const dataView = {
            table: {
                columns: [
                    { queryName: "Product.LOB", roles: { contextFields: true } },
                    { queryName: "Customer.Name", roles: { contextFields: true } },
                ],
                rows: [["GOLD", "A"], ["GOLD", "B"], ["SILVER", "A"], ...many.map(([, c]) => [null, c])],
            },
        };
        const filters = reportFilters(dataView as never);
        expect(filters).toEqual([
            { column: "Product[LOB]", values: ["GOLD", "SILVER"] },
            { column: "Customer[Name]", values: ["A", "B", "x"] },
        ]);

        const wide = { table: { columns: dataView.table.columns.slice(0, 1), rows: many } };
        expect(reportFilters(wide as never)).toEqual([]);
    });
});

// --- markdown --------------------------------------------------------------------------------------

describe("parseMarkdown", () => {
    it("parses the safe subset and keeps HTML as plain text", () => {
        expect(parseMarkdown("Top **3**:\n- GOLD: _1,200_\n- SILVER: 800\n\n<img src=x onerror=alert(1)>")).toEqual([
            { kind: "p", inlines: [{ kind: "text", text: "Top " }, { kind: "bold", text: "3" }, { kind: "text", text: ":" }] },
            {
                kind: "ul",
                items: [
                    [{ kind: "text", text: "GOLD: " }, { kind: "italic", text: "1,200" }],
                    [{ kind: "text", text: "SILVER: 800" }],
                ],
            },
            { kind: "p", inlines: [{ kind: "text", text: "<img src=x onerror=alert(1)>" }] },
        ]);
    });
});

// --- tokens ----------------------------------------------------------------------------------------

function aadService(status: number, token = "tok", expiresOn = Date.now() + 3600_000) {
    return {
        acquireAADTokenstatus: vi.fn(async () => status),
        acquireAADToken: vi.fn(async () => ({ accessToken: token, expiresOn })),
    };
}

// @scenario 21: Authentication API status DisabledByAdmin / NotSupported -> specific message, no backend call
describe("EntraTokenProvider", () => {
    it("acquires once and caches until close to expiry", async () => {
        const service = aadService(0);
        const provider = new EntraTokenProvider(service as never);

        expect(await provider.getToken()).toBe("tok");
        expect(await provider.getToken()).toBe("tok");
        expect(service.acquireAADToken).toHaveBeenCalledTimes(1);

        await provider.getToken(true);
        expect(service.acquireAADToken).toHaveBeenCalledTimes(2);
    });

    it.each([
        [2, "not_supported"],
        [3, "disabled_by_admin"],
        [1, "not_declared"],
    ])("privilege status %s becomes %s", async (status, problem) => {
        const provider = new EntraTokenProvider(aadService(status) as never);
        await expect(provider.getToken()).rejects.toMatchObject({ problem });
    });

    it("treats a missing token as a failed sign-in", async () => {
        const provider = new EntraTokenProvider(aadService(0, "") as never);
        await expect(provider.getToken()).rejects.toBeInstanceOf(AuthUnavailableError);
    });

    it("accepts expiry in seconds or milliseconds", () => {
        expect(toEpochMs(1_800_000_000, 0)).toBe(1_800_000_000_000);
        expect(toEpochMs(1_800_000_000_000, 0)).toBe(1_800_000_000_000);
    });
});

describe("DevTokenProvider", () => {
    it("asks the backend dev endpoint for the configured user", async () => {
        const fetchMock = vi.fn(async () => new Response(JSON.stringify({ access_token: "dev", expires_in: 3600 })));
        const provider = new DevTokenProvider("https://api.test", "user-b", fetchMock as never);

        expect(await provider.getToken()).toBe("dev");
        const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
        expect(url).toBe("https://api.test/api/v1/dev/token");
        expect(JSON.parse(String(init.body))).toEqual({ oid: "user-b" });
    });
});

// --- API client ------------------------------------------------------------------------------------

function sseResponse(text: string): Response {
    const stream = new ReadableStream<Uint8Array>({
        start(controller) {
            // Two chunks, split mid-frame, like a real network stream.
            const bytes = new TextEncoder().encode(text);
            controller.enqueue(bytes.slice(0, 30));
            controller.enqueue(bytes.slice(30));
            controller.close();
        },
    });
    return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}

describe("ApiClient", () => {
    it("retries once with a fresh token after a 401", async () => {
        const tokens = { getToken: vi.fn(async (force?: boolean) => (force ? "new" : "old")) };
        const fetchMock = vi
            .fn()
            .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "token_expired", message: "x" } }), { status: 401 }))
            .mockResolvedValueOnce(new Response(JSON.stringify({ models: [] })));
        const client = new ApiClient("https://api.test", tokens, fetchMock);

        expect(await client.accessibleModels()).toEqual([]);
        expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer new");
    });

    it("surfaces the backend's user-safe error", async () => {
        const fetchMock = vi.fn(
            async () => new Response(JSON.stringify({ error: { code: "model_not_found", message: "Not available." } }), { status: 404 }),
        );
        const client = new ApiClient("https://api.test", { getToken: async () => "t" }, fetchMock);

        await expect(client.getSession("s")).rejects.toEqual(new ApiError(404, "model_not_found", "Not available."));
    });

    it("streams typed events and ignores pings", async () => {
        const body =
            'event: session\ndata: {"session_id":"s1","correlation_id":"c"}\n\n: ping\n\n' +
            'event: token\ndata: {"text":"Hi"}\n\nevent: done\ndata: {"message_id":"m","query_ids":[]}\n\n';
        const fetchMock = vi.fn(async () => sseResponse(body));
        const client = new ApiClient("https://api.test", { getToken: async () => "t" }, fetchMock);

        const events: StreamEvent[] = [];
        for await (const event of client.ask({ question: "q", primaryModelId: "sales-ds" })) events.push(event);

        expect(events.map((e) => e.type)).toEqual(["session", "token", "done"]);
        const sent = JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body));
        expect(sent).toEqual({ question: "q", session_id: null, primary_model_id: "sales-ds", report_filters: [] });
    });
});

// --- conversation storage ----------------------------------------------------------------------------

describe("VisualConversationStore", () => {
    it("uses Power BI local storage when allowed", async () => {
        const data = new Map<string, string>();
        const service = {
            status: async () => 0,
            get: async (k: string) => data.get(k) ?? Promise.reject(new Error("missing")),
            set: async (k: string, v: string) => void data.set(k, v),
            remove: async (k: string) => void data.delete(k),
        };
        const store = new VisualConversationStore(service as never);

        await store.save("sales-ds", "s1");
        expect(await store.load("sales-ds")).toBe("s1");
        expect(data.get("discover-chat:sales-ds")).toBe("s1");
        await store.save("sales-ds", null);
        expect(await new VisualConversationStore(service as never).load("sales-ds")).toBeNull();
    });

    it("falls back to memory when the admin hasn't allowed local storage", async () => {
        const store = new VisualConversationStore({ status: async () => 3 } as never);

        await store.save("k", "s2");
        expect(await store.load("k")).toBe("s2");
    });
});
