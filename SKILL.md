---
name: workbuddy-daily-checkin
description: WorkBuddy 每日自动签到、领取每日赠送积分，跨平台（Windows/Linux 完整支持，macOS 受限）。当用户提到"签到""每日签到""自动签到""领积分""今日积分""连续签到天数""签到状态"，或需要按日定时执行签到任务、排查签到失败原因时使用。
version: 2.0.0
---

# WorkBuddy 每日签到

自动完成 WorkBuddy 客户端的每日签到，领取当日赠送积分（每日 100 积分，另有连续签到奖励）。

**前提**：WorkBuddy 桌面客户端已启动且处于登录状态。客户端未运行时按约定直接跳过（不算失败）。

## 平台支持

| 平台 | 支持程度 | 说明 |
| --- | --- | --- |
| Windows | ✅ 完整 | `tasklist` 探测 + `ReadProcessMemory` 读令牌 + DPAPI 加密缓存 |
| Linux | ✅ 完整 | `ps` 探测 + `/proc/<pid>/maps|mem` 读令牌（同用户，免 root；不落盘缓存） |
| macOS | ⚠️ 受限 | 可探测进程并从 `state.vscdb` 取令牌，但**无法读其他进程内存**（需 root），磁盘副本常已失效 |

先用 `--doctor` 确认本机是否就绪（不联网、不写缓存）：

```bash
python "<skill>/scripts/checkin.py" --doctor
```

`supported: true` 且 `verdict` 为"环境就绪"即可使用；macOS 会返回 `supported: false` 并说明原因。

## 快速使用

```bash
# Windows
py -3 "<skill>/scripts/checkin.py"
# macOS / Linux
python3 "<skill>/scripts/checkin.py"
```

| 参数 | 说明 |
| --- | --- |
| `--doctor` | 打印环境自检报告后退出（平台/路径/进程/能力判定） |
| `--status-only` | 只查询签到状态，不领取、不写缓存 |
| `--verbose` | 把候选令牌来源摘要写入日志（不含令牌内容） |

输出为 JSON。典型结果：

```json
{ "ok": true, "platform": "Windows", "action": "checked-in",
  "checkin": { "granted_credit": 100, "streak_days": 6 } }
```

`action` 含义：

| action | 含义 |
| --- | --- |
| `checked-in` | 本次签到成功并领取积分 |
| `already-checked-in` | 今天已签过（幂等，不重复领取） |
| `skipped` | WorkBuddy 未运行，按约定跳过 |
| `activity-inactive` | 签到活动明确未开放 |
| `status-only` | 仅查询状态（配合 `--status-only`） |

失败时返回 `ok:false` 且带 `error_kind`：

| error_kind | 含义与处理 |
| --- | --- |
| `probe` | 无法探测客户端进程（Windows: `tasklist`；macOS/Linux: `ps`）。非"未运行"，属错误 |
| `platform` | 本平台无法读取进程内存且磁盘令牌无效（典型为 macOS）。见上文支持矩阵 |
| `security` | 官方域名返回异常重定向，已中止以避免令牌被转发到非官方主机 |
| `network` | 网络/代理/证书问题，未取得服务端响应。检查网络后重试 |
| `auth` | 服务端可达但令牌不可用。请在 WorkBuddy 中重新登录 |
| `business` | 服务端返回业务错误（含 HTTP 状态与业务码） |
| `timeout` | 取令牌超出时间预算（客户端繁忙或网络缓慢），稍后重试 |
| `internal` | 未预期异常（已兜底，不会抛栈） |

退出码：`0` 成功 / 已签到 / 已跳过；`1` 失败。

## 工作原理

