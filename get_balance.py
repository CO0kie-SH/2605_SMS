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


def get_balance() -> str:
    """调用 HeroSMS API 获取账户余额"""
    if not API_KEY or API_KEY == "YOUR_SECRET_TOKEN":
        print("[错误] 请先在 .env 文件中设置 HEROSMS_API_KEY")
        sys.exit(1)

    params = {
        "api_key": API_KEY,
        "action": "getBalance",
    }

    try:
        resp = requests.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        return resp.text.strip()
    except requests.RequestException as e:
        print(f"[请求失败] {e}")
        sys.exit(1)


def main():
    result = get_balance()
    print(f"[API 响应] {result}")

    if result.startswith("ACCESS_BALANCE:"):
        balance = result.replace("ACCESS_BALANCE:", "")
        print(f"[当前余额] {balance}")
    else:
        print(f"[异常响应] {result}")


if __name__ == "__main__":
    main()
