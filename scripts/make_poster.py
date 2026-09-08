# -*- coding: utf-8 -*-
"""
make_poster.py — 把一份 HTML 海报渲染成高清长图 PNG（HTML -> 图片）。

设计要点（与「反浏览器自动化铁律」不冲突）：
- 只用**本机已有的浏览器二进制**（Edge / Chrome / Chromium）做 headless 整页截图，
  截的是**本地 HTML 文件**，不访问、不驱动任何直播平台网页，不是平台自动化。
- 不下载 Chromium、不依赖 playwright / selenium（直接用浏览器二进制，零额外安装）。
- 截图后由 Pillow 自动裁掉底部空白，得到精确高度长图。

用法：
  # 直接渲染一份写好的 HTML（推荐：从 poster_template.html 改数据后生成）
  python scripts/make_poster.py --html poster.html --out 分享长图.png

  # 或直接用数据文件（JSON）套模板生成（无需手改 HTML）
  python scripts/make_poster.py --data poster_data.json --out 分享长图.png
  # 指定浏览器（一般可自动探测）
  python scripts/make_poster.py --html poster.html --browser "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"

依赖：pip install pillow   （仅用于裁白边；渲染靠本机浏览器二进制，无需 Python 包）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# 默认示例数据（演示用；实际用 --data 或改 poster_template.html 顶部 POSTER_DATA）。
# 结构：header + blocks(core/cases/qa/conclusion/card) + footer
DEFAULT_DATA = {
    "header": {
        "eyebrow": "OpenBuild 开源社区 · 直播干货",
        "title": "FDE 实战训练营",
        "subtitle": "如何打通业务、数据与系统",
        "meta": "主讲：亮安老师　｜　时长 ≈ 67 分钟<br>AI 自动转写 + 提炼整理",
    },
    "blocks": [
        {
            "type": "core",
            "title": "核心心法（贯穿全场）",
            "items": [
                "模型 / 数据 / 业务规则 / 工程要一起改，不能只训模型",
                "准确率按「整批跑完」算，不是单环节（85%×85%×85%≈60%）",
                "问题拆到「具体判断 + 具体动作」，分清算法 / 数据 / 工程",
                "上下文必须交接清楚、可溯源（录音 / 记录），别只存老师傅脑子里",
                "现场收问题、后方改功能、改完回到原问题验证",
                "系统里已有确定答案的，直接取、不重拆",
            ],
        },
        {
            "type": "cases",
            "title": "八个真实交付案例",
            "items": [
                {"name": "斯凯奇 · 鞋服电商图片", "color": "#2b5cff",
                 "pain": "月几千新品，设计师天天找图 / 排版到很晚",
                 "do": "把老师傅经验写成标注规则；模型补样本再训练 + 工程化套板；客户问题逐条拆算法 / 数据 / 工程",
                 "result": "准确率 85%→95%，1 个月验收，方法论复用到安踏"},
                {"name": "澳洲服装定制 · ERP 跟单", "color": "#7a3cff",
                 "pain": "复杂报价只靠 2 个跟单员（半小时 vs 半天）",
                 "do": "做「询价系统」——收邮件先判信息齐不齐，能报就报、缺项回问；拿不准交跟单确认；录音留痕可溯源",
                 "result": "询价环节效率提升 10 倍以上，14 节点打通"},
                {"name": "菲律宾电销团队", "color": "#009688",
                 "pain": "主管听录音时间不够，难辅导跟进",
                 "do": "把 AI 接进电话质检 + 管理，分析沟通辅助主管；按同一客户归并；建主管检查清单",
                 "result": "团队转化率相对翻倍"},
                {"name": "教育私域转化", "color": "#e65100",
                 "pain": "群发广告，打开率越来越低",
                 "do": "把优秀销售判断 + 表达 + 运营固化进流程；先想清楚发给谁再想话术；整理 16 个 SOP",
                 "result": "打开率 2–3 倍、转化率 +30~40%"},
                {"name": "印刷企业", "color": "#d62839",
                 "pain": "订单 / 物料 / 生产标准不一，协同卡死",
                 "do": "把订单 / 物料 / 标准放一起共享；异常时关联订单与物料、引用公认标准交人处理",
                 "result": "跨部门协同记录统一，异常可溯源处理"},
                {"name": "汽车电机 MES", "color": "#6c63ff",
                 "pain": "驻场半年客户仍不断加临时需求",
                 "do": "把「持续支持」写进交付（运维费制度化）；工作分三类；找核心人定方案",
                 "result": "客户需求持续响应，合作可持续扩展"},
                {"name": "福利商城运营", "color": "#0091c3",
                 "pain": "几百个商城内容雷同却要重复搭建",
                 "do": "固定结构从模板取；不同点写成配置；确定内容直接取系统数据；评测放错 / 链接 / 接口",
                 "result": "重复搭建大幅提效，配置化稳定交付"},
                {"name": "门墙喷绘拼接", "color": "#7cb342",
                 "pain": "零散照片要拼成完整喷绘图",
                 "do": "按「干净 / 错位 / 缺损」几类试，和客户一起看哪种能接受、哪种还得人工补",
                 "result": "图像模型仍在探索，暂以人工补为主"},
            ],
        },
        {
            "type": "qa",
            "title": "Q&A 精华",
            "items": [
                "业务数据与系统最常见断点：上下文没交接清 + AI 结果接不到人 / 系统 / 工程",
                "连老系统（用友 / 金蝶 / 老 SAP）：先接跳板机跑稳，再上生产，别轻易替换",
                "客户说的对不对：按结果判——能否更快上架 / 生产",
                "询价准确率做不到 100%（目前 95%），但不上系统损失更大",
                "FDE = 前沿部署工程师，分方案型 / 工程型，核心是连接业务·模型·数据·系统",
            ],
        },
        {
            "type": "conclusion",
            "title": "关键结论",
            "text": "FDE 的核心职责 = 连接业务、模型、数据与系统，确保 AI 解决方案真正落地、产生实际价值。",
        },
    ],
    "footer": {
        "title": "整理自 OpenBuild 开源社区直播《FDE 实战训练营》",
        "sub": "仅供内部学习分享 · 由 live-class-skill 自动听课整理",
    },
}

# 浏览器二进制候选路径（按优先级）。优先用本机已装的 Edge / Chrome，绝不下载。
BROWSER_CANDIDATES = [
    # Windows Edge
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    # Windows Chrome
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    # macOS
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/opt/microsoft/msedge/msedge",
]


def find_browser(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            sys.exit(f"[错误] 指定的浏览器不存在：{explicit}")
        return explicit
    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    # 再试 PATH
    for name in ("msedge", "google-chrome", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit(
        "[错误] 没找到本机浏览器（Edge / Chrome / Chromium）。\n"
        "        请安装其一，或用 --browser 指定其可执行文件路径。\n"
        "        注意：本工具只用本机浏览器做本地 HTML 渲染，不会下载 Chromium、不驱动任何直播平台网页。"
    )


def build_html(template_path, data):
    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    # 把 __DATA__ 占位符替换为 JSON（安全：转义 </script> 防注入）
    safe = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if "__DATA__" not in tpl:
        sys.exit("[错误] 模板缺少 __DATA__ 占位符：" + template_path)
    return tpl.replace("__DATA__", safe)


def render_html_to_png(html_path, out_png, browser, scale=2, page_w=750):
    # 复制到临时 ASCII 路径，规避中文/空格路径在 subprocess 里的坑
    tmp_dir = tempfile.gettempdir()
    tmp_html = os.path.join(tmp_dir, "lcc_poster_%d.html" % os.getpid())
    tmp_png = os.path.join(tmp_dir, "lcc_poster_%d.png" % os.getpid())
    shutil.copyfile(html_path, tmp_html)
    try:
        cmd = [
            browser,
            "--headless", "--no-sandbox", "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=%d" % scale,
            "--screenshot=%s" % tmp_png,
            "--window-size=%d,%d" % (page_w, 8000),
            "file://" + tmp_html,
        ]
        # headless 截图不读 stdout，静默运行
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if not os.path.exists(tmp_png):
            sys.exit("[错误] 浏览器截图未生成：" + tmp_png)
        crop_whitespace(tmp_png, out_png, pad=50, threshold=230)
    finally:
        for f in (tmp_html, tmp_png):
            try:
                os.remove(f)
            except OSError:
                pass


def crop_whitespace(src_png, out_png, pad=50, threshold=230):
    try:
        from PIL import Image
    except ImportError:
        sys.exit("[错误] 缺少 Pillow：请先 `pip install pillow`（仅裁白边用）。原始截图已留在 " + src_png)
    img = Image.open(src_png).convert("RGB")
    W, H = img.size
    px = img.load()
    last = 0
    for y in range(H - 1, -1, -1):
        hit = False
        for x in range(0, W, 4):
            r, g, b = px[x, y]
            if min(r, g, b) < threshold:
                hit = True
                break
        if hit:
            last = y
            break
    bottom = min(last + pad, H)
    img.crop((0, 0, W, bottom)).save(out_png, "PNG")
    print("渲染完成：%s  (%d x %d)  ->  %s" % (os.path.basename(src_png), W, bottom, out_png))


def main():
    ap = argparse.ArgumentParser(description="HTML 海报 -> 高清长图 PNG（本机浏览器 headless 渲染，不下载 Chromium）")
    ap.add_argument("--html", help="直接渲染的 HTML 文件（如改好的 poster.html）")
    ap.add_argument("--data", help="JSON 数据文件，套用 poster_template.html 生成")
    ap.add_argument("--template", default=None, help="模板路径（默认 scripts/poster_template.html）")
    ap.add_argument("--out", required=True, help="输出 PNG 路径")
    ap.add_argument("--browser", default=None, help="显式指定浏览器二进制路径")
    ap.add_argument("--scale", type=int, default=2, help="设备像素比（2=视网膜清晰），默认 2")
    args = ap.parse_args()

    browser = find_browser(args.browser)

    if args.data:
        tpl = args.template or os.path.join(os.path.dirname(os.path.abspath(__file__)), "poster_template.html")
        if not os.path.exists(tpl):
            sys.exit("[错误] 模板不存在：" + tpl)
        with open(args.data, "r", encoding="utf-8") as f:
            data = json.load(f)
        html = build_html(tpl, data)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
        tmp.write(html)
        tmp.close()
        html_path = tmp.name
        try:
            render_html_to_png(html_path, args.out, browser, scale=args.scale)
        finally:
            try:
                os.remove(html_path)
            except OSError:
                pass
    elif args.html:
        if not os.path.exists(args.html):
            sys.exit("[错误] HTML 不存在：" + args.html)
        render_html_to_png(args.html, args.out, browser, scale=args.scale)
    else:
        sys.exit("[错误] 必须给 --html 或 --data 之一。")


if __name__ == "__main__":
    main()
