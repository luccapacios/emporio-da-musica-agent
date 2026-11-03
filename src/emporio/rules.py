"""Business rules from the store manual, as pure functions.

Anything the manual turns into a number (prices, installments, deadlines, opening hours)
is computed here, so the model never has to do arithmetic or date math on its own.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache

import holidays

from .data import Order

# Manual 3 and 3.1
PIX_DISCOUNT_PERCENT = 5
SPLIT_PAYMENT_ABOVE = 2000.0
MAX_INSTALLMENTS = 12

# Manual 2. Monday is 0; Sunday is closed.
WEEKLY_HOURS = {0: (time(9), time(18)), 1: (time(9), time(18)), 2: (time(9), time(18)),
                3: (time(9), time(18)), 4: (time(9), time(18)), 5: (time(9), time(13))}
WEEKLY_HOURS_TEXT = "Segunda a sexta, 09:00 às 18:00; sábado, 09:00 às 13:00; domingos e feriados, fechado"
# The holidays package covers national and MS state holidays; municipal ones are added here.
CAMPO_GRANDE_HOLIDAYS = {(8, 26): "Aniversário de Campo Grande"}
WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]

# The manual sets no processing time for orders. Past this, an unshipped order is treated
# as stuck and the agent offers a handoff (documented assumption).
STALE_AFTER_DAYS = 15


def min_installment(n: int) -> float:
    """Minimum value of each installment when paying in n interest free installments."""
    if n <= 1:
        return 0.0
    if n <= 3:
        return 50.0
    if n <= 6:
        return 80.0
    return 100.0


def max_installments(amount: float) -> int:
    best = 1
    for n in range(2, MAX_INSTALLMENTS + 1):
        if amount / n >= min_installment(n):
            best = n
    return best


def pix_price(price: float, on_promotion: bool) -> float | None:
    """PIX takes 5% off the list price, but never on top of a promotion (manual 6.2)."""
    if on_promotion:
        return None
    return round(price * (100 - PIX_DISCOUNT_PERCENT) / 100, 2)


def promo_price(price: float, discount_percent: int) -> float:
    return round(price * (100 - discount_percent) / 100, 2)


def payment_options(price: float, on_promotion: bool = False) -> dict:
    n = max_installments(price)
    pix = pix_price(price, on_promotion)
    return {
        "price": price,
        "pix": (
            {"price": pix, "discount_percent": PIX_DISCOUNT_PERCENT}
            if pix is not None
            else {"price": price, "note": "o desconto do PIX não se aplica sobre preço promocional"}
        ),
        "debit": {"price": price},
        "boleto": {"price": price, "note": "compensação em até 3 dias úteis"},
        "credit": {"max_installments": n, "installment_value": round(price / n, 2), "interest_free": True},
        "split_payment_allowed": price > SPLIT_PAYMENT_ABOVE,
    }


def _window(start: date, days: int, today: date, rule: str, conditions: list[str]) -> dict:
    deadline = start + timedelta(days=days)
    return {
        "rule": rule,
        "counted_from": start.isoformat(),
        "deadline": deadline.isoformat(),
        "open": today <= deadline,
        "days_left": max((deadline - today).days, 0),
        "conditions": conditions,
    }


def return_windows(order: Order, today: date) -> dict:
    """Which return, exchange and warranty windows are still open for an order (manual 4 and 8)."""
    if order.status != "delivered":
        reasons = {
            "shipped": "o pedido ainda não foi entregue; os prazos contam a partir do recebimento. "
            "Se chegar avariado, o cliente deve recusar o recebimento e falar com a loja (manual 5.2).",
            "confirmed": "o pedido ainda não foi enviado; cancelamento antes do envio não está no manual, encaminhar para a equipe.",
            "pending": "o pedido ainda não foi enviado; cancelamento antes do envio não está no manual, encaminhar para a equipe.",
            "cancelled": "o pedido foi cancelado; não há produto a devolver.",
        }
        return {"applicable": False, "status": order.status, "reason": reasons.get(order.status, "status sem regra")}

    # Receipt date is not recorded; the estimated delivery date stands in for it.
    received = order.estimated_delivery or order.order_date
    return {
        "applicable": True,
        "received_on": received.isoformat(),
        "received_on_is_estimate": True,
        "windows": {
            "withdrawal": _window(received, 7, today, "Direito de arrependimento (compras online): 7 dias corridos após o recebimento, sem justificativa", [
                "embalagem original, sem sinais de uso, com acessórios e manuais",
                "reembolso na mesma forma de pagamento em até 10 dias úteis",
                "frete de devolução por conta da loja",
            ]),
            "preference_exchange": _window(received, 7, today, "Troca por preferência (cor, modelo, tamanho): até 7 dias", [
                "produto em perfeito estado e na embalagem original",
                "sujeita à disponibilidade do produto desejado",
                "diferença de valor cobrada ou reembolsada",
            ]),
            "defect_exchange": _window(order.order_date, 30, today, "Troca por defeito de fabricação: até 30 dias corridos após a compra", [
                "não cobre mau uso, quedas, umidade excessiva ou modificações não autorizadas",
            ]),
            "legal_warranty": _window(received, 90, today, "Garantia legal (CDC): 90 dias contra defeitos de fabricação, a partir do recebimento", [
                "não cobre desgaste natural, mau uso, reparos por terceiros ou danos apenas estéticos",
            ]),
        },
        "after_windows": "Depois de 30 dias, defeitos seguem pela garantia do fabricante (6 meses a 2 anos, "
        "conforme o certificado do produto); a loja pode intermediar se o cliente pedir.",
        "not_eligible": "Não são elegíveis para troca: instrumentos com personalização ou ajuste sob encomenda, "
        "itens de liquidação marcados como venda final e boquilhas de sopro.",
    }


def delivery_status(order: Order, today: date) -> dict:
    """Flags an order that is late against its own ETA, or stuck before shipping."""
    if order.status == "shipped" and order.estimated_delivery:
        late = (today - order.estimated_delivery).days
        return {"overdue": late > 0, "days_late": max(late, 0)}
    if order.status in ("pending", "confirmed"):
        age = (today - order.order_date).days
        return {"overdue": False, "days_since_order": age, "stale": age > STALE_AFTER_DAYS}
    return {"overdue": False}


@lru_cache(maxsize=8)
def _calendar(year: int) -> holidays.HolidayBase:
    return holidays.country_holidays("BR", subdiv="MS", years=year, language="pt_BR")


def holiday_name(day: date) -> str | None:
    if (day.month, day.day) in CAMPO_GRANDE_HOLIDAYS:
        return CAMPO_GRANDE_HOLIDAYS[(day.month, day.day)]
    return _calendar(day.year).get(day)


def opening_hours(day: date) -> tuple[time, time] | None:
    if holiday_name(day):
        return None
    return WEEKLY_HOURS.get(day.weekday())


def _next_opening(now: datetime) -> datetime | None:
    for offset in range(15):
        day = now.date() + timedelta(days=offset)
        hours = opening_hours(day)
        if hours:
            start = datetime.combine(day, hours[0], tzinfo=now.tzinfo)
            if start > now:
                return start
    return None


def store_status(now: datetime) -> dict:
    today = now.date()
    hours = opening_hours(today)
    holiday = holiday_name(today)
    open_now = hours is not None and hours[0] <= now.time() < hours[1]

    closed_reason = None
    if not open_now:
        if holiday:
            closed_reason = f"feriado ({holiday})"
        elif today.weekday() == 6:
            closed_reason = "domingo"
        elif hours and now.time() < hours[0]:
            closed_reason = "a loja ainda não abriu hoje"
        else:
            closed_reason = "fora do horário de funcionamento"

    next_opening = None if open_now else _next_opening(now)
    return {
        "now": now.isoformat(timespec="minutes"),
        "weekday": WEEKDAYS_PT[today.weekday()],
        "holiday_today": holiday,
        "open_now": open_now,
        "today_hours": f"{hours[0]:%H:%M} às {hours[1]:%H:%M}" if hours else "fechado",
        "closes_at": f"{hours[1]:%H:%M}" if open_now else None,
        "closed_reason": closed_reason,
        "next_opening": next_opening.isoformat(timespec="minutes") if next_opening else None,
        "next_opening_label": (
            f"{WEEKDAYS_PT[next_opening.weekday()]}, {next_opening:%d/%m} às {next_opening:%H:%M}"
            if next_opening else None
        ),
        "weekly_hours": WEEKLY_HOURS_TEXT,
        "special_dates": "Em datas especiais (Black Friday, aniversário da loja, Natal) o horário pode ser "
        "estendido; mudanças são avisadas nas redes sociais e no WhatsApp.",
    }
