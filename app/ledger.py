from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    txn_id: str
    merchant_id: str
    amount: Decimal
    currency: str
    created_at: datetime
    status: str = "captured"


@dataclass(frozen=True, slots=True)
class LedgerRefund:
    refund_id: str
    txn_id: str
    amount: Decimal
    created_at: datetime


class InMemoryLedger:
    def __init__(
        self,
        transactions: Iterable[LedgerTransaction] | None = None,
        refunds: Iterable[LedgerRefund] | None = None,
    ) -> None:
        self._transactions = {tx.txn_id: tx for tx in (transactions or [])}
        self._refunds_by_txn: dict[str, list[LedgerRefund]] = {}
        for refund in refunds or []:
            self._refunds_by_txn.setdefault(refund.txn_id, []).append(refund)

    @classmethod
    def seed_default(cls) -> "InMemoryLedger":
        now = datetime.now(timezone.utc)
        transactions = [
            LedgerTransaction(
                txn_id="txn_981234",
                merchant_id="merchant_01",
                amount=Decimal("4500.00"),
                currency="INR",
                created_at=now,
            ),
            LedgerTransaction(
                txn_id="txn_123456",
                merchant_id="m_001",
                amount=Decimal("499.99"),
                currency="INR",
                created_at=now,
            ),
            LedgerTransaction(
                txn_id="txn_refunded_01",
                merchant_id="merchant_01",
                amount=Decimal("1200.00"),
                currency="INR",
                created_at=now,
            ),
        ]
        refunds = [
            LedgerRefund(
                refund_id="rfnd_0001",
                txn_id="txn_refunded_01",
                amount=Decimal("1200.00"),
                created_at=now,
            )
        ]
        return cls(transactions=transactions, refunds=refunds)

    def get_transaction(self, txn_id: str) -> LedgerTransaction | None:
        return self._transactions.get(txn_id)

    def has_refund_for_transaction(self, txn_id: str) -> bool:
        return bool(self._refunds_by_txn.get(txn_id))

    def record_refund(self, refund: LedgerRefund) -> None:
        self._refunds_by_txn.setdefault(refund.txn_id, []).append(refund)
