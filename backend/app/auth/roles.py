"""Shared role-claim mapping for every identity provider (Entra, Clerk, ...).

The rule is deliberately *exact*, never substring: a claim value maps to a local
role code only via an explicit configured map or by equalling a canonical role
code as a whole token. This closes the privilege-escalation hole where group
names like 'Finance-Admins' / 'Non-Admin-Users' / 'GoldenAdmin' would otherwise
match 'ADMIN' by containment. Both the Entra and Clerk adapters delegate here so
the guarantee holds identically no matter which IdP asserts the claim.
"""
from typing import Iterable, Optional

# Most → least privileged; the first match wins when a token carries several.
ROLE_CODES = ["ADMIN", "APPROVER", "OFFICER", "REQUESTER", "VIEWER"]


def role_for_claim_value(value: str, role_map: Optional[dict] = None) -> Optional[str]:
    """Map ONE claim value to a role code, exact-match only. None if nothing matches.

    1. An explicit configured mapping wins — keyed by the raw claim value or its
       GUID, compared case-insensitively. This is the production path for opaque
       group GUIDs / arbitrary group names.
    2. Otherwise the *whole* claim value must equal a canonical role code
       (case-insensitive). Names that merely contain a role code as a substring
       ('Finance-Admins', 'Non-Admin-Users', 'GoldenAdmin') are rejected.
    """
    norm = str(value).strip().upper()
    for key, code in (role_map or {}).items():
        if str(key).strip().upper() == norm:
            mapped = str(code).strip().upper()
            if mapped in ROLE_CODES:
                return mapped
    if norm in ROLE_CODES:
        return norm
    return None


def map_roles(
    values: Iterable[str], role_map: Optional[dict], default: str
) -> str:
    """Pick the highest-privilege role from a set of claim values, else `default`.

    Each value is resolved independently via `role_for_claim_value` (exact match
    only) and the most privileged match wins.
    """
    matched = {
        role
        for v in (values or [])
        if (role := role_for_claim_value(v, role_map)) is not None
    }
    for code in ROLE_CODES:                 # ADMIN first, so it wins
        if code in matched:
            return code
    return default
