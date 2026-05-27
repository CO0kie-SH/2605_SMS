import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from herosms_tool import (
    HeroSMSWorkflow,
    NumberRequestResult,
    UserInputExit,
    UserInputState,
    WorkflowConfig,
    parse_args,
    parse_balance_value,
    parse_float_levels,
)


def test_parse_args_cli_values_override_environment_defaults():
    args = parse_args([
        "run",
        "--api-key", "cli-key",
        "--base-url", "https://example.test/api",
        "--max-price", "0.02",
        "--service", "dr",
        "--merchant-seed", "7",
        "--retry-limit", "9",
    ])
    env = {
        "HEROSMS_API_KEY": "env-key",
        "HEROSMS_BASE_URL": "https://env.test/api",
        "HEROSMS_MAX_PRICE": "0.03",
    }

    config = WorkflowConfig.from_args(args, env=env)

    assert config.api_key == "cli-key"
    assert config.base_url == "https://example.test/api"
    assert config.max_price == 0.02
    assert config.service == "dr"
    assert config.merchant_seed == 7
    assert config.retry_limit == 9


def test_parse_args_uses_environment_when_cli_omits_values():
    args = parse_args(["run"])
    env = {
        "HEROSMS_API_KEY": "env-key",
        "HEROSMS_BASE_URL": "https://env.test/api",
        "HEROSMS_MAX_PRICE": "0.03",
    }

    config = WorkflowConfig.from_args(args, env=env)

    assert config.api_key == "env-key"
    assert config.base_url == "https://env.test/api"
    assert config.max_price == 0.03


def test_parse_args_supports_max_price_levels():
    args = parse_args(["run", "--max-price", "0.025-0.03-0.035"])

    config = WorkflowConfig.from_args(args, env={})

    assert config.max_price_levels == (0.025, 0.03, 0.035)
    assert config.max_price == 0.035


def test_parse_float_levels_ignores_empty_parts():
    assert parse_float_levels("0.025--0.03") == (0.025, 0.03)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("ACCESS_BALANCE:1.5421", 1.5421), ("BAD", None), ("ACCESS_BALANCE:not-number", None)],
)
def test_parse_balance_value(text, expected):
    assert parse_balance_value(text) == expected


def test_run_exits_when_balance_is_lower_than_max_price():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k", max_price=2.0), logger=logging.getLogger("test"))
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"

    assert workflow.run() == 1


def test_run_exits_in_single_thread_mode_when_active_list_is_not_empty(monkeypatch):
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k", max_price=0.5), logger=logging.getLogger("test"))
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    workflow.get_active_records = lambda limit=100: [{"activationId": "123"}]
    called = {"loop": False}

    def fake_loop(initial_records=None, prompt=None):
        called["loop"] = True
        assert initial_records == [{"activationId": "123"}]
        raise UserInputExit

    workflow.user_input_loop = fake_loop

    assert workflow.run() == 1
    assert called["loop"]


def test_select_merchant_sorts_by_price_and_seed_is_deterministic():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k", merchant_seed=0), logger=logging.getLogger("test"))
    merchants = [
        {"price": 0.03, "operator": "c", "country": 3},
        {"price": 0.01, "operator": "a", "country": 1},
        {"price": 0.02, "operator": "b", "country": 2},
    ]

    selected = workflow.select_merchant(merchants)

    assert selected in merchants
    assert [item["price"] for item in workflow.sort_merchants(merchants)] == [0.01, 0.02, 0.03]


