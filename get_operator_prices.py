import argparse
import sys

from get_service_coverage import api_get, build_coverage, configure_stdout


def load_operator_prices(service: str, country_id: int, operators: list[str]) -> list[dict]:
    rows = []
    for operator in operators:
        payload = api_get("getPrices", service=service, country=country_id, operator=operator)
        if not isinstance(payload, dict):
            continue

        country_node = payload.get(str(country_id)) or payload.get(country_id)
        if not isinstance(country_node, dict):
            continue

        service_node = country_node.get(service)
        if not isinstance(service_node, dict):
            continue

        try:
            price = float(service_node.get("cost"))
        except (TypeError, ValueError):
            continue

        try:
            count = int(service_node.get("count"))
        except (TypeError, ValueError):
            count = None

        try:
            physical_count = int(service_node.get("physicalCount"))
        except (TypeError, ValueError):
            physical_count = None

        rows.append(
            {
                "operator": operator,
                "price": price,
                "count": count,
                "physical_count": physical_count,
            }
        )

    rows.sort(key=lambda item: (item["price"], -(item["count"] or 0), item["operator"]))
    return rows


def parse_args():
    parser = argparse.ArgumentParser(description="按价格排序查询某个 HeroSMS 服务的运营商价格")
    parser.add_argument(
        "-s",
        "--service",
        default="dr",
        help="服务代码，默认 dr",
    )
    parser.add_argument(
        "-n",
        "--countries-limit",
        type=int,
        default=5,
        help="最多查询多少个国家的运营商价格，默认 5",
    )
    parser.add_argument(
        "--operators-limit",
        type=int,
        default=10,
        help="每个国家最多显示多少个运营商，默认 10；传 0 表示全部显示",
    )
    parser.add_argument(
        "--in-stock-only",
        action="store_true",
        help="只看国家库存 count 大于 0 的国家",
    )
    parser.add_argument(
        "--country-id",
        type=int,
        default=0,
        help="只查询指定国家 ID 的运营商价格",
    )
    return parser.parse_args()


def main():
    configure_stdout()
    args = parse_args()

    countries = build_coverage(args.service)
    if args.in_stock_only:
        countries = [row for row in countries if (row["count"] or 0) > 0]

    if args.country_id:
        countries = [row for row in countries if row["id"] == args.country_id]
    else:
        countries = [row for row in countries if row["operators"]]

    if not countries:
        print(f"[结果] 没有查到 service={args.service} 的可用国家/运营商信息")
        sys.exit(1)

    countries_limit = max(args.countries_limit, 0)
    selected_countries = countries if countries_limit == 0 else countries[:countries_limit]

    print(f"[服务代码] {args.service}")
    print(f"[查询国家数] {len(selected_countries)}")
    print("[运营商价格]")

    for country in selected_countries:
        name = country["name_cn"] or country["name_en"] or f"Country {country['id']}"
        english = country["name_en"]
        print(
            f"国家 id={country['id']} name={name}"
            f"{f' ({english})' if english and english != name else ''} "
            f"country_price={country['price']:.4f} country_count={country['count'] if country['count'] is not None else '-'}"
        )

        operator_rows = load_operator_prices(args.service, country["id"], country["operators"])
        if not operator_rows:
            print("  [结果] 该国家没有解析到运营商价格")
            continue

        operators_limit = max(args.operators_limit, 0)
        shown_rows = operator_rows if operators_limit == 0 else operator_rows[:operators_limit]
        for index, item in enumerate(shown_rows, start=1):
            print(
                f"  {index:>2}. operator={item['operator']:<20} "
                f"price={item['price']:.4f} "
                f"count={item['count'] if item['count'] is not None else '-'} "
                f"physical={item['physical_count'] if item['physical_count'] is not None else '-'}"
            )

        if operators_limit and len(operator_rows) > operators_limit:
            print(f"  [提示] 仅显示前 {operators_limit} 个运营商，剩余 {len(operator_rows) - operators_limit} 个")


if __name__ == "__main__":
    main()
