// Report filter context (Q7 hybrid): fields dragged into "Context fields" are filtered by the
// report's slicers, so their distinct values describe the current selection.

import type powerbi from "powerbi-visuals-api";

import { ReportFilter } from "../api/types";

type DataView = powerbi.DataView;

export const MAX_VALUES_PER_FIELD = 50;

/** "Product.LOB" -> "Product[LOB]"; measures/aggregates ("Sum(Sales.Amount)") are not filters. */
export function columnRef(queryName: string | undefined): string | null {
    if (!queryName || queryName.includes("(")) return null;
    const dot = queryName.indexOf(".");
    if (dot <= 0 || dot === queryName.length - 1) return null;
    return `${queryName.slice(0, dot)}[${queryName.slice(dot + 1)}]`;
}

export function reportFilters(dataView: DataView | undefined): ReportFilter[] {
    const table = dataView?.table;
    if (!table?.columns?.length || !table.rows?.length) return [];
    const filters: ReportFilter[] = [];
    table.columns.forEach((column, index) => {
        if (!column.roles?.["contextFields"]) return;
        const ref = columnRef(column.queryName);
        if (!ref) return;
        const values = new Map<string, string | number | boolean>();
        for (const row of table.rows ?? []) {
            const value = row[index];
            if (value === null || value === undefined) continue;
            const scalar =
                typeof value === "number" || typeof value === "boolean" ? value : String(value);
            values.set(`${typeof scalar}:${String(scalar)}`, scalar);
            if (values.size > MAX_VALUES_PER_FIELD) return; // effectively unfiltered: don't send
        }
        if (values.size) filters.push({ column: ref, values: [...values.values()] });
    });
    return filters;
}
