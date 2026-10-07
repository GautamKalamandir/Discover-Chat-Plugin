"use strict";

import { formattingSettings } from "powerbi-visuals-utils-formattingmodel";

import { config } from "./config";
import { ModelOption } from "./api/types";

import FormattingSettingsCard = formattingSettings.SimpleCard;
import FormattingSettingsModel = formattingSettings.Model;

const NONE = { value: "", displayName: "(not selected)" };

/** Format pane → Data source: the semantic model this chatbot answers from (Q7). */
class DataSourceCard extends FormattingSettingsCard {
    modelId = new formattingSettings.ItemDropdown({
        name: "modelId",
        displayName: "Semantic model",
        description: "Only models you can access are listed. Each viewer's access is checked again.",
        items: [NONE],
        value: NONE,
    });

    name = "dataSource";
    displayName = "Data source";
    slices = [this.modelId];
}

class AppearanceCard extends FormattingSettingsCard {
    title = new formattingSettings.TextInput({
        name: "title",
        displayName: "Title",
        placeholder: "Discover Chat Bot",
        value: "Discover Chat Bot",
    });

    name = "appearance";
    displayName = "Appearance";
    slices = [this.title];
}

/** Only shown in development builds (authMode "dev"). */
class DeveloperCard extends FormattingSettingsCard {
    devUser = new formattingSettings.TextInput({
        name: "devUser",
        displayName: "Dev user (local only)",
        placeholder: "user-a",
        value: "user-a",
    });

    name = "developer";
    displayName = "Developer";
    slices = [this.devUser];
}

export class VisualFormattingSettingsModel extends FormattingSettingsModel {
    dataSource = new DataSourceCard();
    appearance = new AppearanceCard();
    developer = new DeveloperCard();

    cards: FormattingSettingsCard[] =
        config.authMode === "dev"
            ? [this.dataSource, this.appearance, this.developer]
            : [this.dataSource, this.appearance];

    /** Fills the model dropdown with the models the signed-in author can access, keeping the
     * saved choice selected even when it isn't in the list (e.g. not loaded yet). */
    applyModelChoice(models: ModelOption[], current: string | null): void {
        const items = [NONE, ...models.map((m) => ({ value: m.id, displayName: m.name }))];
        if (current && !items.some((i) => i.value === current)) {
            items.push({ value: current, displayName: current });
        }
        this.dataSource.modelId.items = items;
        this.dataSource.modelId.value = items.find((i) => i.value === current) ?? NONE;
    }
}
