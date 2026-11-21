/**
 * The `http:` or `https:` URL `value` names, normalized, or undefined. The shared launcher also opens files and custom
 * protocols, so login only opens web URLs. Normalizing percent-encodes control characters, which would otherwise end a
 * terminal hyperlink early.
 */
export declare function webUrl(value: string): string | undefined;
/**
 * `value` normalized as a web URL, else with control characters replaced: an untrusted string printed as is could end a
 * hyperlink early or send its own escape sequences to the terminal.
 */
export declare function displayUrl(value: string): string;
//# sourceMappingURL=web-url.d.ts.map