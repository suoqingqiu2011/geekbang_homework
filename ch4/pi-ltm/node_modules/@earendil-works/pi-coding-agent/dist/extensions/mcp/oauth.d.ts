/**
 * OAuth sign-in for remote MCP servers: pi's credential store and its side of the sign-in.
 *
 * Connections never start a browser flow on their own. They send the stored access token and, after
 * a 401, try the stored refresh token. When that is not possible they fail with
 * `McpOAuthAuthorizationRequiredError`, and the user signs in through `/mcp` or `pi mcp login`, which
 * run pi-mcp's sign-in (PKCE, pi's Client ID Metadata Document or dynamic client registration) against
 * a loopback callback or a pasted redirect URL.
 *
 * Credentials live in `<agent-dir>/mcp-auth.json`, keyed by server name and URL.
 */
import { type McpAuthProvider, type McpOAuthServerStore, type McpOAuthSettings, type McpOAuthState, type OAuthChallenge } from "@earendil-works/pi-mcp/oauth";
import { type AuthStorageBackend } from "../../core/auth-storage.ts";
/** Per-server OAuth state (client registration, tokens, pending PKCE verifier) in `mcp-auth.json`. */
export declare class McpOAuthCredentialStore {
    private readonly backend;
    /** Directory for the refresh lock files. Without one, refreshes are only serialized in this process. */
    private readonly lockDir;
    constructor(backend?: AuthStorageBackend, lockDir?: string);
    forServer(name: string, serverUrl: string): McpOAuthServerStore;
    /**
     * A lock file per server. When the process exits, proper-lockfile removes the locks it holds; when
     * it is killed, the lock goes stale because it is no longer renewed, and the next process takes it over.
     * Throws `McpRefreshLockBusyError` when another process holds the lock for the whole wait.
     */
    private withRefreshLock;
    /** The stored tokens of a server, for noticing sign-ins done by another process. Does not take over legacy state. */
    tokens(name: string, serverUrl: string): McpOAuthState["tokens"];
    /** Returns whether credentials were stored for the server. Removes legacy state the server would take over. */
    remove(name: string, serverUrl: string): boolean;
    private write;
}
/** pi-mcp's `createMcpAuthProvider` with pi's client identity. */
export declare function createMcpAuthProvider(options: {
    serverUrl: string;
    store: McpOAuthServerStore;
    settings: () => McpOAuthSettings;
    onChallenge: (challenge: OAuthChallenge) => void;
}): McpAuthProvider;
export interface McpSignInPrompt {
    /** Show the authorization URL to the user and open it in a browser. */
    showAuthorizationUrl(url: URL): void;
    /**
     * Ask for the redirect URL from the browser address bar, for when the browser cannot reach the
     * loopback callback (for example over SSH). Aborted once the callback arrives or the sign-in is
     * cancelled. Resolves to `undefined` or an empty string when the user cancels.
     */
    promptForRedirectUrl(signal: AbortSignal): Promise<string | undefined>;
}
/**
 * Sign in to an MCP server. Uses the stored refresh token when possible; otherwise runs the browser
 * authorization code flow against a loopback callback or a pasted redirect URL. Tokens are saved to
 * `store`. Aborting `signal` stops the sign-in at any step with `McpSignInCancelledError`; requests to
 * the authorization server are also time-limited.
 */
export declare function signInMcpServer(options: {
    serverUrl: string;
    store: McpOAuthServerStore;
    settings: McpOAuthSettings;
    challenge?: OAuthChallenge;
    prompt: McpSignInPrompt;
    signal?: AbortSignal;
}): Promise<void>;
//# sourceMappingURL=oauth.d.ts.map