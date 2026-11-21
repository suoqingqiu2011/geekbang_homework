/**
 * The OAuth client that sign-in and refreshes use for one MCP server: its settings, how it identifies
 * itself (a pre-registered client, a Client ID Metadata Document, or dynamic client registration), and
 * the `McpOAuthProvider` built from them.
 */
import type { McpFetch } from "../auth-provider.ts";
import { McpOAuthProvider, type McpOAuthStateStore } from "./provider.ts";
import type { OAuthClientInformationMixed } from "./types.ts";
/** Client settings of one MCP server, usually from the application's configuration. */
export interface McpOAuthSettings {
    /** Pre-registered client id. Without it, sign-in uses a Client ID Metadata Document or registers a client. */
    clientId?: string;
    /** Already resolved. */
    clientSecret?: string;
    /**
     * Port of the loopback callback server, for clients registered with a fixed redirect URI. Without
     * `callbackUrl`, the redirect URI is `http://127.0.0.1:<port>/callback`. Only `listenForLoopbackSignIn`
     * and the redirect URI of refreshes use it; `startMcpSignIn` and `completeMcpSignIn` take theirs from
     * the caller.
     */
    callbackPort?: number;
    /**
     * Loopback redirect URI registered for `clientId`, for example `http://localhost:8080/oauth/callback`.
     * Without a port, the callback server listens on `callbackPort` or a free port, which is added to the
     * URI (RFC 8252). Default: `http://127.0.0.1/callback`. Like `callbackPort`, only `listenForLoopbackSignIn`
     * and refreshes use it.
     */
    callbackUrl?: string;
    /** Scopes to request, separated by spaces. Default: the scopes the server advertises. */
    scope?: string;
    /** `client_name` for dynamic client registration. Default: the application's `clientName`. */
    clientName?: string;
    /**
     * How to identify without `clientId`. `auto` (default): a Client ID Metadata Document when the
     * authorization server supports them for public clients and the documents list the redirect URI,
     * otherwise dynamic client registration; with `clientName`, always dynamic client registration.
     * `cimd`: always the document, and sign-in fails without support. `dcr`: always dynamic client
     * registration.
     */
    clientRegistration?: "auto" | "dcr" | "cimd";
    /**
     * Authorization server metadata document to use instead of discovery through the server, for servers
     * that advertise a wrong authorization server or none. It is trusted as configured.
     */
    authServerMetadataUrl?: URL;
}
/**
 * The Client ID Metadata Documents an application publishes: `<baseUrl>/client.json`, and for each MCP
 * server `<baseUrl>/<callback ID>/client.json`, whose redirect URIs carry the path `<path>/<callback ID>`.
 */
export interface McpClientMetadataDocuments {
    /** For example `https://pi.dev/oauth`. */
    baseUrl: string;
    /** Whether the documents list `redirectUrl`. Loopback redirect URIs match on any port (RFC 8252). */
    listsRedirectUrl(redirectUrl: string): boolean;
}
/** State store of one MCP server, with a lock that keeps other processes from refreshing at the same time. */
export interface McpOAuthServerStore extends McpOAuthStateStore {
    /**
     * Run `fn` while no other process refreshes the server's tokens. Not reentrant: `startMcpSignIn` and
     * `createMcpAuthProvider` take it themselves, so never call them from `fn`. Throw
     * `McpRefreshLockBusyError` when another process holds the lock for the whole wait: `startMcpSignIn`
     * then goes to the browser step. Any other error fails the sign-in or refresh.
     */
    withRefreshLock<T>(fn: () => Promise<T>): Promise<T>;
}
/** The OAuth client of one MCP server: where its state lives, its settings, and the application's identity. */
export interface McpOAuthClientOptions {
    serverUrl: string;
    store: McpOAuthServerStore;
    settings: McpOAuthSettings;
    /** `client_name` for dynamic client registration unless `settings.clientName` names another: the application. */
    clientName: string;
    /** Omitted: dynamic client registration only. */
    clientMetadataDocuments?: McpClientMetadataDocuments;
}
/** An `McpOAuthProvider` for the client, redirecting to `redirectUrl` or the document's variant of it. */
export declare function createOAuthClientProvider(options: McpOAuthClientOptions, redirectUrl: string, onRedirect?: (url: URL) => void): McpOAuthProvider;
/**
 * Whether the client may identify with a Client ID Metadata Document instead of registering. `auto`
 * registers when the user asked for a client name or the documents do not list the redirect URI.
 */
export declare function offersClientMetadataDocument(settings: McpOAuthSettings, documents: McpClientMetadataDocuments | undefined, redirectUrl: string): boolean;
/** The path of the redirect URI of a server-specific Client ID Metadata Document: `<path>/<callback ID>`. */
export declare function serverSpecificPath(path: string, serverUrl: string): string;
/** Redirect URIs a stored client was registered with. */
export declare function registeredRedirectUrls(client: OAuthClientInformationMixed | undefined): string[];
/** `fetch` with `OAUTH_REQUEST_TIMEOUT_MS` per request, on top of the request's own signal. */
export declare function timedFetch(fetch?: McpFetch): McpFetch;
/** Redirect URI when the settings name none and no client is stored. */
export declare const DEFAULT_REDIRECT_URL = "http://127.0.0.1/callback";
/** Where the loopback callback server listens and the redirect URI it serves, from `callbackUrl` and `callbackPort`. */
export interface LoopbackSettings {
    /** The redirect URI, without the port unless one is configured. */
    url: string;
    /** Address to listen on. */
    host: string;
    /** Host name in the redirect URI. */
    redirectHost: string;
    port: number | undefined;
    path: string;
    /** The exact redirect URI, when the port is fixed. */
    fixedRedirectUrl: string | undefined;
}
/** The loopback callback settings of `settings`. */
export declare function loopbackSettings(settings: McpOAuthSettings): LoopbackSettings;
//# sourceMappingURL=client-settings.d.ts.map