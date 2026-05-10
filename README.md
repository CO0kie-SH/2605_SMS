# HeroSMS Python 调试工具集

> 当前版本：`26.5.10B`  
> 最后更新：`2026-05-10`  
> 项目定位：合法合规地调试 HeroSMS SMS-Activate 风格 API，观察价格、库存、请求参数、余额变化、活动激活状态与取消退款流程。

---

## 项目简介

本项目围绕 HeroSMS 的 `handler_api.php` 接口，整理了一组 Python 调试脚本，用于逐步验证虚拟号码请求链路。

当前重点不是全自动注册，而是先把 API 调试过程做清楚：

- 查询余额、服务、国家价格、运营商价格
- 根据价格和库存生成 `getNumberV2` 候选
- 随机抽取候选并预览请求体
- 可选真实发送 `getNumberV2`，并打印 HTTP 状态与返回响应体
- 请求前后打印余额变化
- 请求前后打印活动激活列表
- 查看活动激活的 `activationTime`
- 支持将激活状态改为 `8` 取消激活/退款
- 记录 `getHistory` 的已知问题，后续继续验证

---

## 版本管理

版本号格式采用：`YY.M.D + 字母序号`。

示例：

| 版本 | 日期 | 说明 |
|---|---|---|
| `26.5.10B` | 2026-05-10 | 优化 getNumberV2 请求/响应日志、非 200 重试流程与 setStatus 防误退款保护 |
| `26.5.10A` | 2026-05-10 | 今日主版本，加入活动激活列表、状态变更、活动快照、文档整理 |
| `26.5.9A` | 2026-05-09 | 初始调试链路版本，完成服务、价格、候选、请求与余额观察 |

同一天多次重要迭代时，后缀按 `A / B / C` 递增。

---

## 环境要求

- Python：`3.12+`
- 当前解释器：`D:\0Code2\py312\python.exe`
- 依赖：
  - `requests`
  - `python-dotenv`

安装依赖：

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

---

## 环境变量

`.env` 示例：

```env
HEROSMS_API_KEY=你的API密钥
HEROSMS_BASE_URL=https://hero-sms.com/stubs/handler_api.php
HEROSMS_MAX_PRICE=0.025
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `HEROSMS_API_KEY` | 是 | HeroSMS API 密钥 |
| `HEROSMS_BASE_URL` | 否 | 默认 `https://hero-sms.com/stubs/handler_api.php` |
| `HEROSMS_MAX_PRICE` | 否 | `getNumberV2` 的最高价格限制 |

---

## 项目结构

```text
2605_SMS/
├── README.md
├── requirements.txt
├── .env
├── get_balance.py              # 查询余额
├── get_services.py             # 获取服务清单
├── get_service_coverage.py     # 查询服务覆盖国家和运营商
├── get_prices.py               # 查询国家价格，并生成 getNumberV2 候选
├── get_operator_prices.py      # 查询运营商价格
├── get_number_v2.py            # 随机抽取候选，可选真实请求号码
├── get_active_activations.py   # 获取活动激活列表，支持 setStatus
├── get_history.py              # 获取历史记录，目前有已知 BUG
└── 用于处理 HeroSMS 虚拟号码的 API 协议.mhtml
```

---

## 常用命令

### 查询余额

```bash
D:\0Code2\py312\python.exe get_balance.py
```

### 获取服务清单

```bash
D:\0Code2\py312\python.exe get_services.py
D:\0Code2\py312\python.exe get_services.py --keyword OpenAI
```

### 查询国家价格

```bash
D:\0Code2\py312\python.exe get_prices.py --service dr --limit 20
D:\0Code2\py312\python.exe get_prices.py --service dr --in-stock-only --limit 20
D:\0Code2\py312\python.exe get_prices.py --service dr --show-candidates --limit 20
```

### 查询运营商价格

```bash
D:\0Code2\py312\python.exe get_operator_prices.py --service dr --countries-limit 5 --operators-limit 10 --in-stock-only
```

---

## 请求号码：getNumberV2

默认只预览请求体，不发送请求：

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr
```

固定随机种子，方便复现：

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr --seed 7
```

