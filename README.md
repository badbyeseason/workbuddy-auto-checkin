# WorkBuddy 每日自动签到

> 在你的 WorkBuddy 上，**每天自动完成签到并领取赠送积分**。
> 只要 WorkBuddy 处于运行状态，到点自动执行 —— 不需要手动点任何按钮。

[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)](#平台支持)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 它是什么

一个 WorkBuddy **技能包（Skill）**。装好之后，你只需要一句话：

> 帮我设置每天 9:30 自动运行 workbuddy-daily-checkin 技能，把签到结果简要报告给我。

WorkBuddy 就会自动创建一个每日定时任务，到点自动签到、自动领积分，并把结果报告给你。

---

## 快速开始

### 方式一：让 WorkBuddy 自己装（推荐，零门槛）

把这个仓库地址（或下载好的 zip / 解压出的文件夹）发给 WorkBuddy，然后说：

> 我下载了 workbuddy-daily-checkin 技能包，位置是 `C:\Users\你\Downloads\workbuddy-daily-checkin`。
> 请帮我安装成 WorkBuddy 技能、跑一次自检，并设成每天 9:30 自动签到。

### 方式二：一键安装脚本

```bash
git clone https://github.com/badbyeseason/workbuddy-auto-checkin.git /tmp/wb-checkin
cd /tmp/wb-checkin
python install.py            # Windows: py -3 install.py
```

脚本会自动：定位技能目录 → 备份同名旧技能 → 复制文件 → 运行自检 → 打印下一步。

### 方式三：手动放置

```bash
git clone https://github.com/badbyeseason/workbuddy-auto-checkin.git \
  ~/.workbuddy/skills/workbuddy-daily-checkin
```

Windows 对应路径为 `%USERPROFILE%\.workbuddy\skills\workbuddy-daily-checkin`。

### 安装后自检

```bash
python3 ~/.workbuddy/skills/workbuddy-daily-checkin/scripts/checkin.py --doctor
```

期望看到 `"supported": true` 与 `"verdict": "环境就绪：可自动签到。"`

---

## 平台支持

| 平台 | 支持程度 | 说明 |
| --- | --- | --- |
| **Windows 10/11** | ✅ 完整 | `tasklist` 探测 + 读取进程内存取令牌 + DPAPI 加密缓存 |
| **Linux** | ✅ 完整 | `ps` 探测 + `/proc/<pid>/mem` 读取（**同用户免 root**） |
| **macOS** | ⚠️ 受限 | 可探测进程、尝试从 `state.vscdb` 取令牌；但**读其他进程内存需要 root**，本工具不请求提权，因此经常取不到有效令牌 |

> **为什么 macOS 受限**：签到接口需要**当前有效的登录令牌**。WorkBuddy 会持续刷新令牌，磁盘上的副本通常已被服务端判为失效，稳定可用的只有**进程内存中的活跃令牌**；而 macOS 读其他进程内存必须 root 授权（`task_for_pid`）。

---

## 工作原理

1. 按平台探测 WorkBuddy 客户端是否在运行（Windows: `tasklist`；macOS/Linux: `ps`），**精确匹配可执行名**。
2. 从客户端 `product.json` 发现官方接口地址，并**强制命中脚本内硬编码的官方域名白名单**（https、无端口、无 query）。
3. **惰性**取得并逐个验活登录令牌：
   - 本地加密缓存（仅 Windows，DPAPI）
   - **运行中客户端进程内存中的活跃令牌**（主要来源）
   - `state.vscdb` 中 Electron safeStorage 加密的持久化令牌（回退路径）
4. 调用与客户端同源的官方接口完成状态查询与领取（服务端幂等，重复调用只会返回"今天已签到"）。

## 安全设计

- **禁止跟随 HTTP 重定向**：`urllib` 默认会跟随 3xx 并把 `Authorization` 头原样复制到新主机，可绕过域名白名单。本项目使用禁用重定向的 opener，任何 3xx 一律判失败。
- **白名单不可被改写**：官方域名列表硬编码在脚本内，即使 `product.json` 被替换也无法把令牌发往第三方。
- **令牌不出本机**：绝不写入 stdout / 日志；非 Windows 平台不落盘缓存。
- **只读**：只读取本机当前用户自己运行的客户端内存与配置文件，不修改任何进程状态，不做任何提权。
- 前端校验失败会**明确报错**而不是静默跳过（不 fail-open）。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--doctor` | 打印环境自检报告后退出（不联网、不写缓存） |
| `--status-only` | 只查询签到状态，不领取、不写缓存 |
| `--verbose` | 把候选令牌来源摘要写入日志（不含令牌内容） |

输出为 JSON，`action` 取值：`checked-in` / `already-checked-in` / `skipped` / `activity-inactive`。
失败时带 `error_kind`：`probe` / `platform` / `security` / `network` / `auth` / `business` / `timeout` / `internal`。

---

## 依赖

- **Python 3.8+**（仅标准库）
- 可选：`cryptography`（仅用于 `state.vscdb` 回退路径，缺失不影响主流程）

---

## 风险与免责声明

- 本工具代表**使用者本人**在其**本人账号**上完成每日签到的自动化。**请勿**用于批量账号、代他人操作或任何形式的刷量。
- 使用前请自行确认符合 WorkBuddy 的服务条款；若条款不允许自动化操作，请勿使用。
- 遍历进程内存属敏感操作，个别安全软件可能告警。若企业设备策略不允许，请勿使用。
- 本项目按"现状"提供，不对因使用产生的任何后果负责。

## 卸载

```bash
python3 ~/.workbuddy/skills/workbuddy-daily-checkin/install.py --uninstall
```
另外请在 WorkBuddy 的定时任务列表中删除对应的每日任务。

## 许可

[MIT](LICENSE)

---

<sub>关键词：WorkBuddy，每日签到，自动签到，自动领积分，签到脚本，WorkBuddy 积分，定时任务，Skill，跨平台。</sub>
