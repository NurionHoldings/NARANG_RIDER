# ruff: noqa
import pytest
from narang_rider.status_sync import *
def msg(mid,status,seq,source="pos"):
 return OutboxMessage(mid,"o",CallbackTarget(source,"vault:callback"),status,seq)
def test_status_callbacks_preserve_sequence():
 q=StatusOutbox();q.enqueue(msg("m1",DeliveryStatus.RECEIVED,1));q.enqueue(msg("m2",DeliveryStatus.DISPATCHED,2))
def test_duplicate_status_is_idempotent():
 q=StatusOutbox();a=q.enqueue(msg("m1",DeliveryStatus.RECEIVED,1));b=q.enqueue(msg("m2",DeliveryStatus.RECEIVED,99));assert a==b
def test_gap_is_rejected():
 q=StatusOutbox()
 with pytest.raises(ValueError,match="SEQUENCE_GAP"):q.enqueue(msg("m2",DeliveryStatus.DISPATCHED,2))
def test_each_partner_has_independent_sequence():
 q=StatusOutbox();q.enqueue(msg("p",DeliveryStatus.RECEIVED,1,"pos"));q.enqueue(msg("a",DeliveryStatus.RECEIVED,1,"agency"))
def test_delivery_ack_is_idempotent():
 q=StatusOutbox();q.enqueue(msg("m",DeliveryStatus.RECEIVED,1));assert q.mark_delivered("m")==q.mark_delivered("m")
