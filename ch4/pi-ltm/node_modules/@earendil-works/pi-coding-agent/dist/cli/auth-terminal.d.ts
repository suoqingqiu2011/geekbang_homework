/** Output shared by the terminal authentication commands. */
export interface AuthOutput {
    write(line: string): void;
    error(line: string): void;
}
/** Line-based terminal I/O for `pi auth login`. Prompts reject when `signal` aborts. */
export interface LoginTerminal extends AuthOutput {
    /** Whether stdin and stdout are a terminal; login prompts need one. */
    interactive: boolean;
    question(message: string, signal: AbortSignal): Promise<string>;
    /** Read a line without echoing it. */
    secret(message: string, signal: AbortSignal): Promise<string>;
    openUrl(url: string): void;
}
/** Terminal I/O on stdin and stdout. `onInterrupt` runs on Ctrl+C or EOF in a question, or Ctrl+C in a secret. */
export declare function createProcessLoginTerminal(onInterrupt: () => void): LoginTerminal;
//# sourceMappingURL=auth-terminal.d.ts.map