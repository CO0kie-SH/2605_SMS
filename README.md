# HeroSMS Python 调试工具集

> 当前版本：`26.5.26A`  
> 最后更新：`2026-05-26`  
> 项目定位：合法合规地调试 HeroSMS / SMS-Activate 风格 API，重点用于观察余额、价格、库存、号码请求、活动激活状态、状态变更与完整工作流。

---

## 项目简介

本项目围绕 HeroSMS 的 `handler_api.php` 接口，整理了一组 Python 调试脚本与一个统一工作流入口，用于逐步验证虚拟号码请求链路。

当前重点不是全自动注册，而是把 API 调试过程做清楚、做稳、做可复现：

- 查询余额、服务、国家价格、运营商价格
- 根据价格和库存生成 `getNumberV2` 候选
- 按价格优先选择商户，支持固定随机种子复现抽样
- 默认 dry-run，只在显式传入 `--send` 时真实请求号码
- 请求前后观察余额变化
- 请求前后观察活动激活列表
- 验证成功号码是否真实进入活动激活列表
- 支持按 `0.025-0.03-0.035` 这类多级价格逐档查询和申请号码
- 支持交互式将激活状态改为 `3`（请求重发短信）、`6`（完成）或 `8`（退款）
- 支持本地组合 `9` 模式：处理旧激活、确认列表清空、按原服务/国家/商家校验价格并重新申请号码
- 支持查询历史记录与记录日志

---

## 版本管理

版本号格式采用：`YY.M.D + 字母序号`。

示例：

| 版本 | 日期 | 说明 |
|---|---|---|
| `26.5.26A` | 2026-05-26 | 新增多级价格阶梯，`--max-price` / `HEROSMS_MAX_PRICE` 支持 `0.025-0.03-0.035` 格式并按档位逐级查询商户和申请号码；号码申请次数改为按当前商户候选数量自适应；余额查询阶段输出按最高价格估算的可购买次数；`service=dr` 候选生成加入 `country=4` 黑名单；用户输入轮询自然结束后新增自动收尾检查，唯一未收到验证码的活动记录会自动执行 `status=8`；同步 `.env.example`、README 与测试 |
| `26.5.23A` | 2026-05-23 | 修复 Windows 下 `select.select(sys.stdin)` 导致的 `WinError 10038`；新增 `3/3-序号` 请求重发短信模式；新增本地组合 `9/9-序号` 处理后重开模式，并在成功申请新号后进入活动列表 25 次轮询；抽取号码申请重试循环供主流程和 9 模式复用；用户输入轮询默认次数从 100 调整为 50；补充 README 与单元测试覆盖 |
| `26.5.18A` | 2026-05-18 | 新增 `herosms_tool.py` 统一工作流入口、测试目录与日志目录；成功号码展示统一补 `+` 前缀；无商户候选时增加 5 秒等待后重试；补全文档中的全部参数说明 |
| `26.5.16A` | 2026-05-16 | 整合分散 API 脚本为类式统一入口，使用 `WorkflowConfig + HeroSMSWorkflow + UserInputState` 完成完整流程编排；打通余额、活动激活列表、商户候选、号码请求、状态变更、用户输入轮询与历史记录查询 |
| `26.5.10B` | 2026-05-10 | 优化 getNumberV2 请求/响应日志、非 200 重试流程与 setStatus 防误退款保护 |
| `26.5.10A` | 2026-05-10 | 今日主版本，加入活动激活列表、状态变更、活动快照、文档整理 |
| `26.5.9A` | 2026-05-09 | 初始调试链路版本，完成服务、价格、候选、请求与余额观察 |

同一天多次重要迭代时，后缀按 `A / B / C` 递增。

---

## 环境要求

- Python：`3.11+`
- 当前项目依赖文件：`requirements.txt`
- 主要依赖：
  - `requests==2.32.3`
  - `python-dotenv==1.1.0`
- 测试环境：项目内置 `.venv` 与 `pytest`

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
```

---

## 环境变量

`.env` 示例：

```env
HEROSMS_API_KEY=你的API密钥
HEROSMS_BASE_URL=https://hero-sms.com/stubs/handler_api.php
HEROSMS_MAX_PRICE=0.025-0.03-0.035
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `HEROSMS_API_KEY` | 是 | HeroSMS API 密钥 |
| `HEROSMS_BASE_URL` | 否 | API 地址，默认 `https://hero-sms.com/stubs/handler_api.php` |
| `HEROSMS_MAX_PRICE` | 否 | 最高价格限制；`herosms_tool.py` 支持 `0.025-0.03-0.035` 多级价格 |

