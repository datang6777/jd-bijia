from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Product:
    pk_id: str
    third_sku: str
    channel: str
    name: str
    brand: str
    generic: str
    spec: str
    manufacturer: str
    approval: str
    given_link: str = ""
    row_index: int = 0

    @property
    def has_given_link(self) -> bool:
        link = (self.given_link or "").strip()
        return link.startswith("http")


@dataclass
class Offer:
    sku: str
    url: str
    title: str
    shop: str
    coupon_price: float | None = None
    jd_price: float | None = None
    sales: int = 0
    in_stock: bool = True
    region: str = ""
    page_text: str = ""
    price_source: str = ""
    tier: int = 3


@dataclass
class Decision:
    status: str
    price: str = ""
    url: str = ""
    shop: str = ""
    title: str = ""
    factor: int = 1
    factor_text: str = ""
    region: str = ""
    confidence: str = ""
    reason: str = ""
    price_basis: str = ""
    sku: str = ""
