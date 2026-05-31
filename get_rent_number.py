"""HeroSMS getRentNumber 租号接口调试脚本。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

from get_active_activations import print_active_activations
from get_number_v2 import parse_response_payload, print_response_payload
from get_service_coverage import BASE_URL, configure_stdout

load_dotenv()

API_KEY = os.getenv("HEROSMS_API_KEY")
MAX_RENT_DURATION_HOURS = 168


def parse_duration_hours(value: str | int) -> int:
    text = str(value).strip().lower().replace("×", "x").replace("*", "x")
    if not text:
        raise ValueError("duration 不能为空")
    parts = [part.strip() for part in text.split("x")]
    if any(not part.isdigit() for part in parts):
        raise ValueError("duration 只支持正整数小时或 x 公式，例如 24、24x2、24x7")
    duration = 1
    for part in parts:
        number = int(part)
        if number <= 0:
            raise ValueError("duration 必须大于 0")
        duration *= number
    if duration > MAX_RENT_DURATION_HOURS:
        raise ValueError(f"duration 最大为 7 天，即 {MAX_RENT_DURATION_HOURS} 小时")
    return duration


def parse_duration_levels(value: str | int | tuple[int, ...] | list[int]) -> tuple[int, ...]:
    if isinstance(value, (tuple, list)):
        levels = tuple(parse_duration_hours(item) for item in value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("duration 不能为空")
        parts = [part.strip() for part in re.split(r"[、,，;；]+", text) if part.strip()]
        levels = tuple(parse_duration_hours(part) for part in parts)
    if not levels:
        raise ValueError("duration 至少需要 1 个时长档位")
    return levels


def parse_duration_arg(value: str) -> tuple[int, ...]:
    try:
        return parse_duration_levels(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="请求 HeroSMS getRentNumber 租号接口")
    parser.add_argument("-s", "--service", default="dr", help="服务代码，默认 dr")
    parser.add_argument("--country", type=int, default=16, help="国家 ID，默认 16")
    parser.add_argument(
        "--duration",
        type=parse_duration_arg,
        default=(2,),
        help="租号时长小时数，支持 2、4、24x2 或 24*2 多档，单档最大 168",
    )
    parser.add_argument("--operator", default="any", help="商家/运营商代码，默认 any")
    parser.add_argument("--cost", default=None, help="可选价格参数；提供后会原样传给接口")
    parser.add_argument("--currency", default=None, help="可选币种参数；提供后会原样传给接口")
    parser.add_argument("--ref", default=None, help="可选 ref 参数；提供后会原样传给接口")
    parser.add_argument("--send", action="store_true", help="真实发送租号请求；默认 dry-run")
    return parser.parse_args(argv)


def build_rent_number_params(
    service: str = "dr",
    country: int = 16,
    duration: int = 2,
    operator: str = "any",
    cost: str | float | None = None,
    currency: str | int | None = None,
    ref: str | None = None,
) -> dict:
    # duration 是 getRentNumber 的租号时长参数，不属于普通 getNumberV2 申请号码接口。
    params = {
        "action": "getRentNumber",
        "service": service,
        "country": int(country),
        "duration": parse_duration_hours(duration),
    }
    if str(operator or "").strip():
        params["operator"] = str(operator).strip()
    if cost not in (None, ""):
        params["cost"] = cost
    if currency not in (None, ""):
        params["currency"] = currency
    if str(ref or "").strip():
        params["ref"] = str(ref).strip()
    return params


def perform_request(params: dict) -> requests.Response:
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        raise RuntimeError("请先在 .env 文件中设置 HEROSMS_API_KEY")
    query = {
        "api_key": API_KEY,
        **params,
    }
    return requests.get(BASE_URL, params=query, timeout=30)


def get_active_activations_snapshot(limit: int = 5) -> list[dict]:
    response = perform_request({"action": "getActiveActivations", "start": 0, "limit": min(limit, 100)})
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def request_rent_number(params: dict):
    response = perform_request(params)
    payload = parse_response_payload(response)
    print(f"[租号HTTP状态] {response.status_code}")
    print_response_payload(payload)
    response.raise_for_status()
    return payload


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    args = parse_args(argv)
    request_params_list = [
        build_rent_number_params(
            service=args.service,
            country=args.country,
            duration=duration,
            operator=args.operator,
            cost=args.cost,
            currency=args.currency,
            ref=args.ref,
        )
        for duration in args.duration
    ]

    print("[租号接口说明]")
    print("getRentNumber 用于申请租赁号码；duration 是租号时长，不用于普通 getNumberV2。")
    print(f"[租号时长档位] {list(args.duration)}")
    for index, request_params in enumerate(request_params_list, start=1):
        print(f"[租号请求体预览] #{index}/{len(request_params_list)} duration={request_params['duration']}")
        print(json.dumps(request_params, ensure_ascii=False, indent=2))

    if not args.send:
        print("[租号模式] dry-run，未实际发送 getRentNumber 请求；加 --send 才会真实租号")
        return 0

    last_error = None
    for index, request_params in enumerate(request_params_list, start=1):
        print(f"[租号分段] #{index}/{len(request_params_list)} 尝试 duration={request_params['duration']}")
        try:
            print("[租号发送请求]")
            request_rent_number(request_params)
            print("[租号活动列表快照]")
            print_active_activations(get_active_activations_snapshot(limit=5))
            return 0
        except RuntimeError as error:
            last_error = error
            print(f"[租号错误] {error}")
            break
        except requests.RequestException as error:
            last_error = error
            print(f"[租号请求失败] duration={request_params['duration']} error={error}")
            continue

    if last_error is not None:
        print(f"[租号失败] 所有时长档位均未申请成功，最后错误：{last_error}")
    else:
        print("[租号失败] 所有时长档位均未申请成功")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
