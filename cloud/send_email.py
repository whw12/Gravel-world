"""把任意一份 Markdown 报告发到邮箱，用于午盘/尾盘等 Trae 任务复用。

用法：
    python cloud/send_email.py --task=午盘 --file=cloud/reports/2026-11-03.md
"""
import os
import sys
from datetime import datetime

import premarket_report as core

BJ = core.BJ


def main():
    core.load_env_file()
    task_name = "报告"
    file_path = ""
    for arg in sys.argv:
        if arg.startswith("--task="):
            task_name = arg.split("=", 1)[1].strip() or task_name
        if arg.startswith("--file="):
            file_path = arg.split("=", 1)[1].strip()

    if not file_path or not os.path.exists(file_path):
        print("用法：python cloud/send_email.py --task=午盘 --file=<报告路径>")
        return 1

    with open(file_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    content = content.rstrip() + "\n\n> 任务来源：%s｜生成于 %s｜由 cloud/send_email.py 发出\n" % (
        task_name, datetime.now(BJ).strftime("%Y-%m-%d %H:%M"))

    mail_pass = os.environ.get("MAIL_PASS", "").strip()
    if not mail_pass:
        print("未配置 MAIL_PASS（cloud/.env 或环境变量），仅输出内容。")
        print(content)
        return 0

    user = os.environ.get("MAIL_USER", "422745040@qq.com").strip()
    to_addr = os.environ.get("MAIL_TO", user).strip()
    host = os.environ.get("MAIL_HOST", "smtp.qq.com").strip()
    port = os.environ.get("MAIL_PORT", "465").strip()
    subject = "【%s】A股报告 %s" % (task_name, datetime.now(BJ).strftime("%Y-%m-%d"))

    print(core.push_email(host, port, user, mail_pass, to_addr, subject, content))
    return 0


if __name__ == "__main__":
    sys.exit(main())
