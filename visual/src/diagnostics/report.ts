// Phase 2 diagnostics (S5): what the visual itself receives from Power BI, summarised without data
// values. Only used by diagnostics builds (config.diagnostics; docs/spikes-runbook.md).

import type powerbi from "powerbi-visuals-api";

import { ReportFilter } from "../api/types";

type DataView = powerbi.DataView;

export interface DataViewSummary {
    columns: { queryName: string | null; roles: string[]; type: string[] }[];
    rowCount: number;
    distinctValueCounts: number[];
}

/** Column names, roles, types and counts; never a cell value. */
export function dataViewSummary(dataView: DataView | undefined): DataViewSummary {
    const table = dataView?.table;
    const rows = table?.rows ?? [];
    return {
        columns: (dataView?.metadata?.columns ?? []).map((c) => ({
            queryName: c.queryName ?? null,
            roles: Object.keys(c.roles ?? {}),
            type: Object.entries(c.type ?? {})
                .filter(([, flag]) => flag === true)
                .map(([name]) => name),
        })),
        rowCount: rows.length,
        distinctValueCounts: (table?.columns ?? []).map(
            (_, i) => new Set(rows.map((row) => String(row[i]))).size,
        ),
    };
}

/** The filters the chat would send, as columns and value counts only. */
export function filterSummary(filters: ReportFilter[]): { column: string; valueCount: number }[] {
    return filters.map((f) => ({ column: f.column, valueCount: f.values.length }));
}

/** True when stream events arrived spread out (incremental), not all at once (buffered). */
export function streamedIncrementally(arrivalsMs: number[]): boolean {
    return arrivalsMs.length > 1 && arrivalsMs[arrivalsMs.length - 1] - arrivalsMs[0] > 1500;
}

export const PRIVILEGE_NAMES: Record<number, string> = {
    0: "Allowed",
    1: "NotDeclared",
    2: "NotSupported",
    3: "DisabledByAdmin",
};
