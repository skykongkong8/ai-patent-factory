import type { ThemeId, ThemeManifest } from './types';
import { patkongFactoryTheme } from './patkong-factory';
import { tinySwordsCc0Theme } from './tiny-swords-cc0';

const THEMES: Record<ThemeId, ThemeManifest> = {
  'tiny-swords-cc0': tinySwordsCc0Theme,
  'patkong-factory': patkongFactoryTheme,
};

export function getActiveThemeId(): ThemeId {
  return 'patkong-factory';
}

export function getActiveTheme(): ThemeManifest {
  return THEMES[getActiveThemeId()];
}

/**
 * Scale values stored in a MapConfig (hero-scale, per-NPC scale) are
 * absolute numbers authored against the Tiny Swords baseline (0.5).
 * Kept as a passthrough so future themes with different native sizes
 * can rebase without migrating every stored MapConfig.
 */
export function rebaseSavedScale(savedScale: number): number {
  return savedScale;
}
