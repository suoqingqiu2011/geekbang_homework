import { Container, type MarkdownTheme } from "@earendil-works/pi-tui";
import type { MarkdownTransformer } from "../../../core/extensions/types.ts";
/**
 * Component that renders a user message
 */
export declare class UserMessageComponent extends Container {
    private text;
    private markdownTheme;
    private outputPad;
    private markdownTransformers;
    private messageId?;
    private timestamp?;
    constructor(text: string, markdownTheme?: MarkdownTheme, outputPad?: number, markdownTransformers?: readonly MarkdownTransformer[], messageId?: string, timestamp?: string);
    setOutputPad(padding: number): void;
    private rebuild;
    render(width: number): string[];
}
//# sourceMappingURL=user-message.d.ts.map