# ruff: noqa
from datetime import UTC,datetime
import pytest
from narang_rider.connector import *

NOW=datetime(2026,9,16,tzinfo=UTC)
def call(cid,kind,key,digest="a"*64):
 return RiderCall(cid,"order-1",kind,"caller",key,digest,NOW)

def test_merchant_and_company_calls_collapse_to_one():
 s=RiderCallService(); first=s.create(call("c1",CallerType.MERCHANT,"m1"))
 second=s.create(call("c2",CallerType.RIDER_COMPANY,"r1"))
 assert second==first and second.call_id=="c1"

def test_conflicting_second_call_is_blocked():
 s=RiderCallService(); s.create(call("c1",CallerType.MERCHANT,"m1"))
 with pytest.raises(ValueError,match="DUPLICATE_ACTIVE"): s.create(call("c2",CallerType.RIDER_COMPANY,"r1","b"*64))

def test_finished_call_allows_new_call():
 s=RiderCallService(); s.create(call("c1",CallerType.MERCHANT,"m1")); s.finish("c1",CallStatus.CANCELLED)
 assert s.create(call("c2",CallerType.RIDER_COMPANY,"r1")).call_id=="c2"

def test_idempotency_key_cannot_rebind():
 s=RiderCallService(); s.create(call("c1",CallerType.MERCHANT,"same"))
 with pytest.raises(ValueError,match="IDEMPOTENCY_CONFLICT"): s.create(call("c2",CallerType.MERCHANT,"same","b"*64))

def test_device_requires_vault_certificate():
 with pytest.raises(ValueError,match="VAULT"): ConnectorDevice("d","m","b","raw-cert",True)

def test_offline_queue_signature_order_and_replay():
 q=OfflineSyncQueue(b"k"*32)
 sig=q.sign("m1","d1",1,"a"*64); msg=QueueMessage("m1","d1",1,"a"*64,sig)
 assert q.accept(msg); assert not q.accept(msg)
 bad=QueueMessage("m2","d1",3,"b"*64,q.sign("m2","d1",3,"b"*64))
 with pytest.raises(ValueError,match="SEQUENCE_GAP"): q.accept(bad)

def test_offline_queue_rejects_forgery():
 q=OfflineSyncQueue(b"k"*32)
 with pytest.raises(ValueError,match="INVALID_SYNC_SIGNATURE"): q.accept(QueueMessage("m","d",1,"a"*64,"0"*64))
