import { act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// A diagnostics build (npm run configure -- ... --diagnostics on).
vi.mock("../src/config", () => ({
    config: { apiBaseUrl: "https://chatbot-api.example.com", authMode: "entra", diagnostics: true },
}));

import { Visual } from "../src/visual";

afterEach(() => {
    document.body.replaceChildren();
});

describe("Visual (diagnostics build)", () => {
    it("shows the diagnostics panel", async () => {
        const host = {
            eventService: { renderingStarted: vi.fn(), renderingFinished: vi.fn(), renderingFailed: vi.fn() },
            acquireAADTokenService: { acquireAADTokenstatus: vi.fn(async () => 2), acquireAADToken: vi.fn() },
            storageV2Service: { status: async () => 3 },
            colorPalette: {},
            locale: "en-US",
        };
        const element = document.createElement("div");
        document.body.appendChild(element);
        const visual = new Visual({ element, host } as never);

        await act(async () =>
            visual.update({
                dataViews: [{ metadata: { columns: [], objects: { dataSource: { modelId: "sales-ds" } } } }],
                viewport: { width: 400, height: 500 },
            } as never),
        );

        expect(element.querySelector(".diagnostics summary")?.textContent).toBe("Diagnostics (Phase 2)");
        expect(element.textContent).toContain("Model: sales-ds");
        visual.destroy();
    });
});
