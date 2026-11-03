import json
from datetime import date

import pytest

from emporio.config import at
from emporio.tools import ToolBox, brl

FINADOS = at(date(2026, 11, 2), 10, 0)


@pytest.fixture
def tools(store, policies, tmp_path):
    return ToolBox(store, policies, clock=lambda: FINADOS, handoff_path=tmp_path / "handoffs.jsonl")


def test_brl_formatting():
    assert brl(2199) == "R$ 2.199,00"
    assert brl(429.9) == "R$ 429,90"


def test_search_guitars_up_to_1000_only_lists_what_can_be_sold(tools):
    result = tools.call("search_products", {"category": "violões", "max_price": 1000})
    ids = {p["id"] for p in result["results"]}
    assert result["total_matches"] == 12
    assert len(ids) == 8  # default limit; the agent is told how many exist
    assert 96 not in ids  # active but zero stock
    assert 113 not in ids  # discontinued
    assert all(p["price"] <= 1000 for p in result["results"])
    assert [p["price"] for p in result["results"]] == sorted(p["price"] for p in result["results"])


def test_search_filters_by_strings_and_electronics(tools):
    result = tools.call("search_products", {"category": "violao", "strings": "nylon", "electric": True, "limit": 15})
    assert result["results"]
    for card in result["results"]:
        product = tools.store.products[card["id"]]
        assert product.specs["strings"] == "nylon" and product.specs["electronics"] == "yes"


def test_search_reports_empty_categories(tools):
    result = tools.call("search_products", {"category": "saxofone"})
    assert result["results"] == []
    assert "Nenhum" in result["note"]


def test_get_product_by_approximate_name(tools):
    result = tools.call("get_product", {"name_or_id": "takamine gd20"})
    assert result["found"] is True
    assert result["product"]["id"] == 95
    assert result["product"]["price"] == 2199.0
    assert result["product"]["pix_price"] == 2089.05
    assert "12x de R$ 183,25" in result["product"]["display"]


def test_out_of_stock_product_comes_with_alternatives(tools):
    result = tools.call("get_product", {"name_or_id": "96"})
    assert result["product"]["availability"] == "out_of_stock"
    alternatives = result["alternatives"]
    assert alternatives and all(a["availability"] in ("in_stock", "last_units") for a in alternatives)
    assert all(tools.store.products[a["id"]].specs["strings"] == "steel" for a in alternatives)


def test_promotional_price_is_shown_with_original_and_no_pix_discount(tools):
    card = tools.call("get_product", {"name_or_id": "Ohana CK-20"})["product"]
    assert card["promotion"] == {
        "name": "Desconto Exclusivo", "discount_percent": 20, "original_price": 549.0, "promo_price": 439.2,
    }
    assert card["price"] == 439.2
    assert card["pix_price"] is None


def test_conflicting_description_is_not_exposed(tools):
    card = tools.call("get_product", {"name_or_id": "139"})["product"]
    assert "description" not in card
    assert "Pearl" not in json.dumps(card, ensure_ascii=False)


def test_active_promotions(tools):
    promos = tools.call("list_active_promotions", {})["promotions"]
    assert sorted(p["id"] for p in promos) == [90, 94, 121, 127]


def test_order_needs_matching_contact(tools):
    assert tools.call("get_order", {"order_id": 10})["verified"] is False

    wrong = tools.call("get_order", {"order_id": 10, "phone": "(67) 99999-0000"})
    assert wrong["verified"] is False
    assert "order" not in wrong

    shared = tools.call("get_order", {"order_id": 10, "email": "bruno.martins@jmail.com"})
    assert shared["verified"] is False
    assert shared["reason"] == "shared_email"

    ok = tools.call("get_order", {"order_id": 10, "phone": "67 98543-2109"})
    assert ok["verified"] is True
    assert ok["order"]["customer_first_name"] == "Bruno"
    assert ok["order"]["items"] == [{"name": "Music Man Bass 1X Electric Bass", "quantity": 1}]


def test_shipped_order_past_eta_is_flagged(tools):
    result = tools.call("get_order", {"order_id": 8, "email": "anacarol.ferreira@coldmail.com"})
    assert result["order"]["status"] == "shipped"
    assert result["delivery"]["overdue"] is True


def test_find_orders_requires_phone_and_email(tools):
    assert tools.call("find_orders", {"phone": "(67) 98432-1098", "email": "x@y.com"})["verified"] is False
    found = tools.call("find_orders", {"phone": "(67) 98432-1098", "email": "pedro.oliveira@jmail.com"})
    assert [o["order_id"] for o in found["orders"]] == [1, 19]


def test_check_return_applies_the_windows(tools):
    result = tools.call("check_return", {"order_id": 1, "phone": "(67) 98432-1098"})
    windows = result["windows"]
    assert windows["withdrawal"]["open"] is False
    assert windows["legal_warranty"]["open"] is False


def test_store_hours_and_policy(tools):
    assert tools.call("store_hours_now", {})["open_now"] is False
    assert "7 (sete) dias" in tools.call("get_policy", {"topic": "returns"})["text"]
    assert "error" in tools.call("get_policy", {"topic": "refunds"})


def test_escalation_is_logged_with_a_protocol(tools):
    result = tools.call("escalate_to_human", {"reason": "overdue_delivery", "summary": "Pedido 8 atrasado", "order_id": 8})
    assert result["protocol"] == "EMP-20261102-0001"
    assert result["team_available_now"] is False
    logged = [json.loads(line) for line in tools.handoff_path.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["order_id"] == 8


def test_bad_arguments_become_tool_errors(tools):
    assert "error" in tools.call("get_product", {})
    assert "error" in tools.call("nonexistent_tool", {})


def test_schemas_match_the_dispatch_table(tools):
    names = {schema["function"]["name"] for schema in tools.schemas()}
    assert names == set(tools.handlers)
