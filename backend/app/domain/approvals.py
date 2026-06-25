"""Pure approval-routing helpers — no DB, fully unit-testable.

CLAUDE.md Phase 2: route by amount vs the approver's role `approval_limit`.
Seeded limits: OFFICER=5000, APPROVER=50000, ADMIN=None (unlimited). The required
tier for an amount is *derived* at action time from the amount + the approver's
limit; there is no stored tier column.
"""
from typing import Optional

# Roles that may ever approve a requisition (subject to the limit rule below).
APPROVER_ROLES = {"OFFICER", "APPROVER", "ADMIN"}

# Known approval limits per role, used only to describe the required tier for an
# amount. None == unlimited. Kept in sync with db.DEFAULT_ROLES.
ROLE_LIMITS: list[tuple[str, Optional[float]]] = [
    ("OFFICER", 5000.0),
    ("APPROVER", 50000.0),
    ("ADMIN", None),
]


def can_approve(role_code: Optional[str], approval_limit: Optional[float], amount: float) -> bool:
    """True iff a user holding `role_code` with `approval_limit` may approve `amount`.

    A user may approve iff their role is an approver role AND their limit is
    unlimited (None) or at least the amount.
    """
    if role_code not in APPROVER_ROLES:
        return False
    if approval_limit is None:          # unlimited (ADMIN)
        return True
    return approval_limit >= amount


def required_tier(amount: float) -> dict:
    """Describe the lowest approver role that can sign off on `amount`.

    Returns {"amount", "role", "limit", "unlimited"} where role is the cheapest
    tier whose limit covers the amount (ADMIN/unlimited is the fallback).
    """
    for role, limit in ROLE_LIMITS:
        if limit is None or limit >= amount:
            return {
                "amount": amount,
                "role": role,
                "limit": limit,
                "unlimited": limit is None,
            }
    # Unreachable: ADMIN is unlimited, but keep a defined fallback.
    role, limit = ROLE_LIMITS[-1]
    return {"amount": amount, "role": role, "limit": limit, "unlimited": True}
