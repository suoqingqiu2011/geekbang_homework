import { type JsonRpcMessage } from "../protocol/jsonrpc.ts";
import { type McpTransport, TransportEvents } from "./transport.ts";
/** A running program's stdio, as `@earendil-works/pi-env`'s `RemoteProcess` provides it. */
export interface McpPipe {
    write(data: Uint8Array | string): void;
    closeStdin(): void;
    /** Kill the program (and what it started). The pipe owns cleaning up what the program started. */
    close(): void;
    /** Settles when the program has ended, whatever the reason; it must also settle after `close()`. */
    readonly exited: Promise<unknown>;
}
/** Handlers `PipeTransportOptions.open` connects to the program's output. */
export interface McpPipeHandlers {
    onStdout(bytes: Uint8Array): void;
    onStderr(bytes: Uint8Array): void;
}
export interface PipeTransportOptions {
    /** Start the program; `onStdout`/`onStderr` receive its output from then on. */
    open(handlers: McpPipeHandlers): Promise<McpPipe>;
    maxMessageBytes?: number;
    maxStderrBytes?: number;
    onStderr?: (chunk: string) => void;
    /** After closing stdin, how long the program may take to exit before `close()` kills it. Default 2000. */
    closeTimeoutMs?: number;
}
/**
 * MCP's stdio transport over a program that something else runs, for example on another machine through pi-env.
 * The transport closes when the program ends; a rejected `exited` that `close()` did not cause is reported as an
 * error first.
 */
export declare class PipeTransport extends TransportEvents implements McpTransport {
    readonly options: Readonly<PipeTransportOptions>;
    private readonly lines;
    private pipe;
    private started;
    private closed;
    private ended;
    constructor(options: PipeTransportOptions);
    /** The last `maxStderrBytes` (default 64 KiB) of the program's stderr. */
    get stderr(): string;
    start(): Promise<void>;
    send(message: JsonRpcMessage): Promise<void>;
    close(): Promise<void>;
    /** The program ended: report an unfinished message, then close. */
    private exit;
    /** `exited` rejected: an error unless `close()` caused it, then the same as an exit. */
    private fail;
}
//# sourceMappingURL=pipe.d.ts.map