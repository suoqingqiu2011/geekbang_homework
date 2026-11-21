import { Box, type MarkdownTheme } from "@earendil-works/pi-tui";
import type { ParsedSkillBlock } from "../../../core/agent-session.ts";
import type { MarkdownTransformer } from "../../../core/extensions/types.ts";
/**
 * Component that renders a skill invocation message with collapsed/expanded state.
 * Uses same background color as custom messages for visual consistency.
 * Only renders the skill block itself - user message is rendered separately.
 */
export declare class SkillInvocationMessageComponent extends Box {
    private expanded;
    private skillBlock;
    private markdownTheme;
    private markdownTransformers;
    private messageId?;
    private timestamp?;
    constructor(skillBlock: ParsedSkillBlock, markdownTheme?: MarkdownTheme, outputPad?: number, markdownTransformers?: readonly MarkdownTransformer[], messageId?: string, timestamp?: string);
    setExpanded(expanded: boolean): void;
    setOutputPad(outputPad: number): void;
    invalidate(): void;
    private updateDisplay;
}
//# sourceMappingURL=skill-invocation-message.d.ts.map