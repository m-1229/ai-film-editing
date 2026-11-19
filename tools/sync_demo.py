#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成 GitHub Pages 用的静态演示版（docs/）。

源文件永远是 web/，docs/ 只是生成物，不要手改。
改完界面后重新跑一次本脚本即可。

用法：
    python tools/sync_demo.py
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DEMO = ROOT / "demo"
OUT = ROOT / "docs"

REPO_URL = "https://github.com/m-1229/ai-film-editing"

BANNER = """
    <!-- 在线演示提示：由 tools/sync_demo.py 生成，勿手改 -->
    <div class="demo-badge">
      <b>在线演示</b>
      <span>静态预览，不会真正剪辑 · 完整功能请下载后运行 install.bat</span>
      <a href="%s" target="_blank" rel="noopener">源码</a>
    </div>
""" % REPO_URL


def fail(msg):
    print("[错误] " + msg)
    raise SystemExit(1)


def main():
    if not (WEB / "index.html").exists():
        fail("找不到 web/index.html，请在项目根目录运行本脚本")
    if not (DEMO / "demo.js").exists():
        fail("找不到 demo/demo.js")

    for name in ("app.js", "style.css", "icon.ico", "icon.png"):
        if not (WEB / name).exists():
            fail("web/%s 不存在" % name)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    copied = 0
    for name in ("app.js", "style.css", "icon.ico", "icon.png"):
        shutil.copy2(WEB / name, OUT / name)
        copied += 1

    shutil.copy2(DEMO / "demo.js", OUT / "demo.js")
    shutil.copy2(DEMO / "demo.css", OUT / "demo.css")
    copied += 2

    assets = DEMO / "assets"
    if assets.is_dir():
        shutil.copytree(assets, OUT / "assets")
        copied += len(list((OUT / "assets").iterdir()))

    html = (WEB / "index.html").read_text("utf-8")

    # 1) 绝对路径改成相对路径，否则部署在 /ai-film-editing/ 子路径下会 404
    html = html.replace('href="/web/', 'href="./').replace('src="/web/', 'src="./')
    if "/web/" in html:
        fail("index.html 里还有 /web/ 绝对路径没替换掉")

    # 2) 标题标注演示版，避免和本地应用混淆
    old_title = "<title>AI 剪辑工具</title>"
    if old_title not in html:
        fail("index.html 的 <title> 结构变了，请检查")
    html = html.replace(old_title, "<title>AI 剪辑工具 · 在线演示</title>", 1)

    # 3) 先加载演示层，再加载 app.js，让 fetch 在任何请求发出前就被接管
    script_tag = '<script src="./app.js"></script>'
    if script_tag not in html:
        fail("index.html 里找不到 app.js 的引用")
    html = html.replace(
        script_tag, '<script src="./demo.js"></script>\n' + script_tag, 1
    )

    # 4) 演示样式放在 style.css 之后，确保能覆盖
    if "</head>" not in html:
        fail("index.html 结构异常：没有 </head>")
    html = html.replace(
        "</head>", '  <link rel="stylesheet" href="./demo.css" />\n</head>', 1
    )

    # 5) 顶栏中间加一条提示
    header_tag = '<header class="topbar">'
    if header_tag not in html:
        fail("index.html 里找不到顶栏")
    html = html.replace(header_tag, header_tag + BANNER, 1)

    (OUT / "index.html").write_text(html, encoding="utf-8", newline="\n")

    # 跳过 Jekyll 处理，静态文件原样发布
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print("已生成 docs/：%d 个文件，%.1f KB" % (
        sum(1 for p in OUT.rglob("*") if p.is_file()), size / 1024))
    print()
    print("下一步：GitHub 仓库 Settings -> Pages -> Source 选 Deploy from a branch")
    print("      分支 main，目录 /docs，保存后一两分钟即可访问：")
    print("      https://m-1229.github.io/ai-film-editing/")


if __name__ == "__main__":
    main()
