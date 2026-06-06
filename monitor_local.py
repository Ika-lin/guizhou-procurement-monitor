"""
贵州政府采购 — 策划/广告/品牌/宣传类 自动监控推送
=================================================
每天查询贵州省公共资源交易云 API，筛选相关招标公告，
去重后生成报告 + 弹出 Windows 桌面通知。

用法：
    python monitor.py              # 正常模式（去重，只推新内容）
    python monitor.py --force      # 强制模式（忽略去重，重新推送最近内容）
    python monitor.py --test       # 测试模式（只看不写 seen.json）
"""

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import urllib.request
import urllib.error

# Windows 控制台默认 GBK，强制 UTF-8 以支持中文和 emoji
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 配置 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen.json"
REPORT_FILE = BASE_DIR / "latest_report.md"
ENV_FILE = BASE_DIR / ".env"
API_URL = "https://ggzy.guizhou.gov.cn/tradeInfo/es/list"

# 监控关键词
KEYWORDS = ["策划", "广告", "品牌", "宣传", "创意", "营销", "活动执行"]

# 时区：北京时间
BJT = timezone(timedelta(hours=8))

# WxPusher 配置
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


def load_env() -> dict:
    """加载 .env 配置"""
    env = {}
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    return env

# ── 工具函数 ──────────────────────────────────────

def call_api(keyword: str, page_size: int = 10) -> list[dict]:
    """调用 API，按关键词搜索，返回公告列表"""
    body = json.dumps({
        "channelId": "5904543",
        "pageNum": 1,
        "pageSize": page_size,
        "docTitle": keyword
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("list", [])
    except urllib.error.URLError as e:
        print(f"[ERROR] API 请求失败 ({keyword}): {e}")
        return []


def ts_to_date(ts_ms: int | str) -> datetime:
    """Unix 毫秒时间戳 → 北京时间 datetime"""
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=BJT)


def is_today(ts_ms: int | str) -> bool:
    """判断时间戳是否为今天（北京时间）"""
    d = ts_to_date(ts_ms)
    today = datetime.now(BJT).date()
    return d.date() == today


def is_recent(ts_ms: int | str, days: int = 3) -> bool:
    """判断时间戳是否在最近 N 天内"""
    d = ts_to_date(ts_ms)
    cutoff = datetime.now(BJT).date() - timedelta(days=days)
    return d.date() >= cutoff


def load_seen() -> dict:
    """加载已推送记录"""
    if SEEN_FILE.exists():
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen: dict):
    """保存已推送记录"""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def extract_meta_id(url: str) -> str:
    """从 apiUrl 提取 metaId"""
    if "metaId=" in url:
        return url.split("metaId=")[-1]
    return url


def push_windows_toast(title: str, body: str):
    """Windows Toast 通知（通过 PowerShell）"""
    ps_script = f'''
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $template.GetElementsByTagName("text").Item(0).AppendChild(
        $template.CreateTextNode("{title}")) > $null
    $template.GetElementsByTagName("text").Item(1).AppendChild(
        $template.CreateTextNode("{body}")) > $null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
        "Guizhou Procurement Monitor")
    $notification = [Windows.UI.Notifications.ToastNotification]::new($template)
    $notifier.Show($notification)
    '''
    try:
        # 用 PowerShell 5.1 兼容的方式
        subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, timeout=10
        )
    except Exception as e:
        print(f"[WARN] 桌面通知失败: {e}")


# ── 核心逻辑 ──────────────────────────────────────

def fetch_all(keywords: list[str], page_size: int = 10) -> list[dict]:
    """并行搜索所有关键词，去重后返回"""
    all_items = {}
    for kw in keywords:
        results = call_api(kw, page_size)
        for item in results:
            mid = extract_meta_id(item.get("apiUrl", ""))
            if mid and mid not in all_items:
                all_items[mid] = {
                    "metaId": mid,
                    "title": item.get("docTitle", ""),
                    "timestamp": int(item.get("docRelTime", 0)),
                    "source": item.get("docSourceName", ""),
                    "announcement": item.get("announcement", ""),
                    "businessType": item.get("businessTypeName", ""),
                    "url": item.get("apiUrl", ""),
                    "keyword": kw
                }
    return sorted(all_items.values(), key=lambda x: x["timestamp"], reverse=True)


