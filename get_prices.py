import argparse
import os
import sys

from get_service_coverage import build_coverage, configure_stdout
from get_operator_prices import load_operator_prices


def parse_max_price(value: str | None) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"无效的 max price: {value}")


def resolve_max_price(cli_value: str | None) -> float | None:
    if cli_value is not None:
        return parse_max_price(cli_value)
    return parse_max_price(os.getenv("HEROSMS_MAX_PRICE"))


def build_get_number_v2_candidates(
    service: str,
    max_price: float | None = None,
    in_stock_only: bool = True,
    visible_only: bool = False,
) -> list[dict]:
    rows = build_coverage(service)
    if in_stock_only:
        rows = [row for row in rows if (row["count"] or 0) > 0]
    if visible_only:
        rows = [row for row in rows if row["visible"]]
    if max_price is not None:
        rows = [row for row in rows if row["price"] <= max_price]

    candidates = []
    for row in rows:
        country_id = row["id"]
        country_name = row["name_cn"] or row["name_en"] or f"Country {country_id}"
        operators = row["operators"]

        if operators:
            operator_rows = load_operator_prices(service, country_id, operators)
            if in_stock_only:
                operator_rows = [item for item in operator_rows if (item["count"] or 0) > 0]
            if max_price is not None:
                operator_rows = [item for item in operator_rows if item["price"] <= max_price]

            for item in operator_rows:
                candidates.append(
                    {
                        "service": service,
                        "country": country_id,
                        "operator": item["operator"],
                        "maxPrice": max_price,
                        "price": item["price"],
                        "count": item["count"],
                        "physicalCount": item["physical_count"],
                        "countryName": country_name,
                    }
                )
            continue

        candidates.append(
            {
                "service": service,
                "country": country_id,
                "operator": "",
                "maxPrice": max_price,
                "price": row["price"],
                "count": row["count"],
                "physicalCount": row["physical_count"],
                "countryName": country_name,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["price"],
            -(item["count"] or 0),
            item["country"],
            item["operator"],
        )
    )
    return candidates


def parse_args():
    parser = argparse.ArgumentParser(description="按价格排序查询 HeroSMS 各国当前价格和数量")
    parser.add_argument(
        "-s",
        "--service",
        default="dr",
        help="服务代码，默认 dr",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=30,
        help="最多显示多少个国家，默认 30；传 0 表示全部显示",
    )
    parser.add_argument(
        "--in-stock-only",
        action="store_true",
        help="只显示 count 大于 0 的国家",
    )
    parser.add_argument(
        "--visible-only",
        action="store_true",
        help="只显示 visible=1 的国家",
    )
    parser.add_argument(
        "--max-price",
        default=None,
        help="最高价格；默认读取 .env 中的 HEROSMS_MAX_PRICE",
    )
    parser.add_argument(
        "--show-candidates",
        action="store_true",
        help="输出可直接用于 getNumberV2 的候选参数列表",
    )
    return parser.parse_args()


def main():
    configure_stdout()
    args = parse_args()
    try:
        max_price = resolve_max_price(args.max_price)
    except ValueError as error:
        print(f"[错误] {error}")
        sys.exit(1)

    rows = build_coverage(args.service)
    if args.in_stock_only:
        rows = [row for row in rows if (row["count"] or 0) > 0]
    if args.visible_only:
        rows = [row for row in rows if row["visible"]]
    if max_price is not None:
        rows = [row for row in rows if row["price"] <= max_price]

    if not rows:
        print(f"[结果] 没有查到 service={args.service} 的国家价格信息")
        sys.exit(1)

    limit = max(args.limit, 0)
    shown_rows = rows if limit == 0 else rows[:limit]

    print(f"[服务代码] {args.service}")
    if max_price is not None:
        print(f"[最高价格] {max_price}")
    print(f"[国家数量] {len(rows)}")
    print("[按价格升序]")

    for index, item in enumerate(shown_rows, start=1):
        name = item["name_cn"] or item["name_en"] or f"Country {item['id']}"
        english = item["name_en"]
        visible = "yes" if item["visible"] else "no"
        print(
            f"{index:>3}. id={item['id']:<3} "
            f"name={name}"
            f"{f' ({english})' if english and english != name else ''} "
            f"price={item['price']:.4f} "
            f"count={item['count'] if item['count'] is not None else '-'} "
            f"physical={item['physical_count'] if item['physical_count'] is not None else '-'} "
            f"operators={len(item['operators'])} "
            f"visible={visible}"
        )

    if limit and len(rows) > limit:
        print(f"[提示] 仅显示前 {limit} 个国家，可用 --limit 0 查看全部")

    if args.show_candidates:
        candidates = build_get_number_v2_candidates(
            service=args.service,
            max_price=max_price,
            in_stock_only=args.in_stock_only,
            visible_only=args.visible_only,
        )
        shown_candidates = candidates if limit == 0 else candidates[:limit]
        print("[候选参数列表]")
        for index, item in enumerate(shown_candidates, start=1):
            print(
                f"{index:>3}. "
                f"service={item['service']} "
                f"country={item['country']} "
                f"operator={item['operator'] or '-'} "
                f"maxPrice={item['maxPrice']} "
                f"price={item['price']:.4f} "
                f"count={item['count'] if item['count'] is not None else '-'} "
                f"name={item['countryName']}"
            )

        if limit and len(candidates) > limit:
            print(f"[提示] 候选参数仅显示前 {limit} 条，可用 --limit 0 查看全部")


if __name__ == "__main__":
    main()
