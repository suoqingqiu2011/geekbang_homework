import type { AgentTool, AgentToolResult, AgentToolUpdateCallback } from "@earendil-works/pi-agent-core";
import type { Static, TSchema } from "typebox";
import type { ExtensionToolContext, ToolDefinition } from "../extensions/types.ts";
/**
 * An `AgentTool` with a TypeBox parameter schema whose `execute` and `prepareArguments` are typed by TypeBox's
 * `Static`, as the built-in tools are (pi-agent-core types `params` through pi-schema, which maps only the common
 * TypeBox kinds). Spreads into a `ToolDefinition` of the same schema.
 */
export type TypeBoxAgentTool<TParams extends TSchema = TSchema, TDetails = any> = Omit<AgentTool<TParams, TDetails>, "prepareArguments" | "execute"> & {
    prepareArguments?: (args: unknown) => Static<TParams>;
    execute: (toolCallId: string, params: Static<TParams>, signal?: AbortSignal, onUpdate?: AgentToolUpdateCallback<TDetails>) => Promise<AgentToolResult<TDetails>>;
};
/** Creates the context for one tool call. */
export type ToolContextFactory = (toolCallId: string, signal: AbortSignal | undefined) => ExtensionToolContext;
/** Wrap a ToolDefinition into an AgentTool for the core runtime. */
export declare function wrapToolDefinition<TDetails = unknown>(definition: ToolDefinition<any, TDetails>, ctxFactory?: ToolContextFactory): AgentTool<any, TDetails>;
/** Wrap multiple ToolDefinitions into AgentTools for the core runtime. */
export declare function wrapToolDefinitions(definitions: ToolDefinition<any, any>[], ctxFactory?: ToolContextFactory): AgentTool<any>[];
/**
 * Synthesize a minimal ToolDefinition from an AgentTool.
 *
 * This keeps AgentSession's internal registry definition-first even when a caller
 * provides plain AgentTool overrides that do not include prompt metadata or renderers.
 */
export declare function createToolDefinitionFromAgentTool(tool: AgentTool<any>): ToolDefinition<any, unknown>;
//# sourceMappingURL=tool-definition-wrapper.d.ts.map