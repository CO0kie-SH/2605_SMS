# HeroSMS Python 调试工具集

> 版本：`26.5.9A`  
> 最后更新：`2026-05-09`

---

## 项目简介

本项目围绕 [HeroSMS](https://hero-sms.com) 的 SMS-Activate 风格接口，构建了一套 **Python 调试与验证工具集**，用于完成以下工作：

- 查询账户余额
- 拉取服务清单
- 查询国家价格
- 查询运营商价格
- 生成 `getNumberV2` 候选请求列表
- 随机抽取可用候选并预览请求体
- 在真实请求前后轮询余额，观察扣费行为

当前版本的定位是：

1. 先把 **查询链路、参数链路、扣费观察链路** 做扎实
2. 再在这个基础上继续扩展状态管理、黑名单、日志和后续自动化流程

---

## 核心功能

- 读取 `.env` 中的 `HEROSMS_API_KEY` 与 `HEROSMS_MAX_PRICE`
- 查询 `getServicesList`，按名称或 code 过滤服务
- 查询 `getPrices`，按价格从低到高列出各国价格和库存
- 查询 `getOperators` + `getPrices(country, operator)`，单独查看运营商价格
- 生成可直接用于 `getNumberV2` 的候选参数列表：
  `service / country / operator / maxPrice`
- 随机抽取候选参数并打印请求体
- `--send` 默认关闭，避免误请求
- 发送 `getNumberV2` 前先查 1 次余额
- 请求后连续轮询 5 次余额，每次间隔 2 秒
- 一旦余额低于请求前余额，自动计算扣费差值，并与 `HEROSMS_MAX_PRICE` 对比
- 如果某个 `operator` 请求 `getNumberV2` 返回 `404`，标记为 `err`，并从剩余候选中重新随机抽取

---

## 项目结构

```text
m260509_sms/
├── README.md                          # 本说明文件
├── README2.md                         # 参考说明书模板
├── requirements.txt                   # Python 依赖
├── .env                               # 环境变量配置
├── get_balance.py                     # 查询余额
├── get_services.py                    # 查询服务清单
├── get_service_coverage.py            # 查询国家 + 运营商覆盖关系
├── get_prices.py                      # 查询国家价格，并生成 getNumberV2 候选列表
├── get_operator_prices.py             # 查询运营商价格
├── get_number_v2.py                   # 随机抽候选、预览请求体、可选发送
├── codex-registrar2/                  # 参考 Node.js 子项目
└── 用于处理 HeroSMS 虚拟号码的 API 协议.mhtml  # 官方 API 文档存档
```

---

## 环境要求

- Python：`3.12+`
- 当前使用解释器：
  `D:\0Code2\py312\python.exe`
- 依赖：
  - `requests`
  - `python-dotenv`

### 安装依赖

```bash
D:\0Code2\py312\python.exe -m pip install -r requirements.txt
```

---

## 配置说明

### 环境变量：`.env`

```env
HEROSMS_API_KEY=你的API密钥
HEROSMS_BASE_URL=https://hero-sms.com/stubs/handler_api.php
HEROSMS_MAX_PRICE=0.02
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `HEROSMS_API_KEY` | 是 | HeroSMS API 密钥 |
| `HEROSMS_BASE_URL` | 是 | HeroSMS 接口地址，默认 `handler_api.php` |
| `HEROSMS_MAX_PRICE` | 否 | `getNumberV2` 的最高价格限制 |

---

## 命令速查表

### 常用查询

```bash
# 查询余额
D:\0Code2\py312\python.exe get_balance.py

# 查询服务清单
D:\0Code2\py312\python.exe get_services.py

# 查询 OpenAI 服务
D:\0Code2\py312\python.exe get_services.py --keyword OpenAI

# 查询国家价格（按价格升序）
D:\0Code2\py312\python.exe get_prices.py --service dr --in-stock-only --limit 20

# 查询运营商价格
D:\0Code2\py312\python.exe get_operator_prices.py --service dr --countries-limit 5 --operators-limit 10 --in-stock-only
```

### 候选与请求

```bash
# 生成 getNumberV2 候选列表
D:\0Code2\py312\python.exe get_prices.py --service dr --in-stock-only --show-candidates --limit 20

# 随机抽取 1 条候选并预览请求体
D:\0Code2\py312\python.exe get_number_v2.py --service dr

# 固定随机种子，便于复现
D:\0Code2\py312\python.exe get_number_v2.py --service dr --seed 7

# 实际发送 getNumberV2
D:\0Code2\py312\python.exe get_number_v2.py --service dr --send
```

### 调试建议

```bash
# 查看全部国家价格
D:\0Code2\py312\python.exe get_prices.py --service dr --limit 0

# 仅看可见国家
D:\0Code2\py312\python.exe get_prices.py --service dr --visible-only --in-stock-only

# 临时覆盖最高价格
D:\0Code2\py312\python.exe get_number_v2.py --service dr --max-price 0.03
```

---

## 脚本说明

### 1. 查询余额

```bash
D:\0Code2\py312\python.exe get_balance.py
```

输出示例：

```text
[API 响应] ACCESS_BALANCE:2.2751
[当前余额] 2.2751
```

### 2. 查询服务清单

```bash
D:\0Code2\py312\python.exe get_services.py
```

按关键字过滤：

```bash
D:\0Code2\py312\python.exe get_services.py --keyword OpenAI
```

### 3. 查询国家价格

```bash
D:\0Code2\py312\python.exe get_prices.py --service dr --in-stock-only --limit 20
```

特点：

- 按国家价格升序排列
- 支持 `--in-stock-only`
- 支持 `--visible-only`
- 支持 `--max-price`

### 4. 查询运营商价格

```bash
D:\0Code2\py312\python.exe get_operator_prices.py --service dr --countries-limit 5 --operators-limit 10 --in-stock-only
```

特点：

- 先拿国家候选
- 再按国家逐个请求运营商价格
- 单独展示每个运营商的价格与库存

### 5. 生成 `getNumberV2` 候选列表

```bash
D:\0Code2\py312\python.exe get_prices.py --service dr --in-stock-only --show-candidates --limit 20
```

候选数据包含：

```text
service / country / operator / maxPrice / price / count / physicalCount / countryName
```

### 6. 随机抽取 `getNumberV2` 请求并预览

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr
```

固定随机种子，便于复现：

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr --seed 7
```

默认行为：

- 只预览请求体
- 不实际发送请求

### 7. 实际发送 `getNumberV2`

```bash
D:\0Code2\py312\python.exe get_number_v2.py --service dr --send
```

发送行为：

1. 从候选列表随机抽一条
2. 打印请求体
3. 请求前查询 1 次余额
4. 发送 `getNumberV2`
5. 请求后连续查询 5 次余额，每次间隔 2 秒
6. 若余额减少，打印扣费差值并与 `HEROSMS_MAX_PRICE` 比较
7. 若当前 `operator` 返回 `404`，标记 `err`，并从剩余候选中继续随机重试

---

## 当前实现的 `getNumberV2` 参数说明

当前工具链里已经接入并验证过这些常用参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `service` | string | 服务代码，例如 `dr` |
| `country` | number | 国家 ID，例如 `16` |
| `operator` | string | 运营商代码，例如 `o2` |
| `maxPrice` | number | 最高价格，来自 `.env` 的 `HEROSMS_MAX_PRICE` |

实际请求体示例：

```json
{
  "action": "getNumberV2",
  "service": "dr",
  "country": 16,
  "operator": "o2",
  "maxPrice": 0.02
}
```

---

## 执行流程

```text
┌──────────────────────────────────────────────────────────┐
│ 1. 读取 .env                                              │
│    ├─ HEROSMS_API_KEY                                     │
│    ├─ HEROSMS_BASE_URL                                    │
│    └─ HEROSMS_MAX_PRICE                                   │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 2. 查询服务、国家、价格、运营商                            │
│    ├─ getServicesList                                     │
│    ├─ getCountries                                        │
│    ├─ getPrices                                           │
│    └─ getOperators + getPrices(country, operator)         │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 3. 构建候选请求列表                                        │
│    └─ 过滤条件：price <= HEROSMS_MAX_PRICE                │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 4. 随机抽取一条候选                                        │
│    └─ 输出 request body 预览                              │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 5. 可选发送 getNumberV2                                    │
│    ├─ 请求前查余额                                         │
│    ├─ 请求后查余额 5 次                                    │
│    ├─ 计算扣费差值                                         │
│    └─ 对比 HEROSMS_MAX_PRICE                               │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ 6. 如果 operator 返回 404                                  │
│    ├─ 打印 err                                             │
│    ├─ 从剩余候选中移除该 operator                           │
│    └─ 重新随机抽取下一条                                   │
└──────────────────────────────────────────────────────────┘
```

---

## 今日项目总结

今天这个版本主要完成了 **HeroSMS 调试链路的基础建设**，重点不是一口气做完所有自动化，而是先把“查得到、看得懂、控得住”这三件事落稳：

- 把服务、国家、运营商、价格这几层接口都拆开做成独立脚本
- 把 `getNumberV2` 的参数链路打通，并接入 `HEROSMS_MAX_PRICE`
- 把随机抽样和 dry-run 预览加上，避免误触发真实请求
- 把真实请求前后的余额观察机制补齐，能直接看到是否发生扣费
- 把 `404 operator` 的重试策略改成“标记错误并重新随机”，不再自动去掉 `operator`

这一版已经很适合作为后续继续开发的基础版本。

---

## 已知现象

### 1. `getOperators` 返回的运营商，不一定都能直接用于 `getNumberV2`

实测发现，某些运营商代码：

- 可以查出价格
- 但真正请求 `getNumberV2` 时会返回 `404`

所以当前版本已经加入：

- `404` 打印 `err`
- 从候选池中移除
- 继续随机抽取

### 2. 失败请求未必完全“零影响”

实测中，个别 `404` 请求后的余额变化可能不是立刻可见，因此当前版本加入了：

- 请求后连续 5 次余额轮询
- 每次间隔 2 秒
- 自动比较请求前后的余额差值

---

## TODO

- 后续加入 `operator` 404 黑名单机制：当某些运营商多次返回 `404` 时，写入黑名单并在候选生成阶段直接过滤。
- 后续将余额变化结果结构化，输出 `before / after / diff / diff<=maxPrice`。
- 后续接入 `setStatus(1)`、`getStatusV2`、`cancelActivation` 等完整激活生命周期。
- 后续为成功请求增加结果持久化，例如保存 `activationId / phoneNumber / operator / cost / timestamp`。

---

## 参考链接

- [HeroSMS 官网](https://hero-sms.com)
- [HeroSMS API 文档](https://hero-sms.com/cn/api)

---

## 更新日志

### v26.5.9A (2026-05-09)

- 新增：`get_services.py`，支持查询 HeroSMS 服务清单并按关键字过滤
- 新增：`get_service_coverage.py`，支持查询服务对应的国家与运营商覆盖关系
- 新增：`get_prices.py`，支持按价格升序查询国家价格，并生成 `getNumberV2` 候选列表
- 新增：`get_operator_prices.py`，支持按国家展开查询运营商价格
- 新增：`get_number_v2.py`，支持随机抽取候选、dry-run 预览请求体、可选真实发送
- 新增：`HEROSMS_MAX_PRICE` 环境变量，用于限制 `getNumberV2` 的最高价格
- 新增：真实请求前后余额监控逻辑，支持 1 次请求前余额 + 5 次请求后轮询
- 新增：余额差值计算与 `HEROSMS_MAX_PRICE` 比较输出
- 调整：`getNumberV2` 的 `404` 处理改为标记当前 `operator` 为 `err`，并从剩余候选重新随机抽取
- 调整：README 重写为正式项目说明书，补充脚本说明、流程图、TODO、已知现象与版本记录

---

*最后更新：2026-05-09*  
*版本：26.5.9A*
