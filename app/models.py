"""
SQLModel tables. All money is integer cents — never floats, anywhere.
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


def _utcnow() -> datetime:
    """Naive UTC. `datetime.utcnow()` is deprecated from 3.12, but switching to
    an aware datetime would change these to TIMESTAMP WITH TIME ZONE — a schema
    change. This keeps the existing naive-UTC storage without the deprecation."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Group(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # The link is the credential — an unguessable slug, not a sequential id.
    slug: str = Field(default_factory=lambda: secrets.token_urlsafe(8), unique=True, index=True)
    name: str
    currency_symbol: str = "$"
    created_at: datetime = Field(default_factory=_utcnow)


class Member(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("group_id", "name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id")
    name: str
    # Nullable: a password is optional, set by the member themselves.
    password_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class Expense(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id")
    description: str
    amount_cents: int
    payer_id: int = Field(foreign_key="member.id")
    split_type: str  # "equal" | "exact"
    created_by_id: int = Field(foreign_key="member.id")
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    # Soft delete: balance queries filter deleted_at IS NULL.
    deleted_at: Optional[datetime] = None


class Share(SQLModel, table=True):
    """Materialized per-person owed amount for an expense — stored, not
    recomputed, so a member joining mid-trip can't rewrite history."""

    id: Optional[int] = Field(default=None, primary_key=True)
    expense_id: int = Field(foreign_key="expense.id")
    member_id: int = Field(foreign_key="member.id")
    amount_cents: int


class Settlement(SQLModel, table=True):
    """Its own table, not an expense variant — two parties, no shares."""

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="group.id")
    from_member_id: int = Field(foreign_key="member.id")
    to_member_id: int = Field(foreign_key="member.id")
    amount_cents: int
    created_by_id: int = Field(foreign_key="member.id")
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    # Soft delete: undo control for a mistyped settlement.
    deleted_at: Optional[datetime] = None
