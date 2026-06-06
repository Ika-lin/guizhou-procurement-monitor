"""
贵州政府采购 — 云端监控（GitHub Actions 版）
=============================================
每天自动查询贵州省公共资源交易云 API，
筛选策划/广告/品牌/宣传类新公告，通过 WxPusher 推送到微信。

由 GitHub Actions 定时运行，无需本地电脑。
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import urllib.request
import urllib.error

# ── 配置 ──────────────────────────────────────────
API_URL = "https://ggzy.guizhou.gov.cn/tradeInfo/es/list"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"

KEYWORDS = ["策划", "广告", "品牌", "宣传", "创意", "营销", "活动执行"]
BJT = timezone(timedelta(hours=8))


# ── 工具函数 ──────────────────────────────────────

def call_api(keyword: str, page_size: int = 10) -> list[dict]:
    """调用贵州采购 API，按关键词搜索"""
    body = json.dumps({
        "channelId": "5904543",
        "pageNum": 1,
        "pageSize": page_size,
        "docTitle": keyword
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": "Mozilla/5.0"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")).get("list", [])
    except urllib.error.URLError as e:
        print(f"[ERROR] API 失败 ({keyword}): {e}")
        return []


def ts_to_date(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=BJT)


def is_recent(ts_ms: int, days: int = 2) -> bool:
    """是否在最近 N 天内"""
    d = ts_to_date(ts_ms)
    return d.date() >= (datetime.now(BJT).date() - timedelta(days=days))


def is_today(ts_ms: int) -> bool:
    return ts_to_date(ts_ms).date() == datetime.now(BJT).date()


def extract_meta_id(url: str) -> str:
    return url.split("metaId=")[-1] if "metaId=" in url else url


# ── 数据获取 ──────────────────────────────────────

def fetch_all(keywords: list[str]) -> list[dict]:
    """并行搜索所有关键词，去重后按时间倒序"""
    seen_ids = {}
    items = []
    for kw in keywords:
        for item in call_api(kw):
            mid = extract_meta_id(item.get("apiUrl", ""))
            if mid and mid not in seen_ids:
                seen_ids[mid] = True
                items.append({
                    "title": item.get("docTitle", ""),
                    "timestamp": int(item.get("docRelTime", 0)),
                    "source": item.get("docSourceName", ""),
                    "announcement": item.get("announcement", ""),
                    "url": item.get("apiUrl", ""),
                    "keyword": kw
                })
    return sorted(items, key=lambda x: x["timestamp"], reverse=True)


# ── 微信推送 ──────────────────────────────────────

def send_wxpusher(items: list[dict], app_token: str, uids: list[str]):
    """通过 WxPusher 推送 Markdown 消息到微信（支持多人）"""
    if not items:
        return

    now = datetime.now(BJT).strftime("%m/%d %H:%M")
    lines = [
        f"## 📡 贵州采购 · 新公告 ({now})",
        f"**{len(items)} 条** · 策划/广告/品牌/宣传",
        "---", ""
    ]

    current_date = ""
    for item in items[:15]:
        d = ts_to_date(item["timestamp"])
        date_str = d.strftime("%m-%d")
        time_str = d.strftime("%H:%M")
        atype = item["announcement"]

        if date_str != current_date:
            current_date = date_str
            lines.append(f"### 📅 {date_str}")
            lines.append("")

        tag = "🟢" if "采购" in atype else ("🟡" if "需求" in atype else "🔵")
        lines.append(f"**{item['title']}**")
        lines.append(f"> {tag} {atype} | `{item['keyword']}` | {item['source']} | {time_str}")
        lines.append(f"> [查看详情]({item['url']})")
        lines.append("")

    body = json.dumps({
        "appToken": app_token,
        "content": "\n".join(lines),
        "contentType": 3,
        "uids": uids
    }).encode("utf-8")

    req = urllib.request.Request(
        WXPUSHER_API, data=body,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 1000:
                print(f"✅ 微信推送成功 ({len(items)} 条)")
            else:
                print(f"❌ 推送失败: {result.get('msg')}")
                sys.exit(1)
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        sys.exit(1)


# ── 主流程 ────────────────────────────────────────

def main():
    app_token = os.environ.get("WXPUSHER_APP_TOKEN", "")
    uids_str = os.environ.get("WXPUSHER_UIDS", os.environ.get("WXPUSHER_UID", ""))
    uids = [u.strip() for u in uids_str.split(",") if u.strip()]

    if not app_token or not uids:
        print("❌ 未配置 WXPUSHER_APP_TOKEN 或 WXPUSHER_UIDS（GitHub Secrets）")
        sys.exit(1)

    print(f"🔍 贵州政府采购监控 {datetime.now(BJT).strftime('%Y-%m-%d %H:%M')}")
    print(f"   关键词: {KEYWORDS}")

    # 1. 查 API
    all_items = fetch_all(KEYWORDS)
    print(f"   获取 {len(all_items)} 条去重结果")

    # 2. 筛选：今天的内容；兜底查昨天
    today_items = [i for i in all_items if is_today(i["timestamp"])]
    if today_items:
        candidates = today_items
        print(f"   今天发布: {len(candidates)} 条 → 推送")
    else:
        candidates = [i for i in all_items if is_recent(i["timestamp"], 2)]
        if candidates:
            print(f"   今天无更新，推送最近2天: {len(candidates)} 条")
        else:
            print("   最近2天也无更新，跳过")
            return

    # 3. 推送
    send_wxpusher(candidates[:15], app_token, uids)
    print("✅ 完成")


if __name__ == "__main__":
    main()
