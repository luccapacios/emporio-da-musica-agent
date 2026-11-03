"""Data quality checks over the loaded store data.

These findings are not "fixed" silently: the agent works around them (see data.py and
tools.py) and the report in docs/data_quality.md lists them for the store team.
"""

from __future__ import annotations

from collections import Counter

from .data import Store
from .rules import max_installments, min_installment

# Contradictions inside the policy manual itself, found while reading it.
MANUAL_FINDINGS = [
    ("manual 1.2 vs 7", "Two WhatsApp numbers: (67) 3341-4444 in the company data table and "
     "(67) 3321-4500 in section 7. The agent uses the company data table."),
    ("manual cover vs 1.2", "E-mail written as contato@emporiodamusica.com.br on the cover and "
     "contato@empóriodamusica.com.br (accented domain) in 1.2. The agent uses the unaccented one."),
    ("manual 3 vs 3.1", "The payment table says 'minimum installment R$ 100,00' for credit; 3.1 sets "
     "R$ 50 (2x to 3x), R$ 80 (4x to 6x) and R$ 100 (7x to 12x). The detailed rule in 3.1 is used."),
    ("manual 1 vs data", "The manual mentions 300+ instruments, including woodwinds, brass and "
     "orchestral strings; the data has 65 products and none in those three categories."),
]


def quality_report(store: Store) -> list[tuple[str, str]]:
    """Return (where, finding) pairs."""
    findings: list[tuple[str, str]] = []

    for p in store.products.values():
        if p.description_conflict:
            findings.append((f"product {p.id}", f"{p.name}: {p.description_conflict}. Description withheld."))
        if p.status == "discontinued" and p.stock > 0:
            findings.append((f"product {p.id}", f"{p.name} is discontinued but has {p.stock} units in stock. Not offered."))
        if p.status == "active" and p.stock == 0:
            findings.append((f"product {p.id}", f"{p.name} is active with zero stock. Shown as temporarily unavailable."))

    used = {p.category_id for p in store.products.values()}
    for cid, name in store.categories.items():
        if cid not in used:
            findings.append((f"category {cid}", f"{name} has no products."))

    for order in store.orders.values():
        prices = [store.products[i.product_id].price * i.quantity for i in order.items if i.product_id in store.products]
        expected = round(sum(prices), 2)
        if abs(expected - order.total) > 0.01:
            findings.append((f"order {order.id}", f"Recorded total R$ {order.total:.2f} differs from items at "
                             f"current prices (R$ {expected:.2f}). The recorded total is quoted."))
        if order.installments and order.installments > max_installments(order.total):
            value = order.total / order.installments
            findings.append((f"order {order.id}", f"Paid in {order.installments}x of R$ {value:.2f}, below the "
                             f"R$ {min_installment(order.installments):.0f} minimum installment in manual 3.1."))
        if order.status in ("delivered", "shipped"):
            for item in order.items:
                product = store.products.get(item.product_id)
                if product and product.status == "coming_soon":
                    findings.append((f"order {order.id}", f"Is {order.status} but contains {product.name}, "
                                     f"which is marked coming_soon."))

    emails = Counter(c.email for c in store.customers.values())
    for email, count in emails.items():
        if count > 1:
            ids = ", ".join(str(c.id) for c in store.customers_with_email(email))
            findings.append(("customers " + ids, f"Share the e-mail {email}. Order lookups by this e-mail require the phone."))

    rows = Counter(p.product_id for p in store.promotions)
    for product_id, count in sorted(rows.items()):
        if count > 1:
            findings.append((f"product {product_id}", f"Has {count} promotion rows; only active ones count and they never stack."))

    return findings
