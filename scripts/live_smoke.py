"""真实服务器冒烟：起 uvicorn → 上传 → 配置 → 开始 → 结果 → 下载 → 校验产物。

与 tests 的区别：走真实 HTTP + 真实 uvicorn 进程，不依赖 TestClient 的进程内假设。
用法：python scripts/live_smoke.py [端口]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pdfs" / "text_blocks.pdf"


def wait_health(base: str, timeout: float = 40.0) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(base + "/api/health", timeout=2.0)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(0.3)
    raise RuntimeError(f"健康检查超时：{last}")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    base = f"http://127.0.0.1:{port}"
    var_dir = Path(tempfile.mkdtemp(prefix="pdftool-smoke-"))

    proc = subprocess.Popen(
        [sys.executable, "run.py", "--no-browser", "--port", str(port), "--var-dir", str(var_dir)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = wait_health(base)
        print("health:", json.dumps(health, ensure_ascii=False))

        with FIXTURE.open("rb") as fh:
            r = httpx.post(
                base + "/api/jobs",
                files={"files": (FIXTURE.name, fh, "application/pdf")},
                timeout=60.0,
            )
        r.raise_for_status()
        job = r.json()
        job_id = job["id"]
        print("job:", job_id, "tag:", job["tag_detection"]["status"])

        r = httpx.patch(
            base + f"/api/jobs/{job_id}/config",
            json={"exempt_pages_raw": "1", "tag_choice": "generate"},
            timeout=30.0,
        )
        r.raise_for_status()
        print("config:", r.json()["config"])

        r = httpx.post(base + f"/api/jobs/{job_id}/start", timeout=30.0)
        r.raise_for_status()

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            r = httpx.get(base + f"/api/jobs/{job_id}", timeout=10.0)
            r.raise_for_status()
            data = r.json()
            if data["status"] in {"completed", "completed_with_warnings", "failed", "interrupted"}:
                break
            time.sleep(0.4)
        print("status:", data["status"])

        report = httpx.get(base + f"/api/jobs/{job_id}/report", timeout=20.0).json()
        for item in report["results"]:
            print(
                f"  {item['key']:<22} {item['status']:<16} "
                f"checked={item['counts']['checked']} modified={item['counts']['modified']} "
                f"compliant={item['counts']['compliant']} exempt={item['counts']['exempt']} "
                f"na={item['counts']['not_applicable']} failed={item['counts']['failed']}"
            )
            if item.get("reason"):
                print(f"      原因：{item['reason']}")

        r = httpx.get(base + f"/api/jobs/{job_id}/file?kind=output", timeout=60.0)
        r.raise_for_status()
        out = var_dir / "out.pdf"
        out.write_bytes(r.content)
        print("output bytes:", len(r.content))

        import pikepdf

        with pikepdf.open(out) as pdf:
            root = pdf.Root
            print("PageLayout:", "/PageLayout" in root, "PageMode:", str(root.get("/PageMode")))
            print("Metadata:", "/Metadata" in root, "Info:", "/Info" in pdf.trailer)
            print("Linearized:", "/Linearized" in pdf.Root or "/Linearized" in (pdf.objects[0] if pdf.objects else {}))
            print("StructTreeRoot:", "/StructTreeRoot" in root)
            print("pages:", len(pdf.pages))
            annots = sum(len(list(p.get("/Annots", []))) for p in pdf.pages)
            print("remaining annots:", annots)
        print("var_dir:", var_dir)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