优先级说明：

- `herosms_tool.py` 中，`--api-key` / `--base-url` / `--max-price` 的优先级高于环境变量。
- 其他脚本主要直接读取 `.env`。

---

## 项目结构

```text
2605_SMS/
├── README.md
├── requirements.txt
├── .env                         # 本地凭据（不应提交）
├── get_balance.py               # 查询余额
├── get_services.py              # 获取服务清单
├── get_service_coverage.py      # 查询服务支持国家与运营商
├── get_prices.py                # 查询国家价格并生成候选
├── get_operator_prices.py       # 查询运营商价格
├── get_number_v2.py             # 单次请求号码调试脚本
├── get_active_activations.py    # 活动激活列表与 setStatus
├── get_history.py               # 查询历史记录
├── herosms_tool.py              # 统一工作流入口（推荐主入口）
├── tests/
│   └── test_herosms_tool.py     # herosms_tool 单元测试
├── log/                         # 运行日志目录
└── __pycache__/                 # Python 缓存目录
```

说明：

- `herosms_tool.py`、`tests/`、`log/` 目前还是未跟踪文件，属于本地新增内容。
- `.venv/`、`__pycache__/`、`log/` 建议后续通过 `.gitignore` 管理。

---

## 推荐入口：herosms_tool.py

`herosms_tool.py` 是当前项目最完整、最安全的统一入口。

完整流程：

1. 读取命令行与环境变量配置
2. 查询余额
3. 查询活动激活列表
4. 单线程模式下，如果已有活动激活则先要求处理
5. 查询服务国家与商户，生成候选
6. 请求号码，非 200 自动换候选重试
7. 成功后轮询余额变化
8. 获取活动激活列表并确认成功号码存在
9. 轮询活动列表
10. 进入用户输入轮询（0 / 3 / 6 / 8 / 9 / 3-序号 / 6-序号 / 8-序号 / 9-序号 / 99）
11. 最后查询历史记录

默认安全策略：

- 默认 **dry-run**，不会真实发送号码请求
- 只有显式加 `--send` 才会发起真实 `getNumberV2`
- `9-序号` 会先处理旧激活再申请新号码，因此也必须显式加 `--send`
- 默认是单线程保护模式，活动列表非空时会阻止继续请求新号码
- `status=8` 退款时，如果记录中已有 `smsCode` 或 `smsText`，会拒绝退款

### 今日任务梳理（26.5.26A）

本版本围绕价格控制、候选过滤、轮询结束收尾和测试覆盖做了以下更新：

1. 新增多级价格阶梯：`--max-price` 与 `HEROSMS_MAX_PRICE` 都支持 `0.025-0.03-0.035` 格式。
2. 多级价格执行时会按顺序重新查询商户列表；每个价格档只尝试该档返回的商户数量，成功后立即停止，不再进入后续价格档。
3. 单价格模式下，号码申请次数也改为按当前商户候选数量自适应，每个商户最多尝试一次。
4. 余额查询阶段新增 `[可购买次数估算]` 输出，按当前余额除以最高价格取整，估算还能支持多少次号码申请。
5. 候选生成新增服务国家黑名单：当前 `service=dr` 会过滤 `country=4`。
6. 用户输入轮询自然跑满默认 `50` 次后会执行自动收尾检查：如果活动列表恰好 1 条且没有 `smsCode` / `smsText`，自动执行 `status=8`，然后继续后续历史查询。
7. `.env.example` 和 README 的 `HEROSMS_MAX_PRICE` 示例同步为多级价格格式。
8. 补充多级价格、自适应商户尝试、余额次数估算、`dr` 国家黑名单和自动收尾相关测试。

### 类设计说明（26.5.16A 整体流程版）

`herosms_tool.py` 的核心不是简单把多个脚本拼在一起，而是把完整流程收敛成一个可配置、可测试、可复用的类式工作流。

#### 设计分层

