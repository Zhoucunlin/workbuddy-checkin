#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到脚本
==========================

基于社区实测的官方接口（Bearer Token 鉴权，幂等安全）：
  - 查询状态: POST {base}/v2/billing/meter/checkin-activity-status
  - 每日签到: POST {base}/v2/billing/meter/daily-checkin

特性：
  * 无需模拟登录 —— 直接携带 accessToken 调用接口（服务端 Bearer 鉴权）
  * 多账号支持：环境变量(base64 JSON) / 明文 JSON / 单 Token / 本地 config.json
  * 幂等：已签到自动跳过（code=10001 已签到，重复调用不会扣分）
  * 结果日志：stdout 实时输出，可选写入 checkin.log
  * 退出码：全部账号处理完毕且无失败 → 0；任一账号失败 → 1（供 CI 判断）

运行：
  python checkin.py                # 读本地 config.json
  python checkin.py --log-file checkin.log   # 额外写日志文件
"""

import argparse
import base64
import json
import logging
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

# 社区实测域名；若 404 可通过环境变量 WORKBUDDY_API_BASE 切换备选域名
# 备选: https://copilot.tencent.com
API_BASE = os.environ.get("WORKBUDDY_API_BASE", "https://www.codebuddy.cn")
CHECKIN_URL = f"{API_BASE}/v2/billing/meter/daily-checkin"
STATUS_URL = f"{API_BASE}/v2/billing/meter/checkin-activity-status"

REQUEST_TIMEOUT = 30  # 秒

# 日志统一使用北京时间（GitHub Actions runner 默认 UTC）
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()

log = logging.getLogger("workbuddy-checkin")

# 服务端"已签到"的返回特征（幂等跳过判定）
ALREADY_CHECKED_HINTS = ("已签到", "请明天再来", "重复领取", "10001", "already", "duplicated")


def setup_logging(log_file: str | None) -> None:
    """配置日志：stdout 必开，可选文件输出。"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# 账号配置加载（优先级从高到低）
# ---------------------------------------------------------------------------

def _normalize_accounts(raw) -> list[dict]:
    """把各种输入格式归一化为 [{"name": str, "token": str}, ...]。"""
    if isinstance(raw, dict):
        raw = raw.get("accounts", raw)
    if not isinstance(raw, list) or not raw:
        raise ValueError("账号配置必须是非空数组，形如 [{\"name\": \"A\", \"token\": \"...\"}]")

    accounts = []
    for i, item in enumerate(raw):
        if isinstance(item, str):  # 兼容 ["token1", "token2"] 简写
            accounts.append({"name": f"account-{i + 1}", "token": item.strip()})
        elif isinstance(item, dict):
            token = (item.get("token") or "").strip()
            if not token or token in ("your_access_token", "粘贴你的accessToken"):
                raise ValueError(f"账号 #{i + 1} 缺少有效 token")
            accounts.append({
                "name": (item.get("name") or f"account-{i + 1}").strip(),
                "token": token,
            })
        else:
            raise ValueError(f"账号 #{i + 1} 格式非法")
    return accounts


def load_accounts() -> list[dict]:
    """读取账号配置，返回 [{"name", "token"}] 列表。

    优先级：
      1. WORKBUDDY_ACCOUNTS_B64  —— base64 编码的 JSON 数组（GitHub Secrets 推荐）
      2. WORKBUDDY_ACCOUNTS_JSON —— 明文 JSON 数组
      3. WORKBUDDY_ACCESS_TOKEN  —— 单账号 Token
      4. 同目录 config.json        —— 本地调试用（已被 .gitignore 排除）
    """
    # 1. base64 JSON（避免 Secret 中引号/换行的转义问题）
    b64 = os.environ.get("WORKBUDDY_ACCOUNTS_B64")
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
            return _normalize_accounts(json.loads(raw))
        except (ValueError, json.JSONDecodeError) as e:
            raise SystemExit(f"[错误] WORKBUDDY_ACCOUNTS_B64 解码失败：{e}")

    # 2. 明文 JSON
    raw_json = os.environ.get("WORKBUDDY_ACCOUNTS_JSON")
    if raw_json:
        try:
            return _normalize_accounts(json.loads(raw_json))
        except (ValueError, json.JSONDecodeError) as e:
            raise SystemExit(f"[错误] WORKBUDDY_ACCOUNTS_JSON 解析失败：{e}")

    # 3. 单账号 Token
    token = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    if token:
        return _normalize_accounts([{"name": "default", "token": token}])

    # 4. 本地 config.json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                return _normalize_accounts(json.load(f))
        except (ValueError, json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"[错误] config.json 读取失败：{e}")

    raise SystemExit(
        "[错误] 未找到任何账号配置。\n"
        "  GitHub Actions 环境：请在仓库 Secrets 中配置 WORKBUDDY_ACCOUNTS_B64\n"
        "  本地环境：复制 config.example.json 为 config.json 并填入 Token"
    )


