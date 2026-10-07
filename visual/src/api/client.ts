import { TokenProvider } from "../auth/tokenProvider";
import { readSse } from "./sse";
import {
    ApiError,
    DiagnosticsSummary,
    ModelOption,
    ReportFilter,
    SessionDetail,
    SessionUser,
    StreamEvent,
} from "./types";

export interface AskRequest {
    question: string;
    sessionId?: string | null;
    primaryModelId?: string | null;
    reportFilters?: ReportFilter[];
}

/** The visual's only way to the backend. Adds the bearer token; one silent retry with a fresh token
 * when the backend says the token expired or is invalid. */
export class ApiClient {
    constructor(
        private readonly baseUrl: string,
        private readonly tokens: TokenProvider,
        private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
    ) {}

    async session(): Promise<{ user: SessionUser }> {
        return this.json("GET", "/api/v1/session");
    }

    async accessibleModels(): Promise<ModelOption[]> {
        return (await this.json<{ models: ModelOption[] }>("GET", "/api/v1/models/accessible"))
            .models;
    }

    async newSession(primaryModelId: string | null): Promise<{ session_id: string }> {
        return this.json("POST", "/api/v1/chat/sessions", { primary_model_id: primaryModelId });
    }

    async getSession(sessionId: string): Promise<SessionDetail> {
        return this.json("GET", `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`);
    }

    async deleteSession(sessionId: string): Promise<void> {
        await this.request("DELETE", `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}`);
    }

    // --- Phase 2 diagnostics (only called by the diagnostics build; docs/spikes-runbook.md) ---

    async diagnosticsRun(modelId: string, valueTerm?: string): Promise<DiagnosticsSummary> {
        return this.json("POST", "/api/v1/diagnostics/run", {
            model_id: modelId,
            value_term: valueTerm || null,
        });
    }

    /** S6: yields each event's arrival time (ms since the request started). */
    async *diagnosticsStream(now: () => number = () => performance.now()): AsyncGenerator<number> {
        const started = now();
        const response = await this.request("GET", "/api/v1/diagnostics/stream");
        if (!response.body) throw new ApiError(0, "no_stream", "The answer could not be streamed.");
        for await (const frame of readSse(response.body)) {
            if (frame.event === "tick") yield Math.round(now() - started);
        }
    }

    async diagnosticsVisualReport(
        bundleId: string | null,
        report: Record<string, unknown>,
    ): Promise<{ bundle_id: string }> {
        return this.json("POST", "/api/v1/diagnostics/visual", { bundle_id: bundleId, report });
    }

    /** Streams one turn. Aborting `signal` closes the connection, so the backend cancels the turn. */
    async *ask(request: AskRequest, signal?: AbortSignal): AsyncGenerator<StreamEvent> {
        const response = await this.request(
            "POST",
            "/api/v1/chat/stream",
            {
                question: request.question,
                session_id: request.sessionId ?? null,
                primary_model_id: request.primaryModelId ?? null,
                report_filters: request.reportFilters ?? [],
            },
            signal,
        );
        if (!response.body) throw new ApiError(0, "no_stream", "The answer could not be streamed.");
        for await (const frame of readSse(response.body)) {
            let data: Record<string, unknown>;
            try {
                data = JSON.parse(frame.data) as Record<string, unknown>;
            } catch {
                continue; // ignore anything that isn't one of our JSON events
            }
            yield { type: frame.event, ...data } as StreamEvent;
        }
    }

    private async json<T>(method: string, path: string, body?: unknown): Promise<T> {
        return (await (await this.request(method, path, body)).json()) as T;
    }

    private async request(
        method: string,
        path: string,
        body?: unknown,
        signal?: AbortSignal,
        retried = false,
    ): Promise<Response> {
        const token = await this.tokens.getToken(retried);
        const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
            method,
            signal,
            headers: {
                Authorization: `Bearer ${token}`,
                ...(body === undefined ? {} : { "Content-Type": "application/json" }),
            },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (response.ok) return response;
        const error = await toApiError(response);
        if (response.status === 401 && !retried) {
            return this.request(method, path, body, signal, true);
        }
        throw error;
    }
}

async function toApiError(response: Response): Promise<ApiError> {
    try {
        const body = (await response.json()) as { error?: { code?: string; message?: string } };
        if (body.error?.message) {
            return new ApiError(response.status, body.error.code ?? "error", body.error.message);
        }
    } catch {
        // not our JSON envelope
    }
    return new ApiError(response.status, "http_error", "Something went wrong. Please try again.");
}
