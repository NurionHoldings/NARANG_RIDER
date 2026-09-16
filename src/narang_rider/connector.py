# ruff: noqa
from __future__ import annotations
import hashlib,hmac
from dataclasses import dataclass,replace
from datetime import datetime
from enum import StrEnum

class CallerType(StrEnum):
    MERCHANT="MERCHANT"
    RIDER_COMPANY="RIDER_COMPANY"

class CallStatus(StrEnum):
    ACTIVE="ACTIVE"
    CANCELLED="CANCELLED"
    ACCEPTED="ACCEPTED"

@dataclass(frozen=True)
class ConnectorDevice:
    device_id:str; merchant_id:str; branch_id:str; certificate_ref:str; active:bool
    def __post_init__(self):
        if not self.certificate_ref.startswith("vault:"): raise ValueError("DEVICE_CERTIFICATE_VAULT_REQUIRED")

@dataclass(frozen=True)
class RiderCall:
    call_id:str; order_id:str; caller_type:CallerType; caller_id:str; idempotency_key:str
    payload_digest:str; created_at:datetime; status:CallStatus=CallStatus.ACTIVE
    def __post_init__(self):
        if self.created_at.tzinfo is None or len(self.payload_digest)!=64: raise ValueError("VALID_RIDER_CALL_REQUIRED")

class RiderCallService:
    def __init__(self):
        self._calls={}; self._keys={}; self._active_by_order={}
    def create(self,call:RiderCall)->RiderCall:
        prior_id=self._keys.get(call.idempotency_key)
        if prior_id:
            prior=self._calls[prior_id]
            if prior.payload_digest!=call.payload_digest or prior.order_id!=call.order_id: raise ValueError("CALL_IDEMPOTENCY_CONFLICT")
            return prior
        active_id=self._active_by_order.get(call.order_id)
        if active_id:
            active=self._calls[active_id]
            if active.payload_digest==call.payload_digest: 
                self._keys[call.idempotency_key]=active.call_id
                return active
            raise ValueError("DUPLICATE_ACTIVE_RIDER_CALL")
        if call.call_id in self._calls: raise ValueError("DUPLICATE_CALL_ID")
        self._calls[call.call_id]=call; self._keys[call.idempotency_key]=call.call_id; self._active_by_order[call.order_id]=call.call_id
        return call
    def finish(self,call_id:str,status:CallStatus)->RiderCall:
        if status is CallStatus.ACTIVE: raise ValueError("TERMINAL_CALL_STATUS_REQUIRED")
        call=self._calls[call_id]
        if call.status is not CallStatus.ACTIVE: return call
        updated=replace(call,status=status); self._calls[call_id]=updated; self._active_by_order.pop(call.order_id,None); return updated

@dataclass(frozen=True)
class QueueMessage:
    message_id:str; device_id:str; sequence:int; payload_digest:str; signature:str

class OfflineSyncQueue:
    def __init__(self,key:bytes):
        if len(key)<32: raise ValueError("SYNC_KEY_TOO_SHORT")
        self._key=key; self._last={}; self._seen={}
    def sign(self,message_id,device_id,sequence,payload_digest):
        return hmac.new(self._key,f"{message_id}|{device_id}|{sequence}|{payload_digest}".encode(),hashlib.sha256).hexdigest()
    def accept(self,message:QueueMessage):
        fingerprint=f"{message.device_id}:{message.sequence}:{message.payload_digest}"
        if message.message_id in self._seen:
            if self._seen[message.message_id]!=fingerprint: raise ValueError("SYNC_IDEMPOTENCY_CONFLICT")
            return False
        expected=self.sign(message.message_id,message.device_id,message.sequence,message.payload_digest)
        if not hmac.compare_digest(expected,message.signature): raise ValueError("INVALID_SYNC_SIGNATURE")
        if message.sequence!=self._last.get(message.device_id,0)+1: raise ValueError("SYNC_SEQUENCE_GAP")
        self._seen[message.message_id]=fingerprint; self._last[message.device_id]=message.sequence; return True
