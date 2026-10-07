import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../src/api/client";
import { ApiError, StreamEvent } from "../src/api/types";
import { AuthUnavailableError } from "../src/auth/tokenProvider";
import { ConversationStore } from "../src/storage";
import { App, AppProps } from "../src/ui/App";

afterEach(cleanup);

const THEME = { foreground: "#000", background: "#fff", accent: "#00f", highContrast: false };

function memoryStore(initial: Record<string, string> = {}): ConversationStore {
    const data = new Map(Object.entries(initial));
    return {
        load: async (k) => data.get(k) ?? null,
        save: async (k, v) => void (v ? data.set(k, v) : data.delete(k)),
    };
}

function fakeClient(overrides: Partial<Record<keyof ApiClient, unknown>> = {}): ApiClient {
    return {
        session: vi.fn(async () => ({ user: { object_id: "user-a", tenant_id: "t", username: null, display_name: "User A" } })),
        accessibleModels: vi.fn(async () => [{ id: "sales-ds", name: "Sales", domain: "Sales" }]),
        getSession: vi.fn(async () => {
            throw new ApiError(404, "session_not_found", "gone");
        }),
        deleteSession: vi.fn(async () => undefined),
        ask: vi.fn(async function* (): AsyncGenerator<StreamEvent> {
            yield { type: "session", session_id: "s1", correlation_id: null };
            yield { type: "status", stage: "querying" };
            yield {
                type: "table", model_id: "sales-ds", title: "GOLD net sales",
                columns: ["Product[LOB]", "[Total Net Sales]"],
                rows: [{ "Product[LOB]": "GOLD", "[Total Net Sales]": 1200.5 }], truncated: false,
            };
            yield { type: "token", text: "GOLD sales were **1,200.50**." };
            yield { type: "done", message_id: "m1", query_ids: [] };
        }),
        ...overrides,
    } as unknown as ApiClient;
}

function renderApp(props: Partial<AppProps> = {}) {
    const all: AppProps = {
        client: fakeClient(),
        conversations: memoryStore(),
        modelId: "sales-ds",
        reportFilters: [{ column: "Product[LOB]", values: ["GOLD"] }],
        title: "Sales assistant",
        theme: THEME,
        ...props,
    };
    return { ...render(<App {...all} />), props: all };
}

describe("App", () => {
    it("signs in, asks, streams a table and a formatted answer", async () => {
        const onModels = vi.fn();
        const { props } = renderApp({ onModels });
        await screen.findByText("Signed in as User A");
        expect(onModels).toHaveBeenCalledWith([{ id: "sales-ds", name: "Sales", domain: "Sales" }]);

        fireEvent.change(screen.getByLabelText("Ask a question about your data"), { target: { value: "GOLD sales?" } });
        fireEvent.click(screen.getByLabelText("Send question"));

        await screen.findByText("1,200.50");
        expect(screen.getByText("GOLD").tagName).toBe("TD");
        expect(screen.getByText("Total Net Sales").tagName).toBe("TH");
        expect(screen.getByText("1,200.50").tagName).toBe("STRONG");
        expect(props.client.ask).toHaveBeenCalledWith(
            { question: "GOLD sales?", sessionId: null, primaryModelId: "sales-ds", reportFilters: props.reportFilters },
            expect.any(AbortSignal),
        );
    });

    it("restores the stored conversation", async () => {
        const client = fakeClient({
            getSession: vi.fn(async () => ({
                session_id: "s7",
                primary_model_id: "sales-ds",
                messages: [
                    { id: "1", role: "user", kind: "question", content: "Earlier question", created_at: "" },
                    { id: "2", role: "assistant", kind: "answer", content: "Earlier answer", created_at: "" },
                ],
            })),
        });
        renderApp({ client, conversations: memoryStore({ "sales-ds": "s7" }) });

        await screen.findByText("Earlier answer");
        expect(client.getSession).toHaveBeenCalledWith("s7");
    });

    it("shows the sign-in problem instead of the chat", async () => {
        const client = fakeClient({ session: vi.fn(async () => Promise.reject(new AuthUnavailableError("disabled_by_admin"))) });
        renderApp({ client });

        expect((await screen.findByRole("alert")).textContent).toMatch(/administrator hasn.t enabled sign-in/);
        expect(screen.queryByLabelText("Ask a question about your data")).toBeNull();
    });

    it("stop aborts the stream", async () => {
        let aborted = false;
        const client = fakeClient({
            ask: vi.fn(async function* (_req: unknown, signal: AbortSignal): AsyncGenerator<StreamEvent> {
                yield { type: "status", stage: "planning" };
                await new Promise((_, reject) =>
                    signal.addEventListener("abort", () => {
                        aborted = true;
                        reject(new DOMException("aborted", "AbortError"));
                    }),
                );
            }),
        });
        renderApp({ client });
        await screen.findByText("Signed in as User A");

        fireEvent.change(screen.getByLabelText("Ask a question about your data"), { target: { value: "slow" } });
        fireEvent.click(screen.getByLabelText("Send question"));
        await screen.findByText("Planning the query…");
        await act(async () => fireEvent.click(screen.getByLabelText("Stop answering")));

        await waitFor(() => expect(aborted).toBe(true));
        expect(await screen.findByText("Stopped.")).toBeTruthy();
    });

    it("hints authors to pick a model when none is chosen", async () => {
        renderApp({ modelId: null });

        expect(await screen.findByText(/choose a data source in the Format pane/)).toBeTruthy();
    });
});
