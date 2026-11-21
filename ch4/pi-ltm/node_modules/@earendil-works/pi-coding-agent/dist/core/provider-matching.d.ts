/** Match provider IDs and display names exactly, ignoring case and surrounding whitespace in the reference. */
export declare function findMatchingProviders<T extends {
    id: string;
    name: string;
}>(providers: readonly T[], providerRef: string): T[];
//# sourceMappingURL=provider-matching.d.ts.map