真实发送请求：

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr --send
```

### get_number_v2.py 代码流程梳理

发送时流程：

1. 解析命令行参数，读取 `.env` 中的 `HEROSMS_API_KEY`、`HEROSMS_BASE_URL`、`HEROSMS_MAX_PRICE`
2. 根据 `service` 构建候选列表，候选来自国家价格和运营商价格
3. 按库存、可见性、最高价格过滤候选
4. 随机抽取一个候选；传入 `--seed` 时可以复现随机结果
5. 打印 `[随机选中]` 和 `[请求体预览]`
6. 如果没有传入 `--send`，进入 dry-run 模式，只预览不发送请求
7. 如果传入 `--send`，第一次发送前打印活动激活快照，`limit=5`
8. 第一次发送前查询余额，打印 `[余额] before`
9. 打印 `[发送请求]` 和本次 `getNumberV2` 请求体
10. 请求返回后立刻打印 `[HTTP状态]` 和 `[返回响应]`
11. 如果 HTTP 状态码是 `200`，开始轮询余额，最多 5 次，每次间隔 2 秒
12. 余额一旦小于请求前余额，立即停止轮询，打印 `diff` 并与 `HEROSMS_MAX_PRICE` 比较
13. 成功请求后打印 `after getNumberV2` 活动激活快照，`limit=5`
14. 如果 HTTP 状态码不是 `200`，打印当前候选为 `[err]`
15. 非 `200` 时移除当前候选，不再请求余额变化，也不再请求活动激活列表
16. 非 `200` 时重新随机抽取剩余候选，直接进入下一次 `[发送请求]`
17. 如果所有候选都返回非 `200`，打印结果并退出

关键日志顺序：

```text
[发送请求]
{...}
[HTTP状态] 404
[返回响应]
{...}
[err] operator=... country=... service=... status=404
[重试] 剩余候选数: ...，重新随机抽取
[发送请求]
{...}
```

设计原则：

- dry-run 默认安全，不发送号码请求
- `--send` 才真实请求 `getNumberV2`
- `[发送请求]` 后必须紧接 HTTP 状态码和响应体，便于观察接口真实返回
- 非 `200` 不做后置余额和活动列表查询，避免日志噪声，直接重试下一个候选
- 不会在 `404` 时自动改成“不指定运营商”，而是保留运营商并更换候选

候选为空时，会打印抽取过程，包括：

- 当前 `service`
- `maxPrice`
- 是否只要有库存
- 原始国家数量
- 库存过滤后数量
- 最高价过滤后数量
- 最低价国家预览和过滤原因

---

## 活动激活列表

查看当前活动激活：

```bash
D:\0Code2\py312\python.exe get_active_activations.py --start 0 --limit 5
```

该脚本请求：

```text
action=getActiveActivations
```

参数说明：

| 参数 | 默认 | 说明 |
|---|---:|---|
| `--start` | `0` | 偏移量 |
| `--limit` | `100` | 请求数量，文档标注最多 100 |
| `--json` | 关闭 | 输出接口原始 JSON |

列表会打印原始字段名 `activationTime`，用于观察 HeroSMS 服务端时间：

```text
activationTime=2026-05-10 11:59:34
```

---

## 更改激活状态

`get_active_activations.py` 支持通过 `setStatus` 更改激活状态。只有传入 `--set-status-id` 时才会发送状态变更请求。发送前会先查询当前活动激活列表，只有列表中存在该 `activationId` 才继续请求；不存在时会打印 `[跳过]` 并停止。如果目标记录已经出现 `smsCode` 或 `smsText`，说明短信已返回、服务已生效，脚本会打印 `[warning]` 并拒绝发送 `status=8` 取消/退款请求，同时提示可使用 `--status 6` 标记 `status=6(完成激活)`。

默认状态码是 `8`，即取消激活/退款：

```bash
D:\0Code2\py312\python.exe get_active_activations.py --set-status-id 363621340
```

显式指定 `status=8`：

```bash
D:\0Code2\py312\python.exe get_active_activations.py --set-status-id 363621340 --status 8
```

只发送 `setStatus`，不再继续查询活动列表：

```bash
D:\0Code2\py312\python.exe get_active_activations.py --set-status-id 363621340 --status 8 --no-list
```

`setStatus` 状态码：

| 状态码 | 含义 |
|---|---|
| `3` | 请求重新发送短信 |
| `6` | 完成激活 |
| `8` | 取消激活/退款 |

---

## 激活状态码映射

根据 `getActiveActivations` 和 `getHistory` 的实测结果，目前记录：

| 状态码 | 含义 | 来源 |
|---|---|---|
| `4` | 等待短信 | 活动激活列表实测，刚请求号码后返回 `status=4` |
| `6` | 已经完成 | 历史记录接口实测 |
| `8` | 取消/退款 | 历史记录接口实测 |

说明：该映射优先按真实接口结果维护；后续发现更多状态码再继续补充。

---

## 历史记录接口

脚本：

```bash
D:\0Code2\py312\python.exe get_history.py --limit 5 --size 5
```

调试参数：

```bash
# 不自动增加 start/end
D:\0Code2\py312\python.exe get_history.py --limit 5 --size 5 --no-time-range

# 自动时间范围整体偏移，例如向后 5 分钟
D:\0Code2\py312\python.exe get_history.py --limit 5 --size 5 --time-offset 5m

