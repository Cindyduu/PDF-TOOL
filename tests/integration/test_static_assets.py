"""静态资源与前端骨架的契约。

验收点（对应阶段 H 计划门槛）：
- 计划里点名的每一个静态文件都真实存在，且能被后端挂载后正常返回；
- 前端**没有任何外链依赖**（不引 CDN、不引外部字体、不带 SRI），
  因为这台机器可能完全离线，`python run.py` 起来就得能用；
- `index.html` 只引用本地 `/static/` 资源，且没有内联脚本（便于将来收紧 CSP）；
- 无障碍骨架在位：跳过链接、`main[tabindex]`、`aria-live` 区、恰好三项顶部导航；
- 六个视图模块都导出 `render(container, ctx)`，路由表恰好六条且与视图一一对应。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"

# 计划里点名要求产出的文件。少一个前端就起不来。
REQUIRED_FILES = [
    "index.html",
    "css/app.css",
    "js/dom.js",
    "js/api.js",
    "js/router.js",
    "js/app.js",
    "js/views/home.js",
    "js/views/config.js",
    "js/views/processing.js",
    "js/views/result.js",
    "js/views/history.js",
    "js/views/backup.js",
]

VIEW_NAMES = ["home", "config", "processing", "result", "history", "backup"]

TEXT_SUFFIXES = {".html", ".css", ".js"}


def static_files() -> list[Path]:
    return sorted(
        p
        for p in STATIC_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
    )


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 文件存在性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rel", REQUIRED_FILES)
def test_required_static_file_exists(rel: str) -> None:
    path = STATIC_DIR / rel
    assert path.is_file(), f"缺少静态文件 {rel}"
    assert path.stat().st_size > 0, f"{rel} 是空文件"


def test_no_stray_files_in_static_dir() -> None:
    """静态目录里不留半成品：只剩 html/css/js 三类。"""
    extras = [
        str(p.relative_to(STATIC_DIR))
        for p in STATIC_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() not in TEXT_SUFFIXES
    ]
    assert extras == [], f"静态目录里有多余文件：{extras}"


# --------------------------------------------------------------------------- #
# 无外链依赖（离线可用）
# --------------------------------------------------------------------------- #

EXTERNAL_PATTERNS = [
    (re.compile(r"https?://", re.I), "外部 URL"),
    (re.compile(r"//cdn", re.I), "CDN 域名"),
    (re.compile(r"\bintegrity\s*=", re.I), "SRI 完整性校验（外链配套）"),
    (re.compile(r"\bcrossorigin\b", re.I), "crossorigin 属性"),
    (re.compile(r"@import\s+url\s*\(", re.I), "CSS @import 外部样式"),
]


@pytest.mark.parametrize("path", static_files(), ids=lambda p: str(p.name))
def test_static_file_has_no_external_dependency(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [
        f"{label} -> {pattern.pattern}"
        for pattern, label in EXTERNAL_PATTERNS
        if pattern.search(text)
    ]
    assert found == [], f"{path.name} 引用了外部资源：{found}"


# --------------------------------------------------------------------------- #
# index.html 骨架
# --------------------------------------------------------------------------- #


def test_index_html_references_only_local_static_assets() -> None:
    markup = read("index.html")
    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', markup)
    assert refs, "index.html 里没有任何资源引用"

    external = [
        ref for ref in refs if not (ref.startswith("/static/") or ref.startswith("#"))
    ]
    assert external == [], f"index.html 引用了非本地资源：{external}"


def test_index_html_loads_css_and_module_entry() -> None:
    markup = read("index.html")
    assert '<link rel="stylesheet" href="/static/css/app.css"' in markup
    assert '<script type="module" src="/static/js/app.js"></script>' in markup


def test_index_html_has_no_inline_script() -> None:
    """脚本一律走外部模块文件，避免内联脚本挡住将来的 CSP。"""
    markup = read("index.html")
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", markup, re.S)
    assert [s.strip() for s in inline if s.strip()] == []


def test_index_html_accessibility_skeleton() -> None:
    markup = read("index.html")
    assert 'class="skip-link"' in markup, "缺少跳过导航链接"
    assert 'href="#main"' in markup
    assert re.search(r'<main[^>]*id="main"[^>]*tabindex="-1"', markup), (
        "main 必须带 id=main 和 tabindex=-1（跳转后能接住焦点）"
    )
    assert 'id="route-view"' in markup
    assert 'aria-live="polite"' in markup, "路由出口需要 aria-live"
    assert 'role="status"' in markup and 'id="toast"' in markup


def test_top_nav_has_exactly_three_items() -> None:
    markup = read("index.html")
    nav = re.search(r"<nav\b.*?</nav>", markup, re.S)
    assert nav is not None, "缺少 <nav>"
    links = re.findall(r"<a\b[^>]*>.*?</a>", nav.group(0), re.S)
    assert len(links) == 3, f"顶部导航必须恰好三项，实际 {len(links)} 项"

    labels = [re.sub(r"<[^>]+>", "", a).strip() for a in links]
    assert labels == ["首页", "历史", "备份"]
    assert 'aria-label="主导航"' in nav.group(0)


# --------------------------------------------------------------------------- #
# 路由表与视图模块
# --------------------------------------------------------------------------- #


def test_router_declares_the_six_hash_routes() -> None:
    source = read("js/router.js")
    names = re.findall(r"name:\s*'([a-z_]+)'", source)
    assert names == VIEW_NAMES, f"路由表与计划不一致：{names}"

    # 带 id 的路由必须解析出 jobId 参数。
    assert source.count("keys: ['jobId']") == 3


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_view_module_exports_render(view: str) -> None:
    source = read(f"js/views/{view}.js")
    assert re.search(r"export\s+(?:async\s+)?function\s+render\s*\(", source), (
        f"视图 {view} 必须导出 render(container, ctx)"
    )


def test_app_js_imports_every_view() -> None:
    source = read("js/app.js")
    for view in VIEW_NAMES:
        assert f"'./views/{view}.js'" in source, f"app.js 没有引入视图 {view}"
    assert re.search(r"const VIEWS = \{[^}]*\bhome\b[^}]*\bbackup\b", source)


def test_api_js_covers_every_backend_endpoint() -> None:
    """前端只通过 api.js 说话；后端每条路由都要有对应方法。"""
    source = read("js/api.js")
    for fragment in [
        "/api/meta",
        "/api/jobs",
        "/api/backups",
        "/events",
        "/report",
        "/reprocess",
        "/file?kind=",
    ]:
        assert fragment in source, f"api.js 缺少 {fragment} 相关调用"


def test_dom_js_escapes_by_default() -> None:
    """`html`` ` 默认转义，只有显式 raw() 才原样输出——防止 XSS 与二次转义。"""
    source = read("js/dom.js")
    assert "escapeHtml" in source
    assert re.search(r"export\s+function\s+raw\s*\(", source)
    assert "RawHtml" in source


# --------------------------------------------------------------------------- #
# 后端真的把它们发出去了
# --------------------------------------------------------------------------- #


def test_index_is_served_at_root(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "route-view" in response.text


@pytest.mark.parametrize("rel", REQUIRED_FILES[1:], ids=lambda r: r)
def test_static_asset_is_served(client, rel: str) -> None:
    response = client.get(f"/static/{rel}")
    assert response.status_code == 200, f"/static/{rel} 返回 {response.status_code}"
    assert response.content, f"/static/{rel} 内容为空"

    expected = "css" if rel.endswith(".css") else "javascript"
    assert expected in response.headers["content-type"]


def test_static_dir_is_mounted_under_static_prefix(client) -> None:
    """目录外的东西不能顺带被发出去（StaticFiles 自带穿越防护）。"""
    response = client.get("/static/../main.py")
    assert response.status_code in {404, 400}


def test_missing_static_file_returns_404(client) -> None:
    assert client.get("/static/js/views/nope.js").status_code == 404