1. **辅助函数层**
   - `parse_balance_value()`：解析 `ACCESS_BALANCE:` 文本余额
   - `parse_float()`：统一解析命令行 / 环境变量中的浮点配置
   - `parse_float_levels()`：解析 `0.025-0.03-0.035` 这类多级价格配置

2. **状态与配置层**
   - `WorkflowConfig`：统一承载全部运行参数，负责把命令行参数和环境变量合并成不可变配置对象
   - `UserInputState`：承载用户输入模式状态，记录当前是否处于 `3/6/8/9` 模式以及对应活动记录
   - `UserInputExit`：用于 `99` 主动退出流程，避免在多层调用中混乱返回

3. **工作流执行层**
   - `HeroSMSWorkflow`：统一封装完整业务流程
   - 所有 API 调用、商户构造、号码请求、余额轮询、活动激活轮询、`setStatus`、历史记录查询、用户输入处理，都收敛在这个类里

#### HeroSMSWorkflow 的职责划分

- **基础能力**
  - `api_get()`：统一发起 API 请求
  - `log_and_print()`：统一终端输出与日志落盘
  - `mask_secret_in_url()`：输出请求 URL 时隐藏密钥

- **号码获取相关**
  - `build_merchants()`：基于已有价格脚本生成候选
  - `build_merchants_for_max_price()`：按指定价格档重新生成候选
  - `sort_merchants()`：按价格、库存、国家、运营商排序
  - `select_merchant()`：支持默认最低价优先，或通过 `merchant_seed` 固定抽样
  - `request_number()`：统一执行 `getNumberV2` 请求
  - `obtain_number_with_retry()`：统一执行多级价格、商户候选获取、号码请求、非 200 换候选重试和 dry-run 输出

- **成功校验与轮询**
  - `poll_balance_change()`：请求成功后轮询余额变化
  - `extract_phone_number()`：兼容多种响应结构提取号码
  - `phone_exists_in_records()`：确认返回号码是否真正出现在活动激活列表中
  - `poll_active_list()`：持续轮询活动激活列表

- **活动状态操作**
  - `set_activation_status()`：统一发送 `setStatus`
  - `get_sms_payload_fields()`：识别短信是否已到达
  - 当记录已有 `smsCode` / `smsText` 时，禁止进入 `8` 退款模式

- **交互控制**
  - `handle_user_input()`：处理 `0`、`3`、`6`、`8`、`9`、`3-1`、`6-1`、`8-1`、`9-1`、`99`
  - `handle_mode_9_by_index()`：执行本地组合 9 模式，处理旧激活、校验活动列表、价格和库存，并复用号码申请重试循环
  - `user_input_loop()`：做轮询式交互输入，Windows 下使用 `msvcrt`，其他平台使用 `select.select()`
  - `finalize_after_input_timeout()`：用户输入轮询结束后自动检查唯一未收到短信的活动记录并执行 `status=8`
  - `print_history()`：统一查询历史记录

- **总流程入口**
  - `run()`：按固定阶段组织整个工作流，是项目当前最完整的业务主线

#### 这个类式设计相对旧脚本的提升

- 不再依赖多个脚本之间人工切换，统一由一个主入口编排
- 命令行参数、环境变量、日志、轮询、状态变更都集中管理
- 把“查询余额 → 检查活动列表 → 选商户 → 请求号码 → 校验成功 → 交互处理 → 查询历史”串成一个完整闭环
- 更适合单元测试，当前 `tests/test_herosms_tool.py` 已覆盖主要流程分支
- 后续继续扩展配置项或重试策略时，只需要在 `WorkflowConfig` 和 `HeroSMSWorkflow` 上演进

### herosms_tool.py 全部参数

命令格式：

