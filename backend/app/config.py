from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )

    app_env: str = "production"
    database_url: str = "postgresql+psycopg://fmp:fmp@db:5432/fmp"
    # Connection pool (server DBs only; SQLite ignores these). Defaults comfortably
    # cover the request threadpool + 3 background schedulers on the single worker.
    db_pool_size: int = 10
    db_max_overflow: int = 20
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

    # Scheduled BC usage import (SOP §9 cadence). The Order Page recomputes live
    # on every view, but its usage_history input only moves when the BC export is
    # imported — daily keeps the trailing averages current without waiting for an
    # officer to press "Import usage from BC". Idempotent (upsert by item+period).
    usage_import_enabled: bool = True
    usage_import_seconds: int = 86400            # daily

    # Integration outbox processor (retries reliable BC posting; idempotent)
    outbox_process_enabled: bool = True
    outbox_process_seconds: int = 60             # drain pending BC posts ~every minute
    # Drain the outbox inline on the issue request thread for an immediate post.
    # Posting is race-safe (per-row claim + unique crosswalk), but you can disable
    # this so ONLY the background scheduler posts, eliminating overlap entirely.
    outbox_process_on_issue: bool = True

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
    bc_po_entity: str = "PurchaseOrders"  # OData entity set for purchase orders (confirm name)
    bc_receipt_entity: str = "PurchRcptHeaders"  # OData entity for posted receipts (confirm name)
    bc_usage_entity: str = "ItemLedgerEntries"   # OData entity for the usage export
    # Which item-ledger Entry_Type values count as paper usage, comma-separated.
    # Confirmed on GML's BC14 (bc-test): Kiwiplan job consumption posts as
    # 'Negative Adjmt.' item-journal entries ('Consumption' is unused there but
    # kept so a later switch to production orders needs no config change).
    bc_usage_entry_types: str = "Negative Adjmt.,Consumption"
    # --- BC live-mode mapping (all standard BC V4 defaults; override per tenant,
    # no code changes needed — see INTEGRATIONS.md) ---
    bc_po_lines_entity: str = "PurchaseOrderLines"   # purchase-order lines entity set
    # The purchase-order header field that carries THIS app's PO number, used to
    # find-or-create idempotently on retry. Standard/newer BC exposes
    # 'External_Document_No'; BC14 on-prem does NOT surface it on the Purchase
    # Order page — use a field that IS exposed there, e.g. 'Vendor_Order_No'.
    # Must be queryable + writable on the PO page (see the readiness probe).
    bc_po_extref_field: str = "External_Document_No"
    bc_invoice_entity: str = "PurchInvHeaders"       # posted purchase invoices (3-way match)
    bc_vendors_entity: str = "Vendors"               # vendor master
    bc_customers_entity: str = "Customers"           # customer master (publish Page 22 as 'Customers')
    bc_purchase_prices_entity: str = "Purchase_Prices"  # vendor price list (price/SKU/MOQ)
    bc_receipt_post_action: str = "Microsoft.NAV.Post"  # bound action that posts the receive
    # Exactly-once receipt posting. When set, the adapter stamps this order-header
    # field with the canonical grn_no BEFORE posting; BC copies it onto the posted
    # receipt, so a retry after a lost Post response can query BC for a receipt
    # already carrying this grn_no and skip re-posting (no double receive). Standard
    # BC exposes 'Vendor_Shipment_No' on both Purchase Order and Purch. Rcpt. Header;
    # confirm it's free to use as a correlation key on your tenant, then set it.
    # Blank (default) = best-effort readback only (documented double-post risk on a
    # lost Post response — run receipts read-only until this is set). See bc.py.
    bc_receipt_correlation_field: str = ""
    # Grade + deckle (SOP §3): name the item-master OData fields if BC carries them
    # as attributes; when BOTH are blank the adapter parses the item No against the
    # SKU pattern below (one item per grade+deckle, e.g. 'CWT140-1400').
    bc_grade_field: str = ""
    bc_deckle_field: str = ""
    bc_paper_sku_regex: str = r"^([A-Z]{2,4}\d{2,3})-(\d{3,4})$"
    # Optional item-master fields (blank = don't read).
    bc_reorder_point_field: str = "Reorder_Point"
    bc_lead_time_field: str = "Lead_Time_Calculation"   # dateformula, e.g. '45D'
    bc_replenishment_field: str = ""   # e.g. 'Replenishment_System'; 'Prod. Order' => FINISHED
    # Cross-system crosswalk (items.kiwiplan_ref / accura_ref in live mode):
    #   sku    — the BC item No IS the material code in Kiwiplan/Accura (default)
    #   fields — read the OData fields named below from the item master
    #   none   — leave refs unset (per-material stock stays empty until mapped)
    crosswalk_mode: str = "sku"
    bc_kiwiplan_ref_field: str = ""
    bc_accura_ref_field: str = ""

    # Kiwiplan (KDW/SQL read, KMC inject) / Accura (ODBC read).
    # *_stock_sql is a parameterized query you supply (see INTEGRATIONS.md) returning
    # columns: location, on_hand, allocated, on_order — with one :item_ref placeholder.
    kiwiplan_dsn: str = ""
    kiwiplan_stock_sql: str = ""
    accura_dsn: str = ""
    accura_stock_sql: str = ""

    # Azure SQL analytics warehouse (Phase 5). The analytics figures (spend,
    # on-time-delivery, stock-turn) are pushed here for Power BI. Guarded: with no
    # DSN the warehouse writer logs + no-ops ('skipped:not-configured'), so the push
    # endpoint stays usable in demo mode and only writes for real once set.
    # AZURE_SQL_DSN is the documented env var; WAREHOUSE_DSN is accepted as an alias.
    warehouse_dsn: str = Field(
        default="",
        validation_alias=AliasChoices("AZURE_SQL_DSN", "WAREHOUSE_DSN"),
    )

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
    # local role codes. Exact group/role ids are an open question (CLAUDE.md §7).
    # entra_role_map is the recommended production path: an explicit, exact map of
    # Entra app-role / group value (or GUID) -> local role code. When empty we fall
    # back to exact whole-token matching of the role code in the claim value (never
    # substring, so 'Finance-Admins' / 'Non-Admin-Users' cannot escalate to ADMIN).
    entra_role_claim: str = "roles"
    entra_role_map: dict[str, str] = {}
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
    def graph_enabled(self) -> bool:
        """True iff the M365 Graph mailer is fully configured (tenant+client+secret).
        When false the vendor-notify path is skipped rather than attempted."""
        return bool(
            self.graph_tenant_id and self.graph_client_id and self.graph_client_secret
        )

    @property
    def warehouse_enabled(self) -> bool:
        """True iff the Azure SQL analytics warehouse is configured (AZURE_SQL_DSN
        set). When false the warehouse writer no-ops ('skipped:not-configured')."""
        return bool(self.warehouse_dsn)

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
