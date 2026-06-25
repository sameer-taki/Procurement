from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    database_url: str = "postgresql+psycopg://fmp:fmp@db:5432/fmp"
    secret_key: str = "CHANGE_ME_32_CHARS_MINIMUM_PLACEHOLDER"
    first_admin_username: str = "admin"
    first_admin_password: str = "admin"
    first_admin_name: str = "Administrator"

    # Business Central (OData v4 / NTLM) — on-prem, reachable from the Docker host
    bc_base_url: str = ""
    bc_company: str = ""
    bc_username: str = ""
    bc_password: str = ""

    # Kiwiplan (KDW/SQL read, KMC inject) / Accura (ODBC read)
    kiwiplan_dsn: str = ""
    accura_dsn: str = ""

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


settings = Settings()
