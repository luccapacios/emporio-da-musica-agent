from emporio.quality import quality_report


def test_report_flags_the_known_inconsistencies(store):
    report = dict()
    for where, finding in quality_report(store):
        report.setdefault(where, []).append(finding)

    assert any("differs from items" in f for f in report["order 3"])
    assert any("coming_soon" in f for f in report["order 3"])
    assert any("differs from items" in f for f in report["order 20"])
    assert any("below the R$ 80" in f for f in report["order 9"])
    assert any("below the R$ 80" in f for f in report["order 16"])
    assert any("discontinued" in f for f in report["product 136"])
    assert any("zero stock" in f for f in report["product 96"])
    assert "customers 11, 43" in report
    assert {"category 6", "category 7", "category 8"} <= report.keys()
    assert "order 1" not in report
