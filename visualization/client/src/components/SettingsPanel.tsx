import { useCallback, useEffect, useState } from 'react';
import type { AppSettings } from '../hooks/useSettings';
import { playChime } from '../notifications/sound';
import './SettingsPanel.css';

interface SettingsPanelProps {
  settings: AppSettings;
  onChange: (patch: Partial<AppSettings>) => void;
  onClose: () => void;
}

type PermissionState = 'unsupported' | 'default' | 'granted' | 'denied';

function readPermission(): PermissionState {
  if (typeof window === 'undefined' || typeof Notification === 'undefined') return 'unsupported';
  return Notification.permission as PermissionState;
}

/** Small reusable on/off switch — label on the left, toggle on the right. */
function Toggle({
  label,
  hint,
  checked,
  disabled,
  onToggle,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onToggle: (next: boolean) => void;
}) {
  return (
    <label className={`settings-toggle ${disabled === true ? 'is-disabled' : ''}`}>
      <span className="settings-toggle-text">
        <span className="settings-toggle-label">{label}</span>
        {hint !== undefined && <span className="settings-toggle-hint">{hint}</span>}
      </span>
      <span className="settings-switch">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span className="settings-switch-track" aria-hidden="true" />
      </span>
    </label>
  );
}

export function SettingsPanel({ settings, onChange, onClose }: SettingsPanelProps) {
  const [permission, setPermission] = useState<PermissionState>(() => readPermission());

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Keep the permission state in sync with the browser — including changes the
  // user makes in the site settings while the panel is open. Without this, once
  // the permission was 'denied' the "blocked" message would never clear even
  // after the user allowed notifications, because we'd never re-read it.
  useEffect(() => {
    setPermission(readPermission());
    const perms = typeof navigator !== 'undefined' ? navigator.permissions : undefined;
    if (perms?.query === undefined) return;
    let status: PermissionStatus | null = null;
    let cancelled = false;
    const toState = (s: string): PermissionState => (s === 'prompt' ? 'default' : (s as PermissionState));
    // Safari doesn't support querying the 'notifications' permission and may
    // throw synchronously here (not just reject) — wrap the whole call so we
    // fall back cleanly to the direct Notification.permission read above.
    try {
      perms.query({ name: 'notifications' as PermissionName }).then((st) => {
        if (cancelled) { return; }
        status = st;
        setPermission(toState(st.state));
        st.onchange = () => setPermission(toState(st.state));
      }).catch(() => { /* not queryable — fall back to readPermission */ });
    } catch { /* Safari: query('notifications') unsupported — fall back */ }
    return () => { cancelled = true; if (status !== null) status.onchange = null; };
  }, []);

  const sendTestNotification = useCallback(() => {
    try {
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
        new Notification('팥콩 팩토리', { body: 'Notifications are working \u{1F514}', tag: 'agentquest:test' });
      }
    } catch { /* ignore */ }
    if (settings.soundEnabled) playChime('waiting', settings.volume);
  }, [settings.soundEnabled, settings.volume]);

  // Enabling notifications needs the browser's permission. Ask for it the moment
  // the user flips the master switch on; reflect the outcome in the hint below.
  const handleNotificationsToggle = (next: boolean) => {
    onChange({ notificationsEnabled: next });
    // Re-request whenever we don't already have it. On 'default' the browser
    // prompts; on 'denied' it resolves immediately without a prompt (harmless),
    // and the Permissions API subscription will reflect any later change.
    if (next && permission !== 'granted' && permission !== 'unsupported' && typeof Notification !== 'undefined') {
      void Notification.requestPermission().then((p) => setPermission(p as PermissionState));
    }
  };

  const notifyOn = settings.notificationsEnabled;
  const permissionBlocked = notifyOn && permission === 'denied';
  // The category toggles ("which events do I care about") gate both channels,
  // so they stay live as long as at least one channel — desktop or sound — is on.
  const anyChannel = settings.notificationsEnabled || settings.soundEnabled || settings.inAppToasts;

  return (
    <div className="settings-backdrop" onClick={onClose}>
      <div className="settings-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="설정">
        <button className="settings-close" onClick={onClose} title="Close (Esc)">×</button>

        <h2 className="settings-title">설정</h2>

        <div className="settings-section-label">알림</div>
        <Toggle
          label="데스크톱 알림"
          hint="Alert me when an agent needs me or finishes"
          checked={notifyOn}
          onToggle={handleNotificationsToggle}
        />
        {permissionBlocked && (
          <p className="settings-warn">
            Notifications are blocked by your browser. Enable them for this site in the
            browser's site settings, then toggle this back on.
          </p>
        )}
        {notifyOn && permission === 'unsupported' && (
          <p className="settings-warn">
            This browser doesn't support desktop notifications — sounds will still work.
          </p>
        )}
        {notifyOn && permission === 'granted' && (
          <div className="settings-row">
            <span className="settings-muted">Allowed by your browser.</span>
            <button className="settings-btn" onClick={sendTestNotification}>Send a test</button>
          </div>
        )}
        <Toggle
          label="화면 알림"
          hint="Dismissable banners inside the dashboard — works everywhere, no permission"
          checked={settings.inAppToasts}
          onToggle={(v) => onChange({ inAppToasts: v })}
        />

        <div className="settings-subgroup" data-dim={!anyChannel}>
          <Toggle
            label="응답 대기"
            hint="Agent finished its turn and is waiting for you"
            checked={settings.notifyWaiting}
            disabled={!anyChannel}
            onToggle={(v) => onChange({ notifyWaiting: v })}
          />
          <Toggle
            label="오류"
            hint="Agent hit an error"
            checked={settings.notifyError}
            disabled={!anyChannel}
            onToggle={(v) => onChange({ notifyError: v })}
          />
          <Toggle
            label="세션 종료 추정"
            hint="Agent session ended"
            checked={settings.notifyCompleted}
            disabled={!anyChannel}
            onToggle={(v) => onChange({ notifyCompleted: v })}
          />
          <Toggle
            label="컨텍스트 부족"
            hint="Main agent's context window passed 85% — a compaction is coming"
            checked={settings.notifyContext}
            disabled={!anyChannel}
            onToggle={(v) => onChange({ notifyContext: v })}
          />
        </div>

        <div className="settings-section-label">지도</div>
        <Toggle
          label="컨텍스트 막대"
          hint="Show each hero's context-window occupancy above its name"
          checked={settings.showContextBars}
          onToggle={(v) => onChange({ showContextBars: v })}
        />

        <div className="settings-section-label">소리</div>
        <Toggle
          label="알림음"
          hint="Chime alongside notifications"
          checked={settings.soundEnabled}
          onToggle={(v) => onChange({ soundEnabled: v })}
        />
        <div className="settings-row" data-dim={!settings.soundEnabled}>
          <span className="settings-toggle-label">Volume</span>
          <input
            className="settings-volume"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={settings.volume}
            disabled={!settings.soundEnabled}
            onChange={(e) => onChange({ volume: Number(e.target.value) })}
            aria-label="Notification volume"
          />
          <span className="settings-volume-value">{Math.round(settings.volume * 100)}%</span>
        </div>

        <div className="settings-section-label">방해 금지</div>
        <Toggle
          label="Mute everything"
          hint="Temporarily silence all notifications and sounds"
          checked={settings.doNotDisturb}
          onToggle={(v) => onChange({ doNotDisturb: v })}
        />

        <p className="settings-foot">
          Preferences are saved locally in this browser. Nothing is uploaded.
        </p>
      </div>
    </div>
  );
}