```bash
python3 herosms_tool.py run [参数...]
```

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `run` | `run` | 占位执行参数，默认可省略 |
| `--api-key` | `None` | HeroSMS API Key，优先级高于 `HEROSMS_API_KEY` |
| `--base-url` | `None` | API 地址，优先级高于 `HEROSMS_BASE_URL` |
| `-s`, `--service` | `dr` | 服务代码 |
| `--max-price` | `None` | 最高价格，优先级高于 `HEROSMS_MAX_PRICE`；支持 `0.025-0.03-0.035` 多级价格 |
| `--merchant-seed` | `None` | 商户抽取随机种子，用于复现抽样 |
| `--retry-limit` | `10` | 获取商户 / 号码累计重试次数上限 |
| `--send` | 关闭 | 真实发送号码请求；默认 dry-run |
| `--multi-thread` | 关闭 | 跳过单线程检查的预留开关 |
| `--visible-only` | 关闭 | 只从 `visible=1` 的国家中选择 |
| `--include-no-stock` | 关闭 | 允许 `count=0` 候选；默认只选有库存 |
| `--active-limit` | `100` | 活动激活列表查询数量 |
| `--balance-poll-times` | `5` | 余额轮询次数 |
| `--balance-poll-interval` | `2` | 余额轮询间隔秒数 |
| `--active-poll-times` | `25` | 活动列表轮询次数 |
| `--active-poll-interval` | `6` | 活动列表轮询间隔秒数 |
| `--input-poll-times` | `50` | 用户输入轮询次数 |
| `--input-poll-interval` | `10` | 用户输入轮询等待秒数 |
| `--history-limit` | `10` | 历史记录显示数量 |
| `--log-dir` | `./log` | 日志目录 |

### herosms_tool.py 运行示例

只预览、不发送：

```bash
python3 herosms_tool.py run --service dr
```

真实请求号码：

```bash
python3 herosms_tool.py run --service dr --send
```

固定抽样种子：

```bash
python3 herosms_tool.py run --service dr --merchant-seed 7 --send
```

只看可见国家，限制价格：

```bash
python3 herosms_tool.py run --service dr --visible-only --max-price 0.025 --send
```

多级价格逐级试探：

```bash
python3 herosms_tool.py run --service dr --max-price 0.025-0.03-0.035 --send
```

多级价格会按顺序重新查询商户列表并逐个申请号码。例如 `0.025-0.03-0.035` 会先用 `0.025` 查询并尝试该档全部商户；该档全部失败后，再用 `0.03` 查询并尝试；仍失败才进入 `0.035`。任一档成功后立即停止，不再尝试后续价格。

### 用户输入模式说明

在 `herosms_tool.py` 的交互阶段可输入：

| 输入 | 含义 |
|---|---|
| `0` | 查询当前活动激活列表 |
| `3` | 进入请求重发短信模式 |
| `6` | 进入完成模式 |
| `8` | 进入退款模式 |
| `9` | 进入处理后重开模式 |
| `3-1` | 对列表第 1 条执行 `status=3`，请求重新发送短信 |
| `6-1` | 对列表第 1 条执行 `status=6` |
| `8-1` | 对列表第 1 条执行 `status=8` |
| `9-1` | 对列表第 1 条先按是否已有短信执行 `status=6/8`，活动列表清空且同服务/国家/商家价格仍符合限额时重新申请号码 |
| `99` | 退出轮询并查询历史记录 |

`9-序号` 是本工具的本地组合模式，不是 HeroSMS 官方单独接口：

使用 `9-序号` 必须显式传入 `--send`，否则不会处理旧激活，避免旧激活被改状态但新号码只停在 dry-run。

1. 目标记录已有 `smsCode` / `smsText` 时，先执行 `status=6` 完成；没有短信时，先执行 `status=8` 取消/退款。
2. 随后查询活动激活列表；若列表仍不为空，则停止并提示不允许继续 9 模式。
3. 若列表为空，则按目标记录的服务、国家、商家重新查询价格；如果当前价格超过 `--max-price` / `HEROSMS_MAX_PRICE`，停止并提示价格变更。
4. 价格符合限额时，复用普通号码申请的重试循环继续申请号码。

### 近期行为变更

#### 26.5.26A

- `--max-price` 与 `HEROSMS_MAX_PRICE` 支持多级价格，例如 `0.025-0.03-0.035`，会逐档查询商户并逐个申请。
- 号码请求阶段按当前商户候选数量自适应尝试次数，不再用固定 10 次覆盖所有候选。
- 余额查询后输出 `[可购买次数估算]`，按当前余额除以最高价格取整。
- `service=dr` 的候选列表会过滤 `country=4`。
- 用户输入轮询自然结束后会自动查询活动列表；若仅剩 1 条且未收到验证码，会自动执行 `status=8` 后继续历史查询。
- `.env.example` 的 `HEROSMS_MAX_PRICE` 示例更新为多级价格格式。

