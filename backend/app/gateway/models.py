"""Canonical data model (gateway-owned). Separate systems keep their own DBs;
these tables hold the canonical state + the crosswalk (external_refs) only."""
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def uid() -> str:
    return str(uuid.uuid4())


class ItemType(str, Enum):
    FINISHED = "FINISHED"
    SEMI_FINISHED = "SEMI_FINISHED"
    MATERIAL = "MATERIAL"
    SERVICE = "SERVICE"


class BomOwner(str, Enum):
    APP = "APP"
    KIWIPLAN = "KIWIPLAN"
    ACCURA = "ACCURA"


class Item(SQLModel, table=True):
    __tablename__ = "items"
    id: str = Field(default_factory=uid, primary_key=True)
    sku: str = Field(index=True, unique=True)
    name: str
    item_type: ItemType
    uom: str = "EA"
    bc_item_no: Optional[str] = None
    kiwiplan_ref: Optional[str] = None
    accura_ref: Optional[str] = None
    is_purchased: bool = False
    is_made: bool = False
    reorder_point: Optional[float] = None
    lead_time_days: Optional[int] = None
    std_cost: Optional[float] = None          # rolled up from BOM
    sales_price: Optional[float] = None       # cached from BC price list
    price_synced_at: Optional[datetime] = None
    active: bool = True


class ItemPrice(SQLModel, table=True):
    __tablename__ = "item_prices"
    id: str = Field(default_factory=uid, primary_key=True)
    item_id: str = Field(foreign_key="items.id", index=True)
    price_list: str = "DEFAULT"
    currency: str = "FJD"
    unit_price: float
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    source: str = "BC"


class BomHeader(SQLModel, table=True):
    __tablename__ = "bom_headers"
    id: str = Field(default_factory=uid, primary_key=True)
    parent_item_id: str = Field(foreign_key="items.id", index=True)
    version: int = 1
    status: str = "ACTIVE"          # DRAFT|ACTIVE|OBSOLETE
    owner: BomOwner = BomOwner.APP
    yield_qty: float = 1.0
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    synced_at: Optional[datetime] = None


class BomLine(SQLModel, table=True):
    __tablename__ = "bom_lines"
    id: str = Field(default_factory=uid, primary_key=True)
    bom_header_id: str = Field(foreign_key="bom_headers.id", index=True)
    line_no: int
    component_id: str = Field(foreign_key="items.id")
    qty_per: float
    uom: str = "EA"
    scrap_pct: float = 0.0


class Vendor(SQLModel, table=True):
    __tablename__ = "vendors"
    id: str = Field(default_factory=uid, primary_key=True)
    bc_vendor_no: Optional[str] = Field(default=None, index=True)
    name: str
    email: Optional[str] = None


class VendorPrice(SQLModel, table=True):
    __tablename__ = "vendor_prices"
    id: str = Field(default_factory=uid, primary_key=True)
    vendor_id: str = Field(foreign_key="vendors.id", index=True)
    item_id: str = Field(foreign_key="items.id", index=True)
    price: float
    currency: str = "FJD"
    moq: Optional[float] = None
    lead_time_days: Optional[int] = None


class Requisition(SQLModel, table=True):
    __tablename__ = "requisitions"
    id: str = Field(default_factory=uid, primary_key=True)
    number: str = Field(index=True, unique=True)
    requester: str
    status: str = "DRAFT"          # DRAFT|SUBMITTED|IN_APPROVAL|APPROVED|REJECTED|CLOSED|CANCELLED
    source: str = "manual"         # manual|demand|reorder
    cost_center: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RequisitionLine(SQLModel, table=True):
    __tablename__ = "requisition_lines"
    id: str = Field(default_factory=uid, primary_key=True)
    requisition_id: str = Field(foreign_key="requisitions.id", index=True)
    item_id: str = Field(foreign_key="items.id")
    quantity: float
    needed_by: Optional[date] = None


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "purchase_orders"
    id: str = Field(default_factory=uid, primary_key=True)
    number: str = Field(index=True, unique=True)
    vendor_id: str = Field(foreign_key="vendors.id", index=True)
    status: str = "DRAFT"          # DRAFT|PO_ISSUED|ACKNOWLEDGED|PARTIALLY_RECEIVED|RECEIVED|MATCHED|CLOSED|CANCELLED
    total: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class POLine(SQLModel, table=True):
    __tablename__ = "po_lines"
    id: str = Field(default_factory=uid, primary_key=True)
    po_id: str = Field(foreign_key="purchase_orders.id", index=True)
    item_id: str = Field(foreign_key="items.id")
    quantity: float
    unit_price: float


class Receipt(SQLModel, table=True):
    __tablename__ = "receipts"
    id: str = Field(default_factory=uid, primary_key=True)
    po_id: str = Field(foreign_key="purchase_orders.id", index=True)
    grn_no: str
    quantity: float
    received_at: datetime = Field(default_factory=datetime.utcnow)


class StockSnapshot(SQLModel, table=True):
    __tablename__ = "stock_snapshots"
    id: str = Field(default_factory=uid, primary_key=True)
    item_id: str = Field(foreign_key="items.id", index=True)
    system: str                    # KIWIPLAN|ACCURA|BC
    location: Optional[str] = None
    on_hand: float = 0
    allocated: float = 0
    on_order: float = 0
    available: float = 0
    as_of: datetime = Field(default_factory=datetime.utcnow)


class ExternalRef(SQLModel, table=True):
    """The crosswalk: one canonical entity -> its native id in a system."""
    __tablename__ = "external_refs"
    id: str = Field(default_factory=uid, primary_key=True)
    entity_kind: str               # ORDER|ORDER_LINE|REQUISITION|PO|RECEIPT
    entity_id: str
    system: str                    # BC|KIWIPLAN|ACCURA
    external_type: str             # SALES_ORDER|PRODUCTION_ORDER|LABEL_JOB|PURCHASE_ORDER|INVOICE|GRN
    external_id: str
    external_status: Optional[str] = None
    synced_at: datetime = Field(default_factory=datetime.utcnow)


class IntegrationOutbox(SQLModel, table=True):
    __tablename__ = "integration_outbox"
    id: Optional[int] = Field(default=None, primary_key=True)
    target: str                    # BC|KIWIPLAN|ACCURA
    action: str
    request_json: str
    status: str = "PENDING"        # PENDING|SENT|FAILED
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OrderEvent(SQLModel, table=True):
    __tablename__ = "order_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_kind: str
    entity_id: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    event_type: str
    actor: Optional[str] = None
    detail_json: Optional[str] = None
    occurred_at: datetime = Field(default_factory=datetime.utcnow)


class Role(SQLModel, table=True):
    __tablename__ = "roles"
    code: str = Field(primary_key=True)   # REQUESTER|OFFICER|APPROVER|VIEWER|ADMIN
    name: str
    approval_limit: Optional[float] = None


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: str = Field(default_factory=uid, primary_key=True)
    entra_oid: Optional[str] = Field(default=None, index=True)
    email: str = Field(index=True, unique=True)
    name: Optional[str] = None
    role_code: Optional[str] = Field(default=None, foreign_key="roles.code")
    active: bool = True
