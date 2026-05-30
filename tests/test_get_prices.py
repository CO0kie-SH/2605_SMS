import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import get_prices


def test_build_candidates_excludes_country_4_for_dr(monkeypatch):
    monkeypatch.setattr(
        get_prices,
        "build_coverage",
        lambda service: [
            {
                "id": 4,
                "name_cn": "Country 4",
                "name_en": "",
                "visible": 1,
                "price": 0.01,
                "count": 10,
                "physical_count": 10,
                "operators": [],
            },
            {
                "id": 16,
                "name_cn": "Country 16",
                "name_en": "",
                "visible": 1,
                "price": 0.02,
                "count": 10,
                "physical_count": 10,
                "operators": [],
            },
        ],
    )

    candidates = get_prices.build_get_number_v2_candidates(service="dr")

    assert [item["country"] for item in candidates] == [16]


def test_build_candidates_keeps_country_4_for_other_services(monkeypatch):
    monkeypatch.setattr(
        get_prices,
        "build_coverage",
        lambda service: [
            {
                "id": 4,
                "name_cn": "Country 4",
                "name_en": "",
                "visible": 1,
                "price": 0.01,
                "count": 10,
                "physical_count": 10,
                "operators": [],
            },
        ],
    )

    candidates = get_prices.build_get_number_v2_candidates(service="xx")

    assert [item["country"] for item in candidates] == [4]
