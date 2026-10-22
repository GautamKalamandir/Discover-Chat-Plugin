import * as React from "react";

import { ApiClient } from "../api/client";
import {
    ApiError,
    Choice,
    ClarificationOption,
    ModelOption,
    ReportFilter,
    ReportItem,
} from "../api/types";
import { AuthUnavailableError, AUTH_MESSAGES } from "../auth/tokenProvider";
import { ChatMessage, chatReducer, initialState, reportStageLabel } from "../chat/store";
import { DownloadOutcome } from "../reports/download";
import { ConversationStore } from "../storage";
import { Composer, MessageView } from "./components";
import { DiagnosticsContext, DiagnosticsPanel } from "./DiagnosticsPanel";
import { formatDay, ReportActions, ReportView } from "./ReportView";

export interface Theme {
    foreground: string;
    background: string;
    accent: string;
    highContrast: boolean;
}

export interface AppProps {
    client: ApiClient;
    conversations: ConversationStore;
    modelId: string | null;
    reportFilters: ReportFilter[];
    title: string;
    theme: Theme;
    onModels?: (models: ModelOption[]) => void;
    /** Only in diagnostics builds (config.diagnostics). */
    diagnostics?: DiagnosticsContext;
    /** Saves a report's PDF (Power BI download API or a one-time link; src/reports/download.ts). */
    download?: (reportId: string, reportDate: string) => Promise<DownloadOutcome>;
}

export const INTERRUPTED = "The answer was interrupted. Please try again.";
/** Typed in the chat, these ask for today's report instead of asking the agent (ADR 0015; ADR 0016:
 * /sales-summary replaces /summary, which stays as an alias). */
export const SUMMARY_COMMAND = /^\/(sales-)?summary$/i;
export const STILL_BUILDING =
    "Your report is still being prepared. Type /sales-summary again in a minute to see it.";
export const REPORT_FAILED = "The report couldn't be built. Please try again.";

function userMessage(error: unknown): string {
    if (error instanceof AuthUnavailableError || error instanceof ApiError) return error.message;
    return "Something went wrong. Please try again.";
}

