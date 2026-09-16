from dataclasses import dataclass, replace
from enum import StrEnum


class DeliveryStatus(StrEnum):
    RECEIVED = "RECEIVED"
    DISPATCHED = "DISPATCHED"
    ARRIVED = "ARRIVED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CallbackTarget:
    source_ref: str
    endpoint_ref: str

    def __post_init__(self) -> None:
        if not self.endpoint_ref.startswith("vault:"):
            raise ValueError("CALLBACK_ENDPOINT_VAULT_REQUIRED")


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    order_id: str
    target: CallbackTarget
    status: DeliveryStatus
    sequence: int
    delivered: bool = False


class StatusOutbox:
    def __init__(self) -> None:
        self._messages: dict[str, OutboxMessage] = {}
        self._keys: dict[tuple[str, str, DeliveryStatus], str] = {}
        self._last: dict[tuple[str, str], int] = {}

    def enqueue(self, message: OutboxMessage) -> OutboxMessage:
        key = (message.order_id, message.target.source_ref, message.status)
        prior_id = self._keys.get(key)
        if prior_id is not None:
            return self._messages[prior_id]

        stream = (message.order_id, message.target.source_ref)
        expected = self._last.get(stream, 0) + 1
        if message.sequence != expected:
            raise ValueError("CALLBACK_SEQUENCE_GAP")
        if message.message_id in self._messages:
            raise ValueError("DUPLICATE_CALLBACK_MESSAGE_ID")

        self._messages[message.message_id] = message
        self._keys[key] = message.message_id
        self._last[stream] = message.sequence
        return message

    def mark_delivered(self, message_id: str) -> OutboxMessage:
        message = self._messages[message_id]
        if message.delivered:
            return message
        result = replace(message, delivered=True)
        self._messages[message_id] = result
        return result
