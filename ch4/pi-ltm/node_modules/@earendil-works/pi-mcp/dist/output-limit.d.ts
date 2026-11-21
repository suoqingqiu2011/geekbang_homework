/**
 * The size limit of the model-facing text of MCP results. Longer text keeps its start and end with the
 * middle cut out, like Codex does, and the caller may save the full text where the model can read it.
 */
import type { LlmContent } from "./protocol/content.ts";
/** Model-facing text of an MCP result beyond this is cut in the middle. */
export declare const MCP_OUTPUT_MAX_BYTES: number;
/**
 * Saves data the model cannot get in full, such as the full text of a truncated result, and returns
 * where it was saved (a path the model can read). `extension` includes the dot, for example `.txt`.
 */
export type McpOutputSaver = (data: string | Uint8Array, extension: string) => Promise<string>;
/** Model-facing content within the limit, and where the full text went when it was saved. */
export interface LimitedMcpContent {
    content: LlmContent[];
    fullOutputPath?: string;
}
/**
 * Keep model-facing text within {@link MCP_OUTPUT_MAX_BYTES}. Longer text becomes one text block in
 * Codex's truncation format, followed by the path of the file with the full text when `saveOutput`
 * is given; images follow it. Content within the limit is returned as it is.
 */
export declare function limitMcpContent(content: LlmContent[], saveOutput?: McpOutputSaver): Promise<LimitedMcpContent>;
//# sourceMappingURL=output-limit.d.ts.map