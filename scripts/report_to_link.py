#!/usr/bin/env python3
"""把报告文件自动转成 Cloudflare 链接，邮件里只发链接、不贴附件，免聊天确认。

用法:
    report_to_link.py <报告文件> [--name 页面文件名]

做三件事:
1. 把报告文件拷进部署目录 ~/workspace/harness-reports-deploy/（默认用原文件名）
2. 整个目录部署到 Cloudflare Pages 项目 harness-reports（生产分支 main）
3. 打印公开链接 https://harness-reports.pages.dev/<页面文件名>

部署走 custom.cloudflare 连接器的 cf_pages.py（它自己的 venv 解释器）。
注意：cf_pages.py 不带 --project 时默认发到正式站 wire-harness-tool，
这里一律显式带 --project harness-reports --branch main。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

DEPLOY_DIR = Path.home() / "workspace" / "harness-reports-deploy"
CF_PY = Path.home() / "workspace" / "custom-connectors" / "custom.cloudflare" / ".venv" / "bin" / "python"
CF_CLI = Path.home() / "workspace" / "custom-connectors" / "custom.cloudflare" / "bin" / "cf_pages.py"
BASE_URL = "https://harness-reports.pages.dev"


def main() -> int:
    ap = argparse.ArgumentParser(description="报告文件转 Cloudflare 链接")
    ap.add_argument("file", help="报告文件路径（HTML 等）")
    ap.add_argument("--name", default=None, help="页面文件名（默认用原文件名）")
    args = ap.parse_args()

    src = Path(args.file).expanduser()
    if not src.is_file():
        print(f"文件不存在: {src}", file=sys.stderr)
        return 1

    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    page_name = args.name or src.name
    dst = DEPLOY_DIR / page_name
    shutil.copy2(src, dst)
    print(f"已拷入部署目录: {dst}", file=sys.stderr)

    cmd = [
        str(CF_PY), str(CF_CLI), "deploy",
        "--dir", str(DEPLOY_DIR),
        "--branch", "main",
        "--project", "harness-reports",
        "--commit-message", f"报告上链接: {page_name}",
    ]
    print("部署中…", file=sys.stderr)
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        print("部署失败，见上面输出", file=sys.stderr)
        return r.returncode

    url = f"{BASE_URL}/{quote(page_name)}"
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
