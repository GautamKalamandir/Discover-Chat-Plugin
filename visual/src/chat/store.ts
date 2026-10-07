// Pure chat state (used with React's useReducer, unit-tested without a DOM).

import { SessionDetail, StreamEvent, TableData } from "../api/types";

export type MessageKind = "question" | "answer" | "clarification" | "notice" | "error";

export interface ChatMessage {
    id: string;
    role: "user" | "assistant";
    kind: MessageKind;
    text: string;
    tables: TableData[];
    pending: boolean;
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
    | { type: "stopped" };

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
                    ...state.messages,
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
        case "clarification":
            return updateLast(state, (m) => ({ ...m, kind: "clarification", text: event.question }));
        case "error":
            return updateLast(state, (m) => ({ ...m, kind: "error", text: event.message }));
        case "done":
            return finish(state);
    }
}
