/**
 * How the authorization response of a sign-in reaches a local application: a loopback callback server
 * the browser redirects to, or a redirect URL the user pastes from the browser address bar.
 */
import { type OAuthCallbackPage, OAuthCallbackServer } from "./callback.ts";
import { type McpClientMetadataDocuments, type McpOAuthSettings } from "./client-settings.ts";
import type { McpOAuthStateStore } from "./provider.ts";
import { type McpAuthorizationResponse, type McpSignInRedirect } from "./sign-in.ts";
/** A loopback callback server and the redirect URI it serves. */
export interface McpLoopbackCallback {
    callback: OAuthCallbackServer;
    /** Pass to `startMcpSignIn` and `completeMcpSignIn`. */
    redirectUrl: string;
}
/**
 * Listen for the authorization response of a sign-in on loopback, where `settings` say: `callbackUrl`
 * and `callbackPort`, else the port of the stored client's redirect URI (so that client stays valid) or
 * a free one. Pass the sign-in's `clientMetadataDocuments`, so the callback server also serves the
 * server-specific redirect URI. Close the callback server when the sign-in ends.
 */
export declare function listenForLoopbackSignIn(options: {
    serverUrl: string;
    store: McpOAuthStateStore;
    settings: McpOAuthSettings;
    clientMetadataDocuments?: McpClientMetadataDocuments;
    /** Renders the browser page after the redirect. Default: plain text. */
    renderPage?: (page: OAuthCallbackPage) => string;
}): Promise<McpLoopbackCallback>;
/**
 * Wait for the authorization response on a loopback callback server, or for a redirect URL the user
 * pastes from the browser address bar, whichever comes first. Aborting `signal` stops the wait with
 * `McpSignInCancelledError`.
 */
export declare function waitForLoopbackSignIn(options: {
    callback: OAuthCallbackServer;
    /** The `redirect` result of `startMcpSignIn`. */
    authorization: McpSignInRedirect;
    /**
     * Ask for the redirect URL, for when the browser cannot reach the loopback callback (for example over
     * SSH). Aborted once the callback arrives or the sign-in is cancelled. Resolving to `undefined` or an
     * empty string cancels the sign-in.
     */
    promptForRedirectUrl?: (signal: AbortSignal) => Promise<string | undefined>;
    signal?: AbortSignal;
}): Promise<McpAuthorizationResponse>;
/**
 * The authorization response in a redirect URL the user pasted from the browser address bar. Fails when
 * the URL is not on the sign-in's redirect URI, carries another `state`, or reports an error.
 */
export declare function responseFromRedirectUrl(input: string, authorization: Pick<McpSignInRedirect, "state" | "redirectUrl">): McpAuthorizationResponse;
//# sourceMappingURL=loopback.d.ts.map