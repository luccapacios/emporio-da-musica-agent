from datetime import date, timedelta

import pytest

from emporio.config import at
from emporio.data import Order
from emporio.rules import (
    delivery_status,
    holiday_name,
    max_installments,
    payment_options,
    pix_price,
    promo_price,
    return_windows,
    store_status,
)


@pytest.mark.parametrize(
    "amount, expected",
    [
        (90.0, 1),  # 2x would be 45, below the R$ 50 floor
        (159.9, 3),  # 3x of 53.30
        (349.9, 4),  # 4x of 87.48; 5x would be 69.98, below R$ 80
        (429.9, 5),  # 5x of 85.98
        (2199.0, 12),
    ],
)
def test_max_installments_follows_minimum_installment_table(amount, expected):
    assert max_installments(amount) == expected


def test_pix_discount_is_not_cumulative_with_promotions():
    assert pix_price(2199.0, on_promotion=False) == 2089.05
    assert pix_price(2199.0, on_promotion=True) is None


def test_payment_options_summary():
    options = payment_options(2199.0)
    assert options["pix"]["price"] == 2089.05
    assert options["credit"] == {"max_installments": 12, "installment_value": 183.25, "interest_free": True}
    assert options["split_payment_allowed"] is True
    assert payment_options(1500.0)["split_payment_allowed"] is False


def test_promo_price():
    assert promo_price(549.0, 20) == 439.2


def _delivered(received: date) -> Order:
    return Order(
        id=99, customer_id=1, order_date=received - timedelta(days=10), status="delivered",
        total=100.0, payment_method="pix", installments=None, tracking_code="BRAA1234567BR",
        estimated_delivery=received, notes=None, items=(),
    )


def test_return_windows_open_and_close_on_time():
    received = date(2026, 3, 10)
    order = _delivered(received)

    fresh = return_windows(order, received + timedelta(days=7))
    assert fresh["windows"]["withdrawal"]["open"] is True
    assert fresh["windows"]["withdrawal"]["days_left"] == 0

    late = return_windows(order, received + timedelta(days=8))
    assert late["windows"]["withdrawal"]["open"] is False
    assert late["windows"]["defect_exchange"]["open"] is True  # 30 days from purchase
    assert late["windows"]["legal_warranty"]["deadline"] == "2026-06-08"


def test_return_windows_need_a_delivered_order(store):
    assert return_windows(store.orders[8], date(2026, 11, 2))["applicable"] is False  # shipped


def test_shipped_order_past_eta_is_overdue(store):
    status = delivery_status(store.orders[8], date(2026, 11, 2))
    assert status["overdue"] is True
    assert status["days_late"] == 247


def test_finados_is_a_closed_holiday():
    status = store_status(at(date(2026, 11, 2), 10, 0))
    assert status["open_now"] is False
    assert "Finados" in status["closed_reason"]
    assert status["next_opening"] == "2026-11-03T09:00-04:00"


def test_saturday_closes_at_one_pm():
    saturday = date(2026, 11, 7)
    assert store_status(at(saturday, 12, 59))["open_now"] is True
    closed = store_status(at(saturday, 13, 0))
    assert closed["open_now"] is False
    assert closed["next_opening"] == "2026-11-09T09:00-04:00"  # Sunday closed


def test_weekday_hours_and_local_holidays():
    assert store_status(at(date(2026, 11, 3), 17, 59))["open_now"] is True
    assert store_status(at(date(2026, 11, 3), 18, 0))["open_now"] is False
    assert holiday_name(date(2026, 8, 26)) == "Aniversário de Campo Grande"
    assert holiday_name(date(2026, 10, 11)) is not None  # Criação do Estado de MS
    assert holiday_name(date(2026, 11, 4)) is None
