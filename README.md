# WorkBuddy 每日自动签到（Python + GitHub Actions）

基于 WorkBuddy 官方接口（社区实测）的无人值守每日签到工具：**无需模拟登录**，直接携带 `accessToken` 调用接口即可，幂等安全（重复调用不会扣分）。

## 功能特性

| 需求 | 实现 |
|---|---|
| 每日签到 | POST `/v2/billing/meter/daily-checkin`，Bearer Token 鉴权 |
| 定时自动运行 | GitHub Actions `cron: "0 0 * * *"`（每天 08:00 UTC+8） |
| 结果日志 | stdout 实时输出 + 可选文件日志，成功/失败/已签到明确区分 |
| 多账号 | 一个 Secret 存全部账号（base64 JSON 数组），脚本循环处理 |
| 安全 | Token 只经 GitHub Secrets 注入环境变量，不进代码库 |
| 幂等 | 已签到自动跳过（服务端返回 code=10001 时识别，不重复领取） |

> 签到周期规则：7 天一个循环，第 1–6 天每天 100 分，第 7 天一次性 1000 分，单周期共 1600 分。

## 文件结构

```
workbuddy-checkin/
├── checkin.py                  # 主签到脚本（多账号 / 幂等 / 日志 / 退出码）
├── requirements.txt            # 依赖：requests
├── config.example.json         # 本地运行配置示例（勿直接填真实 Token）
├── .gitignore                  # 排除 config.json 与日志
├── .github/
│   └── workflows/
│       └── checkin.yml         # GitHub Actions 定时任务（每日 08:00 UTC+8）
└── README.md                   # 本文档
```

## 一、获取 Token（accessToken）

Token 是桌面端登录态，**约 60 天有效**，只要经常打开 WorkBuddy 会自动刷新。获取方式二选一：

**方式 A：桌面端本地文件**（推荐，来源可靠）

WorkBuddy 桌面端会把登录态写入本机：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

打开该 JSON 文件，取 `auth.accessToken` 字段的值。Windows 可在资源管理器地址栏粘贴上述路径直达，或用 PowerShell 查看：

```powershell
(Get-Content "$env:LOCALAPPDATA\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info" | ConvertFrom-Json).auth.accessToken
```

**方式 B：浏览器开发者工具**

1. 浏览器打开 WorkBuddy 网页版并登录
2. F12 → Network（网络）→ 手动点一次签到
3. 在 `daily-checkin` 请求的 Request Headers 中复制 `Authorization: Bearer <token>` 后面的部分

> ⚠️ Token 等同账号凭证，泄露后可被冒用签到/扣积分，请勿提交到代码仓库或公开渠道。若怀疑泄露，可重新登录 WorkBuddy 使其失效。

## 二、本地运行（测试）

```bash
cd workbuddy-checkin
pip install -r requirements.txt

# 方式 1：复制示例配置并填入 Token
cp config.example.json config.json
python checkin.py

# 方式 2：单账号环境变量（无需 config.json）
export WORKBUDDY_ACCESS_TOKEN="你的token"
python checkin.py

# 方式 3：多账号明文 JSON 环境变量
export WORKBUDDY_ACCOUNTS_JSON='[{"name":"A","token":"xxx"},{"name":"B","token":"yyy"}]'
python checkin.py

# 额外写日志文件
python checkin.py --log-file checkin.log
```

日志示例：

```
2026-08-12 08:00:01 [INFO] 接口域名: https://www.codebuddy.cn | 待处理账号数: 2
2026-08-12 08:00:01 [INFO] ------------------------------------------------------------
2026-08-12 08:00:01 [INFO] [账号A] token=eyJhb****AbCd
2026-08-12 08:00:02 [INFO] [账号A] 状态查询: active=True today_checked_in=False streak_days=3
2026-08-12 08:00:03 [INFO] [账号A] ✓ 签到成功，获得积分 100
2026-08-12 08:00:04 [INFO] [账号B] 今日已签到，无需重复领取 ✓
2026-08-12 08:00:05 [INFO] ------------------------------------------------------------
2026-08-12 08:00:05 [INFO] 汇总: 共 2 个账号 | 签到成功 1 | 已签到跳过 1 | 失败 0
2026-08-12 08:00:05 [INFO] 全部账号处理完毕 ✓（exit=0）
```

## 三、部署到 GitHub Actions

1. **创建仓库**：把本项目推送到 GitHub（公开/私有均可）。
2. **配置 Secret**：仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
   - Name：`WORKBUDDY_ACCOUNTS_B64`
   - Value：对账号 JSON 做 base64 编码后的字符串（推荐此方式，避免 JSON 引号在 Secret 中的转义问题）

   生成方法（PowerShell / 本地任一 Python 环境）：

   ```powershell
   # PowerShell
   [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('[{"name":"账号A","token":"xxx"},{"name":"账号B","token":"yyy"}]'))
   ```

   ```python
   # Python
   import base64
   accounts = '[{"name":"账号A","token":"xxx"},{"name":"账号B","token":"yyy"}]'
   print(base64.b64encode(accounts.encode()).decode())
   ```

3. **推送代码**：workflow 位于 `.github/workflows/checkin.yml`，推送后自动生效。
4. **验证**：仓库 → `Actions` 页面可看到 `WorkBuddy 每日签到` 工作流，点 **Run workflow** 可手动触发测试一次。
5. **后续维护**：每天 08:00（UTC+8）自动运行。Token 过期（约 60 天不打开客户端）时日志会出现失败提示，重新获取 Token 并更新 Secret 即可。

## 四、多账号说明

所有账号放在同一个 Secret 的 JSON 数组里：

```json
[
  {"name": "账号A", "token": "token-1"},
  {"name": "账号B", "token": "token-2"},
  {"name": "账号C", "token": "token-3"}
]
```

脚本按顺序逐个处理，每个账号独立记录日志；任一失败不影响其他账号，但本次运行以失败退出（便于在 Actions 页面直接看到红叉告警）。

## 五、常见问题

| 现象 | 原因与处理 |
|---|---|
| 日志报 HTTP 401/403 | Token 无效或过期，重新获取 Token 并更新 Secret |
| 提示"今日已签到" | 正常，接口幂等，跳过即可 |
| 一直 HTTP 404 | 接口域名可能变更，用环境变量 `WORKBUDDY_API_BASE` 切换到 `https://copilot.tencent.com` 再试 |
| Actions 触发时间不准 | GitHub 的 schedule 有 15 分钟～数小时延迟，属平台机制，不影响每日领取（当天任意时刻签一次即可） |
| 想换签到时间 | 修改 `checkin.yml` 中 `cron`（注意是 UTC 时间：UTC+8 的目标时间减 8 小时） |

## 免责声明

本工具仅用于自动化调用 WorkBuddy 官方签到接口，方便个人每日领取积分。请遵守 WorkBuddy 服务条款，Token 属于个人登录凭证，请勿分享给他人或用于任何商业用途。
