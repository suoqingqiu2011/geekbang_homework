/** An MCP tool's input schema in the shape LLM tool APIs accept. */
/**
 * The tool's input schema as an object schema. Tool parameters must be objects, MCP servers may omit
 * `type`, and some providers reject object schemas without `properties`. Other keywords are kept.
 */
export declare function normalizeMcpInputSchema(schema: Record<string, unknown>): Record<string, unknown>;
//# sourceMappingURL=input-schema.d.ts.map