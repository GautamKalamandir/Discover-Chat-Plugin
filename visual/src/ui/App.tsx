import * as React from "react";

import { ApiClient } from "../api/client";
import { ApiError, ModelOption, ReportFilter } from "../api/types";
import { AuthUnavailableError, AUTH_MESSAGES } from "../auth/tokenProvider";
import { chatReducer, initialState } from "../chat/store";
import { ConversationStore } from "../storage";
import { Composer, MessageView } from "./components";

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
}

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

    const send = async (question: string): Promise<void> => {
        const controller = new AbortController();
        abort.current = controller;
        dispatch({ type: "asked", id: String(Date.now()), question });
        try {
            for await (const event of client.ask(
                { question, sessionId: state.sessionId, primaryModelId: modelId, reportFilters },
                controller.signal,
            )) {
                dispatch({ type: "event", event });
                if (event.type === "session") await conversations.save(storeKey, event.session_id);
            }
        } catch (e) {
            if (controller.signal.aborted) dispatch({ type: "stopped" });
            else dispatch({ type: "failed", message: userMessage(e) });
        } finally {
            abort.current = null;
        }
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
                            <p className="empty">Ask a question about your data to get started.</p>
                        )}
                        {state.messages.map((m) => (
                            <MessageView key={m.id} message={m} stage={state.stage} />
                        ))}
                        <div ref={end} />
                    </div>
                    <Composer
                        busy={state.busy}
                        onSend={(q) => void send(q)}
                        onStop={() => abort.current?.abort()}
                    />
                </>
            )}
        </div>
    );
}
