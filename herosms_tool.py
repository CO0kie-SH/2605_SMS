"""HeroSMS 合法调试工作流统一入口。

按类组织完整调试流程：
1. 读取环境变量与命令行配置，命令行优先。
2. 检查余额。
3. 单线程模式下检查活动激活列表为空。
4. 查询服务国家/运营商，生成商户候选并按价格排序。
5. 抽取商户并请求号码，非 200 自动换下一个候选重试。
6. 成功后轮询余额变化、确认号码进入活动列表。
7. 轮询活动列表、预留用户输入循环、最后查询历史。

默认安全策略：单线程模式默认开启；只有显式传入 --send 才会真实请求号码。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import select
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import Mapping, Sequence

import requests
from dotenv import load_dotenv

from get_active_activations import extract_records, print_active_activations, validate_records_payload
from get_history import print_history
from get_number_v2 import build_request_params, parse_response_payload, print_response_payload
from get_operator_prices import load_operator_prices
from get_prices import build_get_number_v2_candidates
from get_service_coverage import build_coverage

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"


def parse_balance_value(balance_text: str) -> float | None:
    prefix = "ACCESS_BALANCE:"
    if not balance_text.startswith(prefix):
        return None
    try:
        return float(balance_text[len(prefix):].strip())
    except ValueError:
        return None


def parse_float(value: str | float | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def parse_float_levels(value: str | float | None) -> tuple[float, ...]:
    if value is None:
        return ()
    if isinstance(value, (int, float)):
        return (float(value),)
    text = str(value).strip()
    if not text:
        return ()
    parts = [part.strip() for part in text.split("-") if part.strip()]
    return tuple(float(part) for part in parts)


class UserInputExit(Exception):
    """用户输入 99 时主动退出当前流程。"""


@dataclass(frozen=True)
class UserInputState:
    mode: int | None = None
    records: list[dict] | None = None


@dataclass(frozen=True)
class NumberRequestResult:
    payload: object | None = None
    phone: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class WorkflowConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    max_price: float | None = None
    max_price_levels: tuple[float, ...] = ()
    service: str = "dr"
    merchant_seed: int | None = None
    retry_limit: int = 10
    send: bool = False
    multi_thread: bool = False
    visible_only: bool = False
    include_no_stock: bool = False
    active_limit: int = 100
    balance_poll_times: int = 5
    balance_poll_interval: int = 2
    active_poll_times: int = 25
    active_poll_interval: int = 6
    input_poll_times: int = 50
    input_poll_interval: int = 10
    history_limit: int = 10
    log_dir: Path = PROJECT_DIR / "log"

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        env: Mapping[str, str] | None = None,
    ) -> "WorkflowConfig":
        env = env or os.environ
        api_key = args.api_key or env.get("HEROSMS_API_KEY", "")
        base_url = args.base_url or env.get("HEROSMS_BASE_URL", DEFAULT_BASE_URL)
        max_price_raw = args.max_price if args.max_price is not None else env.get("HEROSMS_MAX_PRICE")
        max_price_levels = parse_float_levels(max_price_raw)
        return cls(
            api_key=api_key,
            base_url=base_url,
            max_price=max(max_price_levels) if max_price_levels else None,
            max_price_levels=max_price_levels,
            service=args.service,
            merchant_seed=args.merchant_seed,
            retry_limit=args.retry_limit,
            send=args.send,
            multi_thread=args.multi_thread,
            visible_only=args.visible_only,
            include_no_stock=args.include_no_stock,
            active_limit=args.active_limit,
            balance_poll_times=args.balance_poll_times,
            balance_poll_interval=args.balance_poll_interval,
            active_poll_times=args.active_poll_times,
            active_poll_interval=args.active_poll_interval,
            input_poll_times=args.input_poll_times,
            input_poll_interval=args.input_poll_interval,
            history_limit=args.history_limit,
            log_dir=Path(args.log_dir),
        )


class HeroSMSWorkflow:
    def __init__(self, config: WorkflowConfig, logger: logging.Logger | None = None):
        self.config = config
        self.logger = logger or logging.getLogger("herosms_tool")

    def log_and_print(self, message: str, level: int = logging.INFO) -> None:
        print(message, flush=True)
        self.logger.log(level, message)

    def api_get(self, action: str, **params) -> requests.Response:
        if not self.config.api_key or self.config.api_key == "YOUR_SECRET_TOKEN":
            raise RuntimeError("请先设置 HEROSMS_API_KEY，或使用 --api-key 指定")
        query = {
            "api_key": self.config.api_key,
            "action": action,
            **{key: value for key, value in params.items() if value not in (None, "")},
        }
        return requests.get(self.config.base_url, params=query, timeout=30)

    def get_balance(self) -> str:
        response = self.api_get("getBalance")
        response.raise_for_status()
        return response.text.strip()

    def get_active_records(self, limit: int = 100) -> list[dict]:
        response = self.api_get("getActiveActivations", start=0, limit=min(limit, 100))
        response.raise_for_status()
        payload = response.json()
        return validate_records_payload(payload)

    def build_merchants(self) -> list[dict]:
        return self.build_merchants_for_max_price(self.config.max_price)

    def build_merchants_for_max_price(self, max_price: float | None) -> list[dict]:
        merchants = build_get_number_v2_candidates(
            service=self.config.service,
            max_price=max_price,
            in_stock_only=not self.config.include_no_stock,
            visible_only=self.config.visible_only,
        )
        return self.sort_merchants(merchants)

    def iter_max_price_levels(self) -> tuple[float | None, ...]:
        return self.config.max_price_levels or (self.config.max_price,)

    @staticmethod
    def sort_merchants(merchants: list[dict]) -> list[dict]:
        return sorted(
            merchants,
            key=lambda item: (
                float(item.get("price") or 0),
                -(int(item.get("count") or 0)),
                int(item.get("country") or 0),
                str(item.get("operator") or ""),
            ),
        )

    def select_merchant(self, merchants: list[dict]) -> dict:
        sorted_merchants = self.sort_merchants(merchants)
        if self.config.merchant_seed is None:
            return sorted_merchants[0]
        rng = random.Random(self.config.merchant_seed)
        return rng.choice(sorted_merchants)

    def request_number(self, merchant: dict) -> tuple[int, object]:
        params = build_request_params(merchant)
        self.log_and_print("[发送请求]")
        self.log_and_print(json.dumps(params, ensure_ascii=False, indent=2))
        response = self.api_get(**params)
        payload = parse_response_payload(response)
        return response.status_code, payload

    def poll_balance_change(self, before_balance: float | None) -> None:
        detected = False
        for index in range(self.config.balance_poll_times):
            try:
                text = self.get_balance()
                value = parse_balance_value(text)
                self.log_and_print(f"[余额] after #{index + 1}: {text}")
            except requests.RequestException as error:
                self.log_and_print(f"[余额] after #{index + 1}: 查询失败 {error}", logging.WARNING)
                value = None

            if before_balance is not None and value is not None and value != before_balance:
                diff = round(before_balance - value, 10)
                self.log_and_print(f"[余额变化] before={before_balance} after={value} diff={diff}")
                detected = True
                break
            if index < self.config.balance_poll_times - 1:
                time.sleep(self.config.balance_poll_interval)
        if not detected:
            self.log_and_print(f"[余额变化] {self.config.balance_poll_times} 次查询内未检测到余额变化")

    @staticmethod
    def _normalize_phone(value: str) -> str:
        return "".join(ch for ch in str(value) if ch.isdigit())

    @staticmethod
    def _display_phone(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return text
        return text if text.startswith("+") else f"+{digits}"

    def extract_phone_number(self, payload) -> str:
        if isinstance(payload, dict):
            for key in ("phoneNumber", "phone", "number"):
                if payload.get(key):
                    return str(payload[key])
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("phoneNumber", "phone", "number"):
                    if data.get(key):
                        return str(data[key])
        if isinstance(payload, str):
            # 兼容 ACCESS_NUMBER:id:number 一类文本响应。
            parts = payload.split(":")
            if len(parts) >= 3 and parts[0].upper().startswith("ACCESS"):
                return parts[-1]
        return ""

    def phone_exists_in_records(self, phone_number: str, records: list[dict]) -> bool:
        wanted = self._normalize_phone(phone_number)
        if not wanted:
            return False
        for record in records:
            for key in ("phoneNumber", "phone", "number"):
                current = self._normalize_phone(str(record.get(key, "")))
                if current and (current == wanted or current.endswith(wanted) or wanted.endswith(current)):
                    return True
        return False

    def print_active_records(self, records: list[dict]) -> None:
        print_active_activations(records)
        self.logger.info("active_records=%s", json.dumps(records, ensure_ascii=False, default=str))

    def poll_active_list(self) -> None:
        for index in range(self.config.active_poll_times):
            self.log_and_print(f"[活动激活轮询] #{index + 1}/{self.config.active_poll_times}")
            try:
                records = self.get_active_records(limit=self.config.active_limit)
                self.print_active_records(records)
            except Exception as error:
                self.log_and_print(f"[活动激活轮询] 查询失败: {error}", logging.WARNING)
            if index < self.config.active_poll_times - 1:
                time.sleep(self.config.active_poll_interval)

    def mask_secret_in_url(self, url: str) -> str:
        api_key = self.config.api_key
        if not api_key:
            return url
        masked_key = "***" if len(api_key) <= 8 else f"{api_key[:4]}...{api_key[-4:]}"
        return url.replace(api_key, masked_key)

    def set_activation_status(self, activation_id: str, status: int) -> object:
        response = self.api_get("setStatus", id=activation_id, status=status)
        payload = parse_response_payload(response)
        self.log_and_print(f"[setStatus模式] status={status}")
        self.log_and_print(f"[setStatus请求URL] {self.mask_secret_in_url(response.url)}")
        self.log_and_print(f"[setStatus] id={activation_id} status={status} HTTP={response.status_code}")
        if isinstance(payload, (dict, list)):
            self.log_and_print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            self.log_and_print(str(payload))
        response.raise_for_status()
        return payload

    def _record_by_index(self, records: list[dict], index_text: str) -> dict | None:
        try:
            index = int(index_text)
        except ValueError:
            return None
        if index < 1 or index > len(records):
            return None
        return records[index - 1]

    def _activation_id_by_index(self, records: list[dict], index_text: str) -> str | None:
        record = self._record_by_index(records, index_text)
        if record is None:
            return None
        activation_id = str(record.get("activationId", "")).strip()
        return activation_id or None

    @staticmethod
    def get_sms_payload_fields(record: dict) -> list[str]:
        fields = []
        for field_name in ("smsCode", "smsText"):
            if str(record.get(field_name) or "").strip():
                fields.append(field_name)
        return fields

    def handle_user_input(self, user_input: str, state: UserInputState) -> UserInputState:
        text = user_input.strip()
        if not text:
            return state

        if text == "99":
            self.log_and_print("[用户输入] 99 退出")
            try:
                self.print_history()
            except Exception as error:
                self.log_and_print(f"[历史记录] 99 退出前查询失败: {error}", logging.WARNING)
            raise UserInputExit()

        if text == "0":
            records = self.get_active_records(limit=self.config.active_limit)
            self.print_active_records(records)
            return UserInputState()

        if text in {"3", "6", "8", "9"}:
            mode = int(text)
            records = self.get_active_records(limit=self.config.active_limit)
            self.print_active_records(records)
            mode_text = {3: "请求重发短信", 6: "完成", 8: "退款", 9: "处理后重开"}[mode]
            self.log_and_print(f"[模式] 进入{mode_text}模式；输入 {mode}-序号 执行，例如 {mode}-1")
            return UserInputState(mode=mode, records=records)

        if "-" in text:
            prefix, index_text = text.split("-", 1)
            if not prefix.isdigit():
                self.log_and_print(f"[输入无效] {text}", logging.WARNING)
                return state
            requested_mode = int(prefix)
            if requested_mode == 9 and state.mode is None:
                records = self.get_active_records(limit=self.config.active_limit)
                self.print_active_records(records)
                return self.handle_mode_9_by_index(index_text, records)
            if state.mode is None or requested_mode != state.mode:
                self.log_and_print(f"[输入无效] 当前模式不是 {requested_mode}，请先输入 {requested_mode}", logging.WARNING)
                return state
            records = state.records or []
            if requested_mode == 9:
                return self.handle_mode_9_by_index(index_text, records)
            target_record = self._record_by_index(records, index_text)
            if target_record is None:
                self.log_and_print(f"[输入无效] 序号不存在: {index_text}", logging.WARNING)
                return state
            activation_id = str(target_record.get("activationId", "")).strip()
            if not activation_id:
                self.log_and_print(f"[输入无效] 第 {index_text} 条没有 activationId", logging.WARNING)
                return state
            sms_payload_fields = self.get_sms_payload_fields(target_record)
            if requested_mode == 8 and sms_payload_fields:
                fields_text = "/".join(sms_payload_fields)
                self.log_and_print(
                    f"[模式不匹配] 第 {index_text} 条 activationId={activation_id} 已返回 {fields_text}，"
                    "表示短信已到达/服务已生效；不允许 8 退款模式。请改用 6 完成模式。",
                    logging.WARNING,
                )
                return state
            self.set_activation_status(activation_id=activation_id, status=requested_mode)
            self.log_and_print(f"[模式] 已对第 {index_text} 条 activationId={activation_id} 执行 status={requested_mode}")
            refreshed_records = self.get_active_records(limit=self.config.active_limit)
            self.print_active_records(refreshed_records)
            return UserInputState()

        self.log_and_print(
            "[输入提示] 可输入：0 查询激活列表；3 请求重发短信模式；6 完成模式；8 退款模式；"
            "9 处理后重开模式；3-1/6-1/8-1/9-1 对列表第1条执行。"
        )
        return state

    def handle_mode_9_by_index(self, index_text: str, records: list[dict]) -> UserInputState:
        if not self.config.send:
            self.log_and_print("[9模式错误] 9 模式会处理旧激活并重新申请号码，必须显式加 --send 才允许执行", logging.ERROR)
            return UserInputState(records=records)

        target_record = self._record_by_index(records, index_text)
        if target_record is None:
            self.log_and_print(f"[9模式] 输入无效，序号不存在: {index_text}", logging.WARNING)
            return UserInputState()

        activation_id = str(target_record.get("activationId", "")).strip()
        if not activation_id:
            self.log_and_print(f"[9模式] 输入无效，第 {index_text} 条没有 activationId", logging.WARNING)
            return UserInputState()

        sms_payload_fields = self.get_sms_payload_fields(target_record)
        status = 6 if sms_payload_fields else 8
        status_text = "完成" if status == 6 else "取消/退款"
        self.log_and_print(f"[9模式] 第 {index_text} 条 activationId={activation_id}，先执行 status={status}({status_text})")
        self.set_activation_status(activation_id=activation_id, status=status)

        active_records = self.get_active_records(limit=self.config.active_limit)
        self.print_active_records(active_records)
        if active_records:
            self.log_and_print("[9模式警告] 当前活动激活列表不为空，不允许 9 模式继续申请号码", logging.WARNING)
            return UserInputState(records=active_records)

        try:
            balance_text = self.get_balance()
        except Exception as error:
            self.log_and_print(f"[9模式错误] 查询余额失败: {error}", logging.ERROR)
            return UserInputState()

        before_balance = parse_balance_value(balance_text)
        self.log_and_print(f"[9模式余额] {balance_text}")
        if before_balance is None:
            self.log_and_print("[9模式错误] 余额响应无法解析，停止申请号码", logging.ERROR)
            return UserInputState()
        if self.config.max_price is not None and before_balance < self.config.max_price:
            self.log_and_print(
                f"[9模式余额不足] 当前余额={before_balance}，设置价格={self.config.max_price}，停止申请号码",
                logging.WARNING,
            )
            return UserInputState()

        merchant = self.build_replacement_merchant_from_record(target_record)
        if merchant is None:
            return UserInputState()

        result = self.obtain_number_with_retry(lambda: [merchant])
        if result is None:
            self.log_and_print("[9模式失败] 未获得成功号码响应", logging.ERROR)
            return UserInputState()
        if result.dry_run:
            return UserInputState()

        self.log_and_print("[9模式] 新号码申请成功，开始查询余额并确认活动列表")
        self.poll_balance_change(before_balance)
        try:
            records_after = self.get_active_records(limit=self.config.active_limit)
        except Exception as error:
            self.log_and_print(f"[9模式错误] 获取激活列表失败: {error}", logging.ERROR)
            return UserInputState()
        if self.phone_exists_in_records(result.phone, records_after):
            self.log_and_print(f"[9模式确认] 电话号码 {self._display_phone(result.phone)} 存在于活动激活列表")
        else:
            self.log_and_print(
                f"[9模式警告] 电话号码 {self._display_phone(result.phone)} 不存在于活动激活列表",
                logging.WARNING,
            )
            print_active_activations(records_after)
            return UserInputState(records=records_after)

        self.log_and_print("[9模式] 进入活动激活列表轮询")
        self.poll_active_list()
        return UserInputState(records=records_after)

    @staticmethod
    def _first_record_value(record: dict, keys: Sequence[str]) -> str:
        for key in keys:
            value = str(record.get(key) or "").strip()
            if value:
                return value
        return ""

    def build_replacement_merchant_from_record(self, record: dict) -> dict | None:
        service = self._first_record_value(record, ("serviceCode", "service", "serviceId")) or self.config.service
        country_text = self._first_record_value(record, ("countryCode", "country", "countryId"))
        operator = self._first_record_value(
            record,
            ("operator", "operatorCode", "activationOperator", "merchant", "provider"),
        )
        if not country_text:
            self.log_and_print("[9模式错误] 活动记录缺少 countryCode，无法按原国家重新申请", logging.ERROR)
            return None
        try:
            country = int(country_text)
        except ValueError:
            self.log_and_print(f"[9模式错误] 活动记录 countryCode={country_text} 不是数字，无法重新申请", logging.ERROR)
            return None

        if operator:
            merchant = self.build_operator_replacement_merchant(service=service, country=country, operator=operator)
        else:
            merchant = self.build_country_replacement_merchant(service=service, country=country)

        if merchant is None:
            return None

        price = float(merchant.get("price") or 0)
        if self.config.max_price is not None and price > self.config.max_price:
            self.log_and_print(
                f"[9模式错误] 价格变更，不符合最大限额：service={service} country={country} "
                f"operator={operator or '-'} current_price={price} max_price={self.config.max_price}",
                logging.ERROR,
            )
            return None

        self.log_and_print(
            "[9模式价格确认] "
            f"service={merchant.get('service')} country={merchant.get('country')} "
            f"operator={merchant.get('operator') or '-'} price={merchant.get('price')} "
            f"maxPrice={merchant.get('maxPrice')} count={merchant.get('count')}"
        )
        return merchant

    def build_country_replacement_merchant(self, service: str, country: int) -> dict | None:
        rows = build_coverage(service)
        row = next((item for item in rows if int(item.get("id") or 0) == country), None)
        if row is None:
            self.log_and_print(f"[9模式错误] 未查询到 service={service} country={country} 的国家价格", logging.ERROR)
            return None
        if self.config.visible_only and not row.get("visible"):
            self.log_and_print(f"[9模式错误] country={country} 当前不是 visible=1，不符合 visible-only", logging.ERROR)
            return None
        if not self.config.include_no_stock and (row.get("count") or 0) <= 0:
            self.log_and_print(f"[9模式错误] country={country} 当前无库存 count={row.get('count')}", logging.ERROR)
            return None
        country_name = row.get("name_cn") or row.get("name_en") or f"Country {country}"
        return {
            "service": service,
            "country": country,
            "operator": "",
            "maxPrice": self.config.max_price,
            "price": row.get("price"),
            "count": row.get("count"),
            "physicalCount": row.get("physical_count"),
            "countryName": country_name,
        }

    def build_operator_replacement_merchant(self, service: str, country: int, operator: str) -> dict | None:
        rows = load_operator_prices(service, country, [operator])
        row = next((item for item in rows if str(item.get("operator") or "") == operator), None)
        if row is None:
            self.log_and_print(
                f"[9模式错误] 未查询到 service={service} country={country} operator={operator} 的商家价格",
                logging.ERROR,
            )
            return None
        if not self.config.include_no_stock and (row.get("count") or 0) <= 0:
            self.log_and_print(
                f"[9模式错误] service={service} country={country} operator={operator} 当前无库存 count={row.get('count')}",
                logging.ERROR,
            )
            return None
        return {
            "service": service,
            "country": country,
            "operator": operator,
            "maxPrice": self.config.max_price,
            "price": row.get("price"),
            "count": row.get("count"),
            "physicalCount": row.get("physical_count"),
            "countryName": f"Country {country}",
        }

    def user_input_loop(
        self,
        initial_records: list[dict] | None = None,
        prompt: str | None = None,
    ) -> None:
        self.log_and_print(
            prompt
            or "[用户输入轮询] 可输入：0 查询激活列表；3 请求重发短信模式；6 完成模式；8 退款模式；"
            "9 处理后重开模式；3-1/6-1/8-1/9-1 执行对应列表序号；99 退出"
        )
        state = UserInputState(records=initial_records)
        if initial_records is not None:
            self.print_active_records(initial_records)
        for index in range(self.config.input_poll_times):
            self.log_and_print(f"[用户输入轮询] #{index + 1}/{self.config.input_poll_times} 等待输入")
            user_input = self.read_user_input_with_timeout(self.config.input_poll_interval)
            if user_input is not None:
                try:
                    state = self.handle_user_input(user_input, state)
                except UserInputExit:
                    raise
                except Exception as error:
                    self.log_and_print(f"[用户输入处理失败] {error}", logging.WARNING)
        self.finalize_after_input_timeout()

    def finalize_after_input_timeout(self) -> None:
        self.log_and_print("[用户输入轮询结束] 已达到轮询次数，开始自动收尾检查")
        try:
            records = self.get_active_records(limit=self.config.active_limit)
        except Exception as error:
            self.log_and_print(f"[自动收尾] 查询活动激活列表失败: {error}", logging.WARNING)
            return

        self.print_active_records(records)
        if len(records) != 1:
            self.log_and_print(f"[自动收尾] 当前活动激活数量={len(records)}，不满足自动 8 条件")
            return

        record = records[0]
        activation_id = str(record.get("activationId", "")).strip()
        if not activation_id:
            self.log_and_print("[自动收尾] 唯一活动记录缺少 activationId，跳过自动 8", logging.WARNING)
            return

        sms_payload_fields = self.get_sms_payload_fields(record)
        if sms_payload_fields:
            fields_text = "/".join(sms_payload_fields)
            self.log_and_print(
                f"[自动收尾] activationId={activation_id} 已收到 {fields_text}，不执行自动 8，请手动判断是否完成",
                logging.WARNING,
            )
            return

        self.log_and_print(f"[自动收尾] 仅剩 1 条且未收到验证码，自动执行 status=8 activationId={activation_id}")
        try:
            self.set_activation_status(activation_id=activation_id, status=8)
        except Exception as error:
            self.log_and_print(f"[自动收尾] 自动 status=8 失败: {error}", logging.WARNING)
            return

        try:
            refreshed_records = self.get_active_records(limit=self.config.active_limit)
            self.print_active_records(refreshed_records)
        except Exception as error:
            self.log_and_print(f"[自动收尾] status=8 后刷新活动列表失败: {error}", logging.WARNING)

    def read_user_input_with_timeout(self, timeout: int | float) -> str | None:
        if os.name == "nt":
            return self._read_user_input_windows(timeout)

        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if readable:
            return sys.stdin.readline().strip()
        return None

    @staticmethod
    def _read_user_input_windows(timeout: int | float) -> str | None:
        if not sys.stdin.isatty():
            return None

        try:
            import msvcrt
        except ImportError:
            return None

        deadline = time.monotonic() + max(float(timeout), 0.0)
        chars: list[str] = []
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue

            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                print()
                return "".join(chars).strip()
            if char == "\003":
                raise KeyboardInterrupt
            if char == "\b":
                if chars:
                    chars.pop()
                    print("\b \b", end="", flush=True)
                continue
            chars.append(char)
            print(char, end="", flush=True)

        return None

    def print_history(self) -> None:
        self.log_and_print("[历史记录模式] getHistory")
        response = self.api_get("getHistory")
        self.log_and_print(f"[历史记录请求URL] {self.mask_secret_in_url(response.url)}")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            print_history(payload, limit=self.config.history_limit)
            self.logger.info("history_records=%s", json.dumps(payload, ensure_ascii=False, default=str))
        else:
            self.log_and_print(f"[历史记录异常响应] {payload}", logging.WARNING)

    def obtain_number_with_retry(
        self,
        merchant_provider: Callable[[], list[dict]] | None = None,
    ) -> NumberRequestResult | None:
        if merchant_provider is not None:
            return self.obtain_number_from_provider(merchant_provider)

        levels = self.iter_max_price_levels()
        if len(levels) <= 1:
            return self.obtain_number_from_provider(self.build_merchants)

        for index, max_price in enumerate(levels, start=1):
            self.log_and_print(f"[价格阶梯] #{index}/{len(levels)} maxPrice={max_price}")
            result = self.obtain_number_from_provider(lambda max_price=max_price: self.build_merchants_for_max_price(max_price))
            if result is not None:
                return result
            if index < len(levels):
                self.log_and_print(f"[价格阶梯] maxPrice={max_price} 未成功，继续尝试下一档")

        return None

    def obtain_number_from_provider(
        self,
        merchant_provider: Callable[[], list[dict]],
    ) -> NumberRequestResult | None:
        retry_count = 0

        while retry_count <= self.config.retry_limit:
            try:
                merchants = merchant_provider()
            except Exception as error:
                retry_count += 1
                self.log_and_print(f"[错误] 生成商户列表失败: {error}", logging.ERROR)
                self.log_and_print(f"[重试计数] {retry_count}/{self.config.retry_limit}")
                if retry_count > self.config.retry_limit:
                    self.log_and_print("[失败] 获取商户阶段超过重试次数，退出", logging.ERROR)
                    return None
                continue

            self.log_and_print(f"[商户候选数量] {len(merchants)}")
            if not merchants:
                retry_count += 1
                self.log_and_print("[错误] 没有可用商户候选", logging.ERROR)
                self.log_and_print(f"[重试计数] {retry_count}/{self.config.retry_limit}")
                if retry_count > self.config.retry_limit:
                    self.log_and_print("[失败] 没有可用商户候选且超过重试次数，退出", logging.ERROR)
                    return None
                self.log_and_print("[等待] 5 秒后重试商户查询")
                time.sleep(5)
                continue

            request_attempt_limit = len(merchants)
            request_attempt_count = 0
            self.log_and_print(f"[号码请求上限] 当前商户候选数量={request_attempt_limit}，本轮最多尝试 {request_attempt_limit} 次")
            remaining_merchants = merchants[:]
            while remaining_merchants and request_attempt_count < request_attempt_limit:
                merchant = self.select_merchant(remaining_merchants)
                self.log_and_print(
                    "[抽取商户] "
                    f"service={merchant.get('service')} country={merchant.get('country')} "
                    f"operator={merchant.get('operator') or '-'} price={merchant.get('price')} "
                    f"count={merchant.get('count')}"
                )
                if not self.config.send:
                    self.log_and_print("[模式] dry-run，未实际发送号码请求。加 --send 才会真实请求。")
                    self.log_and_print(json.dumps(build_request_params(merchant), ensure_ascii=False, indent=2))
                    return NumberRequestResult(dry_run=True)

                try:
                    status_code, payload = self.request_number(merchant)
                except Exception as error:
                    self.log_and_print(f"[请求失败] {error}", logging.WARNING)
                    status_code, payload = 0, str(error)

                if status_code != 200:
                    request_attempt_count += 1
                    self.log_and_print(f"[非200] status={status_code}")
                    self.log_and_print(f"[返回信息] {payload}")
                    self.log_and_print(f"[号码请求计数] {request_attempt_count}/{request_attempt_limit}")
                    remaining_merchants = [item for item in remaining_merchants if item is not merchant]
                    if remaining_merchants:
                        self.log_and_print(f"[重试] 剩余商户数量 {len(remaining_merchants)}")
                        continue
                    self.log_and_print("[失败] 当前商户列表已耗尽，停止本轮号码请求")
                    return None

                phone_number = self.extract_phone_number(payload)
                if not phone_number:
                    request_attempt_count += 1
                    self.log_and_print("[业务失败] HTTP 200 但未获得号码，返回信息：", logging.WARNING)
                    print_response_payload(payload)
                    self.logger.warning("number_response_without_phone=%s", json.dumps(payload, ensure_ascii=False, default=str))
                    self.log_and_print(f"[号码请求计数] {request_attempt_count}/{request_attempt_limit}")
                    remaining_merchants = [item for item in remaining_merchants if item is not merchant]
                    if remaining_merchants:
                        self.log_and_print(f"[重试] 剩余商户数量 {len(remaining_merchants)}")
                        continue
                    self.log_and_print("[失败] 当前商户列表已耗尽，停止本轮号码请求")
                    return None

                self.log_and_print("[成功] HTTP 200，返回信息：")
                print_response_payload(payload)
                self.logger.info("number_response=%s", json.dumps(payload, ensure_ascii=False, default=str))
                return NumberRequestResult(payload=payload, phone=phone_number)

            return None

        return None

    def run(self) -> int:
        self.log_and_print("[流程] 1. 读取配置完成，开始查询余额")
        try:
            balance_text = self.get_balance()
        except Exception as error:
            self.log_and_print(f"[错误] 查询余额失败: {error}", logging.ERROR)
            return 1

        before_balance = parse_balance_value(balance_text)
        self.log_and_print(f"[余额] {balance_text}")
        if before_balance is None:
            self.log_and_print("[错误] 余额响应无法解析，退出", logging.ERROR)
            return 1
        if self.config.max_price is not None:
            affordable_count = int(before_balance // self.config.max_price) if self.config.max_price > 0 else 0
            self.log_and_print(
                f"[可购买次数估算] 当前余额={before_balance} maxPrice={self.config.max_price} "
                f"可支持约 {affordable_count} 次"
            )
        if self.config.max_price is not None and before_balance < self.config.max_price:
            self.log_and_print(
                f"[余额不足] 当前余额={before_balance}，设置价格={self.config.max_price}，退出",
                logging.WARNING,
            )
            return 1

        self.log_and_print("[流程] 2. 获取活动激活列表")
        try:
            active_records = self.get_active_records(limit=self.config.active_limit)
        except Exception as error:
            self.log_and_print(f"[错误] 获取激活列表失败: {error}", logging.ERROR)
            return 1
        self.log_and_print(f"[活动激活数量] {len(active_records)}")
        if active_records and not self.config.multi_thread:
            self.log_and_print("单线程模式请先完成激活列表后再试", logging.WARNING)
            try:
                self.user_input_loop(
                    initial_records=active_records,
                    prompt="[单线程处理] 请先处理活动激活列表：0 查询，3 请求重发短信模式，6 完成模式，8 退款模式，9 处理后重开模式，99 退出",
                )
            except UserInputExit:
                self.log_and_print("[退出] 用户输入 99，退出程序")
            return 1

        self.log_and_print("[流程] 3. 查询服务国家和商户，生成商户列表")
        number_result = self.obtain_number_with_retry()
        if number_result is None:
            self.log_and_print("[失败] 未获得成功号码响应", logging.ERROR)
            return 1
        if number_result.dry_run:
            return 0

        self.log_and_print("[流程] 4. 5 次查询余额并计算差价")
        self.poll_balance_change(before_balance)

        self.log_and_print("[流程] 5. 获取激活列表并确认电话号码存在")
        try:
            records_after = self.get_active_records(limit=self.config.active_limit)
        except Exception as error:
            self.log_and_print(f"[错误] 获取激活列表失败: {error}", logging.ERROR)
            return 1
        if self.phone_exists_in_records(number_result.phone, records_after):
            self.log_and_print(f"[确认] 电话号码 {self._display_phone(number_result.phone)} 存在于活动激活列表")
        else:
            self.log_and_print(f"[警告] 电话号码 {self._display_phone(number_result.phone)} 不存在于活动激活列表，退出", logging.WARNING)
            print_active_activations(records_after)
            return 1

        self.log_and_print("[流程] 6. 轮询活动激活列表")
        self.poll_active_list()

        self.log_and_print("[流程] 7. 用户输入模式轮询")
        try:
            self.user_input_loop()
        except UserInputExit:
            self.log_and_print("[退出] 用户输入 99，退出用户输入轮询")

        self.log_and_print("[流程] 8. 查询用户历史并退出")
        try:
            self.print_history()
        except Exception as error:
            self.log_and_print(f"[历史记录] 查询失败: {error}", logging.WARNING)
        return 0


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("herosms_tool")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler = logging.FileHandler(log_dir / f"{datetime.now():%Y%m%d}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HeroSMS 合法调试工作流统一入口")
    parser.add_argument("run", nargs="?", default="run", help="执行完整工作流，默认 run")
    parser.add_argument("--api-key", default=None, help="HeroSMS API Key；优先级高于 HEROSMS_API_KEY")
    parser.add_argument("--base-url", default=None, help="API 地址；优先级高于 HEROSMS_BASE_URL")
    parser.add_argument("-s", "--service", default="dr", help="服务代码，默认 dr")
    parser.add_argument(
        "--max-price",
        default=None,
        help="最高价格；支持 0.025-0.03-0.035 多级价格；优先级高于 HEROSMS_MAX_PRICE",
    )
    parser.add_argument("--merchant-seed", type=int, default=None, help="商户抽取随机种子")
    parser.add_argument("--retry-limit", type=int, default=10, help="获取商户/号码累计重试次数上限，超过后退出，默认 10")
    parser.add_argument("--send", action="store_true", help="真实发送号码请求；默认 dry-run")
    parser.add_argument("--multi-thread", action="store_true", help="启用多线程模式预留开关；当前仅作为跳过单线程检查")
    parser.add_argument("--visible-only", action="store_true", help="只从 visible=1 的国家中选择")
    parser.add_argument("--include-no-stock", action="store_true", help="允许 count=0 候选；默认只选有库存")
    parser.add_argument("--active-limit", type=int, default=100, help="活动激活列表查询数量，默认 100")
    parser.add_argument("--balance-poll-times", type=int, default=5, help="余额轮询次数，默认 5")
    parser.add_argument("--balance-poll-interval", type=int, default=2, help="余额轮询间隔秒数，默认 2")
    parser.add_argument("--active-poll-times", type=int, default=25, help="活动列表轮询次数，默认 25")
    parser.add_argument("--active-poll-interval", type=int, default=6, help="活动列表轮询间隔秒数，默认 6")
    parser.add_argument("--input-poll-times", type=int, default=50, help="用户输入轮询次数，默认 50")
    parser.add_argument("--input-poll-interval", type=int, default=10, help="用户输入轮询间隔秒数，默认 10")
    parser.add_argument("--history-limit", type=int, default=10, help="历史记录显示数量，默认 10")
    parser.add_argument("--log-dir", default=str(PROJECT_DIR / "log"), help="日志目录，默认 ./log")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(PROJECT_DIR / ".env")
    args = parse_args(argv)
    try:
        config = WorkflowConfig.from_args(args)
    except ValueError as error:
        print(f"[错误] 配置解析失败: {error}")
        return 2
    logger = setup_logging(config.log_dir)
    workflow = HeroSMSWorkflow(config=config, logger=logger)
    return workflow.run()


if __name__ == "__main__":
    raise SystemExit(main())