def test_run_retries_non_200_and_continues_until_success():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True, retry_limit=5),
        logger=logging.getLogger("test"),
    )
    merchants = [
        {"service": "dr", "country": 1, "operator": "bad", "maxPrice": 0.5, "price": 0.01, "count": 1},
        {"service": "dr", "country": 2, "operator": "ok", "maxPrice": 0.5, "price": 0.02, "count": 1},
    ]
    calls = []
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    active_calls = {"count": 0}

    def fake_active(limit=100):
        active_calls["count"] += 1
        if active_calls["count"] == 1:
            return []
        return [{"activationId": "a1", "phoneNumber": "5550001"}]

    workflow.get_active_records = fake_active
    workflow.build_merchants = lambda: merchants

    def fake_request(merchant):
        calls.append(merchant["operator"])
        if merchant["operator"] == "bad":
            return 404, {"error": "not found"}
        return 200, {"phoneNumber": "5550001", "activationId": "a1"}

    workflow.request_number = fake_request
    workflow.poll_balance_change = lambda before_balance: None
    workflow.poll_active_list = lambda: None
    workflow.user_input_loop = lambda initial_records=None, prompt=None: None
    workflow.print_history = lambda: None

    assert workflow.run() == 0
    assert calls == ["bad", "ok"]


def test_run_accumulates_retry_count_when_no_merchants_then_exits(monkeypatch):
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True, retry_limit=2),
        logger=logging.getLogger("test"),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    workflow.get_active_records = lambda limit=100: []
    build_calls = {"count": 0}

    def fake_build_merchants():
        build_calls["count"] += 1
        return []

    workflow.build_merchants = fake_build_merchants

    assert workflow.run() == 1
    assert build_calls["count"] == 3


def test_run_accumulates_retry_count_across_merchant_and_number_failures():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True, retry_limit=1),
        logger=logging.getLogger("test"),
    )
    merchants = [
        {"service": "dr", "country": 1, "operator": "bad1", "maxPrice": 0.5, "price": 0.01, "count": 1},
        {"service": "dr", "country": 2, "operator": "bad2", "maxPrice": 0.5, "price": 0.02, "count": 1},
    ]
    calls = []
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    workflow.get_active_records = lambda limit=100: []
    workflow.build_merchants = lambda: merchants

    def fake_request(merchant):
        calls.append(merchant["operator"])
        return 500, {"error": "temporary"}

    workflow.request_number = fake_request

    assert workflow.run() == 1
    assert calls == ["bad1", "bad2"]


def test_run_resets_retry_count_after_number_success():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True, retry_limit=1),
        logger=logging.getLogger("test"),
    )
    merchants = [
        {"service": "dr", "country": 1, "operator": "bad", "maxPrice": 0.5, "price": 0.01, "count": 1},
        {"service": "dr", "country": 2, "operator": "ok", "maxPrice": 0.5, "price": 0.02, "count": 1},
    ]
    calls = []
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    active_calls = {"count": 0}

    def fake_active(limit=100):
        active_calls["count"] += 1
        if active_calls["count"] == 1:
            return []
        return [{"activationId": "a1", "phoneNumber": "5550001"}]

    workflow.get_active_records = fake_active
    workflow.build_merchants = lambda: merchants

    def fake_request(merchant):
        calls.append(merchant["operator"])
        if merchant["operator"] == "bad":
            return 500, {"error": "temporary"}
        return 200, {"phoneNumber": "5550001", "activationId": "a1"}

    workflow.request_number = fake_request
    workflow.poll_balance_change = lambda before_balance: None
    workflow.poll_active_list = lambda: None
    workflow.user_input_loop = lambda initial_records=None, prompt=None: None
    workflow.print_history = lambda: None

    assert workflow.run() == 0
    assert calls == ["bad", "ok"]


def test_run_logs_affordable_count_from_balance_and_max_price():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.25),
        logger=logging.getLogger("test"),
    )
    messages = []
    workflow.log_and_print = lambda message, level=logging.INFO: messages.append(message)
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.10"
    workflow.get_active_records = lambda limit=100: []
    workflow.obtain_number_with_retry = lambda: NumberRequestResult(dry_run=True)

    assert workflow.run() == 0
    assert any("[可购买次数估算] 当前余额=1.1 maxPrice=0.25 可支持约 4 次" in message for message in messages)


def test_obtain_number_attempts_each_merchant_once_without_fixed_retry_limit():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", send=True, retry_limit=99),
        logger=logging.getLogger("test"),
    )
    merchants = [
        {"service": "dr", "country": index, "operator": f"op{index}", "price": 0.01, "count": 1}
        for index in range(3)
    ]
    calls = []
    workflow.request_number = lambda merchant: calls.append(merchant["operator"]) or (500, {"error": "temporary"})

    result = workflow.obtain_number_with_retry(lambda: merchants)

    assert result is None
    assert calls == ["op0", "op1", "op2"]


