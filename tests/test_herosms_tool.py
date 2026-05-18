import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from herosms_tool import HeroSMSWorkflow, UserInputExit, UserInputState, WorkflowConfig, parse_args, parse_balance_value


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


def test_run_accumulates_retry_count_when_no_merchants_then_exits():
    workflow = HeroSMSWorkflow(
        WorkflowConfig(api_key="k", max_price=0.5, send=True, retry_limit=2),
        logger=logging.getLogger("test"),
    )
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
    workflow.print_active_records = lambda records: shown.append(records)
    monkeypatch.setattr("select.select", lambda *_args, **_kwargs: ([object()], [], []))
    monkeypatch.setattr("sys.stdin.readline", lambda: "99\n")

    with pytest.raises(UserInputExit):
        workflow.user_input_loop(initial_records=[{"activationId": "a1"}])

    assert shown == [[{"activationId": "a1"}]]


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
