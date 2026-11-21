import { type JsonRpcMessage } from "../protocol/jsonrpc.ts";
export interface JsonRpcLineReaderOptions {
    maxMessageBytes?: number;
    maxStderrBytes?: number;
    /** Every stderr chunk, decoded as UTF-8. */
    onStderr?: (chunk: string) => void;
    onMessage(message: JsonRpcMessage): void;
    onError(error: unknown): void;
}
/**
 * Reads a program's output as MCP's stdio framing: newline-delimited JSON-RPC messages on stdout, and a bounded
 * tail of stderr for diagnostics.
 */
export declare class JsonRpcLineReader {
    private readonly options;
    /** The bytes of the incomplete message received so far. */
    private stdoutChunks;
    private stdoutPendingBytes;
    private stderrBuffer;
    constructor(options: JsonRpcLineReaderOptions);
    /** The last `maxStderrBytes` of stderr. */
    get stderr(): string;
    /**
     * Takes the next stdout bytes and reports every complete message, or the error parsing it. Bytes of an
     * incomplete message are kept as chunks and joined once when its newline arrives, so a large message that
     * arrives in many chunks is copied and scanned only once.
     */
    readStdout(chunk: Uint8Array): void;
    /** Takes the next stderr bytes into the tail and passes them to `onStderr`. */
    readStderr(chunk: Uint8Array): void;
    /** Ends stdout: a message without its newline is reported as an error and dropped. */
    end(): void;
}
//# sourceMappingURL=lines.d.ts.map