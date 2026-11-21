import type { AuthOperationOptions, Credential, CredentialInfo, CredentialStore } from "@earendil-works/pi-ai";
/** Async credential store overlay for non-persistent runtime API keys. */
export declare class RuntimeCredentials implements CredentialStore {
    private readonly store;
    private readonly overrides;
    /** Modifications not yet written or failed, for example an OAuth refresh. */
    private readonly pendingModifications;
    constructor(store: CredentialStore);
    setRuntimeApiKey(providerId: string, apiKey: string): void;
    removeRuntimeApiKey(providerId: string): void;
    hasRuntimeApiKey(providerId: string): boolean;
    read(providerId: string, options?: AuthOperationOptions): Promise<Credential | undefined>;
    list(options?: AuthOperationOptions): Promise<readonly CredentialInfo[]>;
    modify(providerId: string, fn: (current: Credential | undefined) => Promise<Credential | undefined>, options?: AuthOperationOptions): Promise<Credential | undefined>;
    /**
     * Resolves when every modification started so far is written or failed. An OAuth refresh may
     * already have spent the single-use refresh token, so exiting before its result is written would
     * leave only the spent token in auth.json.
     */
    settled(): Promise<void>;
    delete(providerId: string, options?: AuthOperationOptions): Promise<void>;
}
//# sourceMappingURL=runtime-credentials.d.ts.map