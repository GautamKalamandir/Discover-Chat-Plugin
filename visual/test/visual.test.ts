import { act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Visual } from "../src/visual";

function fakeHost() {
    return {
        eventService: { renderingStarted: vi.fn(), renderingFinished: vi.fn(), renderingFailed: vi.fn() },
        acquireAADTokenService: {
            acquireAADTokenstatus: vi.fn(async () => 2), // NotSupported, e.g. Teams/Embedded
            acquireAADToken: vi.fn(),
        },
        storageV2Service: { status: async () => 3 },
        colorPalette: {
            foreground: { value: "#111" },
            background: { value: "#fff" },
            foregroundSelected: { value: "#06f" },
            isHighContrast: false,
        },
    };
}

function update(visual: Visual, objects: Record<string, unknown> = {}) {
    visual.update({ dataViews: [{ metadata: { columns: [], objects } }], viewport: { width: 400, height: 500 } } as never);
}

afterEach(() => {
    document.body.replaceChildren();
});

describe("Visual", () => {
    it("renders the app and reports rendering events", async () => {
        const host = fakeHost();
        const element = document.createElement("div");
        document.body.appendChild(element);
        const visual = new Visual({ element, host } as never);

        await act(async () => update(visual, { dataSource: { modelId: "sales-ds" } }));

        expect(host.eventService.renderingFinished).toHaveBeenCalled();
        expect(element.querySelector("h2")?.textContent).toBe("Discover Chat Bot");
        await vi.waitFor(() =>
            expect(element.querySelector("[role=alert]")?.textContent).toMatch(/isn't available in this view/),
        );
        visual.destroy();
        expect(element.querySelector(".chat")).toBeNull();
    });

    it("keeps the saved model selected in the Format pane before models are loaded", async () => {
        const element = document.createElement("div");
        const visual = new Visual({ element, host: fakeHost() } as never);

        await act(async () => update(visual, { dataSource: { modelId: "sales-ds" } }));
        const model = visual.getFormattingModel();

        const json = JSON.stringify(model);
        expect(json).toContain('"value":"sales-ds"');
        expect(json).not.toContain("Developer"); // dev-only card absent in production builds
        visual.destroy();
    });
});
