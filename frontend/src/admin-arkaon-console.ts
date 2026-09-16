export type ProposalStage = "PROPOSED" | "PREVIEWED" | "OPERATOR_APPROVED" | "REVIEW_BRANCH_APPLIED" | "REJECTED";

export const adminArkaonRoutes = {
  sessions: "/api/v1/admin/arkaon/sessions",
  messages: "/api/v1/admin/arkaon/sessions/{session_id}/messages",
  instructions: "/api/v1/admin/arkaon/change-instructions",
  preview: "/api/v1/admin/arkaon/proposals/{proposal_id}/preview",
  approve: "/api/v1/admin/arkaon/proposals/{proposal_id}/approve",
  applyReview: "/api/v1/admin/arkaon/proposals/{proposal_id}/apply-review",
  guidance: "/api/v1/admin/arkaon/proposals/{proposal_id}/guidance",
} as const;

export function adminArkaonConsole(): string {
  return `<section aria-labelledby="arkaon-control-title">
    <h2 id="arkaon-control-title">ARKAON 대화·부분 수정 컨트롤</h2>
    <p>운영자와 ARKAON이 수정 범위, 위험과 시험방법을 대화로 확인합니다. 대화만으로 운영환경은 변경되지 않습니다.</p>
    <section aria-labelledby="conversation-title"><h2 id="conversation-title">대화</h2>
      <div id="arkaon-messages" role="log" aria-live="polite" aria-relevant="additions"></div>
      <form id="arkaon-message-form"><label for="arkaon-message">수정 지시 또는 질문</label>
      <textarea id="arkaon-message" name="message" maxlength="4000" required aria-describedby="message-help"></textarea>
      <p id="message-help">비밀번호·토큰·주민등록번호·실제 개인정보를 입력하지 마십시오.</p>
      <button type="submit">ARKAON에게 전달</button></form></section>
    <section aria-labelledby="change-title"><h2 id="change-title">부분 수정지시</h2>
      <form id="partial-change-form">
        <label for="change-scope">수정 범위</label><input id="change-scope" name="scope" required>
        <label for="change-paths">허용 파일경로</label><textarea id="change-paths" name="paths" required></textarea>
        <label for="change-requirement">수정 요구사항</label><textarea id="change-requirement" name="requirement" required></textarea>
        <button type="submit">수정안 생성 요청</button>
      </form>
      <div id="change-preview" aria-live="polite"><h3>수정안 미리보기</h3>
        <p>변경 파일·diff·시험계획·위험·사전보완가이드를 확인한 뒤 승인하십시오.</p></div>
      <button type="button" id="approve-change">검토 브랜치 반영 승인</button>
      <p><strong>승인해도 main 병합·실제 배포·운영 변경은 실행되지 않습니다.</strong></p>
    </section>
    <section aria-labelledby="guide-title"><h2 id="guide-title">사전보완가이드</h2>
      <p>수정안 생성·미리보기·운영자 승인·검토 브랜치 반영 때마다 새 버전으로 갱신됩니다.</p>
      <div id="guidance-status" role="status" aria-live="polite">아직 생성된 수정안이 없습니다.</div>
    </section>
  </section>`;
}

export function stageLabel(stage: ProposalStage): string {
  return ({PROPOSED: "수정안 생성", PREVIEWED: "미리보기 완료", OPERATOR_APPROVED: "운영자 승인",
    REVIEW_BRANCH_APPLIED: "검토 브랜치 반영", REJECTED: "반려"})[stage];
}
