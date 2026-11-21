import type { McpHttpServerConfig } from "./mcp-servers.ts";
/** Change to the global mcp.json that points the Radius MCP server at a Radius login. */
export interface RadiusMcpServerPlan {
    mcpPath: string;
    name: string;
    config: McpHttpServerConfig;
}
/**
 * The global mcp.json entry that makes the Radius MCP server use the login of `providerId`, adding the server when
 * missing, or undefined when a global server already uses that login.
 */
export declare function planRadiusMcpServer(providerId: string, agentDir: string, cwd: string): RadiusMcpServerPlan | undefined;
//# sourceMappingURL=radius-mcp.d.ts.map