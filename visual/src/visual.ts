"use strict";

import powerbi from "powerbi-visuals-api";
import { FormattingSettingsService } from "powerbi-visuals-utils-formattingmodel";
import * as React from "react";
import { createRoot, Root } from "react-dom/client";
import "./../style/visual.less";

import { ApiClient } from "./api/client";
import { ModelOption, ReportFilter } from "./api/types";
import { DevTokenProvider, EntraTokenProvider, TokenProvider } from "./auth/tokenProvider";
import { config } from "./config";
import { reportFilters } from "./context/reportFilters";
import { dataViewSummary, filterSummary } from "./diagnostics/report";
import { VisualFormattingSettingsModel } from "./settings";
import { VisualConversationStore } from "./storage";
import { App, Theme } from "./ui/App";
import { DiagnosticsContext } from "./ui/DiagnosticsPanel";

import VisualConstructorOptions = powerbi.extensibility.visual.VisualConstructorOptions;
import VisualUpdateOptions = powerbi.extensibility.visual.VisualUpdateOptions;
import IVisual = powerbi.extensibility.visual.IVisual;
import IVisualHost = powerbi.extensibility.visual.IVisualHost;
import DataView = powerbi.DataView;

/** The saved Format-pane model id, read directly from the report's objects (Q7). */
function savedModelId(dataView: DataView | undefined): string | null {
    const value = dataView?.metadata?.objects?.["dataSource"]?.["modelId"];
    return typeof value === "string" && value ? value : null;
}

export class Visual implements IVisual {
    private readonly host: IVisualHost;
    private readonly root: Root;
    private readonly tokens: TokenProvider;
    private readonly client: ApiClient;
    private readonly conversations: VisualConversationStore;
    private readonly formattingSettingsService = new FormattingSettingsService();
    private formattingSettings = new VisualFormattingSettingsModel();
    private models: ModelOption[] = [];
    private modelId: string | null = null;

    constructor(options: VisualConstructorOptions) {
        this.host = options.host;
        this.tokens =
            config.authMode === "dev"
                ? new DevTokenProvider(config.apiBaseUrl, "user-a")
                : new EntraTokenProvider(this.host.acquireAADTokenService);
        this.client = new ApiClient(config.apiBaseUrl, this.tokens);
        this.conversations = new VisualConversationStore(this.host.storageV2Service);

        const container = document.createElement("div");
        container.className = "discover-chatbot";
        options.element.appendChild(container);
        this.root = createRoot(container);
    }

    public update(options: VisualUpdateOptions): void {
        this.host.eventService.renderingStarted(options);
        try {
            const dataView = options.dataViews?.[0];
            this.formattingSettings = this.formattingSettingsService.populateFormattingSettingsModel(
                VisualFormattingSettingsModel,
                dataView,
            );
            this.modelId = savedModelId(dataView);
            this.formattingSettings.applyModelChoice(this.models, this.modelId);
            if (this.tokens instanceof DevTokenProvider) {
                this.tokens.setUser(String(this.formattingSettings.developer.devUser.value ?? ""));
            }

            const filters = reportFilters(dataView);
            this.root.render(
                React.createElement(App, {
                    client: this.client,
                    conversations: this.conversations,
                    modelId: this.modelId,
                    reportFilters: filters,
                    diagnostics: config.diagnostics ? this.diagnostics(dataView, filters) : undefined,
                    title: String(this.formattingSettings.appearance.title.value ?? ""),
                    theme: this.theme(),
                    onModels: this.onModels,
                }),
            );
            this.host.eventService.renderingFinished(options);
        } catch (error) {
            this.host.eventService.renderingFailed(options, String(error));
        }
    }

    public getFormattingModel(): powerbi.visuals.FormattingModel {
        this.formattingSettings.applyModelChoice(this.models, this.modelId);
        return this.formattingSettingsService.buildFormattingModel(this.formattingSettings);
    }

    public destroy(): void {
        this.root.unmount();
    }

    private readonly onModels = (models: ModelOption[]): void => {
        const changed = models.map((m) => m.id).join("|") !== this.models.map((m) => m.id).join("|");
        this.models = models;
        this.formattingSettings.applyModelChoice(models, this.modelId);
        // The list arrives after Power BI built the Format pane; ask for an update so the
        // dropdown is rebuilt (getFormattingModel runs again). Only on change: no update loop.
        if (changed) this.host.refreshHostData?.();
    };

    /** Phase 2 (S5): what Power BI gives the visual, as structure and counts only. */
    private diagnostics(dataView: DataView | undefined, filters: ReportFilter[]): DiagnosticsContext {
        const tokens = this.tokens;
        return {
            tokenStatus: () => (tokens instanceof EntraTokenProvider ? tokens.describe() : null),
            visualReport: {
                host: {
                    hostEnv: (this.host as { hostEnv?: unknown }).hostEnv ?? null,
                    locale: this.host.locale ?? null,
                    userAgent: navigator.userAgent,
                },
                savedObjects: dataView?.metadata?.objects ?? null,
                savedModelId: this.modelId,
                dataView: dataViewSummary(dataView),
                reportFilters: filterSummary(filters),
            },
        };
    }

    private theme(): Theme {
        const palette = this.host.colorPalette;
        return {
            foreground: palette.foreground?.value ?? "#252423",
            background: palette.background?.value ?? "#ffffff",
            accent: palette.foregroundSelected?.value ?? "#118dff",
            highContrast: Boolean(palette.isHighContrast),
        };
    }
}