#### 26.5.23A

- Windows 下用户输入轮询改为使用 `msvcrt`，避免在普通终端输入上触发 `WinError 10038`。
- 新增 `3` / `3-序号` 模式，向官方 `setStatus` 接口发送 `status=3`，用于请求重新发送短信。
- 新增本地组合 `9` / `9-序号` 模式。该模式不是官方接口，会先处理旧激活，再确认列表清空、校验价格和库存，最后复用普通号码申请重试循环。
- `9-序号` 新号申请成功并确认进入活动列表后，会直接进入活动列表轮询，默认 `25` 次。
- 用户输入轮询默认次数从 `100` 调整为 `50`。

#### 26.5.18A

- 当成功号码进入活动列表时，确认日志统一显示带 `+` 前缀的号码，例如：

```text
[确认] 电话号码 +44xxxxxx2849 存在于活动激活列表
```

- 当没有可用商户候选且仍可重试时，会先等待 5 秒：

```text
[错误] 没有可用商户候选
[重试计数] 1/10
[等待] 5 秒后重试商户查询
```

---

## 其他脚本与全部参数

### 1) get_balance.py

用途：查询余额。

```bash
python3 get_balance.py
```

参数：无。

---

### 2) get_services.py

用途：获取服务清单并按关键字过滤。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-k`, `--keyword` | `""` | 按服务 `code` 或 `name` 过滤 |
| `-n`, `--limit` | `50` | 最多显示多少条；传 `0` 可视作全部 |
| `--country` | `""` | 透传给接口的 `country` 参数 |
| `--lang` | `""` | 透传给接口的 `lang` 参数 |

示例：

```bash
python3 get_services.py --keyword OpenAI
python3 get_services.py --keyword dr --limit 20
```

---

### 3) get_service_coverage.py

用途：查询服务支持的国家与运营商。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-s`, `--service` | `dr` | 服务代码 |
| `-n`, `--limit` | `30` | 最多显示多少个国家；`0` 表示全部 |
| `--operators-limit` | `8` | 每个国家最多显示多少个运营商；`0` 表示全部 |
| `--all-operators` | 关闭 | 显示每个国家全部运营商 |

示例：

```bash
python3 get_service_coverage.py --service dr
python3 get_service_coverage.py --service dr --limit 10 --all-operators
```

---

### 4) get_prices.py

用途：按价格查看国家价格，并可生成 `getNumberV2` 候选。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-s`, `--service` | `dr` | 服务代码 |
| `-n`, `--limit` | `30` | 最多显示多少个国家；`0` 表示全部 |
| `--in-stock-only` | 关闭 | 只显示 `count > 0` 的国家 |
| `--visible-only` | 关闭 | 只显示 `visible=1` 的国家 |
| `--max-price` | `None` | 最高价格；默认读取 `HEROSMS_MAX_PRICE` |
| `--show-candidates` | 关闭 | 输出可直接用于 `getNumberV2` 的候选参数 |

示例：

```bash
python3 get_prices.py --service dr --limit 20
python3 get_prices.py --service dr --in-stock-only --show-candidates
```

---

### 5) get_operator_prices.py

用途：查看国家下的运营商价格。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-s`, `--service` | `dr` | 服务代码 |
| `-n`, `--countries-limit` | `5` | 最多查询多少个国家 |
| `--operators-limit` | `10` | 每个国家最多显示多少个运营商；`0` 表示全部 |
| `--in-stock-only` | 关闭 | 只看国家库存 `count > 0` 的国家 |
| `--country-id` | `0` | 只查询指定国家 ID；`0` 表示不限制 |

示例：

```bash
python3 get_operator_prices.py --service dr --countries-limit 5 --operators-limit 10
python3 get_operator_prices.py --service dr --country-id 44
```

---

### 6) get_number_v2.py

