"""Tools the model can call. Each returns a JSON-serializable dict.

Two rules hold for every tool:
* facts come from the data and the rules module, never from the model;
* order data is only returned after the contact given matches the order's owner.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .data import Order, Product, Store, normalize, phone_digits
from .policy import TOPIC_DESCRIPTIONS, Policies
from .rules import (
    delivery_status,
    max_installments,
    payment_options,
    pix_price,
    promo_price,
    return_windows,
    store_status,
)

SELLABLE = ("in_stock", "last_units")
STOPWORDS = {"a", "o", "e", "de", "da", "do", "das", "dos", "para", "pra", "com", "um", "uma", "em", "no", "na", "que", "ate"}

# Words a customer may use for each category id.
CATEGORY_KEYWORDS = {
    1: ["guitarra"],
    2: ["baixo", "contrabaixo eletrico", "baixo eletrico"],
    3: ["bateria", "percussao"],
    4: ["teclado", "piano", "sintetizador", "controlador"],
    5: ["violao", "violoes", "folk", "classico"],
    6: ["sopro", "madeira", "flauta", "clarinete", "saxofone", "sax", "oboe"],
    7: ["metais", "trompete", "trombone", "tuba", "trompa"],
    8: ["cordas orquestrais", "violino", "viola", "violoncelo", "cello", "contrabaixo acustico"],
    9: ["ukulele", "uke"],
}

STATUS_LABELS = {
    "pending": "pendente",
    "confirmed": "confirmado, em preparação",
    "shipped": "enviado",
    "delivered": "entregue",
    "cancelled": "cancelado",
}

NOT_FOUND = "Não encontrei um pedido com esse número e esses dados de contato."


def brl(value: float) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def _stem(token: str) -> str:
    if token.endswith("oes") or token.endswith("aes"):
        return token[:-3] + "ao"
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", normalize(text)) if t not in STOPWORDS]


def _availability(p: Product) -> str:
    if p.status == "discontinued":
        return "discontinued"
    if p.status == "coming_soon":
        return "coming_soon"
    if p.stock == 0:
        return "out_of_stock"
    return "last_units" if p.stock <= 2 else "in_stock"


class ToolBox:
    def __init__(self, store: Store, policies: Policies, clock: Callable[[], datetime], handoff_path: Path):
        self.store = store
        self.policies = policies
        self.clock = clock
        self.handoff_path = handoff_path
        self.handlers: dict[str, Callable[..., dict]] = {
            "search_products": self.search_products,
            "get_product": self.get_product,
            "list_active_promotions": self.list_active_promotions,
            "payment_options": self.payment_options,
            "get_order": self.get_order,
            "find_orders": self.find_orders,
            "check_return": self.check_return,
            "store_hours_now": self.store_hours_now,
            "get_policy": self.get_policy,
            "escalate_to_human": self.escalate_to_human,
        }

    # ---- dispatch -------------------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        handler = self.handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return handler(**(args or {}))
        except (TypeError, ValueError, KeyError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ---- products -------------------------------------------------------------------

    def _price_now(self, p: Product) -> tuple[float, dict | None]:
        promo = self.store.active_promotion(p.id)
        if not promo:
            return p.price, None
        return promo_price(p.price, promo.discount_percent), {
            "name": promo.description,
            "discount_percent": promo.discount_percent,
            "original_price": p.price,
            "promo_price": promo_price(p.price, promo.discount_percent),
        }

    def _card(self, p: Product, detail: bool = False) -> dict:
        price, promo = self._price_now(p)
        pix = pix_price(p.price, on_promotion=promo is not None)
        n = max_installments(price)
        if promo:
            display = f"{brl(price)} (de {brl(p.price)}, {promo['discount_percent']}% off: {promo['name']})"
        else:
            display = f"{brl(price)} (PIX {brl(pix)})"
        if n > 1:
            display += f"; ou até {n}x de {brl(round(price / n, 2))} sem juros"
        card = {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "availability": _availability(p),
            "stock_quantity": p.stock,
            "price": price,
            "promotion": promo,
            "pix_price": pix,
            "display": display,
        }
        if detail:
            card["specs"] = p.specs
            if p.description:
                card["description"] = p.description
        return card

    def _resolve_category(self, category: str) -> int | None:
        text = normalize(category)
        for cid, name in self.store.categories.items():
            if normalize(name) == text:
                return cid
        for cid, words in CATEGORY_KEYWORDS.items():
            if any(_stem(w) in _tokens(text) or w in text for w in words):
                return cid
        return None

    def search_products(
        self,
        query: str | None = None,
        category: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        strings: str | None = None,
        electric: bool | None = None,
        include_unavailable: bool = False,
        limit: int = 8,
    ) -> dict:
        products = [p for p in self.store.products.values() if p.status != "discontinued"]

        if category:
            cid = self._resolve_category(category)
            if cid is None:
                return {"error": f"categoria desconhecida: {category}", "categories": list(self.store.categories.values())}
            products = [p for p in products if p.category_id == cid]
            if not products:
                return {"results": [], "total_matches": 0, "note": (
                    f"Nenhum item da categoria {self.store.categories[cid]} cadastrado no sistema no momento.")}

        if not include_unavailable:
            products = [p for p in products if _availability(p) in SELLABLE]
        if strings:
            wanted = "steel" if normalize(strings) in ("aco", "steel", "metal") else "nylon"
            products = [p for p in products if p.specs.get("strings") == wanted]
        if electric is not None:
            flag = "yes" if electric else "no"
            products = [p for p in products if p.specs.get("electronics", flag) == flag]

        priced = [(p, self._price_now(p)[0]) for p in products]
        if min_price is not None:
            priced = [(p, price) for p, price in priced if price >= min_price]
        if max_price is not None:
            priced = [(p, price) for p, price in priced if price <= max_price]

        scored = [(self._text_score(p, query) if query else 1, price, p) for p, price in priced]
        scored = [row for row in scored if row[0] > 0]
        scored.sort(key=lambda row: (-row[0], row[1]))

        limit = max(1, min(int(limit), 15))
        result = {"results": [self._card(p) for _, _, p in scored[:limit]], "total_matches": len(scored)}
        if not scored:
            result["note"] = "Nenhum produto encontrado com esses filtros."
        return result

    def _text_score(self, p: Product, query: str) -> int:
        haystack = set(_tokens(" ".join([p.name, p.category, p.description or "", *map(str, p.specs.values())])))
        terms = _tokens(query)
        return sum(1 for t in terms if t in haystack or (len(t) >= 4 and any(h.startswith(t) for h in haystack)))

    def _match_name(self, text: str) -> list[tuple[float, Product]]:
        query = normalize(text)
        terms = re.findall(r"[a-z0-9]+", query)
        scored = []
        for p in self.store.products.values():
            name = normalize(p.name)
            name_terms = re.findall(r"[a-z0-9]+", name)
            hits = sum(1 for t in terms if any(n == t or n.startswith(t) for n in name_terms))
            token_score = hits / len(terms) if terms else 0
            score = 0.7 * token_score + 0.3 * SequenceMatcher(None, query, name).ratio()
            scored.append((score, p))
        scored.sort(key=lambda row: -row[0])
        return scored

    def _alternatives(self, p: Product, count: int = 3) -> list[dict]:
        candidates = [
            q for q in self.store.products.values()
            if q.id != p.id and q.category_id == p.category_id and _availability(q) in SELLABLE
            and q.specs.get("strings") == p.specs.get("strings")
        ]
        candidates.sort(key=lambda q: abs(self._price_now(q)[0] - p.price))
        return [self._card(q) for q in candidates[:count]]

    def get_product(self, name_or_id: str) -> dict:
        text = str(name_or_id).strip()
        if text.isdigit():
            product = self.store.products.get(int(text))
            if product is None:
                return {"found": False, "message": f"Produto {text} não existe no sistema."}
            others: list[Product] = []
        else:
            ranked = self._match_name(text)
            best_score, product = ranked[0]
            if best_score < 0.6:
                return {"found": False, "suggestions": [p.name for _, p in ranked[:3]]}
            others = [p for score, p in ranked[1:4] if score >= best_score - 0.05]

        result = {"found": True, "product": self._card(product, detail=True)}
        if others:
            result["similar_names"] = [p.name for p in others]
        availability = _availability(product)
        if availability not in SELLABLE:
            result["alternatives"] = self._alternatives(product)
            result["availability_note"] = {
                "out_of_stock": "Temporariamente indisponível. Não confirmar prazo de reposição.",
                "discontinued": "Não faz mais parte do catálogo. Oferecer modelos equivalentes.",
                "coming_soon": "Lançamento em breve, ainda não disponível para venda.",
            }[availability]
        return result

    def list_active_promotions(self) -> dict:
        cards = [self._card(self.store.products[p.product_id]) for p in self.store.active_promotions()]
        return {
            "promotions": cards,
            "note": "Só estas promoções estão ativas. Campanhas do calendário (ex.: Black Friday) só valem quando aparecem aqui.",
        }

    def payment_options(self, product_id: int | None = None, amount: float | None = None) -> dict:
        if product_id is not None:
            product = self.store.products.get(int(product_id))
            if product is None:
                return {"error": f"produto {product_id} não existe"}
            price, promo = self._price_now(product)
            return {"product": product.name, **payment_options(price, on_promotion=promo is not None)}
        if amount is None:
            return {"error": "informe product_id ou amount"}
        return payment_options(float(amount))

    # ---- orders ---------------------------------------------------------------------

    def _owner_check(self, order: Order, phone: str | None, email: str | None) -> str | None:
        """None when the contact matches the order's customer, else a reason code."""
        customer = self.store.customers[order.customer_id]
        if phone:
            digits = phone_digits(phone)
            if len(digits) >= 8 and digits[-8:] == customer.phone_digits[-8:]:
                return None
        if email:
            address = email.strip().lower()
            if address == customer.email:
                return "shared_email" if len(self.store.customers_with_email(address)) > 1 else None
        return "missing_contact" if not (phone or email) else "mismatch"

    def _verified_order(self, order_id, phone, email) -> tuple[Order | None, dict | None]:
        order = self.store.orders.get(int(order_id))
        if order is None:
            return None, {"verified": False, "reason": "mismatch", "message": NOT_FOUND}
        reason = self._owner_check(order, phone, email)
        if reason is None:
            return order, None
        messages = {
            "missing_contact": "Peça o telefone ou o e-mail cadastrado no pedido.",
            "shared_email": "Este e-mail está em mais de um cadastro. Peça o telefone cadastrado.",
            "mismatch": NOT_FOUND,
        }
        return None, {"verified": False, "reason": reason, "message": messages[reason]}

    def _order_view(self, order: Order) -> dict:
        payment = {"pix": "PIX", "debit": "cartão de débito", "boleto": "boleto bancário"}.get(order.payment_method)
        if order.payment_method == "credit":
            payment = f"cartão de crédito em {order.installments}x"
        return {
            "order_id": order.id,
            "customer_first_name": self.store.customers[order.customer_id].first_name,
            "order_date": order.order_date.isoformat(),
            "status": order.status,
            "status_label": STATUS_LABELS.get(order.status, order.status),
            "items": [{"name": self.store.products[i.product_id].name, "quantity": i.quantity} for i in order.items],
            "total": order.total,
            "total_text": brl(order.total),
            "payment": payment,
            "tracking_code": order.tracking_code,
            "estimated_delivery": order.estimated_delivery.isoformat() if order.estimated_delivery else None,
            "notes": order.notes,
        }

    def get_order(self, order_id: int, phone: str | None = None, email: str | None = None) -> dict:
        order, failure = self._verified_order(order_id, phone, email)
        if failure:
            return failure
        today = self.clock().date()
        result = {"verified": True, "today": today.isoformat(), "order": self._order_view(order),
                  "delivery": delivery_status(order, today)}
        if order.tracking_code:
            result["tracking_note"] = "Rastreamento no site dos Correios ou da Jadlog (manual 5.3)."
        return result

    def find_orders(self, phone: str, email: str) -> dict:
        digits = phone_digits(phone)
        address = (email or "").strip().lower()
        owners = [c for c in self.store.customers.values()
                  if len(digits) >= 8 and c.phone_digits[-8:] == digits[-8:] and c.email == address]
        if not owners:
            return {"verified": False, "message": "Não encontrei pedidos com esse telefone e e-mail juntos."}
        orders = [o for o in self.store.orders.values() if o.customer_id in {c.id for c in owners}]
        return {
            "verified": True,
            "customer_first_name": owners[0].first_name,
            "orders": [
                {"order_id": o.id, "order_date": o.order_date.isoformat(), "status_label": STATUS_LABELS[o.status],
                 "total_text": brl(o.total)}
                for o in sorted(orders, key=lambda o: o.order_date)
            ],
        }

    def check_return(self, order_id: int, phone: str | None = None, email: str | None = None) -> dict:
        order, failure = self._verified_order(order_id, phone, email)
        if failure:
            return failure
        today = self.clock().date()
        return {"verified": True, "today": today.isoformat(), "order": self._order_view(order),
                **return_windows(order, today)}

    # ---- store and policy -----------------------------------------------------------

    def store_hours_now(self) -> dict:
        return store_status(self.clock())

    def get_policy(self, topic: str) -> dict:
        try:
            return {"topic": topic, "text": self.policies.get(topic)}
        except KeyError:
            return {"error": f"tópico desconhecido: {topic}", "topics": TOPIC_DESCRIPTIONS}

    def escalate_to_human(
        self,
        reason: str,
        summary: str,
        customer_name: str | None = None,
        contact: str | None = None,
        order_id: int | None = None,
    ) -> dict:
        now = self.clock()
        self.handoff_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.handoff_path.read_text(encoding="utf-8").splitlines() if self.handoff_path.exists() else []
        today_prefix = f"EMP-{now:%Y%m%d}-"
        sequence = sum(1 for line in existing if today_prefix in line) + 1
        protocol = f"{today_prefix}{sequence:04d}"
        record = {"protocol": protocol, "created_at": now.isoformat(), "reason": reason, "summary": summary,
                  "customer_name": customer_name, "contact": contact, "order_id": order_id}
        with self.handoff_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        status = store_status(now)
        return {
            "protocol": protocol,
            "sla": "retorno da equipe em até 24 horas úteis",
            "team_available_now": status["open_now"],
            "next_opening_label": status["next_opening_label"],
        }

    # ---- schemas --------------------------------------------------------------------

    def schemas(self) -> list[dict]:
        def fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
            return {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required or []},
            }}

        contact = {
            "phone": {"type": "string", "description": "Phone on file for the order, any format."},
            "email": {"type": "string", "description": "E-mail on file for the order."},
        }
        return [
            fn("search_products",
               "Search the catalog. Use for any question about which instruments exist, prices in a range, "
               "or recommendations. Returns only items that can be sold unless include_unavailable is true.",
               {
                   "query": {"type": "string", "description": "Free text, e.g. 'dreadnought', 'iniciante', 'Yamaha'."},
                   "category": {"type": "string", "description": "e.g. guitarra, baixo, bateria, teclado, violão, sopro, cordas orquestrais, ukulele."},
                   "min_price": {"type": "number"},
                   "max_price": {"type": "number"},
                   "strings": {"type": "string", "enum": ["nylon", "steel"], "description": "Violões only."},
                   "electric": {"type": "boolean", "description": "Violões and ukuleles with pickup (eletroacústico)."},
                   "include_unavailable": {"type": "boolean"},
                   "limit": {"type": "integer", "description": "Max results, default 8."},
               }),
            fn("get_product",
               "Look up one product by approximate name or id: price, stock, active promotion, PIX price, "
               "installments, specs. Suggests alternatives when it cannot be sold.",
               {"name_or_id": {"type": "string"}}, ["name_or_id"]),
            fn("list_active_promotions", "List promotions active right now.", {}),
            fn("payment_options",
               "Payment conditions for a product or an amount: PIX price, debit, boleto, maximum interest free "
               "installments, split payment.",
               {"product_id": {"type": "integer"}, "amount": {"type": "number"}}),
            fn("get_order",
               "Order status, items, tracking and ETA. Requires the order id plus the phone or e-mail on file.",
               {"order_id": {"type": "integer"}, **contact}, ["order_id"]),
            fn("find_orders",
               "List a customer's orders when they do not know the order id. Requires phone AND e-mail.",
               contact, ["phone", "email"]),
            fn("check_return",
               "Apply return, exchange and warranty windows to a verified order (regret, defect, preference).",
               {"order_id": {"type": "integer"}, **contact}, ["order_id"]),
            fn("store_hours_now",
               "Whether the store is open right now (Campo Grande time, holidays included) and the next opening.", {}),
            fn("get_policy",
               "Read a section of the store manual. Topics: " + "; ".join(f"{k}: {v}" for k, v in TOPIC_DESCRIPTIONS.items()),
               {"topic": {"type": "string", "enum": list(TOPIC_DESCRIPTIONS)}}, ["topic"]),
            fn("escalate_to_human",
               "Hand the case to the human team: complaints, overdue or stuck orders, return or exchange "
               "requests, data deletion requests, or anything the tools and manual do not cover.",
               {
                   "reason": {"type": "string", "enum": ["complaint", "overdue_delivery", "stuck_order",
                                                         "return_request", "data_request", "other"]},
                   "summary": {"type": "string", "description": "One or two sentences for the team, in Portuguese."},
                   "customer_name": {"type": "string"},
                   "contact": {"type": "string"},
                   "order_id": {"type": "integer"},
               }, ["reason", "summary"]),
        ]