def test_obtain_number_tries_price_levels_until_success():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", send=True, max_price=0.035, max_price_levels=(0.025, 0.03, 0.035)),
        logger=logging.getLogger("test"),
    )
    built_prices = []
    calls = []

    def fake_build(max_price):
        built_prices.append(max_price)
        if max_price == 0.025:
            return [
                {"service": "dr", "country": 1, "operator": "a", "price": 0.025, "count": 1},
                {"service": "dr", "country": 2, "operator": "b", "price": 0.025, "count": 1},
            ]
        if max_price == 0.03:
            return [
                {"service": "dr", "country": 3, "operator": "c", "price": 0.03, "count": 1},
            ]
        return [
            {"service": "dr", "country": 4, "operator": "d", "price": 0.035, "count": 1},
        ]

    def fake_request(merchant):
        calls.append(merchant["operator"])
        if merchant["operator"] == "c":
            return 200, {"phoneNumber": "5550001"}
        return 500, {"error": "temporary"}

    workflow.build_merchants_for_max_price = fake_build
    workflow.request_number = fake_request

    result = workflow.obtain_number_with_retry()

    assert result is not None
    assert result.phone == "5550001"
    assert built_prices == [0.025, 0.03]
    assert calls == ["a", "b", "c"]


def test_obtain_number_treats_http_200_without_phone_as_failure():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", send=True),
        logger=logging.getLogger("test"),
    )
    merchants = [
        {"service": "dr", "country": 16, "operator": "airtel", "price": 0.03, "count": 1},
        {"service": "dr", "country": 16, "operator": "vodafone", "price": 0.03, "count": 1},
    ]
    calls = []

    def fake_request(merchant):
        calls.append(merchant["operator"])
        if merchant["operator"] == "airtel":
            return 200, "NO_NUMBERS"
        return 200, {"phoneNumber": "5550002"}

    workflow.request_number = fake_request

    result = workflow.obtain_number_with_retry(lambda: merchants)

    assert result is not None
    assert result.phone == "5550002"
    assert calls == ["airtel", "vodafone"]


def test_phone_presence_checks_common_fields():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))

    assert workflow.phone_exists_in_records("+15550001", [{"phoneNumber": "15550001"}])
    assert workflow.phone_exists_in_records("5550002", [{"phone": "+15550002"}])
    assert not workflow.phone_exists_in_records("5550003", [{"phoneNumber": "5550004"}])


def test_user_input_zero_prints_active_list():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    records = [{"activationId": "a1"}]
    calls = []
    workflow.get_active_records = lambda limit=100: records
    workflow.print_active_records = lambda input_records: calls.append(input_records)

    state = workflow.handle_user_input("0", UserInputState())

    assert state.mode is None
    assert calls == [records]


def test_user_input_six_enters_finish_mode_and_targets_first_record():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    records = [{"activationId": "first"}, {"activationId": "second"}]
    refreshed_records = [{"activationId": "after"}]
    requested = []
    printed = []
    workflow.get_active_records = lambda limit=100: records
    workflow.print_active_records = lambda input_records: printed.append(input_records)
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    state = workflow.handle_user_input("6", UserInputState())
    assert state.mode == 6
    assert state.records == records

    workflow.get_active_records = lambda limit=100: refreshed_records
    state = workflow.handle_user_input("6-1", state)
    assert requested == [("first", 6)]
    assert printed[-1] == refreshed_records
    assert state.mode is None


def test_user_input_three_enters_resend_mode_and_targets_first_record():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    records = [{"activationId": "first"}]
    refreshed_records = [{"activationId": "after"}]
    requested = []
    printed = []
    workflow.get_active_records = lambda limit=100: records
    workflow.print_active_records = lambda input_records: printed.append(input_records)
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    state = workflow.handle_user_input("3", UserInputState())
    assert state.mode == 3
    assert state.records == records

    workflow.get_active_records = lambda limit=100: refreshed_records
    state = workflow.handle_user_input("3-1", state)
    assert requested == [("first", 3)]
    assert printed[-1] == refreshed_records
    assert state.mode is None


