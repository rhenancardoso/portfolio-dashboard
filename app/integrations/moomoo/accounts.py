"""Trading accounts returned by ``get_acc_list``."""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.moomoo.client import Row
from app.integrations.moomoo.parsing import is_missing, require_text


@dataclass(frozen=True)
class BrokerAccountRecord:
    account_id: int
    trading_environment: str
    account_type: str | None
    card_number: str | None
    security_firm: str | None
    markets: tuple[str, ...]
    status: str | None

    @property
    def display_name(self) -> str:
        suffix = (self.card_number or str(self.account_id))[-4:]
        return f"Moomoo {self.account_type or 'account'} ****{suffix}"


def _optional(value: object) -> str | None:
    return None if is_missing(value) else str(value)


def parse_accounts(rows: list[Row], trading_environment: str) -> list[BrokerAccountRecord]:
    """Active accounts in the given environment (REAL or SIMULATE)."""
    accounts = []
    for row in rows:
        if row.get("trd_env") != trading_environment or row.get("acc_status") == "DISABLED":
            continue
        accounts.append(BrokerAccountRecord(
            account_id=int(require_text(row.get("acc_id"), "acc_id")),
            trading_environment=str(row["trd_env"]),
            account_type=_optional(row.get("acc_type")),
            card_number=_optional(row.get("card_num")),
            security_firm=_optional(row.get("security_firm")),
            markets=tuple(str(market) for market in row.get("trdmarket_auth") or ()),
            status=_optional(row.get("acc_status")),
        ))
    return accounts
