"""盘前报告云端版：行情 + 资金 + 涨跌家数 + 政策速览 -> Markdown -> 飞书推送 -> 本地存档

依赖：requests
环境变量：
  FEISHU_WEBHOOK   飞书自定义机器人 webhook（必填，缺失则只打印不推送）
  IFIND_TOKEN      同花顺 iFinD 密钥（可选，用于政策/新闻检索）
用法：
  python premarket_report.py            # 正常执行并推送
  python premarket_report.py --dry-run  # 只生成报告，不推送
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

BJ = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(ROOT, "reports")

INDEX_CODES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000510", "中证A500"),
    ("sh000016", "上证50"),
]

ETF_CODES = [
    ("sh588000", "科创50ETF"),
    ("sh510500", "中证500ETF"),
    ("sz159915", "创业板ETF"),
    ("sh512880", "证券ETF"),
    ("sh510300", "沪深300ETF"),
    ("sh518880", "黄金ETF"),
]

POLICY_KEYWORDS = [
    "央行", "证监会", "金融监管总局", "降准", "降息", "逆回购", "互换便利", "SFISF",
    "再贷款", "财政部", "发改委", "国务院", "LPR", "外汇局", "工作会议", "稳市",
]
# 出现这些词说明是个股新闻而非宏观政策，直接剔除
POLICY_EXCLUDE = ["早盘涨", "涨超", "跌超", "盘中涨", "盘中跌", "获受理", "中标", "股东", "业绩"]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url, timeout=20, encoding=None):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    if encoding:
        resp.encoding = encoding
    return resp


def fetch_quotes():
    codes = ",".join([c for c, _ in INDEX_CODES] + [c for c, _ in ETF_CODES])
    text = _get("https://qt.gtimg.cn/q=" + codes, encoding="gbk").text
    rows = []
    for line in text.strip().split("\n"):
        parts = line.split("~")
        if len(parts) < 40:
            continue
        try:
            rows.append({
                "code": parts[2],
                "name": parts[1],
                "price": float(parts[3]),
                "prev": float(parts[4]),
                "open": float(parts[5]),
                "pct": float(parts[32]),
                "high": float(parts[33]),
                "low": float(parts[34]),
                "amount": float(parts[37]) / 10000.0,
                "time": parts[30],
            })
        except (ValueError, IndexError):
            continue
    return rows


def fetch_ma(code, days=60):
    """用日 K 计算 MA5/MA20/MA60，失败返回 None。"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={},day,,,{},qfq".format(code, days + 5)
    try:
        data = _get(url).json()["data"][code]
    except Exception:
        return None
    kline = data.get("qfqday") or data.get("day") or []
    closes = []
    for item in kline:
        try:
            closes.append(float(item[2]))
        except (TypeError, ValueError):
            continue
    if len(closes) < days:
        return None
    out = {}
    for ma in (5, 20, 60):
        if len(closes) >= ma:
            out["MA%d" % ma] = round(sum(closes[-ma:]) / ma, 2)
    return out


def fetch_fund_flow():
    """东方财富主力净流入（元转亿）：上证 / 深证。"""
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get"
        "?fltt=2&fields=f2,f3,f62&secids=1.000001,0.399001"
    )
    try:
        diff = _get(url).json()["data"]["diff"]
        if len(diff) >= 2:
            return {
                "沪": round(float(diff[0].get("f62", 0)) / 1e8, 1),
                "深": round(float(diff[1].get("f62", 0)) / 1e8, 1),
            }
    except Exception:
        return None
    return None


def fetch_breadth():
    """涨跌家数（东方财富）：沪市 / 深市分别返回上涨、下跌、平盘家数。"""
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get"
        "?fltt=2&fields=f104,f105,f106&secids=1.000001,0.399001"
    )
    try:
        diff = _get(url).json()["data"]["diff"]
        if len(diff) < 2:
            return None
        up = int(diff[0]["f104"]) + int(diff[1]["f104"])
        down = int(diff[0]["f105"]) + int(diff[1]["f105"])
        flat = int(diff[0]["f106"]) + int(diff[1]["f106"])
        return {"上涨": up, "下跌": down, "平盘": flat}
    except Exception:
        return None


