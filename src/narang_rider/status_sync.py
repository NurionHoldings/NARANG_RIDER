# ruff: noqa
from dataclasses import dataclass,replace
from enum import StrEnum

class DeliveryStatus(StrEnum):
 RECEIVED="RECEIVED"; DISPATCHED="DISPATCHED"; ARRIVED="ARRIVED"; PICKED_UP="PICKED_UP"; DELIVERED="DELIVERED"; CANCELLED="CANCELLED"
@dataclass(frozen=True)
class CallbackTarget:
 source_ref:str; endpoint_ref:str
 def __post_init__(self):
  if not self.endpoint_ref.startswith("vault:"): raise ValueError("CALLBACK_ENDPOINT_VAULT_REQUIRED")
@dataclass(frozen=True)
class OutboxMessage:
 message_id:str; order_id:str; target:CallbackTarget; status:DeliveryStatus; sequence:int; delivered:bool=False
class StatusOutbox:
 def __init__(self): self._messages={}; self._keys={}; self._last={}
 def enqueue(self,message:OutboxMessage):
  key=(message.order_id,message.target.source_ref,message.status)
  if key in self._keys:return self._messages[self._keys[key]]
  expected=self._last.get((message.order_id,message.target.source_ref),0)+1
  if message.sequence!=expected: raise ValueError("CALLBACK_SEQUENCE_GAP")
  self._messages[message.message_id]=message;self._keys[key]=message.message_id
  self._last[(message.order_id,message.target.source_ref)]=message.sequence;return message
 def mark_delivered(self,message_id):
  msg=self._messages[message_id]
  if msg.delivered:return msg
  result=replace(msg,delivered=True);self._messages[message_id]=result;return result
