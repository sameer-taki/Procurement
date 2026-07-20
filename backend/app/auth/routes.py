"""Auth routes: Entra OIDC (login/callback), bootstrap admin login, logout, /api/me."""
import hmac

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..gateway.models import User
from .deps import CurrentUser, get_current_user
from .entra import get_oauth, upsert_user_from_claims

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(prefix="/api", tags=["auth"])


def _admin_email() -> str:
    return settings.first_admin_email or settings.first_admin_username


def _login_user(request: Request, user: User) -> None:
    request.session["uid"] = user.id


@router.get("/providers")
def providers():
    """What sign-in methods the SPA should offer."""
    return {
        "clerk": settings.clerk_enabled,
        "entra": settings.entra_enabled,
        "admin_login": True,
    }


@router.get("/login")
async def login(request: Request):
    if not settings.entra_enabled:
        # No SSO configured yet — the SPA shows the bootstrap admin form at "/".
        return RedirectResponse(url="/")
    redirect_uri = settings.entra_redirect_uri or str(request.url_for("auth_callback"))
    return await get_oauth().entra.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def callback(request: Request, session: Session = Depends(get_session)):
    if not settings.entra_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO not configured")
    try:
        token = await get_oauth().entra.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"SSO failed: {e.error}")
    claims = token.get("userinfo") or {}
    user = upsert_user_from_claims(session, claims)
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No email claim from Entra")
    _login_user(request, user)
    return RedirectResponse(url="/")


class AdminLogin(BaseModel):
    username: str
    password: str


@router.post("/admin-login")
def admin_login(body: AdminLogin, request: Request, session: Session = Depends(get_session)):
    """Bootstrap/break-glass login for the seeded admin using the env credentials.

    No password is stored in the DB — secrets stay in env (Portainer). All other
    users sign in via Entra SSO."""
    ok_user = hmac.compare_digest(body.username, settings.first_admin_username)
    ok_pass = hmac.compare_digest(body.password, settings.first_admin_password)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    user = session.exec(select(User).where(User.email == _admin_email())).first()
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin user not provisioned")
    _login_user(request, user)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@me_router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role_code,
        "approval_limit": user.approval_limit,
        "is_admin": user.is_admin,
        "can_mutate": user.can_mutate,
    }
