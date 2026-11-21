/**
 * The auth provider for connections to an OAuth-protected MCP server: it sends the stored access token
 * and refreshes it, but never starts a browser flow. When the user has to sign in, it fails with
 * `McpOAuthAuthorizationRequiredError` and the application runs `startMcpSignIn`.
 */
import type { AuthProvider, McpFetch } from "../auth-provider.ts";
import { type McpOAuthClientOptions, type McpOAuthSettings } from "./client-settings.ts";
import { type OAuthFlowResult } from "./flow.ts";
import type { McpOAuthState } from "./provider.ts";
import type { OAuthChallenge } from "./types.ts";
/** An `AuthProvider` that can say when its refreshes have finished. */
export interface McpAuthProvider extends AuthProvider {
    /** Resolves when no refresh is running, so shutdown does not drop rotated tokens before they are saved. */
    settled(): Promise<void>;
}
/**
 * Auth provider for MCP connections: sends the stored access token and refreshes it when it is about
 * to expire or after a 401. Throws `McpOAuthAuthorizationRequiredError` when the user has to sign in,
 * including when the server asks for more scope (`insufficient_scope`). `onChallenge` receives the
 * server's `WWW-Authenticate` challenge so sign-in can use its resource metadata URL and scope.
 * `settings` is only called when a refresh is needed, so a secret that fails to resolve fails the
 * refresh instead of the whole connection setup.
 *
 * Many servers rotate refresh tokens, so two refreshes with the same refresh token lose the grant.
 * Requests in this process share one refresh, and other processes are kept out by the store's
 * refresh lock, held from reading the tokens to saving new ones. Tokens that changed meanwhile
 * (another process refreshed them, or the user signed in) are used without refreshing.
 */
export declare function createMcpAuthProvider(options: Omit<McpOAuthClientOptions, "settings"> & {
    settings: () => McpOAuthSettings;
    onChallenge: (challenge: OAuthChallenge) => void;
    /**
     * A redirect URI the application signs in with, such as its https callback route. Refreshing tokens
     * from a sign-in with a Client ID Metadata Document needs a redirect URI the documents list; a
     * refresh never redirects, so any listed one works. Default: the loopback redirect URI of `settings`.
     */
    redirectUrl?: string;
}): McpAuthProvider;
/**
 * Refresh the tokens of `state` with the stored client (or the document it signed in with), or report
 * `REDIRECT` when that is not possible. `options.redirectUrl` is a redirect URI the application signs in
 * with, tried before the loopback ones of the settings and the stored client. Never registers a client or
 * prepares an authorization request, so a pending sign-in stays usable. Call while holding the store's
 * refresh lock. Not cancellable: a refresh the server answered may have rotated the refresh token, so its
 * answer must be saved.
 */
export declare function refreshStoredTokens(client: McpOAuthClientOptions, state: McpOAuthState, options?: {
    fetch?: McpFetch;
    challenge?: OAuthChallenge;
    redirectUrl?: string;
}): Promise<OAuthFlowResult>;
//# sourceMappingURL=auth-provider.d.ts.map