"""Entra ID (Azure AD) SSO via OIDC. Map Entra group/app-role claims to local
roles (REQUESTER / OFFICER / APPROVER / VIEWER / ADMIN). Approval limit lives on
the role. TODO (Phase 1): wire Authlib OAuth, /auth/login + /auth/callback,
session cookie signed with SECRET_KEY, and a get_current_user dependency."""
