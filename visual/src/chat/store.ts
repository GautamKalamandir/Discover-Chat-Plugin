// Pure chat state (used with React's useReducer, unit-tested without a DOM).

import {
    ClarificationOption,
    ReportDetail,
    ReportEvent,
    ReportItem,
    SessionDetail,
    StreamEvent,
    TableData,
} from "../api/types";

export type MessageKind = "question" | "answer" | "clarification" | "notice" | "error" | "report";

/** A /sales-summary report in the chat (ADR 0015, 0016). Not part of the stored conversation: /summary shows
 * it again. */
export interface ReportState {
    detail: ReportDetail | null;
    history: ReportItem[];
    /** What the build is doing, while it runs. */
    progress: string | null;
}

export interface ChatMessage {
    id: string;
    role: "user" | "assistant";
    kind: MessageKind;
    text: string;
    tables: TableData[];
    pending: boolean;
    /** Clarifications (ADR 0014): the options to click while `open`. */
    clarificationId?: string | null;
    options?: ClarificationOption[];
    open?: boolean;
    report?: ReportState;
}

export interface ChatState {
    sessionId: string | null;
    messages: ChatMessage[];
    busy: boolean;
    stage: string | null;
}

export const initialState: ChatState = { sessionId: null, messages: [], busy: false, stage: null };

export type ChatAction =
    | { type: "restored"; session: SessionDetail }
    | { type: "reset" }
    | { type: "asked"; id: string; question: string }
    | { type: "event"; event: StreamEvent }
    | { type: "failed"; message: string }
    | { type: "stopped" }
    | { type: "summary_asked"; id: string; text: string }
    | { type: "summary_progress"; label: string }
    | { type: "summary_ready"; detail: ReportDetail; history: ReportItem[] }
    | { type: "summary_stopped" };

export const SUMMARY_STOPPED =
    "Stopped watching. The report keeps being prepared: type /sales-summary again in a minute to see it.";

const REPORT_STAGES: Record<string, string> = {
    starting: "Starting your report…",
    // /sales-summary (ADR 0016): finding the sales data, then each table of each data source.
    discovering: "Finding your sales data…",
    lob_growth: "LOB-wise growth: {model}…",
    subcategory_loss: "Sub-category loss: {model}…",
    stock_turn: "Stock turn: {model}…",
    price_range: "Price ranges: {model}…",
    reading_model: "Reading {model}…",
    choosing_kpis: "Choosing the key figures for {model}…",
    querying: "Querying {model}…",
    explaining: "Looking at what changed in {model}…",
    writing: "Writing the {model} section…",
    summarising: "Writing the summary…",
    rendering_pdf: "Preparing the PDF…",
};

/** The progress line for a report event; null for events that don't change it. */
export function reportStageLabel(event: ReportEvent): string | null {
    if (event.type !== "progress") return null;
    const template = REPORT_STAGES[event.stage] ?? "Preparing your report…";
    const label = template.replace("{model}", event.model_name ?? "your data");
    // Users may have 30-40 data sources (ADR 0015 amendment 1): say how far the build is.
    const counts = event.total && event.total > 1 && event.stage !== "summarising" && event.stage !== "rendering_pdf";
    return counts ? `${label} (${event.done ?? 0} of ${event.total} done)` : label;
}

const STAGE_LABELS: Record<string, string> = {
    understanding: "Understanding your question…",
    finding_data: "Finding the right data…",
    planning: "Planning the query…",
    querying: "Querying Power BI…",
    analyzing: "Analysing the results…",
    answering: "Writing the answer…",
};

export function stageLabel(stage: string | null): string | null {
    return stage ? (STAGE_LABELS[stage] ?? "Working…") : null;
}

function updateLast(state: ChatState, change: (m: ChatMessage) => ChatMessage): ChatState {
    const index = state.messages.length - 1;
    const last = state.messages[index];
    if (!last || last.role !== "assistant" || !last.pending) return state;
    const messages = state.messages.slice();
    messages[index] = change(last);
    return { ...state, messages };
}

function finish(state: ChatState, change: (m: ChatMessage) => ChatMessage = (m) => m): ChatState {
    const next = updateLast(state, (m) => ({ ...change(m), pending: false }));
    return { ...next, busy: false, stage: null };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
    switch (action.type) {
        case "restored":
            return {
                ...initialState,
                sessionId: action.session.session_id,
                messages: action.session.messages.map((m) => ({
                    id: m.id,
                    role: m.role,
                    kind: m.kind,
                    text: m.content,
                    tables: [],
                    pending: false,
                    clarificationId: m.clarification_id ?? null,
                    options: m.options ?? [],
                    open: Boolean(m.open && m.options?.length),
                })),
            };
        case "reset":
            return initialState;
        case "asked":
            return {
                ...state,
                busy: true,
                stage: "understanding",
                messages: [
                    // Asking anything (a click or a typed reply) answers the open question.
                    ...state.messages.map((m) => (m.open ? { ...m, open: false } : m)),
                    { id: `${action.id}-q`, role: "user", kind: "question", text: action.question, tables: [], pending: false },
                    { id: `${action.id}-a`, role: "assistant", kind: "answer", text: "", tables: [], pending: true },
                ],
            };
        case "event":
            return applyEvent(state, action.event);
        case "failed":
            return finish(state, (m) => ({ ...m, kind: "error", text: action.message }));
        case "stopped":
            return finish(state, (m) => (m.text || m.tables.length ? m : { ...m, kind: "notice", text: "Stopped." }));
        case "summary_asked":
            return {
                ...state,
                busy: true,
                stage: null,
                messages: [
                    ...state.messages.map((m) => (m.open ? { ...m, open: false } : m)),
                    { id: `${action.id}-q`, role: "user", kind: "question", text: action.text, tables: [], pending: false },
                    {
                        id: `${action.id}-a`, role: "assistant", kind: "report", text: "", tables: [], pending: true,
                        report: { detail: null, history: [], progress: REPORT_STAGES.starting },
                    },
                ],
            };
        case "summary_progress":
            return updateLast(state, (m) =>
                m.report ? { ...m, report: { ...m.report, progress: action.label } } : m,
            );
        case "summary_ready":
            return finish(state, (m) => ({
                ...m,
                kind: "report",
                report: { detail: action.detail, history: action.history, progress: null },
            }));
        case "summary_stopped":
            return finish(state, (m) => ({ ...m, kind: "notice", text: SUMMARY_STOPPED, report: undefined }));
    }
}

function applyEvent(state: ChatState, event: StreamEvent): ChatState {
    switch (event.type) {
        case "session":
            return { ...state, sessionId: event.session_id };
        case "status":
            return { ...state, stage: event.stage };
        case "table":
            return updateLast(state, (m) => ({ ...m, tables: [...m.tables, event] }));
        case "token":
            return updateLast(state, (m) => ({ ...m, text: m.text + event.text }));
        case "clarification": {
            const options = event.options ?? [];
            return updateLast(state, (m) => ({
                ...m,
                kind: "clarification",
                text: event.question,
                clarificationId: event.id ?? null,
                options,
                open: options.length > 0,
            }));
        }
        case "error":
            return updateLast(state, (m) => ({ ...m, kind: "error", text: event.message }));
        case "done":
            return finish(state);
        default:
            return state; // an event this version doesn't know (newer backend): ignored
    }
}
