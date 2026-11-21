export declare function loadAllHighlightLanguages(): Promise<void>;
export type HighlightFormatter = (text: string) => string;
export type HighlightTheme = Partial<Record<string, HighlightFormatter>>;
export interface HighlightOptions {
    language?: string;
    ignoreIllegals?: boolean;
    languageSubset?: string[];
    theme?: HighlightTheme;
}
export declare function renderHighlightedHtml(html: string, theme?: HighlightTheme): string;
export declare function highlight(code: string, options?: HighlightOptions): string;
export declare function supportsLanguage(name: string): boolean;
/**
 * Number of registered grammars. It grows when the deferred grammars load, which can change the highlighting of
 * code that embeds another language, such as HTML in a JavaScript template literal.
 */
export declare function getHighlightLanguageCount(): number;
//# sourceMappingURL=syntax-highlight.d.ts.map