/**
 * Sign-in to an OAuth-protected MCP server, in two halves so an application can complete it in another
 * request with the same state store: `startMcpSignIn` refreshes the stored tokens when it can, or else
 * prepares the browser step and returns the authorization URL; `completeMcpSignIn` exchanges the
 * authorization code. The code arrives through a loopback callback server (`loopback.ts`), the
 * application's own https callback route, or a redirect URL the user pastes.
 */
import type { OAuthCallback } from "./callback.ts";
import { type McpOAuthClientOptions } from "./client-settings.ts";
import type { OAuthChallenge } from "./types.ts";
/** Options of both halves of a sign-in. Pass the same values to `startMcpSignIn` and `completeMcpSignIn`. */
export interface McpSignInOptions extends McpOAuthClientOptions {
    /**
     * Redirect URI chosen by the caller: a loopback one (`listenForLoopbackSignIn`) or an https callback
     * route of the application. Pass the same one to both halves: the authorization request may use a
     * variant specific to the MCP server (`McpSignInRedirect.redirectUrl`), which the flow derives from it.
     */
    redirectUrl: string;
    /** The server's challenge that asked for the sign-in; its resource metadata URL and scope apply. */
    challenge?: OAuthChallenge;
    /**
     * Aborting stops the sign-in at once with `McpSignInCancelledError`, also while `startMcpSignIn` waits
     * for the store's refresh lock. A refresh already sent still completes and saves its tokens.
     */
    signal?: AbortSignal;
}
/** The browser step of a sign-in, from `startMcpSignIn`. Plain data, so an application can persist it. */
export interface McpSignInRedirect {
    kind: "redirect";
    /** Open in the user's browser. */
    authorizationUrl: string;
    /** The redirect URI of the authorization request; the authorization response must arrive on it. */
    redirectUrl: string;
    /** The `state` parameter the authorization response must carry. */
    state: string;
    /** The scope requested, if any. Saved with the tokens when the token response names none. */
    scope?: string;
}
/** Result of `startMcpSignIn`: signed in by refreshing the stored tokens, or the browser step. */
export type McpSignInStart = {
    kind: "authorized";
} | McpSignInRedirect;
/** An authorization response: from a callback server, a callback route, or `responseFromRedirectUrl`. */
export type McpAuthorizationResponse = OAuthCallback;
/** The user or the application cancelled a sign-in (its `signal` was aborted, or the user gave up). */
export declare class McpSignInCancelledError extends Error {
    constructor();
}
/**
 * First half of a sign-in. Without a step-up (`insufficient_scope`), it first tries to refresh the stored
 * tokens with the stored client, whatever `redirectUrl` is (`authorized`). The refresh takes the store's
 * refresh lock, so never call this while holding it; when another process holds the lock for the whole
 * wait (`McpRefreshLockBusyError`), the refresh is skipped, and any other failure of the lock fails the
 * sign-in. Otherwise it prepares the browser step (`redirect`): a fresh `state`, and a stored client that
 * cannot use `redirectUrl` is dropped with its tokens. Requests to the authorization server are
 * time-limited.
 */
export declare function startMcpSignIn(options: McpSignInOptions): Promise<McpSignInStart>;
/**
 * Second half of a sign-in: exchanges the authorization code of `response` and saves the tokens. Pass
 * the options given to `startMcpSignIn`, including its `redirectUrl` (not `authorization.redirectUrl`).
 * Fails when the response carries another `state`, when another sign-in started with the store since, or
 * when the exchange would use another redirect URI than the authorization request (the options changed).
 * A redirect URL the user pasted goes through `responseFromRedirectUrl` first, which checks its path.
 */
export declare function completeMcpSignIn(options: McpSignInOptions & {
    /** The `redirect` result of `startMcpSignIn`. */
    authorization: McpSignInRedirect;
    response: McpAuthorizationResponse;
}): Promise<void>;
/** Run `fn`, reporting an abort of `signal` as `McpSignInCancelledError`. */
export declare function cancellable<T>(signal: AbortSignal | undefined, fn: () => Promise<T>): Promise<T>;
//# sourceMappingURL=sign-in.d.ts.map