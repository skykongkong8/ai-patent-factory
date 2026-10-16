import { useEffect } from 'react';
import { BUILDING_DEFS } from '../game/data/building-layout';
import './Tutorial.css';

export function Tutorial({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const close = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [onClose]);
  return <div className="tutorial-backdrop" onClick={onClose}>
    <div className="tutorial-modal" role="dialog" aria-label="사용 안내" onClick={(e) => e.stopPropagation()}>
      <button className="tutorial-close" onClick={onClose} aria-label="닫기">×</button>
      <img src="assets/patkong-brand.svg" alt="팥콩 팩토리" className="tutorial-logo" />
      <h2 className="tutorial-title">에이전트가 일하는 공장</h2>
      <p className="tutorial-text">이 프로젝트 폴더에서 Codex 또는 Claude Code를 사용하면 세션마다 팥콩이 나타납니다. 팥콩이 방문하는 방은 최근에 관찰한 도구 활동을 보여줍니다.</p>
      <table className="tutorial-legend"><tbody>{BUILDING_DEFS.map((b) => <tr key={b.id}>
        <td className="tutorial-legend-v">{b.label}</td><td className="tutorial-legend-desc">{b.toolCalls.join(' · ')}</td>
      </tr>)}</tbody></table>
      <p className="tutorial-text">팥콩이나 아래 세션 목록을 누르면 대화, 명령, 파일 경로와 컨텍스트를 볼 수 있습니다. 작은 팥콩은 같은 색의 부모에게 연결된 하위 에이전트입니다. 상단 ✦는 스킬·플러그인 사용 현황, 지도 버튼은 공장 편집기를 엽니다.</p>
      <p className="tutorial-text">대기·종료는 로그로 추정한 세션 활동입니다. 발명 공정의 완료나 게이트 승인을 뜻하지 않습니다. 공정 실행과 결정은 기존 CLI에서 진행합니다.</p>
      <p className="tutorial-text">화면은 이 컴퓨터에서만 열립니다. 상세 대화는 메모리에서 표시하며 새로고침하면 활동 피드가 초기화됩니다.</p>
      <button className="tutorial-close-btn" onClick={onClose}>공장 둘러보기</button>
    </div>
  </div>;
}
