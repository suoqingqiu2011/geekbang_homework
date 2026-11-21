export interface ModelSearchItem {
    id: string;
    provider: string;
    name?: string;
}
export declare function getModelSearchText(item: ModelSearchItem): string;
/**
 * Returns the model name when it has words the ID does not, e.g. a renamed override like
 * "MiniMax M3 [rasmus@openrouter, parasail]" for "minimax/minimax-m3". Names that only
 * restate the ID with different casing, separators, or a dropped namespace/version suffix
 * ("Claude Sonnet 4.5 (EU)" for "eu.anthropic.claude-sonnet-4-5-20250929-v1:0") return undefined.
 */
export declare function getDistinctModelName(item: {
    id: string;
    name?: string;
}): string | undefined;
/**
 * The /model selector search should rank exact provider-prefixed queries before proxy-provider IDs
 * like openrouter/openai/gpt-5, so keep the bare model ID out of the leading position.
 */
export declare function getModelSelectorSearchText(item: ModelSearchItem): string;
//# sourceMappingURL=model-search.d.ts.map