from datetime import date

from emporio.data import normalize, phone_digits


def test_loads_every_table(store):
    assert len(store.products) == 65
    assert len(store.customers) == 50
    assert len(store.orders) == 20
    assert len(store.promotions) == 25
    assert store.categories[5] == "Violões"


def test_product_fields_are_typed(store):
    takamine = store.products[95]
    assert takamine.name == "Takamine GD20 Dreadnought Natural"
    assert takamine.price == 2199.0
    assert takamine.category == "Violões"
    assert takamine.stock == 5
    assert takamine.specs["strings"] == "steel"
    assert takamine.description_conflict is None
    assert takamine.description.startswith("Violão dreadnought Takamine")


def test_descriptions_that_contradict_the_product_are_withheld(store):
    drum = store.products[139]  # "Yamaha Kit 1" described as a Pearl Export
    assert drum.description is None
    assert "pearl" in drum.description_conflict

    synth = store.products[142]  # "Korg Synth 1" described as an 88-key Roland, specs say 61
    assert synth.description is None
    assert "roland" in synth.description_conflict and "88" in synth.description_conflict

    for consistent in (131, 133, 138, 141, 145):  # Gibson, PRS, Music Man, Yamaha, Fender
        assert store.products[consistent].description_conflict is None, consistent


def test_orders_are_parsed(store):
    order = store.orders[15]
    assert order.order_date == date(2026, 3, 18)
    assert order.installments == 12
    assert [item.product_id for item in order.items] == [82, 106]
    assert store.orders[8].tracking_code == "BRJL5544332BR"
    assert store.orders[8].estimated_delivery == date(2026, 2, 28)
    assert store.orders[9].tracking_code is None
    assert store.orders[1].installments is None


def test_only_active_promotions_count(store):
    assert store.active_promotion(127).discount_percent == 10
    assert store.active_promotion(102) is None  # Black Friday row, inactive
    assert sorted(p.product_id for p in store.active_promotions()) == [90, 94, 121, 127]


def test_contact_lookup_is_normalized(store):
    assert len(store.customers_with_email(" Bruno.Martins@jmail.com ")) == 2
    assert store.customers[11].phone_digits == "67985432109"
    assert store.customers[11].first_name == "Bruno"


def test_normalize_and_phone_digits():
    assert normalize("  Violão   Elétrico ") == "violao eletrico"
    assert phone_digits("(67) 98543-2109") == "67985432109"