# PowerShell 下负数建议这样写
D:\0Code2\py312\python.exe get_history.py --limit 5 --size 5 --time-offset=-5m
```

支持的偏移格式：`30s`、`5m`、`5h`、`1d`、`-5m`。

### 已知 BUG

`get_history.py` 目前已支持：

- 自动 14 天时间范围
- `--no-time-range`
- `--time-offset`
- 打印脱敏请求 URL
- 按时间倒序打印，最新记录在前

但实测结果仍可能与 HeroSMS 官网页面搜索不完全一致。该问题先记录为已知 BUG，后续需要继续确认服务端对 `start / end / offset / size` 的真实语义、时区和默认排序行为。

---

## 当前脚本一览

| 脚本 | 用途 |
|---|---|
| `get_balance.py` | 查询余额 |
| `get_services.py` | 获取服务清单 |
| `get_service_coverage.py` | 查询服务覆盖国家和运营商 |
| `get_prices.py` | 查询国家价格，构建号码候选 |
| `get_operator_prices.py` | 展开运营商价格 |
| `get_number_v2.py` | 随机抽取候选，可选真实请求号码 |
| `get_active_activations.py` | 获取活动激活列表，支持取消激活/退款 |
| `get_history.py` | 获取激活历史记录，目前有已知 BUG |

---

## TODO

- 为 `404 operator` 增加持久化黑名单，避免重复命中无效服务商
- 把成功请求结果保存到本地日志，例如 `activationId / phoneNumber / operator / cost / activationTime`
- 继续验证 `getHistory` 与官网搜索结果不一致的问题
- 后续接入 `getStatusV2`、`finishActivation`、`cancelActivation` 等完整生命周期接口
- 将余额变化、活动列表、状态变更结果结构化保存，方便后续自动化使用

---

## 更新日志

### v26.5.10B (2026-05-10)

- 调整：`get_number_v2.py --send` 的日志顺序，确保 `[发送请求]` 后紧接 `[HTTP状态]` 和 `[返回响应]`
- 调整：`request_number()` 会先解析并打印接口返回体，再根据 HTTP 状态决定是否抛出错误
- 优化：HTTP 非 `200` 时标记当前候选为 `[err]`，移除该候选并重新随机抽取
- 优化：HTTP 非 `200` 时不再请求余额变化，也不再请求活动激活列表，直接进入下一次申请号码
- 优化：重试时不重复发送前活动快照和余额查询，减少无效日志
- 优化：`get_active_activations.py --set-status-id` 会先检查当前活动列表中是否存在目标 ID，不存在则跳过 `setStatus`
- 优化：目标激活已出现 `smsCode` 或 `smsText` 时，阻止发送 `status=8` 取消/退款请求
- 优化：防误退款 warning 增加 `--status 6` 提示，用于标记 `status=6(完成激活)`
- 文档：补充 `get_number_v2.py` 代码流程梳理、关键日志顺序和非 `200` 重试策略

### v26.5.10A (2026-05-10)

- 新增：`get_active_activations.py`，支持请求 `getActiveActivations` 获取活动激活列表
- 新增：活动列表打印脱敏请求 URL
- 新增：活动列表输出原始字段名 `activationTime`，用于观察 HeroSMS 服务端时间
- 新增：`get_active_activations.py --set-status-id` 支持请求 `setStatus`
- 新增：`setStatus` 默认 `status=8`，用于取消激活/退款
- 新增：`--no-list`，允许只发送状态变更，不继续查询活动列表
- 调整：`get_number_v2.py --send` 请求前后从 `getHistory` 快照改为活动激活列表快照，固定 `limit=5`
- 调整：余额轮询检测到余额减少后立即停止后续轮询
- 调整：活动状态码 `4` 修正为“等待短信”
- 文档：补充版本管理、状态码映射、活动激活列表、状态变更命令和 `getHistory` 已知 BUG

### v26.5.9A (2026-05-09)

- 新增：`get_services.py`，支持查询服务清单并按关键字过滤
- 新增：`get_service_coverage.py`，支持查询服务对应国家与运营商覆盖关系
- 新增：`get_prices.py`，支持按国家价格升序查询，并生成 `getNumberV2` 候选列表
- 新增：`get_operator_prices.py`，支持按国家展开查询运营商价格
- 新增：`get_number_v2.py`，支持随机抽取候选、dry-run 预览请求体、可选真实发送
- 新增：`HEROSMS_MAX_PRICE` 环境变量，用于限制 `getNumberV2` 最高价格
- 新增：真实请求前后余额监控，支持请求前余额 + 请求后轮询
- 新增：余额差值计算与 `HEROSMS_MAX_PRICE` 比较输出
- 调整：`getNumberV2` 的 `404` 处理改为标记当前 `operator` 为 `err`，并从剩余候选重新随机抽取

---

## 参考链接

- HeroSMS 官网：https://hero-sms.com
- HeroSMS API 文档：https://hero-sms.com/cn/api
