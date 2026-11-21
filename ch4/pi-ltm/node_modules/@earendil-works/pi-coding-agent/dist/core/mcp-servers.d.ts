/**
 * MCP server configuration and the servers extensions register with `pi.registerMcpServer()`.
 *
 * The core only validates and stores registrations. The MCP extension (built in, or another
 * extension that handles `mcp_servers_change`) connects them next to the servers from `mcp.json`.
 * The `mcpServers` shape itself (stdio and HTTP fields, `oauth`, `enabled`, `description`, `timeout`)
 * is pi-mcp's; this file adds pi's fields to it.
 */
import { type McpHttpServerConfig as McpHttpServerFields, type McpOAuthConfig as McpOAuthFields, type McpStdioServerConfig as McpStdioServerFields } from "@earendil-works/pi-mcp";
/** Whether a tool name matches any of the entries, each an exact name or a pattern. */
export declare function createToolNameMatcher(entries: readonly string[]): (name: string) => boolean;
/** MCP resource tools, which reach every server with resources. */
export declare const LIST_MCP_RESOURCES_TOOL = "list_mcp_resources";
export declare const LIST_MCP_RESOURCE_TEMPLATES_TOOL = "list_mcp_resource_templates";
export declare const READ_MCP_RESOURCE_TOOL = "read_mcp_resource";
/** Whether a tool comes from MCP: a server tool (`mcp__<server>__<tool>`) or a resource tool. */
export declare function isMcpToolName(name: string): boolean;
/**
 * - `codemode`: tools are callable from codemode scripts but neither declared to the model nor
 *   listed in the codemode description, which lists only the server's namespace. Scripts find them
 *   with `searchTools()`. `codemode-deferred` is accepted as an alias.
 * - `deferred`: not declared to the model until the `tool_search` tool loads them; the model then
 *   calls them directly. Does not need codemode.
 * - `direct`: tools are declared to the model like any other tool (and callable from codemode).
 * - `hidden`: tools are registered but unreachable.
 */
export type McpExposure = "codemode" | "deferred" | "direct" | "hidden";
/**
 * pi's fields of every server entry, beside the shared `mcpServers` fields of pi-mcp. `description`
 * and `timeout` are shared fields, declared again for pi's documentation.
 */
interface McpServerConfigBase {
    /** Default: `codemode`. */
    exposure?: McpExposure;
    /**
     * What the server offers, in a sentence. The `mcp_servers` system prompt section lists the server
     * with it, tool search ranks the server's tools by it, and codemode's `describeNamespace()` returns it.
     */
    description?: string;
    /**
     * Exposure of single tools, overriding `exposure`. Keys are tool names as the server offers them,
     * or patterns where `*` matches any characters. An exact name wins over patterns; among patterns
     * the first match in the object wins. `hidden` removes tools, so `"exposure": "hidden"` with
     * overrides for a few tools exposes only those.
     */
    toolExposure?: Record<string, McpExposure>;
    /** Per-request timeout in seconds. Progress notifications from the server reset it. Default: 60. */
    timeout?: number;
}
export interface McpStdioServerConfig extends McpStdioServerFields, McpServerConfigBase {
    /** Values may reference environment variables (`${NAME}`) or commands (`!cmd`). */
    env?: Record<string, string>;
    /** Relative paths resolve against the session working directory. */
    cwd?: string;
}
/** OAuth client settings for servers that do not support dynamic client registration. */
export interface McpOAuthConfig extends McpOAuthFields {
    /** May reference environment variables (`${NAME}`) or commands (`!cmd`). */
    clientSecret?: string;
    /**
     * `client_name` sent with dynamic client registration, for servers that only accept known clients.
     * Default: `pi`.
     */
    clientName?: string;
    /**
     * How pi identifies itself without `clientId`. `auto` (default): pi's Client ID Metadata Document on
     * pi.dev when the authorization server supports it for public clients, otherwise dynamic client
     * registration; with `clientName` or a `callbackUrl` other than `/callback` on localhost or 127.0.0.1,
     * always dynamic client registration. `cimd`: always the document, and sign-in fails without support.
     * `dcr`: always dynamic client registration.
     */
    clientRegistration?: "auto" | "dcr" | "cimd";
}
/**
 * Whether pi's Client ID Metadata Documents list a redirect URI for the loopback `callbackUrl`: they list
 * `/callback` on localhost and 127.0.0.1, on any port. Without `callbackUrl`, pi uses such a URI.
 */
export declare function isClientMetadataDocumentCallback(callbackUrl: string | undefined): boolean;
export interface McpHttpServerConfig extends McpHttpServerFields, McpServerConfigBase {
    /** Values may reference environment variables (`${NAME}`) or commands (`!cmd`). */
    headers?: Record<string, string>;
    oauth?: McpOAuthConfig;
    /**
     * Send the token of a pi provider (`/login <provider>`) instead of using OAuth. Not allowed in project
     * `mcp.json` files, and requires https except on loopback hosts, since it sends the credential to `url`.
     */
    auth?: {
        provider: string;
    };
}
export type McpServerConfig = McpStdioServerConfig | McpHttpServerConfig;
/** Exposure of one tool of a server: its `toolExposure` entry, else the server's `exposure`. */
export declare function getMcpToolExposure(config: McpServerConfig, toolName: string): McpExposure;
/**
 * Validate one server entry of the `mcpServers` shape: pi-mcp's shared fields and pi's own. Returns a
 * copy of the config with exposure aliases resolved, or an error message.
 */
export declare function validateMcpServerConfig(name: string, raw: unknown): McpServerConfig | string;
/** A server an extension registered with `pi.registerMcpServer()`. */
export interface RegisteredMcpServer {
    name: string;
    config: McpServerConfig;
    /** Path of the extension that registered the server. */
    extensionPath: string;
}
/** Servers registered by the extensions of one runtime. */
export declare class McpServerRegistry {
    private readonly servers;
    private changeListener;
    /** Register or replace a server. The caller checks ownership. */
    register(server: RegisteredMcpServer): void;
    /** Remove a server registered by `extensionPath`. Servers of other extensions are left alone. */
    unregister(name: string, extensionPath: string): void;
    get(name: string): RegisteredMcpServer | undefined;
    /** Copies of the registered servers, in registration order. */
    list(): RegisteredMcpServer[];
    /** Called after every change. The runner sets it when it binds, to emit `mcp_servers_change`. */
    setChangeListener(listener: (() => void) | undefined): void;
}
export {};
//# sourceMappingURL=mcp-servers.d.ts.map