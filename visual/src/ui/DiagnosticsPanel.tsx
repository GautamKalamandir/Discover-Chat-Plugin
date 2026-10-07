import * as React from "react";

import { ApiClient } from "../api/client";
import { ApiError, DiagnosticsSummary } from "../api/types";
import { TokenStatus } from "../auth/tokenProvider";
import { PRIVILEGE_NAMES, streamedIncrementally } from "../diagnostics/report";

/** Supplied by the visual only in diagnostics builds (config.diagnostics). */
export interface DiagnosticsContext {
    tokenStatus: () => TokenStatus | null;
    /** Host, saved objects, dataView and filter summaries: structure and counts, no values. */
    visualReport: Record<string, unknown>;
}

export interface DiagnosticsPanelProps {
    client: ApiClient;
    modelId: string | null;
    context: DiagnosticsContext;
}

function describeError(e: unknown): string {
    if (e instanceof ApiError) return `${e.status} ${e.code}: ${e.message}`;
    return e instanceof Error ? e.name : "error";
}

function ssoLine(status: TokenStatus | null): string {
    if (!status) return "SSO: not used (dev sign-in)";
    const privilege =
        status.privilegeStatus === null
            ? "not requested yet"
            : (PRIVILEGE_NAMES[status.privilegeStatus] ?? String(status.privilegeStatus));
    const expires = status.expiresAt ? new Date(status.expiresAt).toLocaleTimeString() : "-";
    return `SSO: ${privilege}${status.error ? ` (${status.error})` : ""}, token expires ${expires}`;
}

/** Phase 2 spike runner (docs/spikes-runbook.md): runs S1–S6 and saves a local capture bundle. */
export function DiagnosticsPanel({ client, modelId, context }: DiagnosticsPanelProps): React.JSX.Element {
    const [valueTerm, setValueTerm] = React.useState("");
    const [running, setRunning] = React.useState(false);
    const [summary, setSummary] = React.useState<DiagnosticsSummary | null>(null);
    const [arrivals, setArrivals] = React.useState<number[]>([]);
    const [bundleId, setBundleId] = React.useState<string | null>(null);
    const [error, setError] = React.useState<string | null>(null);

    const run = async (): Promise<void> => {
        setRunning(true);
        setError(null);
        setSummary(null);
        setArrivals([]);
        setBundleId(null);
        try {
            let id: string | null = null;
            if (modelId) {
                const result = await client.diagnosticsRun(modelId, valueTerm.trim());
                setSummary(result);
                id = result.bundle_id;
            }
            const times: number[] = [];
            for await (const ms of client.diagnosticsStream()) {
                times.push(ms);
                setArrivals([...times]);
            }
            const saved = await client.diagnosticsVisualReport(id, {
                ...context.visualReport,
                sso: context.tokenStatus(),
                stream: { arrival_ms: times, incremental: streamedIncrementally(times) },
            });
            setBundleId(saved.bundle_id);
        } catch (e) {
            setError(describeError(e));
        } finally {
            setRunning(false);
        }
    };

    return (
        <details className="diagnostics" open>
            <summary>Diagnostics (Phase 2)</summary>
            <p>{ssoLine(context.tokenStatus())}</p>
            <p>Model: {modelId ?? "none selected (only stream and visual checks run)"}</p>
            <div className="diag-controls">
                <input
                    aria-label="Value to search (optional)"
                    placeholder="Value to search (optional)"
                    value={valueTerm}
                    maxLength={100}
                    onChange={(e) => setValueTerm(e.target.value)}
                />
                <button type="button" onClick={() => void run()} disabled={running}>
                    {running ? "Running…" : "Run Phase 2 checks"}
                </button>
            </div>
            {error && (
                <p className="banner error" role="alert">
                    {error}
                </p>
            )}
            {summary && (
                <table className="diag-steps">
                    <thead>
                        <tr>
                            <th>Spike</th>
                            <th>Check</th>
                            <th>Result</th>
                            <th>ms</th>
                            <th>Error</th>
                        </tr>
                    </thead>
                    <tbody>
                        {summary.steps.map((s) => (
                            <tr key={`${s.spike}-${s.name}`} className={s.status}>
                                <td>{s.spike}</td>
                                <td>{s.name}</td>
                                <td>{s.status}</td>
                                <td>{s.duration_ms}</td>
                                <td>{s.error ?? ""}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
            {arrivals.length > 0 && (
                <p>
                    Stream: {arrivals.length} events at {arrivals.join(", ")} ms (
                    {streamedIncrementally(arrivals) ? "incremental" : "buffered"})
                </p>
            )}
            {bundleId && <p>Saved capture bundle: {bundleId}</p>}
        </details>
    );
}