def test_user_input_eight_enters_refund_mode_and_targets_first_record():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    records = [{"activationId": "first"}]
    refreshed_records = [{"activationId": "after"}]
    requested = []
    printed = []
    workflow.get_active_records = lambda limit=100: records
    workflow.print_active_records = lambda input_records: printed.append(input_records)
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    state = workflow.handle_user_input("8", UserInputState())
    assert state.mode == 8
    assert state.records == records

    workflow.get_active_records = lambda limit=100: refreshed_records
    state = workflow.handle_user_input("8-1", state)
    assert requested == [("first", 8)]
    assert printed[-1] == refreshed_records
    assert state.mode is None


def test_user_input_nine_refunds_empty_list_then_requests_replacement():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True),
        logger=logging.getLogger("test"),
    )
    original_records = [
        {"activationId": "first", "serviceCode": "dr", "countryCode": "16"},
    ]
    requested_statuses = []
    requested_numbers = []
    printed = []
    polled = {"active": False}
    active_after_new_number = [{"activationId": "new", "phoneNumber": "5550009"}]
    active_responses = [original_records, [], active_after_new_number]

    workflow.get_active_records = lambda limit=100: active_responses.pop(0)
    workflow.print_active_records = lambda input_records: printed.append(input_records)
    workflow.set_activation_status = lambda activation_id, status: requested_statuses.append((activation_id, status))
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    workflow.build_replacement_merchant_from_record = lambda record: {"service": "dr", "country": 16, "operator": "", "price": 0.1, "count": 1}
    workflow.obtain_number_with_retry = lambda provider=None: requested_numbers.append(provider()[0]) or NumberRequestResult(
        payload={"phoneNumber": "5550009"},
        phone="5550009",
    )
    workflow.poll_balance_change = lambda before_balance: None
    workflow.poll_active_list = lambda: polled.__setitem__("active", True)

    state = workflow.handle_user_input("9-1", UserInputState())

    assert requested_statuses == [("first", 8)]
    assert requested_numbers == [{"service": "dr", "country": 16, "operator": "", "price": 0.1, "count": 1}]
    assert printed[0] == original_records
    assert printed[1] == []
    assert polled["active"]
    assert state.records == active_after_new_number


def test_user_input_nine_completes_record_with_sms_payload():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True),
        logger=logging.getLogger("test"),
    )
    original_records = [
        {"activationId": "first", "serviceCode": "dr", "countryCode": "16", "smsCode": "123456"},
    ]
    requested_statuses = []
    active_responses = [original_records, [], [{"activationId": "new", "phoneNumber": "5550010"}]]
    polled = {"active": False}

    workflow.get_active_records = lambda limit=100: active_responses.pop(0)
    workflow.print_active_records = lambda input_records: None
    workflow.set_activation_status = lambda activation_id, status: requested_statuses.append((activation_id, status))
    workflow.get_balance = lambda: "ACCESS_BALANCE:1.0"
    workflow.build_replacement_merchant_from_record = lambda record: {"service": "dr", "country": 16, "operator": "", "price": 0.1, "count": 1}
    workflow.obtain_number_with_retry = lambda provider=None: NumberRequestResult(
        payload={"phoneNumber": "5550010"},
        phone="5550010",
    )
    workflow.poll_balance_change = lambda before_balance: None
    workflow.poll_active_list = lambda: polled.__setitem__("active", True)

    workflow.handle_user_input("9-1", UserInputState())

    assert requested_statuses == [("first", 6)]
    assert polled["active"]


