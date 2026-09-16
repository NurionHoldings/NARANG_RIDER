from datetime import UTC, datetime, time

import pytest

from narang_rider.notifications import (
    CallbackVerifier,
    Channel,
    DeliveryState,
    NoticeCommand,
    NoticeErrorCode,
    NoticePrincipal,
    NoticeRejected,
    NotificationEvent,
    NotificationService,
    Preference,
)


@pytest.fixture
def setup():
    service = NotificationService(b"deep-link-secret-32-bytes-long")
    recipient = NoticePrincipal("user_1", "branch_1", "user_1")
    staff = NoticePrincipal("staff_1", "branch_1", staff=True)
    service.set_preference(recipient, Preference(
        "user_1", "branch_1", "vault://contact/user_1",
        frozenset({Channel.PUSH, Channel.SMS}), frozenset({"DELIVERY", "SETTLEMENT"}),
        time(22), time(7),
    ))
    return service, recipient, staff


def command(event=NotificationEvent.DELIVERY_COMPLETE, event_id="event_1", purpose="DELIVERY", mandatory=False):
    return NoticeCommand("user_1", "branch_1", event, event_id, purpose, {}, mandatory)


def test_in_app_is_primary_and_external_channels_use_vault_only(setup):
    service, _, staff = setup
    notices = service.enqueue(staff, command(), datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert {n.channel for n in notices} == {Channel.IN_APP, Channel.PUSH, Channel.SMS}
    in_app = next(n for n in notices if n.channel is Channel.IN_APP)
    assert in_app.contact_vault_ref is None
    assert all(n.lockscreen_text == "배송이 완료되었습니다." for n in notices)
    assert all("user_1" not in n.lockscreen_text for n in notices)


def test_duplicate_event_is_idempotent_not_spam(setup):
    service, _, staff = setup
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    first = service.enqueue(staff, command(), now)
    second = service.enqueue(staff, command(), now)
    assert second == first
    assert len(service.outbox) == 3


def test_wrong_recipient_and_branch_are_blocked(setup):
    service, recipient, staff = setup
    with pytest.raises(NoticeRejected):
        service.set_preference(recipient, Preference("other_1", "branch_1", None, frozenset(), frozenset()))
    with pytest.raises(NoticeRejected) as error:
        service.enqueue(NoticePrincipal(staff.actor_id, "branch_2", staff=True), command(), datetime.now(UTC))
    assert error.value.code is NoticeErrorCode.BRANCH_SCOPE


def test_preferences_and_quiet_hours_cannot_be_bypassed(setup):
    service, _, staff = setup
    with pytest.raises(NoticeRejected) as blocked:
        service.enqueue(staff, command(purpose="MARKETING"), datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert blocked.value.code is NoticeErrorCode.PREFERENCE_BLOCKED
    notices = service.enqueue(staff, command(event_id="event_night"), datetime(2026, 1, 1, 23, tzinfo=UTC))
    assert {n.channel for n in notices} == {Channel.IN_APP}


def test_mandatory_classification_cannot_be_abused(setup):
    service, _, staff = setup
    with pytest.raises(NoticeRejected) as abuse:
        service.enqueue(staff, command(mandatory=True), datetime.now(UTC))
    assert abuse.value.code is NoticeErrorCode.MANDATORY_ABUSE
    with pytest.raises(NoticeRejected):
        service.enqueue(staff, command(NotificationEvent.BRANCH_EMERGENCY, "emergency_1", "SAFETY", False), datetime.now(UTC))
    notices = service.enqueue(staff, command(NotificationEvent.BRANCH_EMERGENCY, "emergency_1", "SAFETY", True), datetime.now(UTC))
    assert notices


@pytest.mark.parametrize("field", ["address", "phone", "email", "precise_location", "rider_name", "order_contents", "medical"])
def test_raw_pii_never_enters_payload(setup, field):
    service, _, staff = setup
    bad = NoticeCommand("user_1", "branch_1", NotificationEvent.DELIVERY_COMPLETE, f"event_{field}", "DELIVERY", {field: "secret"})
    with pytest.raises(NoticeRejected) as error:
        service.enqueue(staff, bad, datetime.now(UTC))
    assert error.value.code is NoticeErrorCode.RAW_PII


def test_deep_link_is_opaque_recipient_bound_and_tamper_evident(setup):
    service, recipient, staff = setup
    notice = service.enqueue(staff, command(), datetime.now(UTC))[0]
    assert service.resolve_link(recipient, notice.deep_link) == notice
    with pytest.raises(NoticeRejected):
        service.resolve_link(recipient, notice.deep_link + "0")
    with pytest.raises(NoticeRejected):
        service.resolve_link(NoticePrincipal("other", "branch_1", "other"), notice.deep_link)


def test_callback_signature_and_replay_protection(setup):
    service, _, staff = setup
    notice = service.enqueue(staff, command(), datetime.now(UTC))[0]
    verifier = CallbackVerifier(b"provider-secret")
    payload = f"cb_1.{notice.notice_id}.ACKNOWLEDGED".encode()
    with pytest.raises(NoticeRejected):
        service.callback("cb_1", notice.notice_id, "ACKNOWLEDGED", "forged", verifier)
    result = service.callback("cb_1", notice.notice_id, "ACKNOWLEDGED", verifier.sign(payload), verifier)
    assert result.state is DeliveryState.ACKNOWLEDGED
    assert not result.acknowledgement_limits_rights
    with pytest.raises(NoticeRejected) as replay:
        service.callback("cb_1", notice.notice_id, "ACKNOWLEDGED", verifier.sign(payload), verifier)
    assert replay.value.code is NoticeErrorCode.CALLBACK_REPLAY


def test_arkaon_cannot_coerce_rank_or_penalize(setup):
    service, _, _ = setup
    with pytest.raises(NoticeRejected) as error:
        service.arkaon_target(coercive=True, ranking_or_penalty=False)
    assert error.value.code is NoticeErrorCode.ARKAON_AUTHORITY_DENIED
    with pytest.raises(NoticeRejected):
        service.arkaon_target(coercive=False, ranking_or_penalty=True)


class Provider:
    def send(self, notice):
        return f"provider/{notice.notice_id}"


def test_transactional_outbox_preserves_recipient_order(setup):
    service, _, staff = setup
    first = service.enqueue(staff, command(event_id="ordered_1"), datetime.now(UTC))[0]
    second = service.enqueue(staff, command(event_id="ordered_2"), datetime.now(UTC))[0]
    with pytest.raises(NoticeRejected):
        service.deliver(second.notice_id, {Channel.IN_APP: Provider()})
    assert service.deliver(first.notice_id, {Channel.IN_APP: Provider()}).state is DeliveryState.SENT

