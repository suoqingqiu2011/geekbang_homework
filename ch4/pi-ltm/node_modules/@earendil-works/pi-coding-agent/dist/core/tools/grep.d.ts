import { type Static, Type } from "typebox";
import type { ToolDefinition } from "../extensions/types.ts";
import { type TypeBoxAgentTool } from "./tool-definition-wrapper.ts";
import { type TruncationResult } from "./truncate.ts";
declare const grepSchema: Type.TObject<{
    pattern: Type.TString;
    path: Type.TOptional<Type.TString>;
    glob: Type.TOptional<Type.TString>;
    ignoreCase: Type.TOptional<Type.TBoolean>;
    literal: Type.TOptional<Type.TBoolean>;
    context: Type.TOptional<Type.TNumber>;
    limit: Type.TOptional<Type.TNumber>;
}>;
export declare const grepToolSystemPromptContribution: {
    readonly snippet: "Search file contents for patterns (respects .gitignore)";
    readonly guidelines: readonly [];
};
export type GrepToolInput = Static<typeof grepSchema>;
export interface GrepToolDetails {
    truncation?: TruncationResult;
    matchLimitReached?: number;
    linesTruncated?: boolean;
}
/**
 * Pluggable operations for the grep tool.
 * The search itself always runs the local ripgrep; to search a remote system, override the tool's execute().
 */
export interface GrepOperations {
    /** Check if path is a directory. Throws if path does not exist. */
    isDirectory: (absolutePath: string) => Promise<boolean> | boolean;
    /**
     * @deprecated Ignored. Context lines come directly from ripgrep. Override the tool's execute() for remote search.
     */
    readFile?: (absolutePath: string) => Promise<string> | string;
}
export interface GrepToolOptions {
    /** Custom operations for grep. Default: local filesystem plus ripgrep */
    operations?: GrepOperations;
}
export declare function createGrepToolDefinition(cwd: string, options?: GrepToolOptions): ToolDefinition<typeof grepSchema, GrepToolDetails | undefined>;
export declare function createGrepTool(cwd: string, options?: GrepToolOptions): TypeBoxAgentTool<typeof grepSchema>;
export {};
//# sourceMappingURL=grep.d.ts.map