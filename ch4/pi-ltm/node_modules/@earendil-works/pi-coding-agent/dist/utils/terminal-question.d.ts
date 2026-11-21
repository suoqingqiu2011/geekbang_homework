/** Read one line from stdin, rejecting on abort, Ctrl+C, or EOF and always closing readline. */
export declare function questionTerminal(message: string, options: {
    output: NodeJS.WritableStream;
    signal: AbortSignal;
    /** Called on Ctrl+C or EOF, not when the caller aborts just this prompt. */
    onInterrupt?: () => void;
}): Promise<string>;
//# sourceMappingURL=terminal-question.d.ts.map