import { Box, type MarkdownTheme } from "@earendil-works/pi-tui";
import type { MarkdownTransformer } from "../../../core/extensions/types.ts";
import type { CompactionSummaryMessage } from "../../../core/messages.ts";
/**
 * Component that renders a compaction message with collapsed/expanded state.
 * Uses same background color as custom messages for visual consistency.
 */
export declare class CompactionSummaryMessageComponent extends Box {
    private expanded;
    private message;
    private markdownTheme;
    private markdownTransformers;
    private messageId?;
    private timestamp?;
    constructor(message: CompactionSummaryMessage, markdownTheme?: MarkdownTheme, outputPad?: number, markdownTransformers?: readonly MarkdownTransformer[], messageId?: string, timestamp?: string);
    setExpanded(expanded: boolean): void;
    setOutputPad(outputPad: number): void;
    invalidate(): void;
    private updateDisplay;
}
//# sourceMappingURL=compaction-summary-message.d.ts.map