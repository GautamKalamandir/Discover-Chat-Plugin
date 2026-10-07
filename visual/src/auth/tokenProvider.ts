import type powerbi from "powerbi-visuals-api";

type IAcquireAADTokenService = powerbi.extensibility.IAcquireAADTokenService;

// powerbi.PrivilegeStatus is a `const enum` in a declaration file, which disappears in transpile-only
// builds and tests, so its documented order is mirrored here.
export const PRIVILEGE_ALLOWED = 0;
export const PRIVILEGE_NOT_DECLARED = 1;
export const PRIVILEGE_NOT_SUPPORTED = 2;
export const PRIVILEGE_DISABLED_BY_ADMIN = 3;

export type AuthProblem = "not_supported" | "disabled_by_admin" | "not_declared" | "sign_in_failed";

export const AUTH_MESSAGES: Record<AuthProblem, string> = {
    not_supported: "The chatbot isn't available in this view of Power BI.",
    disabled_by_admin: "Your Power BI administrator hasn't enabled sign-in for custom visuals.",
    not_declared: "This version of the chatbot is not configured for sign-in.",
    sign_in_failed: "We couldn't sign you in. Please reload the report.",
};

export class AuthUnavailableError extends Error {
    constructor(readonly problem: AuthProblem) {
        super(AUTH_MESSAGES[problem]);
        this.name = "AuthUnavailableError";
    }
}

export interface TokenProvider {
    /** A bearer token for the backend; `forceRefresh` after the backend rejected the last one. */
    getToken(forceRefresh?: boolean): Promise<string>;
}

/** What the diagnostics panel may show about sign-in: status and expiry, never the token. */
export interface TokenStatus {
    privilegeStatus: number | null;
    expiresAt: number | null;
    error: AuthProblem | null;
}

const REFRESH_MARGIN_MS = 2 * 60 * 1000;

/** Power BI SSO (Q1): the token of the user signed in to Power BI, for our backend's audience. */
export class EntraTokenProvider implements TokenProvider {
    private cached: { token: string; expiresAt: number } | null = null;
    private pending: Promise<string> | null = null;
    private lastStatus: number | null = null;
    private lastError: AuthProblem | null = null;

    constructor(
        private readonly service: IAcquireAADTokenService,
        private readonly now: () => number = Date.now,
    ) {}

    getToken(forceRefresh = false): Promise<string> {
        if (!forceRefresh && this.cached && this.cached.expiresAt - REFRESH_MARGIN_MS > this.now()) {
            return Promise.resolve(this.cached.token);
        }
        this.pending ??= this.acquire().finally(() => {
            this.pending = null;
        });
        return this.pending;
    }

    describe(): TokenStatus {
        return {
            privilegeStatus: this.lastStatus,
            expiresAt: this.cached?.expiresAt ?? null,
            error: this.lastError,
        };
    }

    private async acquire(): Promise<string> {
        try {
            const token = await this.acquireOnce();
            this.lastError = null;
            return token;
        } catch (e) {
            this.lastError = e instanceof AuthUnavailableError ? e.problem : "sign_in_failed";
            throw e;
        }
    }

    private async acquireOnce(): Promise<string> {
        const status = (await this.service.acquireAADTokenstatus()) as number;
        this.lastStatus = status;
        if (status === PRIVILEGE_NOT_SUPPORTED) throw new AuthUnavailableError("not_supported");
        if (status === PRIVILEGE_DISABLED_BY_ADMIN) throw new AuthUnavailableError("disabled_by_admin");
        if (status === PRIVILEGE_NOT_DECLARED) throw new AuthUnavailableError("not_declared");
        if (status !== PRIVILEGE_ALLOWED) throw new AuthUnavailableError("sign_in_failed");

        const result = await this.service.acquireAADToken();
        if (!result?.accessToken) throw new AuthUnavailableError("sign_in_failed");
        this.cached = { token: result.accessToken, expiresAt: toEpochMs(result.expiresOn, this.now()) };
        return result.accessToken;
    }
}

/** Seconds or milliseconds since the epoch (the API doesn't say which); unknown → 5 minutes. */
export function toEpochMs(expiresOn: number | undefined, now: number): number {
    if (!expiresOn) return now + 5 * 60 * 1000;
    return expiresOn < 1e12 ? expiresOn * 1000 : expiresOn;
}

/** Local development only (authMode "dev"): the backend's dev token endpoint (ADR 0010). */
export class DevTokenProvider implements TokenProvider {
    private cached: { token: string; expiresAt: number; user: string } | null = null;

    constructor(
        private readonly apiBaseUrl: string,
        private user: string,
        private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
        private readonly now: () => number = Date.now,
    ) {}

    setUser(user: string): void {
        this.user = user.trim() || "user-a";
    }

    async getToken(forceRefresh = false): Promise<string> {
        const fresh = this.cached && this.cached.expiresAt - REFRESH_MARGIN_MS > this.now();
        if (!forceRefresh && fresh && this.cached?.user === this.user) return this.cached.token;
        const response = await this.fetchImpl(`${this.apiBaseUrl}/api/v1/dev/token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ oid: this.user }),
        });
        if (!response.ok) throw new AuthUnavailableError("sign_in_failed");
        const body = (await response.json()) as { access_token: string; expires_in: number };
        this.cached = {
            token: body.access_token,
            expiresAt: this.now() + body.expires_in * 1000,
            user: this.user,
        };
        return body.access_token;
    }
}
