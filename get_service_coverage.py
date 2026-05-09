import argparse
import os
import sys

import requests
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

API_KEY = os.getenv("HEROSMS_API_KEY")
BASE_URL = os.getenv(
    "HEROSMS_BASE_URL",
    "https://hero-sms.com/stubs/handler_api.php",
)


def configure_stdout() -> None:
    """尽量避免 Windows 终端输出 Unicode 时出现编码错误。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def api_get(action: str, **params):
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        print("[错误] 请先在 .env 文件中设置 HEROSMS_API_KEY")
        sys.exit(1)

    query = {
        "api_key": API_KEY,
        "action": action,
        **{k: v for k, v in params.items() if v not in ("", None)},
    }

    try:
        resp = requests.get(BASE_URL, params=query, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[请求失败] action={action} error={e}")
        sys.exit(1)
    except ValueError:
        print(f"[错误] action={action} 返回的不是合法 JSON")
        sys.exit(1)


def load_countries() -> dict[int, dict]:
    payload = api_get("getCountries")
    if not isinstance(payload, list):
        return {}

    countries = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        country_id = item.get("id")
        if not isinstance(country_id, int):
            continue
        countries[country_id] = {
            "id": country_id,
            "name_cn": str(item.get("chn") or "").strip(),
            "name_en": str(item.get("eng") or "").strip(),
            "visible": int(item.get("visible") or 0),
        }
    return countries


def load_prices(service: str) -> dict[int, dict]:
    payload = api_get("getPrices", service=service)
    if not isinstance(payload, dict):
        return {}

    rows = {}
    for key, value in payload.items():
        try:
            country_id = int(key)
        except (TypeError, ValueError):
            continue

        if not isinstance(value, dict):
            continue
        service_node = value.get(service)
        if not isinstance(service_node, dict):
            continue

        try:
            price = float(service_node.get("cost"))
        except (TypeError, ValueError):
            price = None

        try:
            count = int(service_node.get("count"))
        except (TypeError, ValueError):
            count = None

        try:
            physical_count = int(service_node.get("physicalCount"))
        except (TypeError, ValueError):
            physical_count = None

        if price is None:
            continue

        rows[country_id] = {
            "price": price,
            "count": count,
            "physical_count": physical_count,
        }
    return rows


def load_operators(service: str) -> dict[int, list[str]]:
    payload = api_get("getOperators", service=service)
    if not isinstance(payload, dict):
        return {}

    raw = payload.get("countryOperators", {})
    if not isinstance(raw, dict):
        return {}

    rows = {}
    for key, value in raw.items():
        try:
            country_id = int(key)
        except (TypeError, ValueError):
            continue

        if not isinstance(value, list):
            continue

        operators = [str(item).strip() for item in value if str(item).strip()]
        rows[country_id] = operators
    return rows


def build_coverage(service: str) -> list[dict]:
    countries = load_countries()
    prices = load_prices(service)
    operators = load_operators(service)

    result = []
    for country_id, price_info in prices.items():
        country = countries.get(country_id, {})
        provider_list = operators.get(country_id, [])
        result.append(
            {
                "id": country_id,
                "name_cn": country.get("name_cn") or "",
                "name_en": country.get("name_en") or "",
                "visible": country.get("visible", 0),
                "price": price_info["price"],
                "count": price_info["count"],
                "physical_count": price_info["physical_count"],
                "operators": provider_list,
            }
        )

    result.sort(key=lambda item: (item["price"], -(item["count"] or 0), item["id"]))
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="查询某个 HeroSMS 服务支持的国家和运营商")
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
        "--operators-limit",
        type=int,
        default=8,
        help="每个国家最多显示多少个运营商，默认 8；传 0 表示全部显示",
    )
    parser.add_argument(
        "--all-operators",
        action="store_true",
        help="显示每个国家的全部运营商",
    )
    return parser.parse_args()


def format_operator_list(operators: list[str], operators_limit: int, all_operators: bool) -> str:
    if not operators:
        return "-"

    if all_operators or operators_limit == 0:
        return ", ".join(operators)

    shown = operators[:operators_limit]
    text = ", ".join(shown)
    if len(operators) > operators_limit:
        text += f" ... (+{len(operators) - operators_limit})"
    return text


def main():
    configure_stdout()
    args = parse_args()

    rows = build_coverage(args.service)
    if not rows:
        print(f"[结果] 没有查到 service={args.service} 的国家价格信息")
        sys.exit(1)

    print(f"[服务代码] {args.service}")
    print(f"[国家数量] {len(rows)}")
    print("[国家与运营商]")

    limit = max(args.limit, 0)
    shown_rows = rows if limit == 0 else rows[:limit]

    for index, item in enumerate(shown_rows, start=1):
        name = item["name_cn"] or item["name_en"] or f"Country {item['id']}"
        english = item["name_en"]
        visible = "yes" if item["visible"] else "no"
        operators_text = format_operator_list(
            item["operators"],
            args.operators_limit,
            args.all_operators,
        )
        print(
            f"{index:>3}. id={item['id']:<3} "
            f"name={name}"
            f"{f' ({english})' if english and english != name else ''} "
            f"price={item['price']:.4f} "
            f"count={item['count'] if item['count'] is not None else '-'} "
            f"physical={item['physical_count'] if item['physical_count'] is not None else '-'} "
            f"visible={visible}"
        )
        print(f"     operators[{len(item['operators'])}] = {operators_text}")

    if limit and len(rows) > limit:
        print(f"[提示] 仅显示前 {limit} 个国家，可用 --limit 0 查看全部")


if __name__ == "__main__":
    main()
