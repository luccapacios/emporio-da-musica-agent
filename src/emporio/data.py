"""Load and clean the store's operational data (CSV exports).

Cleaning decisions made here:
* name, price, status, stock and specs are the source of truth for a product;
* a description that names another brand, or contradicts the specs, is withheld from the
  agent and recorded in `description_conflict` (see docs/data_quality.md);
* phones are compared by digits and e-mails case-insensitively.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BRANDS = [
    "yamaha", "tagima", "giannini", "rozini", "shelby", "crafter", "martin", "taylor",
    "takamine", "kala", "ohana", "kalani", "fender", "gibson", "ibanez", "prs",
    "music man", "pearl", "tama", "korg", "nord", "roland",
]
# Model names that identify a brand even when the brand itself is not written.
MODEL_BRANDS = {
    "stratocaster": "fender", "telecaster": "fender", "jazz bass": "fender",
    "precision": "fender", "stingray": "music man", "les paul": "gibson",
    "export": "pearl", "stage custom": "yamaha", "supernatural": "roland",
}


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace, for matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.lower().split())


def phone_digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _mentions(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    price: float
    category_id: int
    category: str
    description: str | None
    description_conflict: str | None
    stock: int
    status: str  # active | discontinued | coming_soon
    specs: dict
    created_at: date

    @property
    def brand(self) -> str | None:
        name = normalize(self.name)
        return next((b for b in BRANDS if _mentions(b, name)), None)


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    phone: str
    phone_digits: str
    email: str
    city: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int


@dataclass(frozen=True)
class Order:
    id: int
    customer_id: int
    order_date: date
    status: str  # pending | confirmed | shipped | delivered | cancelled
    total: float
    payment_method: str
    installments: int | None
    tracking_code: str | None
    estimated_delivery: date | None
    notes: str | None
    items: tuple[OrderItem, ...]


@dataclass(frozen=True)
class Promotion:
    id: int
    product_id: int
    discount_percent: int
    description: str
    is_active: bool


@dataclass
class Store:
    products: dict[int, Product]
    categories: dict[int, str]
    customers: dict[int, Customer]
    orders: dict[int, Order]
    promotions: list[Promotion]
    _by_email: dict[str, list[Customer]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for customer in self.customers.values():
            self._by_email.setdefault(customer.email, []).append(customer)

    def active_promotions(self) -> list[Promotion]:
        return [p for p in self.promotions if p.is_active and p.product_id in self.products]

    def active_promotion(self, product_id: int) -> Promotion | None:
        """Promotions are not cumulative (manual 6.2): the best active one applies."""
        candidates = [p for p in self.active_promotions() if p.product_id == product_id]
        return max(candidates, key=lambda p: p.discount_percent, default=None)

    def customers_with_email(self, email: str) -> list[Customer]:
        return list(self._by_email.get(email.strip().lower(), []))


def description_conflict(name: str, description: str, specs: dict) -> str | None:
    """Explain why a description cannot be trusted, or return None if it is consistent."""
    text = normalize(description)
    name_brand = next((b for b in BRANDS if _mentions(b, normalize(name))), None)
    problems = []
    if name_brand:
        mentioned = {b for b in BRANDS if _mentions(b, text)}
        mentioned |= {brand for model, brand in MODEL_BRANDS.items() if _mentions(model, text)}
        others = sorted(mentioned - {name_brand})
        if others:
            problems.append(f"description refers to {', '.join(others)} but the product is {name_brand}")
    for unit, spec_key in (("teclas", "keys"), ("cordas", "strings")):
        said = re.search(rf"(\d+) {unit}", text)
        spec = str(specs.get(spec_key, ""))
        if said and spec.isdigit() and said.group(1) != spec:
            problems.append(f"description says {said.group(1)} {unit} but specs say {spec}")
    return "; ".join(problems) or None


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def load_store(raw_dir: Path) -> Store:
    categories = {int(r["category_id"]): r["name"] for r in _read(raw_dir / "categories.csv")}

    products = {}
    for r in _read(raw_dir / "products.csv"):
        specs = json.loads(r["specs"]) if r["specs"] else {}
        conflict = description_conflict(r["name"], r["description"], specs)
        product = Product(
            id=int(r["product_id"]),
            name=r["name"].strip(),
            price=float(r["price_brl"]),
            category_id=int(r["category_id"]),
            category=categories[int(r["category_id"])],
            description=None if conflict else r["description"].strip(),
            description_conflict=conflict,
            stock=int(r["stock_quantity"]),
            status=r["status"].strip(),
            specs=specs,
            created_at=date.fromisoformat(r["created_at"]),
        )
        products[product.id] = product

    customers = {}
    for r in _read(raw_dir / "customers.csv"):
        customer = Customer(
            id=int(r["customer_id"]),
            name=r["name"].strip(),
            phone=r["phone"].strip(),
            phone_digits=phone_digits(r["phone"]),
            email=r["email"].strip().lower(),
            city=r["city"].strip(),
        )
        customers[customer.id] = customer

    items: dict[int, list[OrderItem]] = {}
    for r in _read(raw_dir / "order_items.csv"):
        items.setdefault(int(r["order_id"]), []).append(
            OrderItem(product_id=int(r["product_id"]), quantity=int(r["quantity"]))
        )

    orders = {}
    for r in _read(raw_dir / "orders.csv"):
        method = r["payment_method"].strip()
        credit = re.fullmatch(r"credit_(\d+)x", method)
        order = Order(
            id=int(r["order_id"]),
            customer_id=int(r["customer_id"]),
            order_date=date.fromisoformat(r["order_date"]),
            status=r["status"].strip(),
            total=float(r["total_brl"]),
            payment_method="credit" if credit else method,
            installments=int(credit.group(1)) if credit else None,
            tracking_code=r["tracking_code"].strip() or None,
            estimated_delivery=_date(r["estimated_delivery"].strip()),
            notes=r["notes"].strip() or None,
            items=tuple(items.get(int(r["order_id"]), [])),
        )
        orders[order.id] = order

    promotions = [
        Promotion(
            id=int(r["promotion_id"]),
            product_id=int(r["product_id"]),
            discount_percent=int(r["discount_percent"]),
            description=r["description"].strip(),
            is_active=r["is_active"].strip() == "1",
        )
        for r in _read(raw_dir / "promotions.csv")
    ]

    return Store(products, categories, customers, orders, promotions)
