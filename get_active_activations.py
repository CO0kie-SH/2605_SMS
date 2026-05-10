import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("HEROSMS_API_KEY")
BASE_URL = os.getenv(
    "HEROSMS_BASE_URL",
    "https://hero-sms.com/stubs/handler_api.php",
)


def configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


def parse_args():
    parser = argparse.ArgumentParser(description="获取 HeroSMS 活动激活列表")
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="偏移量，默认 0",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="请求数量，文档标注最多 100，默认 100",
    )
    parser.add_argument(
        "--set-status-id",
        default=None,
        help="更改指定 activationId 的激活状态；提供该参数才会发送 setStatus 请求",
    )
    parser.add_argument(
        "--status",
        type=int,
        default=8,
        help="setStatus 请求码，默认 8：取消激活并退款",
    )
    parser.add_argument(
        "--no-list",
        action="store_true",
        help="发送 setStatus 后不再查询活动激活列表",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出接口原始 JSON",
    )
    return parser.parse_args()


def build_params(start: int | None = 0, limit: int | None = 100) -> dict:
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        raise RuntimeError("请先在 .env 文件中设置 HEROSMS_API_KEY")

    params = {
        "api_key": API_KEY,
        "action": "getActiveActivations",
    }
    if start is not None:
        params["start"] = start
    if limit is not None:
        if limit > 100:
            print(f"[提示] 文档标注 limit 最多 100，已从 {limit} 调整为 100")
            limit = 100
        params["limit"] = limit
    return params




def summarize_set_status(status: int | str) -> str:
    mapping = {
        "3": "请求重新发送短信",
        "6": "完成激活",
        "8": "取消激活/退款",
    }
    return mapping.get(str(status), f"未知({status})")


def build_set_status_params(activation_id: str, status: int = 8) -> dict:
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        raise RuntimeError("请先在 .env 文件中设置 HEROSMS_API_KEY")
    if not str(activation_id).strip():
        raise ValueError("activation_id 不能为空")

    return {
        "api_key": API_KEY,
        "action": "setStatus",
        "id": str(activation_id).strip(),
        "status": int(status),
    }


def parse_response_payload(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


def set_activation_status(activation_id: str, status: int = 8):
    params = build_set_status_params(activation_id=activation_id, status=status)
    request_url = build_request_url(params)
    print(f"[更改激活状态请求URL] {mask_api_key_in_url(request_url)}")
    print(f"[更改激活状态请求] id={activation_id} status={status}({summarize_set_status(status)})")

    try:
        response = requests.get(BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = parse_response_payload(response)
        print("[更改激活状态响应]")
        if isinstance(payload, (dict, list)):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload)
        return payload
    except requests.RequestException as error:
        print(f"[请求失败] {error}")
        sys.exit(1)

def get_active_activations(start: int | None = 0, limit: int | None = 100):
    params = build_params(start=start, limit=limit)
    request_url = build_request_url(params)
    print(f"[活动激活请求URL] {mask_api_key_in_url(request_url)}")

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


def extract_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def activation_sort_key(item: dict) -> tuple[str, int]:
    activation_time = str(item.get("activationTime", "")).strip()
    try:
        activation_id = int(str(item.get("activationId", "0")).strip() or 0)
    except ValueError:
        activation_id = 0
    return activation_time, activation_id


def summarize_activation_status(status: str) -> str:
    mapping = {
        "1": "准备接收",
        "3": "等待重发",
        "4": "等待短信",
        "6": "完成",
        "8": "取消/退款",
    }
    return mapping.get(str(status), f"未知({status})")


def print_active_activations(records: list[dict]) -> None:
    sorted_records = sorted(records, key=activation_sort_key, reverse=True)

    print("[接口说明]")
    print("getActiveActivations 用于获取当前账号仍处于活动状态的激活列表。")
    print("文档参数包含：start、limit；start 是偏移量，limit 最多 100。")
    print()
    print(f"[活动激活总数] {len(records)}")

    if not sorted_records:
        print("[活动激活列表] 当前没有活动激活")
        return

    print("[活动激活列表]")
    for index, item in enumerate(sorted_records, start=1):
        activation_id = str(item.get("activationId", "")).strip()
        service_code = str(item.get("serviceCode", "")).strip()
        phone_number = str(item.get("phoneNumber", "")).strip()
        cost = item.get("activationCost")
        status = str(item.get("activationStatus", "")).strip()
        sms_code = item.get("smsCode")
        sms_text = item.get("smsText")
        activation_time = str(item.get("activationTime", "")).strip()
        country_code = str(item.get("countryCode", "")).strip()
        country_name = str(item.get("countryName", "")).strip()
        can_get_another_sms = item.get("canGetAnotherSms")
        currency = item.get("currency")

        print(
            f"{index:>3}. id={activation_id} "
            f"activationTime={activation_time} "
            f"service={service_code} "
            f"phone={phone_number} "
            f"cost={cost} "
            f"status={status}({summarize_activation_status(status)}) "
            f"country={country_code}/{country_name} "
            f"canGetAnotherSms={can_get_another_sms} "
            f"currency={currency}"
        )
        if sms_code:
            print(f"     smsCode={sms_code}")
        if sms_text:
            print(f"     smsText={sms_text}")


def main():
    configure_stdout()
    args = parse_args()

    try:
        if args.set_status_id:
            set_activation_status(activation_id=args.set_status_id, status=args.status)
            if args.no_list:
                return

        payload = get_active_activations(start=args.start, limit=args.limit)
    except (RuntimeError, ValueError) as error:
        print(f"[错误] {error}")
        sys.exit(1)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
        print(f"[接口状态] {payload.get('status')}")

    records = extract_records(payload)
    if not records and not (isinstance(payload, dict) and isinstance(payload.get("data"), list)):
        print(f"[异常响应] {payload}")
        sys.exit(1)

    print_active_activations(records)

if __name__ == "__main__":
    main()