export function App(props: AppProps): React.JSX.Element {
    const { client, conversations, modelId, reportFilters, onModels } = props;
    const [state, dispatch] = React.useReducer(chatReducer, initialState);
    const [user, setUser] = React.useState<string | null>(null);
    const [authError, setAuthError] = React.useState<string | null>(null);
    const abort = React.useRef<AbortController | null>(null);
    const end = React.useRef<HTMLDivElement | null>(null);
    const input = React.useRef<HTMLTextAreaElement | null>(null);
    const storeKey = modelId ?? "none";

    // Sign-in: the backend tells us who it believes is signed in (Power BI identity, Q1).
    React.useEffect(() => {
        let live = true;
        client
            .session()
            .then((s) => {
                if (!live) return;
                setUser(s.user.display_name || s.user.username || s.user.object_id);
                setAuthError(null);
                return client.accessibleModels().then((models) => live && onModels?.(models));
            })
            .catch((e: unknown) => {
                if (!live) return;
                setAuthError(
                    e instanceof ApiError && e.status === 401
                        ? AUTH_MESSAGES.sign_in_failed
                        : userMessage(e),
                );
            });
        return () => {
            live = false;
        };
    }, [client, onModels]);

    // Restore this visual's conversation after Power BI re-renders it (Q17).
    React.useEffect(() => {
        if (!user) return;
        let live = true;
        conversations.load(storeKey).then(async (sessionId) => {
            if (!sessionId || !live) return;
            try {
                const session = await client.getSession(sessionId);
                if (live) dispatch({ type: "restored", session });
            } catch {
                await conversations.save(storeKey, null); // expired or deleted
                if (live) dispatch({ type: "reset" });
            }
        });
        return () => {
            live = false;
        };
    }, [client, conversations, storeKey, user]);

    React.useEffect(() => {
        end.current?.scrollIntoView?.({ block: "end" });
    }, [state.messages]);

    React.useEffect(() => () => abort.current?.abort(), []);

    const send = async (question: string, choice: Choice | null = null): Promise<void> => {
        if (!choice && SUMMARY_COMMAND.test(question.trim())) return runSummary(question.trim());
        const controller = new AbortController();
        abort.current = controller;
        dispatch({ type: "asked", id: String(Date.now()), question });
        let finished = false;
        try {
            for await (const event of client.ask(
                {
                    question,
                    sessionId: state.sessionId,
                    primaryModelId: modelId,
                    reportFilters,
                    choice,
                },
                controller.signal,
            )) {
                dispatch({ type: "event", event });
                if (event.type === "session") await conversations.save(storeKey, event.session_id);
                if (event.type === "done") finished = true;
            }
            // The connection closed before the answer finished (restart, network): never stay busy.
            if (!finished) dispatch({ type: "failed", message: INTERRUPTED });
        } catch (e) {
            if (controller.signal.aborted) dispatch({ type: "stopped" });
            else dispatch({ type: "failed", message: userMessage(e) });
        } finally {
            abort.current = null;
        }
    };

    /** Loads a finished (or failed) report with the history list and shows it. */
    const showReport = async (reportId: string, failure: string | null): Promise<void> => {
        const [detail, history] = await Promise.all([
            client.getReport(reportId),
            client.listReports().catch((): ReportItem[] => []),
        ]);
        if (detail.status === "ready" && detail.content) {
            dispatch({ type: "summary_ready", detail, history });
        } else if (detail.status === "building") {
            dispatch({ type: "failed", message: STILL_BUILDING }); // e.g. built by another server
        } else {
            dispatch({ type: "failed", message: detail.error_message ?? failure ?? REPORT_FAILED });
        }
    };

    /** /sales-summary: today's report, or follow its build. Stop only stops watching. */
    const runSummary = async (text: string, regenerate = false): Promise<void> => {
        const controller = new AbortController();
        abort.current = controller;
        dispatch({ type: "summary_asked", id: String(Date.now()), text });
        try {
            const started = await client.startSummary(regenerate);
            let failure: string | null = null;
            if (started.status !== "ready") {
                for await (const event of client.reportEvents(started.report_id, controller.signal)) {
                    const label = reportStageLabel(event);
                    if (label) dispatch({ type: "summary_progress", label });
                    if (event.type === "error") failure = event.message;
                }
            }
            await showReport(started.report_id, failure);
        } catch (e) {
            if (controller.signal.aborted) dispatch({ type: "summary_stopped" });
            else dispatch({ type: "failed", message: userMessage(e) });
        } finally {
            abort.current = null;
        }
    };

    const openReport = async (item: ReportItem): Promise<void> => {
        dispatch({ type: "summary_asked", id: String(Date.now()), text: `Open the report from ${formatDay(item.report_date)}` });
        try {
            await showReport(item.id, null);
        } catch (e) {
            dispatch({ type: "failed", message: userMessage(e) });
        }
    };

    const download =
        props.download ??
        (async (reportId: string): Promise<DownloadOutcome> => {
            const link = await client.downloadLink(reportId);
            window.open(client.url(link.url_path), "_blank", "noopener");
            return "opened";
        });
    const reportActions: ReportActions = {
        download: (report) => download(report.id, report.report_date),
        regenerate: () => void runSummary("Regenerate the report", true),
        open: (report) => void openReport(report),
    };

    // A click on an option: the label becomes the user's message, the ids say what was chosen.
    const choose = (message: ChatMessage, option: ClarificationOption): void => {
        if (state.busy || !message.open || !message.clarificationId) return;
        void send(option.label, { clarificationId: message.clarificationId, optionId: option.id });
    };

    const newChat = async (): Promise<void> => {
        abort.current?.abort();
        await conversations.save(storeKey, null);
        dispatch({ type: "reset" });
    };

    const deleteChat = async (): Promise<void> => {
        const id = state.sessionId;
        await newChat();
        if (id) await client.deleteSession(id).catch(() => undefined);
    };

    const style = {
        "--fg": props.theme.foreground,
        "--bg": props.theme.background,
        "--accent": props.theme.accent,
    } as React.CSSProperties;

    return (
        <div className={`chat${props.theme.highContrast ? " hc" : ""}`} style={style}>
            <header>
                <h2>{props.title || "Discover Chat Bot"}</h2>
                {user && <span className="user">Signed in as {user}</span>}
                <div className="actions">
                    <button type="button" onClick={newChat} disabled={!state.messages.length}>
                        New chat
                    </button>
                    <button type="button" onClick={deleteChat} disabled={!state.sessionId}>
                        Delete chat
                    </button>
                </div>
            </header>
            {props.diagnostics && (
                <DiagnosticsPanel client={client} modelId={modelId} context={props.diagnostics} />
            )}
            {authError ? (
                <p className="banner error" role="alert">
                    {authError}
                </p>
            ) : (
                <>
                    {!modelId && (
                        <p className="banner hint">
                            Tip for report authors: choose a data source in the Format pane → Data
                            source.
                        </p>
                    )}
                    <div className="messages" aria-live="polite">
                        {!state.messages.length && (
                            <p className="empty">
                                Ask a question about your data, or type /sales-summary for today&apos;s sales report.
                            </p>
                        )}
                        {state.messages.map((m) => (
                            <MessageView
                                key={m.id}
                                message={m}
                                stage={state.stage}
                                busy={state.busy}
                                onChoose={choose}
                                onNone={() => input.current?.focus()}
                            >
                                {m.kind === "report" && m.report && !m.pending && (
                                    <ReportView state={m.report} actions={reportActions} busy={state.busy} />
                                )}
                            </MessageView>
                        ))}
                        <div ref={end} />
                    </div>
                    <Composer
                        busy={state.busy}
                        onSend={(q) => void send(q)}
                        onStop={() => abort.current?.abort()}
                        inputRef={input}
                    />
                </>
            )}
        </div>
    );
}
