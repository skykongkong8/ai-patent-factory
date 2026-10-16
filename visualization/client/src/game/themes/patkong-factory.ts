import { tinySwordsCc0Theme } from './tiny-swords-cc0';
import type { ThemeManifest, UnitColor } from './types';

const base = 'assets/themes/patkong-factory';
const colors: UnitColor[] = ['blue', 'yellow', 'red', 'black', 'purple'];

export const patkongFactoryTheme: ThemeManifest = {
  ...tinySwordsCc0Theme,
  id: 'patkong-factory',
  name: '팥콩 팩토리',
  heroScale: 0.55,
  terrain: { tilesetKey: 'factory-floor', path: `${base}/floor.png`, tileSize: 64, grassFrame: 0 },
  getHeroPreload: () => colors.flatMap((color) => ['idle', 'run'].map((mode) => ({
    key: `patkong-${color}-${mode}`, path: `${base}/patkong-${color}.png`, frameWidth: 192, frameHeight: 192,
  }))),
  getHeroConfig: (color) => ({
    idleKey: `patkong-${color}-idle`, runKey: `patkong-${color}-run`,
    idleFrames: 6, runFrames: 6, idleFrameIndices: [0, 1, 2, 3, 4, 5],
    runFrameIndices: [6, 7, 8, 9, 10, 11], facesLeft: false, tint: null,
  }),
  getHeroPreview: (color) => ({
    url: `/${base}/patkong-${color}.png`, sheetColumns: 6, sheetRows: 2, frameWidth: 192, frameHeight: 192,
  }),
  getBuildingImage: (id) => `${base}/${id}.png`,
  getBuildingScale: () => 0.85,
};
