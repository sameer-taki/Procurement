from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    database_url: str = "postgresql+psycopg://fmp:fmp@db:5432/fmp"
    secret_key: str = "CHANGE_ME_32_CHARS_MINIMUM_PLACEHOLDER"
    first_admin_username: str = "admin"
    first_admin_password: str = "admin"
    first_admin_name: str = "Administrator"
    first_admin_email: str = ""        # defaults to first_admin_username if blank

    # Session cookie (signed with secret_key). Secure flag follows app_env.
    session_cookie: str = "gp_session"
    session_max_age: int = 60 * 60 * 8          # 8h
    session_secure: Optional[bool] = None       # None => secure in production

    # Adapter behaviour. When a system is unconfigured we serve demo data so the
    # Stock view is usable; flip explicitly with USE_FAKE_ADAPTERS if needed.
    use_fake_adapters: Optional[bool] = None     # None => auto (fake when unconfigured)

    # Stock refresh scheduler
    stock_refresh_enabled: bool = True
    stock_refresh_seconds: int = 1800            # ~30 min
    seed_demo_on_empty: bool = True              # populate items + stock on first boot

    # Run alembic upgrade head on startup (off in tests)
    run_migrations_on_startup: bool = True

    # Business Central (OData v4 / NTLM) — on-prem, reachable from the Docker host
    bc_base_url: str = ""
    bc_company: str = ""
    bc_username: str = ""
    bc_password: str = ""
    bc_auth: str = "ntlm"              # "ntlm" | "basic"
    bc_verify_tls: bool = True         # set false only for a self-signed on-prem cert
    bc_items_entity: str = "Items"     # OData entity set for the item master (confirm name)

    # Kiwiplan (KDW/SQL read, KMC inject) / Accura (ODBC read).
    # *_stock_sql is a parameterized query you supply (see INTEGRATIONS.md) returning
    # columns: location, on_hand, allocated, on_order — with one :item_ref placeholder.
    kiwiplan_dsn: str = ""
    kiwiplan_stock_sql: str = ""
    accura_dsn: str = ""
    accura_stock_sql: str = ""

    # M365 Graph mailer
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_sender: str = "no-reply@golden.com.fj"

    # Entra ID SSO
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_redirect_uri: str = ""
    entra_scope: str = "openid profile email"
    # Which claim carries the user's app roles/groups, and how those map to our
    # local role codes. Exact group/role ids are an open question (CLAUDE.md §7);
    # default mapping matches on the role *name* case-insensitively.
    entra_role_claim: str = "roles"
    default_role: str = "VIEWER"

    # --- capability helpers (never raise; safe to read anywhere) ---
    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production if self.session_secure is None else self.session_secure

    @property
    def entra_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_client_id and self.entra_client_secret)

    @property
    def bc_enabled(self) -> bool:
        return bool(self.bc_base_url and self.bc_username and self.bc_password)

    @property
    def kiwiplan_enabled(self) -> bool:
        # Needs both the connection and the query before it can read live.
        return bool(self.kiwiplan_dsn and self.kiwiplan_stock_sql)

    @property
    def accura_enabled(self) -> bool:
        return bool(self.accura_dsn and self.accura_stock_sql)

    def fakes_for(self, system_enabled: bool) -> bool:
        """Use demo data for a given system when forced, or when it is unconfigured."""
        if self.use_fake_adapters is not None:
            return self.use_fake_adapters
        return not system_enabled


settings = Settings()
