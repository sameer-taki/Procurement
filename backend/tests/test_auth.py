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
    assert map_role({"roles": ["Procurement.Viewer", "Procurement.Approver"]}) == "APPROVER"
    assert map_role({"roles": ["GoldenAdmin"]}) == "ADMIN"
    assert map_role({"roles": []}) == "VIEWER"          # default
    assert map_role({}) == "VIEWER"
