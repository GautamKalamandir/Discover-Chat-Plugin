// Numbers in a /summary report, as the PDF shows them (backend app/reports/text.py, ADR 0015
// amendment 1): Indian grouping (1,47,61,530.70), compact lakh / crore (14.76 Cr), and changes in
// words ("down 7.95%") coloured by whether they are good news for that KPI.

export function formatIndian(value: number | null | undefined, decimals?: number): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
    const places = decimals ?? (Number.isInteger(value) ? 0 : 2);
    const fixed = Math.abs(value).toFixed(places);
    const [whole, fraction] = fixed.split(".");
    let grouped = whole;
    if (whole.length > 3) {
        const head = whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
        grouped = `${head},${whole.slice(-3)}`;
    }
    const sign = value < 0 && Number(fixed) !== 0 ? "-" : "";
    return sign + grouped + (fraction ? `.${fraction}` : "");
}

export function compactIndian(value: number | null | undefined): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
    const size = Math.abs(value);
    if (size >= 1e7) return `${(value / 1e7).toFixed(2)} Cr`;
    if (size >= 1e5) return `${(value / 1e5).toFixed(2)} lakh`;
    return formatIndian(Math.round(value * 100) / 100);
}

/** "down 7.95%", "up 2.00%", "no change"; "" when unknown. */
export function percentWords(percent: number | null | undefined): string {
    if (percent === null || percent === undefined) return "";
    if (percent === 0) return "no change";
    return `${percent > 0 ? "up" : "down"} ${Math.abs(percent).toFixed(2)}%`;
}

/** "down 19,41,14,359.36" when there is no percent to show. */
export function changeWords(change: number | null | undefined): string {
    if (change === null || change === undefined) return "n/a";
    if (change === 0) return "no change";
    return `${change > 0 ? "up" : "down"} ${formatIndian(Math.abs(change))}`;
}

/** Whether a change is good news for a KPI; null for no change or unknown. */
export function isGood(change: number | null | undefined, higherIsBetter: boolean): boolean | null {
    if (!change) return null;
    return change > 0 === higherIsBetter;
}

/** CSS class for a change: "good", "bad" or "neutral". */
export function tone(good: boolean | null): "good" | "bad" | "neutral" {
    return good === null ? "neutral" : good ? "good" : "bad";
}