1. **确认客户端状态**：按平台用 `tasklist`（Windows）/ `ps`（macOS/Linux）探测，**精确匹配可执行名**（避免命中 `notworkbuddy-helper` 之类同名子串进程）。未运行 → `skipped`；**探测失败 → 报错**（不与"未运行"混同）。
2. **确定 endpoint**：从客户端 `product.json` 读取 `endpoint`，并**强制命中脚本内硬编码的官方主机白名单**（必须为 https、无显式端口、无 query/fragment）；不匹配则回退内置常量。白名单是**代码内的信任锚**，不读取可写文件中的任何"白名单"字段。
3. **取得有效登录令牌**（惰性、逐个验活、命中即止）：
   - 本地加密缓存（仅 Windows，经 DPAPI 加密；Linux/macOS 出于不落明文令牌的考虑**默认不落盘**）
   - **运行中客户端进程内存中的活跃令牌**——客户端持续刷新令牌，磁盘持久化副本常已失效，内存是唯一稳定新鲜的来源
   - `state.vscdb` 中 Electron safeStorage 加密的持久化令牌（回退路径，需 `cryptography`）
4. **账号一致性**：以令牌自身的 JWT `sub` 为准，与配置中的账号交叉校验，避免多账号错配。
5. **调用官方接口**（与客户端同源）：
   - `POST {endpoint}/v2/billing/meter/checkin-activity-status` — 查询状态
   - `POST {endpoint}/v2/billing/meter/daily-checkin` — 领取积分
   - 头含 `Authorization: Bearer <token>`、`X-User-Id`、`X-Domain`；**不可携带 `X-Device-Token`**，否则网关以 `invalid_token` 拒绝
6. **判定**：只有 `active` **明确为 false** 才判活动未开放；字段缺失时不跳过，继续尝试签到（服务端幂等保护，重复领取只会返回"今天已签到"）。

## 安全设计

- **禁止跟随重定向**：`urllib` 默认会跟随 3xx 并把 `Authorization` 原样复制到新主机，可绕过域名白名单。脚本使用禁用重定向的 opener，任何 3xx 一律判失败（`error_kind=security`）。
- **白名单不可被改写**：官方主机列表硬编码在脚本内，即使 `product.json` 被替换也无法把令牌发往第三方。
- **令牌不出本机**：绝不写入 stdout/日志；非 Windows 不落盘缓存；缓存经系统级加密（Windows DPAPI）。
- **只读**：仅读取本机当前用户自己运行的客户端内存与配置文件，不修改任何进程状态。

## 定时执行

建议通过 WorkBuddy 定时任务每日触发。任务提示词示例：

> 运行 WorkBuddy 每日签到脚本并报告结果：`python "<skill>/scripts/checkin.py"`

按脚本 JSON 输出报告：`checked-in` 报积分与连续天数；`already-checked-in` 说明今天已签；`skipped` 说明客户端未启动（正常跳过）；`ok:false` 时给出 `error_kind` 与处理建议。

## 排错

| 现象 | 原因与处理 |
| --- | --- |
| `skipped` | WorkBuddy 未启动。启动客户端后重试。 |
| `error_kind=probe` | `tasklist`/`ps` 不可用或超时，**不是**未运行。检查系统命令可用性。 |
| `error_kind=platform` | macOS：无法读进程内存且磁盘令牌无效。见平台支持矩阵。 |
| `error_kind=security` | 官方域名返回 3xx 重定向。检查网络/代理是否被劫持。 |
| `error_kind=auth` | 客户端登录态失效，请在 WorkBuddy 中重新登录后重试。 |
| `error_kind=network` | 断网、代理或证书问题。检查网络后重试。 |
| 首次运行约 10-15 秒 | 正常：需扫描客户端进程内存以获取活跃令牌。 |
| 内存路径始终无候选 | 客户端可能以更高权限运行（Windows）/ 需同等权限执行本脚本。 |

日志：`~/.workbuddy/workbuddy-daily-checkin/checkin.log`（含轮转，不含任何令牌内容）。

## 注意

- 遍历进程可读内存属敏感操作，部分安全软件（EDR/杀软）可能告警或拦截，可将该脚本加入信任白名单。
- 依赖 Python 3 标准库；`cryptography` 仅用于可选的磁盘令牌回退路径，缺失不影响主流程。
- **合规提醒**：本工具代表**使用者本人**在其**本人账号**上完成每日签到的自动化，请勿用于批量账号或代他人操作；使用前请自行确认符合 WorkBuddy 的服务条款。
