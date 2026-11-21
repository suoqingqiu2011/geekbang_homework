/**
 * Names of MCP tools and of their servers' namespaces as models and code mode scripts see them.
 * Applications that name tools the same way rank and group them the same way in tool search and
 * code mode.
 */
/**
 * `mcp__<server>__<tool>`, sanitized and shortened with a hash suffix when too long. Like Codex,
 * everything but `[A-Za-z0-9_]` becomes `_`, so the name is also the identifier code mode scripts
 * call it by. `isTaken` reports names used by a different MCP tool: sanitizing can map two tools to
 * one name (`a-b` and `a_b`), which then get the hash suffix.
 */
export declare function createMcpToolName(server: string, tool: string, isTaken?: (name: string) => boolean): string;
/**
 * Namespace of a server's tools: `mcp__<server>` with `-` replaced by `_`, like the tool names. Server
 * names that differ only in `-` and `_` share a namespace, so a configuration must not have both.
 */
export declare function mcpNamespace(server: string): string;
//# sourceMappingURL=naming.d.ts.map