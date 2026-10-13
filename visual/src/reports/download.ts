// "Download PDF" for a /summary report (ADR 0015 §9).
// 1. Power BI's file download API, when the admin switch "Allow downloads from custom visuals" is
//    on (the user confirms each download in a Power BI prompt).
// 2. Otherwise (switch off, mobile, an error): a one-time link, valid about a minute, opened in a new
//    browser tab with host.launchUrl.

import type powerbi from "powerbi-visuals-api";

import { ApiClient } from "../api/client";
import { PRIVILEGE_ALLOWED } from "../auth/tokenProvider";

export interface DownloadHost {
    downloadService?: Pick<powerbi.extensibility.IDownloadService, "exportStatus" | "exportVisualsContentExtended">;
    launchUrl: (url: string) => void;
}

/** downloaded: saved by Power BI; cancelled: the user declined Power BI's prompt; opened: the
 * one-time link was opened in the browser. */
export type DownloadOutcome = "downloaded" | "cancelled" | "opened";

export async function downloadReport(
    client: ApiClient,
    host: DownloadHost,
    reportId: string,
    reportDate: string,
): Promise<DownloadOutcome> {
    const service = host.downloadService;
    if (service && (await allowed(service))) {
        try {
            const pdf = await client.reportPdf(reportId);
            const result = await service.exportVisualsContentExtended(
                pdf,
                `summary-${reportDate}.pdf`,
                "base64",
                "Daily summary report",
            );
            return result.downloadCompleted ? "downloaded" : "cancelled";
        } catch {
            // e.g. not supported in this host after all: fall back to the link
        }
    }
    const link = await client.downloadLink(reportId);
    host.launchUrl(client.url(link.url_path));
    return "opened";
}

async function allowed(service: NonNullable<DownloadHost["downloadService"]>): Promise<boolean> {
    try {
        return ((await service.exportStatus()) as number) === PRIVILEGE_ALLOWED;
    } catch {
        return false;
    }
}
