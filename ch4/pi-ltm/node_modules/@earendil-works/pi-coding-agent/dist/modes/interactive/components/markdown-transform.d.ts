import type { MarkdownTransformContext, MarkdownTransformer } from "../../../core/extensions/types.ts";
/** Applies extension Markdown transformers. `isStreaming` is read per transform, so a reused component sees a stream end. */
export declare function createMarkdownTransform(messageType: MarkdownTransformContext["messageType"], isStreaming: () => boolean, transformers: readonly MarkdownTransformer[], messageId?: string, timestamp?: string): (markdown: string, availableWidth: number) => string;
//# sourceMappingURL=markdown-transform.d.ts.map