from app.auth.entra import map_role


def test_me_requires_auth(client):
    assert client.get("/api/me").status_code == 401


def test_providers(client):
    body = client.get("/auth/providers").json()
    assert body["admin_login"] is True
    assert body["entra"] is False          # unconfigured in tests


def test_admin_login_then_me(admin_client):
    me = admin_client.get("/api/me").json()
    assert me["role"] == "ADMIN"
    assert me["is_admin"] is True
    assert me["can_mutate"] is True


def test_bad_admin_login(client):
    r = client.post("/auth/admin-login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_logout_clears_session(admin_client):
    assert admin_client.get("/api/me").status_code == 200
    admin_client.post("/auth/logout")
    assert admin_client.get("/api/me").status_code == 401


def test_map_role_picks_highest_privilege():
    # Exact, case-insensitive role-code values resolve; highest privilege wins.
    assert map_role({"roles": ["VIEWER", "APPROVER"]}) == "APPROVER"
    assert map_role({"roles": ["admin"]}) == "ADMIN"
    assert map_role({"roles": ["Officer"]}) == "OFFICER"
    assert map_role({"roles": []}) == "VIEWER"          # default
    assert map_role({}) == "VIEWER"


def test_map_role_does_not_escalate_on_substring_group_names():
    # Regression for the privilege-escalation finding: a claim value that merely
    # *contains* a role code (as a substring or as a token of a longer name) must
    # NOT be granted that role. All of these previously mapped to ADMIN.
    assert map_role({"roles": ["Finance-Admins"]}) == "VIEWER"
    assert map_role({"roles": ["Administrative-Assistants"]}) == "VIEWER"
    assert map_role({"roles": ["Non-Admin-Users"]}) == "VIEWER"
    assert map_role({"roles": ["GoldenAdmin"]}) == "VIEWER"
    # Joining several values must not be flattened into one substring match.
    assert map_role({"roles": ["Finance-Admins", "Procurement.Viewer"]}) == "VIEWER"


def test_map_role_explicit_map_resolves_opaque_group_values(monkeypatch):
    from app.auth import entra
    monkeypatch.setattr(
        entra.settings, "entra_role_map",
        {
            "11111111-1111-1111-1111-111111111111": "ADMIN",
            "Procurement.Approver": "APPROVER",
        },
        raising=False,
    )
    # Opaque GUID + arbitrary group names resolve via the explicit map.
    assert map_role({"roles": ["11111111-1111-1111-1111-111111111111"]}) == "ADMIN"
    assert map_role({"roles": ["Procurement.Approver"]}) == "APPROVER"
    # A name not in the map and not equal to a role code does not escalate.
    assert map_role({"roles": ["Non-Admin-Users"]}) == "VIEWER"
