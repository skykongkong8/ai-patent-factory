import type { AssetManifest } from './types';
import { buildAssetManifestCc0 } from './asset-manifest-cc0';

export type ThemeId = 'tiny-swords-cc0' | 'patkong-factory';

export function buildAssetManifest(_theme: ThemeId | string = 'patkong-factory'): AssetManifest {
  const manifest = buildAssetManifestCc0();
  manifest.tilesets.unshift({ key: 'factory-floor', label: '청사진 바닥', path: '/assets/themes/patkong-factory/floor.png', tileWidth: 64, tileHeight: 64, columns: 1, rows: 1 });
  manifest.protectedBuildings = manifest.protectedBuildings.map((b) => ({ ...b, path: `/assets/themes/patkong-factory/${b.id}.png`, defaultScale: 0.85 }));
  return manifest;
}
