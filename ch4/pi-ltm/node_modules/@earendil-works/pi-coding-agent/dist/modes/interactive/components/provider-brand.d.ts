/**
 * Animated brand styles for the names of preferred partner providers in selectors: Radius shimmers in its logo
 * colors and OpenRouter glows in its brand color. Selectors apply them to the selected row only.
 */
import { type TUI } from "@earendil-works/pi-tui";
/** Colors `text` for animation time `elapsedMs`. */
export type ProviderBrandStyle = (text: string, elapsedMs: number) => string;
/** The animated style of a provider's name, or `undefined` when the provider has none. */
export declare function getProviderBrandStyle(providerId: string): ProviderBrandStyle | undefined;
/** Colors `text` with the Radius logo colors flowing left to right. */
export declare function radiusShimmer(text: string, elapsedMs: number): string;
/** Re-renders `tui` every animation frame while `isAnimating()` holds; returns the function that stops it. */
export declare function startBrandAnimation(tui: TUI, isAnimating: () => boolean): () => void;
//# sourceMappingURL=provider-brand.d.ts.map