# ---------------------------------------------------------------------------
# HTTP 请求与响应解析
# ---------------------------------------------------------------------------

def _request_json(url: str, token: str) -> dict:
    """携带 Bearer Token 发起 POST 请求并返回 JSON。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }
    resp = requests.post(url, headers=headers, json={}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"响应不是合法 JSON（HTTP {resp.status_code}）: {resp.text[:200]}")


def unwrap_data(payload: dict) -> dict | None:
    """兼容 {code, data, message} 包装结构，返回 data 部分；否则原样返回。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def is_already_checked(raw: dict, data: dict | None) -> bool:
    """判断是否"今日已签到"（幂等跳过）。"""
    if data and data.get("today_checked_in") is True:
        return True
    text = json.dumps(raw, ensure_ascii=False)
    return any(hint in text for hint in ALREADY_CHECKED_HINTS)


def extract_points(data: dict | None):
    """兼容多种积分字段名。"""
    if not isinstance(data, dict):
        return None
    for key in ("credit", "today_credit", "points", "reward_points", "daily_credit", "balance"):
        val = data.get(key)
        if val is not None:
            return val
    return None


# ---------------------------------------------------------------------------
# 核心签到逻辑
# ---------------------------------------------------------------------------

def checkin_account(name: str, token: str) -> dict:
    """处理单个账号，返回结果字典。"""
    result = {"name": name, "ok": False, "already": False, "points": None, "error": None}

    # 步骤 1：查询签到状态（失败不阻断，直接尝试签到）
    try:
        raw = _request_json(STATUS_URL, token)
        data = unwrap_data(raw)
        if data is not None:
            log.info(
                "[%s] 状态查询: active=%s today_checked_in=%s streak_days=%s",
                name,
                data.get("active"),
                data.get("today_checked_in"),
                data.get("streak_days"),
            )
        if is_already_checked(raw, data):
            result.update(ok=True, already=True)
            log.info("[%s] 今日已签到，无需重复领取 ✓", name)
            return result
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] 状态查询失败（%s），继续尝试签到…", name, e)

    # 步骤 2：执行每日签到
    try:
        raw = _request_json(CHECKIN_URL, token)
        data = unwrap_data(raw)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"HTTP 请求失败: {e}"
        log.error("[%s] ✗ 签到失败：%s", name, result["error"])
        return result

    # 已签到（幂等返回）
    if is_already_checked(raw, data):
        result.update(ok=True, already=True)
        log.info("[%s] 今日已签到（幂等返回），无需重复领取 ✓", name)
        return result

    # 业务层失败：code != 0 且不是"已签到"
    code = raw.get("code") if isinstance(raw, dict) else None
    msg = raw.get("message") if isinstance(raw, dict) else None
    if code not in (None, 0):
        result["error"] = f"业务失败 code={code} message={msg or raw}"
        log.error("[%s] ✗ 签到未成功：%s", name, result["error"])
        return result

    # 成功
    points = extract_points(data)
    result.update(ok=True, points=points)
    log.info(
        "[%s] ✓ 签到成功%s",
        name,
        f"，获得积分 {points}" if points is not None else "",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy 每日自动签到")
    parser.add_argument("--log-file", help="将日志同时写入该文件（默认不写文件）")
    args = parser.parse_args()
    setup_logging(args.log_file)

    try:
        accounts = load_accounts()
    except SystemExit as e:
        log.error("%s", e)
        return 2

    log.info("接口域名: %s | 待处理账号数: %d", API_BASE, len(accounts))
    log.info("-" * 60)

    results = []
    for acc in accounts:
        # 防御：Token 脱敏展示
        mask = acc["token"][:6] + "****" + acc["token"][-4:]
        log.info("[%s] token=%s", acc["name"], mask)
        results.append(checkin_account(acc["name"], acc["token"]))

    log.info("-" * 60)
    ok = sum(1 for r in results if r["ok"])
    already = sum(1 for r in results if r["already"])
    failed = sum(1 for r in results if not r["ok"])
    log.info("汇总: 共 %d 个账号 | 签到成功 %d | 已签到跳过 %d | 失败 %d", len(results), ok, already, failed)

    if failed:
        log.error("存在失败账号，本次运行标记为失败（exit=1）")
        return 1
    log.info("全部账号处理完毕 ✓（exit=0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