def fetch_policy_ifind(keyword_list, limit=6):
    """优先用 iFinD news 服务检索政策，失败返回空列表。"""
    token = os.environ.get("IFIND_TOKEN", "").strip()
    if not token:
        return []
    try:
        sys.path.insert(0, os.path.join(ROOT, "..", ".trae", "skills", "ifind-finance-data"))
        from call import call  # noqa: WPS433
        res = call("news", "search_news", {
            "query": " ".join(keyword_list),
            "time_start": (datetime.now(BJ) - timedelta(days=3)).strftime("%Y-%m-%d"),
            "time_end": datetime.now(BJ).strftime("%Y-%m-%d"),
            "size": limit,
        })
    except Exception as exc:
        print("iFinD 检索失败：%s" % exc)
        return []
    items = []
    try:
        raw = res["data"]["result"]["content"][0]["text"]
        payload = json.loads(raw)["data"]["data"]
        for row in json.loads(payload):
            title = str(row.get("资讯标题", "")).strip()
            date = str(row.get("日期", "")).strip()
            url = str(row.get("URL", "")).strip()
            # 时效过滤：只要近 3 个交易日内的
            if date and date < (datetime.now(BJ) - timedelta(days=4)).strftime("%Y-%m-%d"):
                continue
            items.append({"title": title, "date": date, "url": url})
    except Exception:
        return []
    return items


def fetch_policy_sina(limit=12):
    """新浪财经滚动新闻兜底，按关键词筛政策相关条目。"""
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=%d&page=1" % limit
    try:
        data = _get(url, timeout=15).json()["result"]["data"]
    except Exception:
        return []
    out = []
    for row in data:
        title = str(row.get("title", "")).strip()
        if not title:
            continue
        if any(k in title for k in POLICY_KEYWORDS) and not any(k in title for k in POLICY_EXCLUDE):
            stamp = row.get("ctime")
            try:
                ts = datetime.fromtimestamp(int(stamp), BJ).strftime("%m-%d %H:%M")
            except Exception:
                ts = ""
            out.append({"title": title, "date": ts, "url": str(row.get("url", ""))})
    return out[:8]


def fmt_pct(value):
    return "%+.2f%%" % value


def fmt_quote_time(raw):
    """腾讯时间戳 20261103101009 -> 2026-11-03 10:10。"""
    if raw and len(raw) >= 14:
        return "%s-%s-%s %s:%s" % (raw[0:4], raw[4:6], raw[6:8], raw[8:10], raw[10:12])
    return "数据暂缺"