def push_wxpusher(items: list[dict], new_count: int, env: dict):
    """通过 WxPusher 推送微信消息（支持多人）"""
    app_token = env.get("WXPUSHER_APP_TOKEN", "")
    uids_str = env.get("WXPUSHER_UIDS", env.get("WXPUSHER_UID", ""))  # 兼容旧配置
    uids = [u.strip() for u in uids_str.split(",") if u.strip()]

    if not app_token or not uids:
        print("[WARN] WxPusher 未配置，跳过微信推送（请创建 .env 文件）")
        return False

    if not items:
        return False

    # 构建 Markdown 消息
    today_str = datetime.now(BJT).strftime("%m/%d %H:%M")
    lines = [
        f"## 📡 贵州采购新公告 ({today_str})",
        f"**新增 {new_count} 条** · 策划/广告/品牌/宣传",
        "",
        "---",
        ""
    ]

    current_date = ""
    for item in items[:15]:  # 微信单条消息长度有限，最多15条
        d = ts_to_date(item["timestamp"])
        date_str = d.strftime("%m-%d")
        time_str = d.strftime("%H:%M")

        if date_str != current_date:
            current_date = date_str
            lines.append(f"### 📅 {date_str}")
            lines.append("")

        # 类型标签
        atype = item["announcement"]
        tag = "🟢" if "采购" in atype else ("🟡" if "需求" in atype else "🔵")
        lines.append(f"**{item['title']}**")
        lines.append(f"> {tag} {atype} | `{item['keyword']}` | {item['source']} | {time_str}")
        lines.append(f"> [查看详情]({item['url']})")
        lines.append("")

    content = "\n".join(lines)

    body = json.dumps({
        "appToken": app_token,
        "content": content,
        "contentType": 3,  # Markdown
        "uids": uids
    }).encode("utf-8")

    req = urllib.request.Request(
        WXPUSHER_API,
        data=body,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 1000:
                print(f"✅ 微信推送成功（{new_count} 条）")
                return True
            else:
                print(f"[WARN] 微信推送失败: {result.get('msg', '未知错误')}")
                return False
    except Exception as e:
        print(f"[WARN] 微信推送异常: {e}")
        return False


def generate_report(items: list[dict], mode: str, new_count: int) -> str:
    """生成 Markdown 报告"""
    today_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 📡 贵州政府采购监控报告",
        f"**生成时间**: {today_str} (北京时间) | **模式**: {mode}",
        f"**本次新增**: {new_count} 条 | **展示**: {len(items)} 条",
        "",
        "---",
        ""
    ]

    if not items:
        lines.append("> 🫧 暂无策划/广告/品牌/宣传类的新公告。")
        return "\n".join(lines)

    # 按日期分组
    current_date = ""
    for item in items:
        d = ts_to_date(item["timestamp"])
        date_str = d.strftime("%Y-%m-%d")
        time_str = d.strftime("%H:%M")

        if date_str != current_date:
            current_date = date_str
            lines.append(f"## 📅 {date_str}")
            lines.append("")

        lines.append(f"### {item['title']}")
        lines.append(f"- **类型**: {item['announcement']} | **关键词**: `{item['keyword']}`")
        lines.append(f"- **来源**: {item['source']} | **时间**: {time_str}")
        lines.append(f"- **链接**: [查看详情]({item['url']})")
        lines.append("")

    return "\n".join(lines)


def main():
    mode = "normal"
    if "--force" in sys.argv:
        mode = "force"
    elif "--test" in sys.argv:
        mode = "test"

    print(f"🔍 贵州政府采购监控启动 (模式: {mode})")
    print(f"   查询关键词: {KEYWORDS}")
    env = load_env()
    print(f"   WxPusher: {'已配置' if env.get('WXPUSHER_APP_TOKEN') else '未配置'}")
    print()

    # 1. 查询 API
    print("📡 查询 API...")
    all_items = fetch_all(KEYWORDS, page_size=10)
    print(f"   获取到 {len(all_items)} 条去重结果")

    # 2. 加载已推送记录
    seen = load_seen() if mode != "force" else {}
    print(f"   已推送记录: {len(seen)} 条")

    # 3. 过滤
    today_items = [i for i in all_items if is_today(i["timestamp"])]
    print(f"   今天发布: {len(today_items)} 条")

    if mode == "force":
        # 强制模式：展示最近3天
        candidates = [i for i in all_items if is_recent(i["timestamp"], 3)]
    elif today_items:
        candidates = today_items
    else:
        # 兜底：今天没有 → 最近3天
        print("   ⚠️ 今天无更新，切换至兜底模式（最近3天）")
        candidates = [i for i in all_items if is_recent(i["timestamp"], 3)]

    # 4. 去重：只保留未见过的
    new_items = [i for i in candidates if i["metaId"] not in seen]
    old_items = [i for i in candidates if i["metaId"] in seen]

    # 如果新内容太少，加入最近已推送的内容凑数
    display_items = new_items[:20]  # 最多展示20条
    if len(display_items) < 5 and old_items:
        display_items += old_items[: (10 - len(display_items))]

    # 5. 标记为新已推送
    if mode != "test":
        for item in new_items:
            seen[item["metaId"]] = datetime.now(BJT).isoformat()
        save_seen(seen)

    # 6. 生成报告
    report = generate_report(display_items, mode, len(new_items))
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"📄 报告已写入: {REPORT_FILE}")

    # 7. 微信推送（有新增才推）
    if new_items:
        push_wxpusher(new_items, len(new_items), env)
    else:
        print("🫧 无新公告，跳过微信推送")

    # 8. 控制台摘要
    print()
    print("─── 本次推送内容 ───")
    for item in display_items[:10]:
        d = ts_to_date(item["timestamp"])
        print(f"  [{d.strftime('%m-%d %H:%M')}] {item['title'][:60]}")
    print(f"  ... 共 {len(display_items)} 条")
    print("✅ 完成")


if __name__ == "__main__":
    main()
