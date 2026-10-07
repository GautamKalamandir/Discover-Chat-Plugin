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

export interface StoredMessage {
    id: string;
    role: "user" | "assistant";
    kind: "question" | "answer" | "clarification" | "notice";
    content: string;
    created_at: string;
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
    | { type: "clarification"; question: string }
    | { type: "error"; code: string; message: string }
    | { type: "done"; message_id: string | null; query_ids: string[] };

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
