# ruff: noqa
from dataclasses import dataclass
from enum import StrEnum

class CancelActor(StrEnum):
 CUSTOMER="CUSTOMER"; MERCHANT="MERCHANT"; RIDER="RIDER"; PLATFORM="PLATFORM"
class CancelStage(StrEnum):
 BEFORE_ASSIGNMENT="BEFORE_ASSIGNMENT"; ASSIGNED="ASSIGNED"; ARRIVED="ARRIVED"; PICKED_UP="PICKED_UP"
@dataclass(frozen=True)
class CancellationPolicy:
 assigned_pay_won:int; arrived_pay_won:int
@dataclass(frozen=True)
class CancellationResult:
 order_id:str; rider_pay_won:int; customer_charge_won:int; merchant_charge_won:int
 redispatch_allowed:bool; human_review_required:bool; rider_penalty_allowed:bool=False
def cancel(*,order_id:str,actor:CancelActor,stage:CancelStage,policy:CancellationPolicy,food_handed_over:bool)->CancellationResult:
 if min(policy.assigned_pay_won,policy.arrived_pay_won)<0: raise ValueError("INVALID_CANCEL_POLICY")
 rider=0
 if stage is CancelStage.ASSIGNED: rider=policy.assigned_pay_won
 if stage is CancelStage.ARRIVED: rider=policy.arrived_pay_won
 if stage is CancelStage.PICKED_UP or food_handed_over:
  return CancellationResult(order_id,rider,0,0,False,True)
 customer=rider if actor is CancelActor.CUSTOMER else 0
 merchant=rider if actor is CancelActor.MERCHANT else 0
 return CancellationResult(order_id,rider,customer,merchant,True,False)
