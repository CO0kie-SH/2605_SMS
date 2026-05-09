import argparse
import json
import os
import random
import sys
import time

import requests
from dotenv import load_dotenv

from get_prices import build_get_number_v2_candidates, resolve_max_price
from get_service_coverage import BASE_URL, configure_stdout

load_dotenv()

API_KEY = os.getenv("HEROSMS_API_KEY")


def parse_args():
    parser = argparse.ArgumentParser(description="随机选择一个 getNumberV2 候选请求")
    parser.add_argument(
        "-s",
        "--service",
        default="dr",
        help="服务代码，默认 dr",
    )
    parser.add_argument(
        "--max-price",
        default=None,
        help="最高价格；默认读取 .env 中的 HEROSMS_MAX_PRICE",
    )
    parser.add_argument(
        "--visible-only",
        action="store_true",
        help="只从 visible=1 的国家中选择",
    )
    parser.add_argument(
        "--include-no-stock",
        action="store_true",
        help="允许选择 count=0 的候选；默认只选有库存的",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子，便于复现抽样结果",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="实际发送 getNumberV2 请求；默认只打印请求体",
    )
    return parser.parse_args()


def pick_random_candidate(candidates: list[dict], seed: int | None = None) -> dict:
    rng = random.Random(seed)
    return rng.choice(candidates)


def pick_random_candidate_with_rng(candidates: list[dict], rng: random.Random) -> dict:
    return rng.choice(candidates)


def build_request_params(candidate: dict) -> dict:
    params = {
        "action": "getNumberV2",
        "service": candidate["service"],
        "country": candidate["country"],
    }
    if candidate.get("operator"):
        params["operator"] = candidate["operator"]
    if candidate.get("maxPrice") is not None:
        params["maxPrice"] = candidate["maxPrice"]
    return params


def perform_request(params: dict) -> requests.Response:
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        raise RuntimeError("请先在 .env 文件中设置 HEROSMS_API_KEY")

    query = {
        "api_key": API_KEY,
        **params,
    }
    return requests.get(BASE_URL, params=query, timeout=30)


def fetch_balance_text() -> str:
    response = perform_request({"action": "getBalance"})
    response.raise_for_status()
    return response.text.strip()


def parse_balance_value(balance_text: str) -> float | None:
    prefix = "ACCESS_BALANCE:"
    if not balance_text.startswith(prefix):
        return None
    try:
        return float(balance_text[len(prefix):].strip())
    except ValueError:
        return None


def print_balance(label: str) -> tuple[str | None, float | None]:
    try:
        balance_text = fetch_balance_text()
        print(f"[余额] {label}: {balance_text}")
        return balance_text, parse_balance_value(balance_text)
    except requests.RequestException as error:
        print(f"[余额] {label}: 查询失败 {error}")
        return None, None


def print_balance_series(
    before_balance_value: float | None,
    max_price: float | None,
    times: int = 5,
    interval_seconds: int = 2,
) -> None:
    detected_drop = False
    for index in range(times):
        _, current_balance_value = print_balance(f"after #{index + 1}")
        if (
            before_balance_value is not None
            and current_balance_value is not None
            and current_balance_value < before_balance_value
        ):
            diff = round(before_balance_value - current_balance_value, 10)
            compare_text = (
                f", HEROSMS_MAX_PRICE={max_price}, diff<=maxPrice={diff <= max_price}"
                if max_price is not None
                else ""
            )
            print(f"[余额变化] diff={diff}{compare_text}")
            detected_drop = True
        if index < times - 1:
            time.sleep(interval_seconds)
    if not detected_drop:
        print("[余额变化] 在这 5 次轮询内，未检测到余额低于请求前余额")


def request_number(params: dict) -> dict:
    response = perform_request(params)
    response.raise_for_status()
    return response.json()


def main():
    configure_stdout()
    args = parse_args()

    try:
        max_price = resolve_max_price(args.max_price)
    except ValueError as error:
        print(f"[错误] {error}")
        sys.exit(1)

    candidates = build_get_number_v2_candidates(
        service=args.service,
        max_price=max_price,
        in_stock_only=not args.include_no_stock,
        visible_only=args.visible_only,
    )
    if not candidates:
        print("[结果] 当前过滤条件下没有可用候选")
        sys.exit(1)

    rng = random.Random(args.seed)
    candidate = pick_random_candidate_with_rng(candidates, rng)
    request_params = build_request_params(candidate)

    print(f"[候选总数] {len(candidates)}")
    if args.seed is not None:
        print(f"[随机种子] {args.seed}")
    print("[随机选中]")
    print(
        f"service={candidate['service']} "
        f"country={candidate['country']} "
        f"operator={candidate['operator'] or '-'} "
        f"maxPrice={candidate['maxPrice']} "
        f"price={candidate['price']:.4f} "
        f"count={candidate['count'] if candidate['count'] is not None else '-'} "
        f"name={candidate['countryName']}"
    )
    print("[请求体预览]")
    print(json.dumps(request_params, ensure_ascii=False, indent=2))

    if not args.send:
        print("[模式] dry-run，未实际发送 getNumberV2 请求")
        return

    remaining_candidates = candidates[:]
    while remaining_candidates:
        if candidate and any(
            item["service"] == candidate["service"]
            and item["country"] == candidate["country"]
            and item["operator"] == candidate["operator"]
            for item in remaining_candidates
        ):
            current_candidate = candidate
        else:
            current_candidate = pick_random_candidate_with_rng(remaining_candidates, rng)
        request_params = build_request_params(current_candidate)

        print("[发送请求]")
        print(json.dumps(request_params, ensure_ascii=False, indent=2))
        # TODO: 后续加入 404 operator 黑名单，避免重复命中已知无效服务商。
        _, before_balance_value = print_balance("before")

        try:
            response = request_number(request_params)
            print_balance_series(
                before_balance_value=before_balance_value,
                max_price=max_price,
                times=5,
                interval_seconds=2,
            )
            print("[接口响应]")
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return
        except RuntimeError as error:
            print(f"[错误] {error}")
            sys.exit(1)
        except requests.HTTPError as error:
            print_balance_series(
                before_balance_value=before_balance_value,
                max_price=max_price,
                times=5,
                interval_seconds=2,
            )
            status_code = error.response.status_code if error.response is not None else "unknown"
            if status_code == 404:
                print(
                    f"[err] operator={current_candidate['operator'] or '-'} "
                    f"country={current_candidate['country']} "
                    f"service={current_candidate['service']} status=404"
                )
                remaining_candidates = [
                    item for item in remaining_candidates
                    if not (
                        item["service"] == current_candidate["service"]
                        and item["country"] == current_candidate["country"]
                        and item["operator"] == current_candidate["operator"]
                    )
                ]
                candidate = None
                if remaining_candidates:
                    print(f"[重试] 剩余候选数: {len(remaining_candidates)}，重新随机抽取")
                    continue
                print("[结果] 所有候选都已尝试，但都返回 404")
                sys.exit(1)
            print(f"[请求失败] {error}")
            sys.exit(1)
        except requests.RequestException as error:
            print_balance_series(
                before_balance_value=before_balance_value,
                max_price=max_price,
                times=5,
                interval_seconds=2,
            )
            print(f"[请求失败] {error}")
            sys.exit(1)


if __name__ == "__main__":
    main()
