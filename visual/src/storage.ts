// Remembers the current conversation id so a re-rendered visual (page switch, resize) can restore
// it (Q17). Uses Power BI's local-storage service when the admin allows it, otherwise memory only.

import type powerbi from "powerbi-visuals-api";

import { PRIVILEGE_ALLOWED } from "./auth/tokenProvider";

type StorageService = powerbi.extensibility.IVisualLocalStorageV2Service;

export interface ConversationStore {
    load(key: string): Promise<string | null>;
    save(key: string, sessionId: string | null): Promise<void>;
}

const memory = new Map<string, string>();

export class VisualConversationStore implements ConversationStore {
    private available: Promise<boolean>;

    constructor(private readonly service: StorageService | undefined) {
        this.available = service
            ? Promise.resolve(service.status())
                  .then((status) => (status as number) === PRIVILEGE_ALLOWED)
                  .catch(() => false)
            : Promise.resolve(false);
    }

    async load(key: string): Promise<string | null> {
        if (await this.available) {
            try {
                return (await this.service?.get(storageKey(key))) || null;
            } catch {
                return null; // not found
            }
        }
        return memory.get(key) ?? null;
    }

    async save(key: string, sessionId: string | null): Promise<void> {
        if (sessionId) memory.set(key, sessionId);
        else memory.delete(key);
        if (!(await this.available)) return;
        try {
            if (sessionId) await this.service?.set(storageKey(key), sessionId);
            else await this.service?.remove(storageKey(key));
        } catch {
            // storage is a convenience: losing it only means a new chat after re-render
        }
    }
}

function storageKey(key: string): string {
    return `discover-chat:${key}`;
}