def build_report(quotes, ma_map, flow, breadth, policies, now):
    idx = [q for q in quotes if q["code"] in [c[2:] for c, _ in INDEX_CODES]]
    etf = [q for q in quotes if q["code"] in [c[2:] for c, _ in ETF_CODES]]

    lines = []
    add = lines.append
    add("# A股盘前报告 %s" % now.strftime("%Y-%m-%d %H:%M"))
    add("")
    add("> 数据时点：%s，来源：腾讯行情、东方财富" % fmt_quote_time(quotes[0]["time"] if quotes else ""))
    add("")

    add("## ★ 政策速览（优先级最高）")
    if policies:
        add("| 时间 | 标题 | 来源 |")
        add("|---|---|---|")
        for p in policies:
            add("| %s | %s | %s |" % (p["date"] or "-", p["title"], p["url"] or "-"))
        add("")
        add("> 影响判断需人工确认：请结合政策原文判断受益/受损板块与影响时长（短期情绪 / 中期趋势 / 长期结构）。")
    else:
        add("本时段未检索到政策类新闻（iFinD 与新浪兜底均未命中）。**不排除接口时效不足，需人工复核。**")
    add("")

    add("## 一、主要指数（最近交易日）")
    add("| 指数 | 收盘 | 涨跌 | 最高 | 最低 | 成交额(亿) |")
    add("|---|---|---|---|---|---|")
    for q in idx:
        add("| %s | %.2f | %s | %.2f | %.2f | %.1f |" % (
            q["name"], q["price"], fmt_pct(q["pct"]), q["high"], q["low"], q["amount"]))
    add("")

    add("## 二、均线位置")
    add("| 指数 | 收盘 | MA5 | MA20 | MA60 | 距MA20 |")
    add("|---|---|---|---|---|---|")
    for name, key in [("上证指数", "sh000001"), ("创业板指", "sz399006"), ("科创50", "sh000688")]:
        ma = ma_map.get(key)
        q = next((x for x in quotes if x["code"] == key[2:]), None)
        if not ma or not q:
            add("| %s | 数据暂缺 | - | - | - | - |" % name)
            continue
        dev = (q["price"] - ma["MA20"]) / ma["MA20"] * 100
        add("| %s | %.2f | %.2f | %.2f | %.2f | %s |" % (
            name, q["price"], ma["MA5"], ma["MA20"], ma["MA60"], fmt_pct(dev)))
    add("")

    add("## 三、资金与情绪")
    if flow:
        total = flow["沪"] + flow["深"]
        add("- 主力净流入：沪 %+.1f 亿 / 深 %+.1f 亿 / 两市合计 %+.1f 亿" % (flow["沪"], flow["深"], total))
    else:
        add("- 主力净流入：数据暂缺")
    if breadth:
        add("- 涨跌家数：上涨 %s / 下跌 %s / 平盘 %s" % (breadth["上涨"], breadth["下跌"], breadth["平盘"]))
        try:
            ratio = float(breadth["上涨"]) / float(breadth["下跌"])
            add("- 涨跌家数比：%.2f" % ratio)
        except Exception:
            pass
    else:
        add("- 涨跌家数：数据暂缺")
    add("")

    add("## 四、ETF 观察池")
    add("| ETF | 代码 | 价格 | 涨跌 | 成交额(亿) |")
    add("|---|---|---|---|---|")
    for q in etf:
        add("| %s | %s | %.3f | %s | %.1f |" % (
            q["name"], q["code"], q["price"], fmt_pct(q["pct"]), q["amount"]))
    add("")

    add("## 五、缺口清单")
    gaps = []
    if not flow:
        gaps.append("主力资金（东财接口失败）")
    if not breadth:
        gaps.append("涨跌家数（东财接口失败）")
    if not policies:
        gaps.append("政策新闻（检索未命中，需人工复核）")
    if not ma_map:
        gaps.append("均线（K线接口失败）")
    add("- " + "；".join(gaps) if gaps else "- 无")
    add("")
    add("---")
    add("本报告由脚本自动生成，仅供研究参考，不构成个人投资建议。")
    return "\n".join(lines)


def md_to_lark(text):
    """把 Markdown 转成飞书 lark_md 可读形式：表格改行式，去掉分隔线。"""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if re.fullmatch(r"\|[-:\s|]+\|", stripped):  # 表格分隔行
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c]
            out.append("｜".join(cells))
            continue
        if stripped.startswith("## "):
            out.append("**%s**" % stripped[3:])
            continue
        if stripped.startswith("# "):
            out.append("**%s**" % stripped[2:])
            continue
        if stripped.startswith("> "):
            out.append(stripped[2:])
            continue
        out.append(stripped)
    return "\n".join(out)


def push_lark(app_id, app_secret, open_id, title, content):
    """用飞书开放接口（应用凭证）发送交互卡片，返回 message_id。"""
    token_resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    token_data = token_resp.json()
    if token_data.get("code") != 0:
        raise RuntimeError("取 tenant_access_token 失败：%s" % token_data)
    token = token_data["tenant_access_token"]

    body = md_to_lark(content)
    chunks = [body[i:i + 2600] for i in range(0, len(body), 2600)] or [""]
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": c}} for c in chunks]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"},
        json={"receive_id": open_id, "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=20,
    )
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError("发送消息失败：%s" % result)
    return result["data"]["message_id"]


