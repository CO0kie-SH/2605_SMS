import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

API_KEY = os.getenv("HEROSMS_API_KEY")
BASE_URL = os.getenv(
    "HEROSMS_BASE_URL",
    "https://hero-sms.com/stubs/handler_api.php",
)




HISTORY_LOOKBACK_DAYS = 14


def mask_api_key_in_url(url: str) -> str:
    if not API_KEY:
        return url
    if len(API_KEY) <= 8:
        masked_key = "***"
    else:
        masked_key = f"{API_KEY[:4]}...{API_KEY[-4:]}"
    return url.replace(API_KEY, masked_key)


def build_request_url(params: dict) -> str:
    request = requests.Request("GET", BASE_URL, params=params).prepare()
    return request.url or BASE_URL





def parse_time_offset(value: str | None) -> int:
    if value is None:
        return 0
    raw = str(value).strip().lower()
    if not raw or raw == "0":
        return 0

    sign = 1
    if raw[0] in "+-":
        if raw[0] == "-":
            sign = -1
        raw = raw[1:]

    units = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
    }
    unit = raw[-1]
    if unit in units:
        number_text = raw[:-1]
        multiplier = units[unit]
    else:
        number_text = raw
        multiplier = 1

    try:
        amount = float(number_text)
    except ValueError:
        raise ValueError(f"invalid time offset: {value}")
    return int(sign * amount * multiplier)

def resolve_history_time_range(start=None, end=None, time_offset_seconds: int = 0) -> tuple[int, int]:
    resolved_end = (int(time.time()) if end is None else int(end)) + time_offset_seconds
    resolved_start = (
        resolved_end - HISTORY_LOOKBACK_DAYS * 24 * 60 * 60
        if start is None
        else int(start) + time_offset_seconds
    )
    return resolved_start, resolved_end

def configure_stdout() -> None:
    """尽量避免 Windows 终端输出 Unicode 时出现编码错误。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_args():
    parser = argparse.ArgumentParser(description="获取 HeroSMS 激活历史记录")
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        help="最多显示多少条，默认 20；传 0 表示全部显示",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="可选：历史查询开始时间/位置参数，按 HeroSMS 文档传给 start",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="可选：历史查询结束时间/位置参数，按 HeroSMS 文档传给 end",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="可选：分页偏移量，按 HeroSMS 文档传给 offset",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=None,
        help="可选：分页数量，按 HeroSMS 文档传给 size",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出接口原始 JSON",
    )
    parser.add_argument(
        "--time-offset",
        default=None,
        help="自动生成 start/end 时的 Unix 时间偏移量，支持 5m、-5m、5h、30s、1d",
    )
    parser.add_argument(
        "--no-time-range",
        action="store_true",
        help="不自动添加 start/end 时间范围参数，用于按接口默认行为请求历史",
    )
    return parser.parse_args()


def get_history(start=None, end=None, offset=None, size=None, use_time_range: bool = True, time_offset: str | None = None):
    """调用 HeroSMS getHistory 获取激活历史记录。"""
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        print("[错误] 请先在 .env 文件中设置 HEROSMS_API_KEY")
        sys.exit(1)
    if use_time_range:
        time_offset_seconds = parse_time_offset(time_offset)
        start, end = resolve_history_time_range(
            start=start,
            end=end,
            time_offset_seconds=time_offset_seconds,
        )

    params = {
        "api_key": API_KEY,
        "action": "getHistory",
    }
    optional_params = {
        "start": start,
        "end": end,
        "offset": offset,
        "size": size,
    }
    params.update({key: value for key, value in optional_params.items() if value is not None})

    request_url = build_request_url(params)
    print(f"[历史记录请求URL] {mask_api_key_in_url(request_url)}")

    try:
        response = requests.get(BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        print(f"[请求失败] {error}")
        sys.exit(1)
    except ValueError:
        print("[错误] 接口返回的不是合法 JSON")
        sys.exit(1)


def summarize_status(status: str) -> str:
    mapping = {
        "1": "准备接收",
        "3": "等待重发",
        "6": "完成",
        "8": "取消/退款",
    }
    return mapping.get(str(status), f"未知({status})")




def history_sort_key(item: dict) -> tuple[str, int]:
    date_text = str(item.get("date", "")).strip()
    try:
        record_id = int(str(item.get("id", "0")).strip() or 0)
    except ValueError:
        record_id = 0
    return date_text, record_id

def print_history(records: list[dict], limit: int) -> None:
    sorted_records = sorted(records, key=history_sort_key, reverse=True)
    shown = sorted_records if limit == 0 else sorted_records[:limit]

    print("[接口说明]")
    print("getHistory 用于获取当前账号的激活历史记录。")
    print("文档参数包含：start、end、offset、size，均为可选查询参数。")
    print("每条记录通常包含：激活ID、时间、手机号、短信内容、费用、状态、货币。")
    print()
    print(f"[总记录数] {len(records)}")
    print("[历史记录]")

    for index, item in enumerate(shown, start=1):
        record_id = str(item.get("id", "")).strip()
        date = str(item.get("date", "")).strip()
        phone = str(item.get("phone", "")).strip()
        sms = item.get("sms")
        cost = item.get("cost")
        status = str(item.get("status", "")).strip()
        currency = item.get("currency")

        print(
            f"{index:>3}. id={record_id} "
            f"date={date} "
            f"phone={phone} "
            f"cost={cost} "
            f"status={status}({summarize_status(status)}) "
            f"currency={currency}"
        )
        if sms:
            print(f"     sms={sms}")

    if limit and len(records) > limit:
        print(f"[提示] 仅显示前 {limit} 条，可用 --limit 0 查看全部")


def main():
    configure_stdout()
    args = parse_args()

    records = get_history(
        start=args.start,
        end=args.end,
        offset=args.offset,
        size=args.size,
        use_time_range=not args.no_time_range,
        time_offset=args.time_offset,
    )
    if not isinstance(records, list):
        print(f"[异常响应] {records}")
        sys.exit(1)

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return

    print_history(records, max(args.limit, 0))


if __name__ == "__main__":
    main()
