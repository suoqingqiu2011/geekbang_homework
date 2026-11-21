/**
 * Preferred partner providers and the order provider lists show them in. Kept free of UI code so the list can move
 * to pi-ai once provider lists are offered there.
 */
/** Ids of the preferred partner providers, most preferred first. */
export declare const PREFERRED_PROVIDER_IDS: readonly string[];
/** Whether the provider is a preferred partner. */
export declare function isPreferredProvider(providerId: string): boolean;
/** Orders providers: preferred partners first, in their order, then all others alphabetically by name. */
export declare function compareProviders(a: {
    id: string;
    name: string;
}, b: {
    id: string;
    name: string;
}): number;
//# sourceMappingURL=preferred-providers.d.ts.map