用途：单次调试号码请求流程。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-s`, `--service` | `dr` | 服务代码 |
| `--max-price` | `None` | 最高价格；默认读取 `HEROSMS_MAX_PRICE` |
| `--visible-only` | 关闭 | 只从 `visible=1` 国家中抽取 |
| `--include-no-stock` | 关闭 | 允许选择 `count=0` 候选 |
| `--seed` | `None` | 随机种子 |
| `--send` | 关闭 | 实际发送 `getNumberV2` 请求 |

示例：

```bash
python3 get_number_v2.py --service dr
python3 get_number_v2.py --service dr --seed 7 --send
```

设计原则：

- dry-run 默认安全，不发送号码请求
- `--send` 才真实请求 `getNumberV2`
- 非 `200` 直接重试下一个候选
- 候选为空时输出详细抽取诊断

---

### 7) get_active_activations.py

用途：获取活动激活列表，并支持 `setStatus`。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--start` | `0` | 偏移量 |
| `--limit` | `100` | 请求数量，最大 100 |
| `--set-status-id` | `None` | 指定要发送 `setStatus` 的 activationId |
| `--status` | `8` | setStatus 状态码，默认 `8`=取消激活/退款 |
| `--no-list` | 关闭 | 发送 setStatus 后不再查询列表 |
| `--json` | 关闭 | 输出接口原始 JSON |

`setStatus` 已知状态码：

| 状态码 | 含义 |
|---|---|
| `3` | 请求重新发送短信 |
| `6` | 完成激活 |
| `8` | 取消激活 / 退款 |

安全保护：

- 只有活动列表中存在该 `activationId` 才会发送 `setStatus`
- 当目标记录已存在 `smsCode` 或 `smsText` 时，拒绝发送 `status=8`

示例：

```bash
python3 get_active_activations.py --limit 5
python3 get_active_activations.py --set-status-id 363621340 --status 6
python3 get_active_activations.py --set-status-id 363621340 --status 8 --no-list
```

---

### 8) get_history.py

用途：查询历史记录。

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--start` | `None` | 可选：历史查询开始时间/位置参数，按 HeroSMS 文档传给 `start` |
| `--end` | `None` | 可选：历史查询结束时间/位置参数，按 HeroSMS 文档传给 `end` |
| `--offset` | `None` | 可选：分页偏移量 |
| `--size` | `None` | 可选：分页数量 |
| `--json` | 关闭 | 输出原始 JSON |
| `--time-offset` | `None` | 自动生成 `start/end` 时的 Unix 时间偏移量，支持 `5m`、`-5m`、`5h`、`30s`、`1d` |
| `--no-time-range` | 关闭 | 不自动添加 `start/end` 时间范围参数 |

示例：

```bash
python3 get_history.py
python3 get_history.py --limit 20 --json
python3 get_history.py --time-offset -5m
python3 get_history.py --no-time-range --offset 0 --size 50
```

---

## 测试与验证

语法检查：

```powershell
& D:\0Code2\py312\python.exe -m py_compile herosms_tool.py tests\test_herosms_tool.py
```

运行单元测试：

```powershell
& D:\0Code2\py312\python.exe -m pytest -q
```

本次文档更新前已确认（2026-05-26）：

- `& D:\0Code2\py312\python.exe -m py_compile herosms_tool.py get_prices.py tests\test_herosms_tool.py tests\test_get_prices.py` 通过
- `& D:\0Code2\py312\python.exe -m pytest -q` 通过，结果为 `36 passed in 2.66s`

---

## 当前项目变化检查（本次会话）

本次会话主要变更集中在：

- `.env.example`：将 `HEROSMS_MAX_PRICE` 示例同步为多级价格格式
- `README.md`：更新版本号、版本日志、多级价格、自动收尾、黑名单和验证记录
- `get_prices.py`：新增 `service=dr` 的 `country=4` 黑名单
- `herosms_tool.py`：新增多级价格阶梯、自适应商户尝试、余额次数估算和用户输入轮询结束自动收尾
- `tests/test_herosms_tool.py`、`tests/test_get_prices.py`：补充多级价格、自适应重试、余额估算、黑名单和自动收尾测试覆盖

如需后续做版本提交，建议先：

```bash
git add .env.example README.md get_prices.py herosms_tool.py tests/
```

再继续检查 staged diff。

---

## 后续建议

- 增加 `.gitignore`，忽略 `.env`、`.venv/`、`__pycache__/`、`log/`
- 将 `5 秒商户重试等待` 提升为可配置参数，例如 `--merchant-retry-delay`
- 为 README 中列出的每个脚本补充对应测试，避免后续文档和实现漂移
