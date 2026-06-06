"""
腾讯云云函数 SCF 入口
每天定时查询贵州政府采购 API，通过 WxPusher 推送到微信。
"""

import json
import os
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

API_URL = "https://ggzy.guizhou.gov.cn/tradeInfo/es/list"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
KEYWORDS = ["策划", "广告", "品牌", "宣传", "创意", "营销", "活动执行"]
BJT = timezone(timedelta(hours=8))


def call_api(keyword: str) -> list:
    body = json.dumps({
        "channelId": "5904543", "pageNum": 1, "pageSize": 10, "docTitle": keyword
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": "Mozilla/5.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("list", [])
    except Exception as e:
        print(f"[ERROR] {keyword}: {e}")
        return []


def fetch_all() -> list:
    seen = set()
    items = []
    for kw in KEYWORDS:
        for item in call_api(kw):
            mid = item.get("apiUrl", "").split("metaId=")[-1]
            if mid not in seen:
                seen.add(mid)
                items.append({
                    "title": item.get("docTitle", ""),
                    "timestamp": int(item.get("docRelTime", 0)),
                    "source": item.get("docSourceName", ""),
                    "announcement": item.get("announcement", ""),
                    "url": item.get("apiUrl", ""),
                    "keyword": kw
                })
    return sorted(items, key=lambda x: x["timestamp"], reverse=True)


def push_wxpusher(items: list):
    app_token = os.environ.get("WXPUSHER_APP_TOKEN", "")
    uids_str = os.environ.get("WXPUSHER_UIDS", "")
    uids = [u.strip() for u in uids_str.split(",") if u.strip()]

    if not app_token or not uids or not items:
        return

    now = datetime.now(BJT).strftime("%m/%d %H:%M")
    lines = [
        f"## 📡 贵州采购 · 新公告 ({now})",
        f"**{len(items)} 条** · 策划/广告/品牌/宣传",
        "---", ""
    ]
    current_date = ""
    for item in items[:15]:
        d = datetime.fromtimestamp(item["timestamp"] / 1000, tz=BJT)
        ds = d.strftime("%m-%d")
        if ds != current_date:
            current_date = ds
            lines.append(f"### 📅 {ds}")
            lines.append("")
        atype = item["announcement"]
        tag = "🟢" if "采购" in atype else ("🟡" if "需求" in atype else "🔵")
        lines.append(f"**{item['title']}**")
        lines.append(f"> {tag} {atype} | `{item['keyword']}` | {item['source']} | {d.strftime('%H:%M')}")
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
            print(f"WxPusher: {result.get('code')} - {result.get('msg')}")
    except Exception as e:
        print(f"WxPusher error: {e}")


def main_handler(event, context):
    """腾讯云 SCF 入口函数"""
    print(f"🔍 {datetime.now(BJT).strftime('%Y-%m-%d %H:%M')}")

    items = fetch_all()
    print(f"获取 {len(items)} 条")

    today = datetime.now(BJT).date()
    today_items = [i for i in items
                   if datetime.fromtimestamp(i["timestamp"] / 1000, tz=BJT).date() == today]

    if today_items:
        candidates = today_items
        print(f"今天: {len(candidates)} 条 → 推送")
    else:
        cutoff = today - timedelta(days=2)
        candidates = [i for i in items
                      if datetime.fromtimestamp(i["timestamp"] / 1000, tz=BJT).date() >= cutoff]
        print(f"今天无更新，最近2天: {len(candidates)} 条")

    if candidates:
        push_wxpusher(candidates[:15])
        print("✅ 完成")
    else:
        print("无内容，跳过")

    return {"code": 0, "msg": "ok"}