def test_user_input_nine_stops_when_active_list_is_not_empty_after_status():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k", send=True), logger=logging.getLogger("test"))
    original_records = [{"activationId": "first", "serviceCode": "dr", "countryCode": "16"}]
    still_active = [{"activationId": "other"}]
    active_responses = [original_records, still_active]
    requested_numbers = []

    workflow.get_active_records = lambda limit=100: active_responses.pop(0)
    workflow.print_active_records = lambda input_records: None
    workflow.set_activation_status = lambda activation_id, status: None
    workflow.obtain_number_with_retry = lambda provider=None: requested_numbers.append(True)

    state = workflow.handle_user_input("9-1", UserInputState())

    assert requested_numbers == []
    assert state.records == still_active


def test_user_input_eight_refuses_record_with_sms_payload():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    requested = []
    state = UserInputState(mode=8, records=[{"activationId": "first", "smsCode": "123456"}])
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    next_state = workflow.handle_user_input("8-1", state)

    assert next_state == state
    assert requested == []


def test_user_input_eight_refuses_record_with_sms_text():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    requested = []
    state = UserInputState(mode=8, records=[{"activationId": "first", "smsText": "验证码 123456"}])
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    next_state = workflow.handle_user_input("8-1", state)

    assert next_state == state
    assert requested == []


def test_user_input_rejects_wrong_mode_prefix():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    requested = []
    state = UserInputState(mode=6, records=[{"activationId": "first"}])
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    next_state = workflow.handle_user_input("8-1", state)

    assert next_state == state
    assert requested == []


def test_user_input_99_raises_exit():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))

    with pytest.raises(UserInputExit):
        workflow.handle_user_input("99", UserInputState())


def test_user_input_loop_accepts_initial_records_and_exit(monkeypatch):
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", input_poll_times=1, input_poll_interval=0),
        logger=logging.getLogger("test"),
    )
    shown = []
    finalized = {"called": False}
    workflow.print_active_records = lambda records: shown.append(records)
    workflow.read_user_input_with_timeout = lambda timeout: "99"
    workflow.finalize_after_input_timeout = lambda: finalized.__setitem__("called", True)

    with pytest.raises(UserInputExit):
        workflow.user_input_loop(initial_records=[{"activationId": "a1"}])

    assert shown == [[{"activationId": "a1"}]]
    assert not finalized["called"]


def test_user_input_loop_auto_refunds_single_record_without_sms_after_timeout():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", input_poll_times=1, input_poll_interval=0),
        logger=logging.getLogger("test"),
    )
    active_records = [[{"activationId": "a1"}], []]
    requested = []
    printed = []
    workflow.read_user_input_with_timeout = lambda timeout: None
    workflow.get_active_records = lambda limit=100: active_records.pop(0)
    workflow.print_active_records = lambda records: printed.append(records)
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    workflow.user_input_loop()

    assert requested == [("a1", 8)]
    assert printed == [[{"activationId": "a1"}], []]


def test_finalize_after_input_timeout_does_not_refund_record_with_sms():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    requested = []
    workflow.get_active_records = lambda limit=100: [{"activationId": "a1", "smsCode": "123456"}]
    workflow.print_active_records = lambda records: None
    workflow.set_activation_status = lambda activation_id, status: requested.append((activation_id, status))

    workflow.finalize_after_input_timeout()

    assert requested == []


def test_user_input_99_queries_history_before_exit():
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="k"), logger=logging.getLogger("test"))
    called = {"history": False}
    workflow.print_history = lambda: called.__setitem__("history", True)

    with pytest.raises(UserInputExit):
        workflow.handle_user_input("99", UserInputState())

    assert called["history"]


def test_print_history_prints_request_url_and_mode(monkeypatch):
    workflow = HeroSMSWorkflow(WorkflowConfig(api_key="secret-key"), logger=logging.getLogger("test"))
    messages = []
    workflow.log_and_print = lambda message, level=logging.INFO: messages.append(message)

    class Response:
        status_code = 200
        url = "https://hero-sms.com/stubs/handler_api.php?api_key=secret-key&action=getHistory"

        def raise_for_status(self):
            return None

        def json(self):
            return []

    workflow.api_get = lambda action: Response()

    workflow.print_history()

    assert any("[历史记录模式] getHistory" in message for message in messages)
    assert any("api_key=secr...-key" in message for message in messages)
