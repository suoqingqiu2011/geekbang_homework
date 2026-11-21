/**
 * The `mcpServers` configuration shape that MCP clients share, so configurations can be copied between
 * them: stdio servers (`command`, `args`, `env`, `cwd`), Streamable HTTP servers (`url`, `headers`,
 * `oauth`), and `enabled`, `description` and `timeout` for both.
 *
 * ```json
 * {
 *   "mcpServers": {
 *     "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
 *     "docs": { "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } }
 *   }
 * }
 * ```
 *
 * Applications add fields of their own: {@link validateMcpServerConfig} keeps fields it does not know
 * for the caller to check afterwards.
 */
/** Fields every server entry may have. */
interface McpServerConfigBase {
    /** What the server offers, in a sentence. */
    description?: string;
    /** Set to false to keep the entry without connecting. Default: true. */
    enabled?: boolean;
    /** Per-request timeout in seconds. */
    timeout?: number;
}
/** A server started as a local program that speaks MCP over stdin and stdout. */
export interface McpStdioServerConfig extends McpServerConfigBase {
    type?: "stdio";
    command: string;
    args?: string[];
    /** Added to the environment. The application decides whether values may reference variables (`${NAME}`). */
    env?: Record<string, string>;
    /** The application decides what relative paths resolve against. */
    cwd?: string;
}
/** OAuth client settings for servers that do not support dynamic client registration. */
export interface McpOAuthConfig {
    /** Pre-registered client id. Without it, the application registers a client with the authorization server. */
    clientId?: string;
    /** As configured; the application decides whether it may reference variables. */
    clientSecret?: string;
    /**
     * Port of the loopback callback server, for clients registered with a fixed redirect URI. Without
     * `callbackUrl`, the redirect URI is `http://127.0.0.1:<port>/callback`.
     */
    callbackPort?: number;
    /**
     * Redirect URI registered for `clientId`, for example `http://localhost:8080/oauth/callback`. It must
     * be an `http` URI on `localhost`, `127.0.0.1`, or `[::1]`. Without a port, the callback server
     * listens on `callbackPort` or a free port, which is added to the URI (RFC 8252).
     */
    callbackUrl?: string;
    /** Scopes to request, separated by spaces. Default: the scopes the server advertises. */
    scope?: string;
    /** `client_name` sent with dynamic client registration. Default: the application's name. */
    clientName?: string;
    /**
     * How the application identifies itself without `clientId`. `auto` (default): its Client ID Metadata
     * Document when the authorization server supports them for public clients, otherwise dynamic client
     * registration. `cimd`: always the document, and sign-in fails without support. `dcr`: always
     * dynamic client registration.
     */
    clientRegistration?: "auto" | "dcr" | "cimd";
    /**
     * Authorization server metadata document (RFC 8414 or OpenID Connect discovery) to use instead of
     * discovery through the server, for servers that advertise a wrong authorization server or none.
     * The document is trusted as configured. Must use https, except on loopback hosts.
     */
    authServerMetadataUrl?: string;
}
/** A server reached over Streamable HTTP. */
export interface McpHttpServerConfig extends McpServerConfigBase {
    type?: "http";
    url: string;
    /** Sent with every request. The application decides whether values may reference variables. */
    headers?: Record<string, string>;
    oauth?: McpOAuthConfig;
}
/** One entry of the `mcpServers` shape: a stdio or a Streamable HTTP server. */
export type McpServerConfig = McpStdioServerConfig | McpHttpServerConfig;
/**
 * A validated server entry, tagged with its transport, or why it is invalid. The config is the entry
 * as given: fields the validator does not know are kept, unchecked, for the caller to check.
 */
export type McpServerConfigResult = {
    kind: "stdio";
    config: McpStdioServerConfig & Record<string, unknown>;
} | {
    kind: "http";
    config: McpHttpServerConfig & Record<string, unknown>;
} | {
    kind: "invalid";
    error: string;
};
/** What the application adds to the validation of server entries. */
export interface McpServerConfigOptions {
    /**
     * Checks the `oauth.callbackUrl` (undefined: none configured) of an entry with
     * `oauth.clientRegistration: "cimd"`: the application's Client ID Metadata Documents must list it.
     * Returns an error message. Without this check, `"cimd"` is refused.
     */
    checkMetadataDocumentCallback?(callbackUrl: string | undefined): string | undefined;
}
/**
 * Validate one entry of the `mcpServers` shape (configuration files are a trust boundary). Server
 * names are letters, digits, `_` and `-`, which tool names and namespaces are built from.
 */
export declare function validateMcpServerConfig(name: string, raw: unknown, options?: McpServerConfigOptions): McpServerConfigResult;
/**
 * Whether an entry overrides a server defined elsewhere instead of defining one: it has no `command`,
 * `url` or `type`. Applications let a less trusted file (a project's) override a few fields, such as
 * `enabled`, of a server a trusted file defines; which fields is theirs to decide.
 */
export declare function isMcpServerOverride(entry: Record<string, unknown>): boolean;
export {};
//# sourceMappingURL=config.d.ts.map