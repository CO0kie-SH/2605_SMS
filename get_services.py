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


def get_services(country: str = "", lang: str = ""):
    """调用 HeroSMS API 获取服务清单。"""
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        print("[错误] 请先在 .env 文件中设置 HEROSMS_API_KEY")
        sys.exit(1)

    params = {
        "api_key": API_KEY,
        "action": "getServicesList",
    }
    if country:
        params["country"] = country
    if lang:
        params["lang"] = lang

    try:
        resp = requests.get(BASE_URL, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[请求失败] {e}")
        sys.exit(1)
    except ValueError:
        print("[错误] 接口返回的不是合法 JSON")
        sys.exit(1)


def normalize_services(payload) -> list[dict]:
    """兼容接口返回结构，抽取服务列表。"""
    if isinstance(payload, dict):
        services = payload.get("services", [])
    else:
        services = payload

    if not isinstance(services, list):
        return []

    normalized = []
    for item in services:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if code and name:
            normalized.append({"code": code, "name": name})
    return normalized


def filter_services(services: list[dict], keyword: str) -> list[dict]:
    if not keyword:
        return services

    needle = keyword.strip().lower()
    return [
        item
        for item in services
        if needle in item["code"].lower() or needle in item["name"].lower()
    ]


def parse_args():
    parser = argparse.ArgumentParser(description="获取 HeroSMS 服务清单")
    parser.add_argument(
        "-k",
        "--keyword",
        default="",
        help="按服务 code 或 name 过滤，例如 OpenAI / dr",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=50,
        help="最多显示多少条，默认 50",
    )
    parser.add_argument(
        "--country",
        default="",
        help="可选：传给接口的 country 参数",
    )
    parser.add_argument(
        "--lang",
        default="",
        help="可选：传给接口的 lang 参数",
    )
    return parser.parse_args()


def main():
    configure_stdout()
    args = parse_args()

    payload = get_services(country=args.country, lang=args.lang)
    services = normalize_services(payload)

    if not services:
        print(f"[异常响应] {payload}")
        sys.exit(1)

    filtered = filter_services(services, args.keyword)

    print(f"[服务总数] {len(services)}")
    if args.keyword:
        print(f"[过滤关键字] {args.keyword}")
        print(f"[匹配数量] {len(filtered)}")

    if not filtered:
        print("[结果] 没有匹配到服务")
        return

    limit = max(args.limit, 0)
    shown = filtered[:limit] if limit else filtered

    print("[服务列表]")
    for index, item in enumerate(shown, start=1):
        print(f"{index:>3}. code={item['code']:<8} name={item['name']}")

    if limit and len(filtered) > limit:
        print(f"[提示] 仅显示前 {limit} 条，可用 --limit 查看更多")


if __name__ == "__main__":
    main()
