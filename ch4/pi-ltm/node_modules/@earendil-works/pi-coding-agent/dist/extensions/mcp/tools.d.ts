/**
 * Adapts MCP tools to pi tool definitions.
 *
 * Results map onto pi's model-facing content (text and images). Text over 20KB keeps its start and
 * end with the middle cut out, like Codex does, and the full text is saved to a temp file the model
 * can read. Binary resources other than images are saved to temp files too, and resource links name
 * the `read_mcp_resource` tool. Codemode scripts receive the whole `CallToolResult` without `_meta`
 * (`content` blocks as sent by the server, `structuredContent`, `isError`), never truncated: it is
 * the tool's `structuredContent`, and every MCP tool declares a `CallToolResult` output schema. MCP
 * errors (`isError`) are error results for the model, but scripts still resolve to the result.
 */
import type { AgentToolResult } from "@earendil-works/pi-agent-core";
import type { ImageContent, TextContent } from "@earendil-works/pi-ai";
import { type CallToolResult, type ContentBlock, type McpOutputSaver, type McpRequestOptions, type Tool as McpTool } from "@earendil-works/pi-mcp";
import type { TSchema } from "typebox";
import type { ToolDefinition, ToolExposure, ToolNamespace, ToolRenderers } from "../../core/extensions/types.ts";
import { READ_MCP_RESOURCE_TOOL } from "../../core/mcp-servers.ts";
import type { McpExposure } from "./config.ts";
/**
 * Tool exposure of an MCP exposure. `codemode` and `deferred` both leave tools out of the codemode
 * description; they differ only in which tool the MCP extension activates to reach them.
 */
export declare function toToolExposure(exposure: McpExposure): ToolExposure;
/** Tool that reads the resources named by resource links. */
export { READ_MCP_RESOURCE_TOOL };
export interface McpToolDetails {
    server: string;
    tool: string;
    /** Temp file with the full text output, when the model-facing text was truncated. */
    fullOutputPath?: string;
}
/** Saves the full text of a truncated result, or a binary resource, to a temp file and returns its path. */
export declare function saveToTempFile(data: string | Uint8Array, extension: string): Promise<string>;
export interface McpToolCaller {
    callTool(name: string, args: Record<string, unknown>, options: McpRequestOptions): Promise<CallToolResult>;
}
/**
 * Output schema of every MCP tool: the `CallToolResult` scripts receive (without `_meta`, which
 * {@link convertMcpResult} drops), with the tool's own output schema as `structuredContent`.
 */
export declare function createMcpResultSchema(structuredContentSchema: Record<string, unknown> | undefined): TSchema;
export interface ConvertMcpResultOptions {
    /** Saves truncated text and binary resources. Default: a temp file. */
    saveOutput?: McpOutputSaver;
    /** Whether the server's resources can be read with `read_mcp_resource`, which resource links then name. */
    readableResources?: boolean;
}
/** Model-facing content of `server`'s content blocks, before the output limit. */
export declare function toModelContent(server: string, blocks: readonly ContentBlock[], options?: ConvertMcpResultOptions): Promise<(TextContent | ImageContent)[]>;
/** Convert an MCP result. `isError` results become error results that keep the structured result. */
export declare function convertMcpResult(server: string, tool: string, result: CallToolResult, options?: ConvertMcpResultOptions): Promise<AgentToolResult<McpToolDetails>>;
export declare function createMcpToolDefinition(options: {
    server: string;
    tool: McpTool;
    name: string;
    exposure: McpExposure;
    namespace: ToolNamespace;
    timeoutMs: number;
    getClient: () => Promise<McpToolCaller>;
    /** Whether `read_mcp_resource` can read the server's resources. */
    readableResources?: () => boolean;
}): ToolDefinition<TSchema, McpToolDetails>;
/** Renderers of calls to an MCP tool, labeled `server/tool`, also used before the tool is registered. */
export declare function createMcpToolRenderers(label: string): ToolRenderers;
//# sourceMappingURL=tools.d.ts.map