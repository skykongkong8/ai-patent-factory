import type { BuildingPosition } from './types';

/**
 * The 8 interactive "protected" buildings — the editor can reposition them,
 * but it cannot add or remove entries. Mirrors the defaults from
 * client/src/game/data/building-layout.ts (BUILDING_DEFS).
 */
export interface ProtectedBuildingDefault {
  id: string;
  label: string;
  activity: string;
  x: number;
  y: number;
  defaultScale: number;
}

export const DEFAULT_PROTECTED_BUILDINGS: ProtectedBuildingDefault[] = [
  { id: 'library',    label: '자료실',    activity: 'reading',   x: 1010, y: 650,  defaultScale: 0.52 },
  { id: 'alchemist',  label: '정비실',  activity: 'debugging', x: 1010, y: 980,  defaultScale: 0.50 },
  { id: 'castle',     label: '설계실',     activity: 'thinking',  x: 1270, y: 650,  defaultScale: 0.46 },
  { id: 'watchtower', label: '검토실', activity: 'reviewing', x: 1530, y: 650,  defaultScale: 0.42 },
  { id: 'chapel',     label: '기록실',     activity: 'git',       x: 1530, y: 980,  defaultScale: 0.38 },
  { id: 'tavern',     label: '휴게실',     activity: 'idle',      x: 1790, y: 980,  defaultScale: 0.50 },
  { id: 'forge',      label: '작업장',      activity: 'editing',   x: 1270, y: 980,  defaultScale: 0.42 },
  { id: 'arena',      label: '실행실',      activity: 'bash',      x: 1790, y: 650,  defaultScale: 0.42 },
];

export const PROTECTED_BUILDING_IDS: readonly string[] = DEFAULT_PROTECTED_BUILDINGS.map((b) => b.id);

export const DEFAULT_WORLD = { width: 2800, height: 1800 } as const;

export function defaultBuildingPositions(): BuildingPosition[] {
  return DEFAULT_PROTECTED_BUILDINGS.map(({ id, x, y }) => ({ id, x, y }));
}
