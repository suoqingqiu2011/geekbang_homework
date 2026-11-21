import { Container, type MarkdownTheme } from "@earendil-works/pi-tui";
import type { MarkdownTransformer, MessageRenderer } from "../../../core/extensions/types.ts";
import type { CustomMessage } from "../../../core/messages.ts";
/**
 * Component that renders a custom message entry from extensions.
 * Uses distinct styling to differentiate from user messages.
 */
export declare class CustomMessageComponent extends Container {
    private message;
    private customRenderer?;
    private box;
    private customComponent?;
    private markdownTheme;
    private _expanded;
    private outputPad;
    private markdownTransformers;
    private messageId?;
    private timestamp?;
    constructor(message: CustomMessage<unknown>, customRenderer?: MessageRenderer, markdownTheme?: MarkdownTheme, outputPad?: number, markdownTransformers?: readonly MarkdownTransformer[], messageId?: string, timestamp?: string);
    setExpanded(expanded: boolean): void;
    setOutputPad(outputPad: number): void;
    invalidate(): void;
    private rebuild;
}
//# sourceMappingURL=custom-message.d.ts.map