def md_to_html(text):
    """极简 Markdown -> HTML（覆盖本项目报告用到的语法：标题、表格、列表、引用）。"""
    out = []
    in_table = False
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if re.fullmatch(r"\|[-:\s|]+\|", stripped):
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not in_table:
                out.append('<table cellpadding="6" cellspacing="0" '
                           'style="border-collapse:collapse;border:1px solid #ddd;font-size:13px">')
                out.append("<tr>" + "".join(
                    '<th style="background:#f5f6f8;border:1px solid #ddd;text-align:left">%s</th>' % c
                    for c in cells) + "</tr>")
                in_table = True
            else:
                out.append("<tr>" + "".join(
                    '<td style="border:1px solid #ddd">%s</td>' % c for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if not stripped:
            continue
        if stripped.startswith("## "):
            out.append("<h2 style=\"font-size:15px;margin:18px 0 8px\">%s</h2>" % stripped[3:])
        elif stripped.startswith("# "):
            out.append("<h1 style=\"font-size:18px;margin:0 0 10px\">%s</h1>" % stripped[2:])
        elif stripped.startswith("> "):
            out.append("<blockquote style=\"margin:6px 0;padding-left:10px;"
                       "border-left:3px solid #ddd;color:#666\">%s</blockquote>" % stripped[2:])
        elif stripped.startswith("- "):
            out.append("<li>%s</li>" % stripped[2:])
        else:
            out.append('<p style="margin:6px 0">%s</p>' % stripped)
    if in_table:
        out.append("</table>")
    html = "\n".join(out)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)


def push_email(host, port, user, password, to_addr, subject, content):
    """SMTP 发送 HTML 邮件。"""
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText

    msg = MIMEText(md_to_html(content), "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to_addr
    server = smtplib.SMTP_SSL(host, int(port), timeout=30)
    try:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    finally:
        server.quit()
    return "邮件已发送至 %s" % to_addr


def load_env_file():
    """从 cloud/.env 读取 KEY=VALUE（该文件不入库），仅补齐缺失的环境变量。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def main():
    load_env_file()
    dry_run = "--dry-run" in sys.argv
    task_name = "盘前"
    for arg in sys.argv:
        if arg.startswith("--task="):
            task_name = arg.split("=", 1)[1].strip() or task_name
    now = datetime.now(BJ)
    if now.weekday() >= 5:
        print("周末休市，跳过报告生成。")
        return 0

    quotes = fetch_quotes()
    if not quotes:
        print("行情获取失败，中止。")
        return 1

    ma_map = {}
    for key in ("sh000001", "sz399006", "sh000688"):
        ma = fetch_ma(key)
        if ma:
            ma_map[key] = ma

    flow = fetch_fund_flow()
    breadth = fetch_breadth()
    policies = fetch_policy_ifind(POLICY_KEYWORDS[:8]) or fetch_policy_sina()

    report = build_report(quotes, ma_map, flow, breadth, policies, now)
    report = report.rstrip() + "\n\n> 任务来源：%s｜生成于 %s｜由云端脚本 cloud/premarket_report.py 发出\n" % (
        task_name, now.strftime("%Y-%m-%d %H:%M"))

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, "%s.md" % now.strftime("%Y-%m-%d"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report)
    print("已生成：%s" % path)

    title = "【%s】A股盘前报告 %s" % (task_name, now.strftime("%Y-%m-%d"))
    mail_user = os.environ.get("MAIL_USER", "422745040@qq.com").strip()
    mail_pass = os.environ.get("MAIL_PASS", "").strip()
    mail_to = os.environ.get("MAIL_TO", mail_user).strip()
    mail_host = os.environ.get("MAIL_HOST", "smtp.qq.com").strip()
    mail_port = os.environ.get("MAIL_PORT", "465").strip()

    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    open_id = os.environ.get("LARK_USER_OPEN_ID", "").strip()

    if dry_run:
        print(report)
        return 0

    pushed = False
    if mail_pass:
        try:
            print("邮件推送：%s" % push_email(mail_host, mail_port, mail_user, mail_pass, mail_to, title, report))
            pushed = True
        except Exception as exc:
            print("邮件推送失败：%s" % exc)
    if app_id and app_secret and open_id:
        try:
            print("飞书推送成功 message_id：%s" % push_lark(app_id, app_secret, open_id, title, report))
            pushed = True
        except Exception as exc:
            print("飞书推送失败：%s" % exc)

    if not pushed:
        print(report)
        if mail_pass or (app_id and app_secret and open_id):
            print("\n[提示] 推送渠道均失败，报告已在上方输出并归档。")
            return 2
        print("\n[提示] 未配置 MAIL_PASS 或飞书凭证，仅本地生成，未推送。")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
