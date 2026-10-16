import { useEffect, useState } from 'react';
import './NoInstallBanner.css';
import { isDismissed, setDismissed, resetDismissal } from './noInstallBannerStorage';

interface Props {
  /** null until the first snapshot arrives; empty array means nothing was discovered. */
  configDirs: string[] | null;
  connected: boolean;
}

/**
 * Full-width banner shown when the WebSocket is connected AND the server
 * reported zero config dirs across both providers. Disambiguates the two
 * empty-village states:
 *   - "Neither Claude Code nor Codex is installed"  → this banner
 *   - "At least one provider is installed but idle" → no banner
 * Dismiss is persisted (with one-time migration from the old Claude-only key)
 * so returning users don't see it every load.
 */
export function NoInstallBanner({ configDirs, connected }: Props) {
  const [dismissed, setDismissedState] = useState<boolean>(() => isDismissed());

  // Reset dismissal if a config dir appears later (user installed Claude Code
  // or Codex while the dashboard was open) — don't bury a helpful banner forever.
  useEffect(() => {
    if (configDirs !== null && configDirs.length > 0 && dismissed) {
      resetDismissal();
      setDismissedState(false);
    }
  }, [configDirs, dismissed]);

  if (!connected) return null;
  if (configDirs === null) return null;
  if (configDirs.length > 0) return null;
  if (dismissed) return null;

  const onDismiss = () => {
    setDismissedState(true);
    setDismissed();
  };

  return (
    <div className="no-install-banner" role="status">
      <span className="no-install-icon" aria-hidden>⚠</span>
      <div className="no-install-body">
        <div className="no-install-title">Codex·Claude Code 로그를 찾지 못했습니다</div>
        <div className="no-install-text">
          로그가 있는 <code>~/.claude*</code> or <code>~/.codex</code> 디렉터리가 없습니다.
          {' '}이 프로젝트 폴더에서 Codex 또는 Claude Code 세션을 시작하세요.
        </div>
      </div>
      <button
        className="no-install-dismiss"
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  );
}
