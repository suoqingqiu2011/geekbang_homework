/**
 * Shared command execution utilities for extensions and custom tools.
 */
/**
 * Options for executing shell commands.
 */
export interface ExecOptions {
    /** AbortSignal to cancel the command */
    signal?: AbortSignal;
    /** Timeout in milliseconds */
    timeout?: number;
    /** Working directory */
    cwd?: string;
    /**
     * Combined stdout/stderr byte limit; exceeding it terminates the command with `truncated: true`.
     * Nonnegative integer up to the runtime string limit. Default: 256 MiB, capped by that limit.
     */
    maxBuffer?: number;
}
/**
 * Result of executing a shell command.
 */
export interface ExecResult {
    stdout: string;
    stderr: string;
    code: number;
    /** The command was terminated by pi: timeout, abort, or output over `maxBuffer`. */
    killed: boolean;
    /** Output exceeded `maxBuffer`; stdout and stderr are incomplete and `code` is nonzero. */
    truncated: boolean;
}
/**
 * Execute a shell command and return stdout/stderr/code.
 * Supports timeout, abort signal, and an output cap (`maxBuffer`).
 */
export declare function execCommand(command: string, args: string[], cwd: string, options?: ExecOptions): Promise<ExecResult>;
//# sourceMappingURL=exec.d.ts.map