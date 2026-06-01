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
from get_rent_number import build_rent_number_params, parse_duration_arg
from get_service_coverage import build_coverage
from tools.feishu import FeishuNotifier

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
class SmsSnapshot:
    collected_at: float
    activation_id: str
    phone: str
    sms_code: str
    sms_text: str
    display_value: str
    source: str
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ActivationApplicationContext:
    activation_id: str
    phone: str
    applied_unixtime: int
    duration_hours: int | None = None
    source: str = ""


class SmsActivationTracker:
    def __init__(self, clock: Callable[[], float] | None = None):
        self.clock = clock or time.time
        self.history_by_id: dict[str, list[SmsSnapshot]] = {}

    @staticmethod
    def _first_record_value(record: dict, keys: Sequence[str]) -> str:
        for key in keys:
            value = str(record.get(key) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def extract_sms_identity(cls, record: dict) -> tuple[str, str, str, str, str]:
        activation_id = cls._first_record_value(record, ("activationId", "id"))
        phone = cls._first_record_value(record, ("phoneNumber", "phone", "number"))
        sms_code = cls._first_record_value(record, ("smsCode",))
        sms_text = cls._first_record_value(record, ("smsText",))
        display_parts = []
        if sms_code:
            display_parts.append(f"smsCode={sms_code}")
        if sms_text:
            display_parts.append(f"smsText={sms_text}")
        display_value = " | ".join(display_parts)
        return activation_id, phone, sms_code, sms_text, display_value

    def record(self, records: list[dict], source: str, timeout_seconds: float | None = None) -> list[SmsSnapshot]:
        collected_at = self.clock()
        snapshots = []
        for record in records:
            activation_id, phone, sms_code, sms_text, display_value = self.extract_sms_identity(record)
            if not activation_id:
                continue
            snapshot = SmsSnapshot(
                collected_at=collected_at,
                activation_id=activation_id,
                phone=phone,
                sms_code=sms_code,
                sms_text=sms_text,
                display_value=display_value,
                source=source,
                timeout_seconds=timeout_seconds,
            )
            self.history_by_id.setdefault(activation_id, []).append(snapshot)
            snapshots.append(snapshot)
        return snapshots

    def latest_history(self, activation_id: str) -> list[SmsSnapshot]:
        return self.history_by_id.get(activation_id, [])

    @staticmethod
    def _last_distinct_values(history: list[SmsSnapshot], limit: int = 3) -> list[SmsSnapshot]:
        distinct = []
        seen_values = set()
        for snapshot in reversed(history):
            value_key = snapshot.display_value or "<无验证码>"
            if value_key in seen_values:
                continue
            seen_values.add(value_key)
            distinct.append(snapshot)
            if len(distinct) >= limit:
                break
        return distinct

    @staticmethod
    def seconds_since_previous_change(history: list[SmsSnapshot]) -> float | None:
        distinct = SmsActivationTracker._last_distinct_values(history, limit=2)
        if len(distinct) < 2:
            return None
        return max(0.0, distinct[0].collected_at - distinct[1].collected_at)

    @staticmethod
    def received_sms_events(history: list[SmsSnapshot]) -> list[tuple[str, float]]:
        events = []
        last_value = ""
        for snapshot in history:
            if not snapshot.display_value:
                continue
            if snapshot.display_value == last_value:
                continue
            events.append((snapshot.display_value, snapshot.collected_at))
            last_value = snapshot.display_value
        return events

    def summarize(self, record: dict) -> str | None:
        activation_id, phone, sms_code, sms_text, display_value = self.extract_sms_identity(record)
        if not activation_id:
            return None
        history = self.latest_history(activation_id)
        if history:
            latest = history[-1]
            phone = phone or latest.phone
            sms_code = sms_code or latest.sms_code
            sms_text = sms_text or latest.sms_text
            display_value = display_value or latest.display_value
        distinct = self._last_distinct_values(history, limit=3)
        previous = distinct[1].display_value if len(distinct) >= 2 else "-"
        previous_previous = distinct[2].display_value if len(distinct) >= 3 else "-"
        changed_seconds = self.seconds_since_previous_change(history)
        changed_text = "-" if changed_seconds is None else f"{changed_seconds:.1f}s"
        timeout_seconds = history[-1].timeout_seconds if history else None
        timeout_text = "-" if timeout_seconds is None else f"{timeout_seconds:g}s"
        return (
            f"id={activation_id} phone={phone or '-'} smsCode={sms_code or '-'} smsText={sms_text or '-'} "
            f"当前={display_value or '<无验证码>'} 上次={previous or '<无验证码>'} "
            f"上上次={previous_previous or '<无验证码>'} 不同间隔={changed_text} timeout={timeout_text}"
        )


@dataclass(frozen=True)
class WorkflowConfig:
    command: str = "run"
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    max_price: float | None = None
    max_price_levels: tuple[float, ...] = ()
    service: str = "dr"
    merchant_seed: int | None = None
    retry_limit: int = 10
    run_loop: bool = False
    rent_duration: int = 2
    rent_duration_levels: tuple[int, ...] = (2,)
    rent_country: int = 16
    rent_operator: str = "any"
    rent_cost: str | None = None
    rent_currency: str | None = None
    rent_ref: str | None = None
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

    def __post_init__(self) -> None:
        if self.rent_duration_levels == (2,) and self.rent_duration != 2:
            object.__setattr__(self, "rent_duration_levels", (self.rent_duration,))

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
            command=args.run,
            api_key=api_key,
            base_url=base_url,
            max_price=max(max_price_levels) if max_price_levels else None,
            max_price_levels=max_price_levels,
            service=args.service,
            merchant_seed=args.merchant_seed,
            retry_limit=args.retry_limit,
            run_loop=args.run_loop,
            rent_duration=args.duration[-1],
            rent_duration_levels=args.duration,
            rent_country=args.country,
            rent_operator=args.operator,
            rent_cost=args.cost,
            rent_currency=args.currency,
            rent_ref=args.ref,
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
        self.feishu_notifier = FeishuNotifier(logger=self.logger)
        self.sms_tracker = SmsActivationTracker()
        self.notified_sms_codes_by_activation_id: dict[str, set[str]] = {}
        self.notified_sms_codes_by_phone: dict[str, set[str]] = {}
        self.application_context_by_activation_id: dict[str, ActivationApplicationContext] = {}
        self.application_context_by_phone: dict[str, ActivationApplicationContext] = {}
        self.last_run_restartable = False

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

    def build_rent_number_request_params(self, duration: int | None = None) -> dict:
        return build_rent_number_params(
            service=self.config.service,
            country=self.config.rent_country,
            duration=duration if duration is not None else self.config.rent_duration,
            operator=self.config.rent_operator,
            cost=self.config.rent_cost,
            currency=self.config.rent_currency,
            ref=self.config.rent_ref,
        )

    def request_rent_number(self, duration: int | None = None) -> tuple[int, object]:
        params = self.build_rent_number_request_params(duration=duration)
        self.log_and_print("[租号发送请求]")
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

    @staticmethod
    def _format_elapsed_minutes_seconds(seconds: float | None) -> str:
        if seconds is None:
            return "-"
        total_seconds = max(0, int(seconds))
        minutes, remain_seconds = divmod(total_seconds, 60)
        return f"{minutes}分{remain_seconds}秒"

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

    def find_record_by_phone(self, phone_number: str, records: list[dict]) -> dict | None:
        wanted = self._normalize_phone(phone_number)
        if not wanted:
            return None
        for record in records:
            phone = self._record_first_value(record, ("phoneNumber", "phone", "number"))
            current = self._normalize_phone(phone)
            if current and (current == wanted or current.endswith(wanted) or wanted.endswith(current)):
                return record
        return None

    @staticmethod
    def _record_first_value(record: dict, keys: Sequence[str]) -> str:
        for key in keys:
            value = str(record.get(key) or "").strip()
            if value:
                return value
        return ""

    def infer_phone_from_new_active_record(self, records_before: list[dict], records_after: list[dict]) -> str:
        # 租号接口有时不会在响应体里直接返回号码，这里用活动列表前后差异兜底推断本次号码。
        before_ids = {
            self._record_first_value(record, ("activationId", "id"))
            for record in records_before
        }
        before_ids.discard("")
        before_phones = {
            self._normalize_phone(self._record_first_value(record, ("phoneNumber", "phone", "number")))
            for record in records_before
        }
        before_phones.discard("")

        candidates: list[str] = []
        for record in records_after:
            phone = self._record_first_value(record, ("phoneNumber", "phone", "number"))
            if not phone:
                continue
            activation_id = self._record_first_value(record, ("activationId", "id"))
            normalized_phone = self._normalize_phone(phone)
            if before_ids and activation_id and activation_id not in before_ids:
                candidates.append(phone)
            elif not before_ids and normalized_phone and normalized_phone not in before_phones:
                candidates.append(phone)

        unique_candidates = []
        seen = set()
        for phone in candidates:
            normalized_phone = self._normalize_phone(phone)
            if normalized_phone in seen:
                continue
            seen.add(normalized_phone)
            unique_candidates.append(phone)
        if len(unique_candidates) == 1:
            return unique_candidates[0]
        if not records_before and len(records_after) == 1:
            return self._record_first_value(records_after[0], ("phoneNumber", "phone", "number"))
        return ""

    def record_application_context(
        self,
        phone_number: str,
        records: list[dict],
        duration_hours: int | None = None,
        source: str = "",
    ) -> None:
        record = self.find_record_by_phone(phone_number, records)
        if record is None:
            self.log_and_print(
                f"[号码申请记录] 电话号码 {self._display_phone(phone_number)} 未在活动列表中匹配到记录，跳过申请时间记录",
                logging.WARNING,
            )
            return
        activation_id = self._record_first_value(record, ("activationId", "id"))
        phone = self._record_first_value(record, ("phoneNumber", "phone", "number")) or phone_number
        context = ActivationApplicationContext(
            activation_id=activation_id,
            phone=phone,
            applied_unixtime=int(time.time()),
            duration_hours=duration_hours,
            source=source,
        )
        normalized_phone = self._normalize_phone(phone)
        if activation_id:
            self.application_context_by_activation_id[activation_id] = context
        if normalized_phone:
            self.application_context_by_phone[normalized_phone] = context
        self.log_and_print(
            f"[号码申请记录] 来源={source or '-'} activationId={activation_id or '-'} "
            f"phone={self._display_phone(phone)} applied_unixtime={context.applied_unixtime} "
            f"duration={duration_hours if duration_hours is not None else '-'}"
        )

    def get_application_context_for_record(self, record: dict) -> ActivationApplicationContext | None:
        activation_id = self._record_first_value(record, ("activationId", "id"))
        if activation_id and activation_id in self.application_context_by_activation_id:
            return self.application_context_by_activation_id[activation_id]
        phone = self._record_first_value(record, ("phoneNumber", "phone", "number"))
        normalized_phone = self._normalize_phone(phone)
        if normalized_phone:
            return self.application_context_by_phone.get(normalized_phone)
        return None

    def notify_phone_active_presence(self, phone_number: str, exists: bool) -> None:
        display_phone = self._display_phone(phone_number)
        try:
            self.feishu_notifier.notify_phone_active_presence(display_phone, exists)
        except Exception as error:
            self.log_and_print(f"[飞书通知] 发送失败: {error}", logging.WARNING)

    def notify_new_sms_codes(self, snapshots: list[SmsSnapshot]) -> None:
        for snapshot in snapshots:
            sms_code = str(snapshot.sms_code or "").strip()
            if not sms_code:
                continue
            activation_id = str(snapshot.activation_id or "").strip()
            normalized_phone = self._normalize_phone(snapshot.phone)
            activation_codes = self.notified_sms_codes_by_activation_id.setdefault(activation_id, set()) if activation_id else set()
            phone_codes = self.notified_sms_codes_by_phone.setdefault(normalized_phone, set()) if normalized_phone else set()
            if sms_code in activation_codes or sms_code in phone_codes:
                continue

            history = self.sms_tracker.latest_history(activation_id) if activation_id else []
            code_history = []
            seen_codes = set()
            if history:
                for history_snapshot in history:
                    history_code = str(history_snapshot.sms_code or "").strip()
                    if not history_code or history_code in seen_codes:
                        continue
                    seen_codes.add(history_code)
                    code_history.append(history_code)
                code_index = len(code_history) if sms_code in seen_codes else len(code_history) + 1
            else:
                code_index = len(phone_codes) + 1
            display_phone = self._display_phone(snapshot.phone)
            try:
                self.feishu_notifier.notify_sms_code(
                    display_phone,
                    sms_code,
                    sms_text=snapshot.sms_text,
                    code_index=code_index,
                )
                if activation_id:
                    activation_codes.add(sms_code)
                if normalized_phone:
                    phone_codes.add(sms_code)
                self.log_and_print(
                    f"[飞书验证码] 已发送 phone={display_phone} smsCode={sms_code} 第{code_index}次 来源={snapshot.source}"
                )
            except Exception as error:
                self.log_and_print(f"[飞书验证码] 发送失败 phone={display_phone} smsCode={sms_code}: {error}", logging.WARNING)

    def print_active_records(self, records: list[dict]) -> None:
        print_active_activations(records)
        self.logger.info("active_records=%s", json.dumps(records, ensure_ascii=False, default=str))

    def record_sms_snapshots(
        self,
        records: list[dict],
        source: str,
        timeout_seconds: float | None = None,
    ) -> None:
        # 记录验证码快照用于用户输入轮询期间对比多次短信变化。
        snapshots = self.sms_tracker.record(records, source=source, timeout_seconds=timeout_seconds)
        self.log_and_print(
            f"[验证码记录] 来源={source} 记录数量={len(snapshots)} timeout={timeout_seconds if timeout_seconds is not None else '-'}"
        )
        self.notify_new_sms_codes(snapshots)
        for record in records:
            summary = self.summarize_sms_history(record)
            if summary:
                self.log_and_print(f"[验证码记录明细] {summary}")

    def summarize_sms_history(self, record: dict) -> str | None:
        summary = self.sms_tracker.summarize(record)
        if not summary:
            return None
        context = self.get_application_context_for_record(record)
        if context is None:
            return summary

        activation_id, _phone, _sms_code, _sms_text, _display_value = self.sms_tracker.extract_sms_identity(record)
        history = self.sms_tracker.latest_history(activation_id)
        sms_events = self.sms_tracker.received_sms_events(history)
        latest_snapshot_time = history[-1].collected_at if history else time.time()
        if sms_events:
            reference_value, reference_time = sms_events[-1]
            reference_text = f"上次验证码 {reference_value}"
        else:
            reference_time = float(context.applied_unixtime)
            reference_text = "号码申请时间"
        elapsed_text = self._format_elapsed_minutes_seconds(latest_snapshot_time - reference_time)
        duration_text = "-" if context.duration_hours is None else f"{context.duration_hours}小时"
        return (
            f"{summary} 申请unixtime={context.applied_unixtime} 申请duration={duration_text} "
            f"距上次验证码={elapsed_text} 等待基准={reference_text}"
        )

    def print_and_record_active_records(
        self,
        records: list[dict],
        source: str,
        timeout_seconds: float | None = None,
    ) -> None:
        self.print_active_records(records)
        self.record_sms_snapshots(records, source=source, timeout_seconds=timeout_seconds)

    def fetch_print_and_record_active_records(
        self,
        source: str,
        timeout_seconds: float | None = None,
    ) -> list[dict]:
        records = self.get_active_records(limit=self.config.active_limit)
        self.print_and_record_active_records(records, source=source, timeout_seconds=timeout_seconds)
        return records

    def poll_active_list(self) -> None:
        for index in range(self.config.active_poll_times):
            self.log_and_print(f"[活动激活轮询] #{index + 1}/{self.config.active_poll_times}")
            try:
                records = self.get_active_records(limit=self.config.active_limit)
                self.print_and_record_active_records(records, source="活动激活轮询")
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
            records = self.fetch_print_and_record_active_records(source="0查询")
            return UserInputState()

        if text in {"3", "6", "8", "9"}:
            mode = int(text)
            source = "进入3模式" if mode == 3 else f"进入{mode}模式"
            records = self.fetch_print_and_record_active_records(source=source)
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
                records = self.fetch_print_and_record_active_records(source="9模式直接执行前查询")
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
            refresh_source = "执行3模式后刷新" if requested_mode == 3 else f"执行{requested_mode}模式后刷新"
            refreshed_records = self.fetch_print_and_record_active_records(source=refresh_source)
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

        active_records = self.fetch_print_and_record_active_records(source="9模式处理后查询")
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
        phone_exists = self.phone_exists_in_records(result.phone, records_after)
        self.notify_phone_active_presence(result.phone, phone_exists)
        if phone_exists:
            self.record_application_context(result.phone, records_after, source="9模式")
            self.log_and_print(f"[9模式确认] 电话号码 {self._display_phone(result.phone)} 存在于活动激活列表")
        else:
            self.log_and_print(
                f"[9模式警告] 电话号码 {self._display_phone(result.phone)} 不存在于活动激活列表",
                logging.WARNING,
            )
            self.print_and_record_active_records(records_after, source="9模式确认失败")
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
            self.print_and_record_active_records(initial_records, source="用户输入初始列表")
        for index in range(self.config.input_poll_times):
            self.log_and_print(
                f"[用户输入轮询] #{index + 1}/{self.config.input_poll_times} "
                f"刷新活动列表并等待输入 timeout={self.config.input_poll_interval:g}s"
            )
            try:
                # 每轮等待前主动刷新活动列表，便于记录验证码到达和变化耗时。
                refreshed_records = self.fetch_print_and_record_active_records(
                    source="用户输入轮询",
                    timeout_seconds=float(self.config.input_poll_interval),
                )
                state = UserInputState(mode=state.mode, records=refreshed_records)
            except Exception as error:
                self.log_and_print(f"[用户输入轮询] 刷新活动激活列表失败，继续等待输入: {error}", logging.WARNING)
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

        self.print_and_record_active_records(records, source="自动收尾")
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
            self.print_and_record_active_records(refreshed_records, source="自动收尾status8后刷新")
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
        self.last_run_restartable = False
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
            self.last_run_restartable = True
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
        phone_exists = self.phone_exists_in_records(number_result.phone, records_after)
        self.notify_phone_active_presence(number_result.phone, phone_exists)
        if phone_exists:
            self.record_application_context(number_result.phone, records_after, source="run")
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

    def run_rent_number(self) -> int:
        self.last_run_restartable = False
        self.log_and_print("[租号流程] 1. 读取配置完成，开始查询余额")
        try:
            balance_text = self.get_balance()
        except Exception as error:
            self.log_and_print(f"[租号错误] 查询余额失败: {error}", logging.ERROR)
            return 1

        self.log_and_print(f"[租号余额] {balance_text}")
        self.log_and_print("[租号流程] 2. 获取租号前活动激活列表")
        records_before: list[dict] = []
        try:
            records_before = self.get_active_records(limit=self.config.active_limit)
            self.print_and_record_active_records(records_before, source="租号前活动列表")
        except Exception as error:
            self.log_and_print(f"[租号警告] 查询租号前活动激活列表失败: {error}", logging.WARNING)

        duration_levels = self.config.rent_duration_levels or (self.config.rent_duration,)
        self.log_and_print("[租号接口说明] getRentNumber 用于申请租赁号码；duration 是租号时长，不用于普通 getNumberV2。")
        self.log_and_print(f"[租号时长档位] {list(duration_levels)}")
        for index, duration in enumerate(duration_levels, start=1):
            request_params = self.build_rent_number_request_params(duration=duration)
            self.log_and_print(f"[租号请求体预览] #{index}/{len(duration_levels)} duration={duration}")
            self.log_and_print(json.dumps(request_params, ensure_ascii=False, indent=2))
        if not self.config.send:
            self.log_and_print("[租号模式] dry-run，未实际发送 getRentNumber 请求。加 --send 才会真实租号。")
            return 0

        records_after: list[dict] = []
        payload: object | None = None
        rent_phone = ""
        selected_duration: int | None = None
        for index, duration in enumerate(duration_levels, start=1):
            self.log_and_print(f"[租号分段] #{index}/{len(duration_levels)} 尝试 duration={duration}")
            try:
                status_code, current_payload = self.request_rent_number(duration=duration)
            except Exception as error:
                self.log_and_print(f"[租号请求失败] duration={duration} error={error}", logging.WARNING)
                continue

            self.log_and_print(f"[租号HTTP状态] duration={duration} HTTP={status_code}")
            print_response_payload(current_payload)
            if status_code != 200:
                self.log_and_print(f"[租号分段失败] duration={duration} HTTP={status_code}，继续尝试下一档", logging.WARNING)
                continue

            self.log_and_print(f"[租号分段] duration={duration} 获得 HTTP 200，开始确认是否实际获得号码")
            current_records_after: list[dict] = []
            try:
                current_records_after = self.get_active_records(limit=self.config.active_limit)
                self.print_and_record_active_records(current_records_after, source="租号后活动列表")
            except Exception as error:
                self.log_and_print(f"[租号警告] 查询活动激活列表失败: {error}", logging.WARNING)

            current_phone = self.extract_phone_number(current_payload)
            if current_phone:
                self.log_and_print(f"[租号确认准备] 响应中提取到电话号码 {self._display_phone(current_phone)}")
            else:
                current_phone = self.infer_phone_from_new_active_record(records_before, current_records_after)
                if current_phone:
                    self.log_and_print(
                        f"[租号确认准备] 响应未直接返回号码，已从活动列表新增记录推断电话号码 {self._display_phone(current_phone)}"
                    )

            if current_phone:
                payload = current_payload
                records_after = current_records_after
                rent_phone = current_phone
                selected_duration = duration
                self.log_and_print(f"[租号分段成功] duration={duration} 已确认获得号码 {self._display_phone(rent_phone)}")
                break
            self.log_and_print(
                f"[租号分段失败] duration={duration} HTTP=200 但未确认获得号码，继续尝试下一档",
                logging.WARNING,
            )

        if payload is None or selected_duration is None:
            self.last_run_restartable = True
            self.log_and_print("[租号失败] 所有时长档位均未获得成功响应", logging.ERROR)
            return 1

        self.log_and_print(f"[租号流程] 3. 已完成租号后活动激活列表快照，成功时长={selected_duration}")

        if rent_phone and records_after:
            phone_exists = self.phone_exists_in_records(rent_phone, records_after)
            self.notify_phone_active_presence(rent_phone, phone_exists)
            if phone_exists:
                self.record_application_context(rent_phone, records_after, duration_hours=selected_duration, source="rent-run")
                self.log_and_print(f"[租号确认] 电话号码 {self._display_phone(rent_phone)} 存在于活动激活列表")
            else:
                self.log_and_print(
                    f"[租号警告] 电话号码 {self._display_phone(rent_phone)} 不存在于活动激活列表，退出",
                    logging.WARNING,
                )
                return 1
        elif rent_phone:
            self.log_and_print(
                f"[租号警告] 已获得电话号码 {self._display_phone(rent_phone)}，但活动激活列表为空或查询失败，无法确认是否存在",
                logging.WARNING,
            )
        else:
            self.log_and_print("[租号警告] 响应和活动列表中未能确定本次租号号码，跳过号码存在性确认", logging.WARNING)

        self.log_and_print("[租号流程] 4. 轮询活动激活列表")
        self.poll_active_list()

        self.log_and_print("[租号流程] 5. 用户输入模式轮询")
        try:
            self.user_input_loop()
        except UserInputExit:
            self.log_and_print("[退出] 用户输入 99，退出租号用户输入轮询")

        self.log_and_print("[租号流程] 6. 查询用户历史并退出")
        try:
            self.print_history()
        except Exception as error:
            self.log_and_print(f"[租号历史记录] 查询失败: {error}", logging.WARNING)
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
    parser.add_argument("run", nargs="?", default="run", choices=("run", "rent-run"), help="执行模式：run 普通号码；rent-run 租号")
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
    parser.add_argument("--run-loop", action="store_true", help="号码申请失败时重新读取 env 并从头重跑整个流程")
    parser.add_argument("--country", type=int, default=16, help="租号国家 ID，rent-run 默认 16")
    parser.add_argument(
        "--duration",
        type=parse_duration_arg,
        default=(2,),
        help="租号时长小时数，仅 rent-run 使用；支持 2、4、24x2 或 24*2 多档，单档最大 168",
    )
    parser.add_argument("--operator", default="any", help="租号商家/运营商，仅 rent-run 使用，默认 any")
    parser.add_argument("--cost", default=None, help="租号价格参数，仅 rent-run 使用；提供后传给接口")
    parser.add_argument("--currency", default=None, help="租号币种参数，仅 rent-run 使用")
    parser.add_argument("--ref", default=None, help="租号 ref 参数，仅 rent-run 使用")
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


def execute_workflow(args: argparse.Namespace) -> int:
    attempt = 1
    while True:
        load_dotenv(PROJECT_DIR / ".env", override=True)
        try:
            config = WorkflowConfig.from_args(args)
        except ValueError as error:
            print(f"[错误] 配置解析失败: {error}")
            return 2

        logger = setup_logging(config.log_dir)
        workflow = HeroSMSWorkflow(config=config, logger=logger)
        if config.command == "rent-run":
            result = workflow.run_rent_number()
        else:
            result = workflow.run()
        if result == 0:
            return 0
        if not config.run_loop or not workflow.last_run_restartable:
            return result
        attempt += 1
        print(f"[大循环] 第{attempt}轮重新读取 env 并从头执行")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return execute_workflow(args)


if __name__ == "__main__":
    raise SystemExit(main())
