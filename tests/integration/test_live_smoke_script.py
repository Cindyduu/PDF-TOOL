"""`scripts/live_smoke.py` 真的能拿外部文件跑完整流程。

这是脚本本身唯一的端到端测试：起真服务、传真文件、下载产物、自己校验。
用 `/FitH` 书签做样本，顺带把 P3 的转换路径也走一遍——如果脚本对
「非 /XYZ 目的地」的校验写错了，这里会红。

注意：这条用例会起一个真实的 uvicorn 子进程，比其他用例慢几秒。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

from app.schemas import DEFAULT_PROCESS_GROUPS
from tests.fixtures.make_fixtures import add_page, build_outline, new_pdf, save

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "live_smoke.py"


@pytest.fixture(scope="module")
def smoke():
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("live_smoke_script_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_source(path: Path) -> Path:
    """两页、一个 `/FitH` 书签、一条页面内 `/FitR` 链接。"""
    pdf = new_pdf()
    pages = [add_page(pdf) for _ in range(2)]
    build_outline(pdf, [{"title": "FitH", "dest": Array([pages[1], Name("/FitH"), 700])}])
    pages[0].Annots = Array(
        [
            Dictionary(
                Type=Name("/Annot"),
                Subtype=Name("/Link"),
                Rect=Array([10, 10, 200, 40]),
                Border=Array([0, 0, 0]),
                A=Dictionary(
                    S=Name("/GoTo"),
                    D=Array([pages[1], Name("/FitR"), 0, 0, 595, 842]),
                ),
            )
        ]
    )
    return save(pdf, path)


def test_script_runs_on_external_file_and_keeps_output(tmp_path):
    source = build_source(tmp_path / "外部样本.pdf")
    kept = tmp_path / "产物.pdf"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(source),
            "--port",
            str(free_port()),
            "--keep-output",
            "--out",
            str(kept),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
    )

    assert result.returncode == 0, f"退出码 {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert "全部检查通过" in result.stdout
    assert source.name in result.stdout, "脚本没把传进来的文件名打出来"
    assert kept.is_file(), "--keep-output 没把产物留下来"

    # 产物本身要真的是承前缩放，不能只信脚本自己说通过
    with pikepdf.open(kept) as pdf:
        assert len(pdf.pages) == 2
        node = pdf.Root.Outlines.get("/First")
        dest = node.get("/Dest")
        assert str(dest[1]) == "/XYZ"
        assert dest[4] is None
        assert float(dest[3]) == 700, "/FitH 的纵向位置没搬过来"

        link = pdf.pages[0].get("/Annots")[0].get("/A").get("/D")
        assert str(link[1]) == "/XYZ"
        assert link[4] is None


def test_script_reports_missing_file(tmp_path):
    """文件不存在时要给人话，而不是抛栈。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "不存在.pdf")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )
    assert result.returncode == 2
    assert "文件不存在" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    ("args", "expect"),
    [
        (["--exempt", "1,2"], "豁免页：[1, 2]"),
        (["--exempt", "1，2"], "豁免页：[1, 2]"),
    ],
)
def test_script_argument_parsing(smoke, args, expect):
    """中文逗号也要认——用户从文档里复制粘贴时很容易带上全角逗号。"""
    parsed = smoke.parse_args(["某文件.pdf", *args])
    assert parsed.pdf == "某文件.pdf"
    # parse_args 只做字符串解析，逗号归一化在 main() 里，这里直接验归一化结果
    raw = parsed.exempt.replace("，", ",")
    pages = [int(part) for part in raw.split(",") if part.strip()]
    assert f"豁免页：{pages}" == expect


def test_tag_option_is_gone(smoke):
    """v4.0 起标签由系统自动识别，`--tag` 已移除。

    留着它的后果不是「没效果」，而是后端 `extra="forbid"` 直接判 422，
    冒烟脚本自己先炸，报出来的还是「流程跑不起来」——很难定位。
    """
    with pytest.raises(SystemExit):
        smoke.parse_args(["某文件.pdf", "--tag", "keep"])


def _fake_flow(smoke, monkeypatch, tmp_path, *, flatten: bool) -> dict:
    """跑一遍 `run_flow`，全程走假 httpx，返回 `PATCH /config` 发出的 payload。

    `var_dir` 里要先造出「落盘原件」，因为 `run_flow` 会自己去读那份文件算
    sha256（AC-R13-N4 的取证不能靠服务端自证）。真实服务里这个路径由
    `storage.job_file` 生成，这里按同样的形状摆一份。
    """
    captured: dict = {}
    polls = {"count": 0}

    class FakeResponse:
        def __init__(self, payload: dict | None = None) -> None:
            self._payload = payload or {
                "id": "job-1",
                "batch_id": "batch-1",
                "status": "completed",
                "tag_detection": {"status": "untagged"},
            }

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def fake_post(url, **_kwargs):
        if url.endswith("/verify"):
            return FakeResponse(
                {"id": "job-1", "batch_id": "batch-1", "status": "verify_queued"}
            )
        return FakeResponse()

    def fake_patch(url, json=None, **_kwargs):
        captured["payload"] = json
        return FakeResponse()

    def fake_get(url, **_kwargs):
        # v4.0 的动线多了一次「等前置校验结束」的轮询：第一次返回 `verified`，
        # 之后的轮询返回终态，让脚本能走到下载那一步。
        polls["count"] += 1
        if polls["count"] == 1:
            return FakeResponse({"id": "job-1", "batch_id": "batch-1", "status": "verified"})
        return FakeResponse(
            {
                "id": "job-1",
                "batch_id": "batch-1",
                "status": "completed",
                "tag_detection": {"status": "untagged"},
            }
        )

    monkeypatch.setattr(smoke.httpx, "post", fake_post)
    monkeypatch.setattr(smoke.httpx, "patch", fake_patch)
    monkeypatch.setattr(smoke.httpx, "get", fake_get)

    job_dir = tmp_path / "generations" / "gen-1" / "jobs" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / smoke.SOURCE_FILE_NAME).write_bytes(b"%PDF-1.7\n%fake\n")

    data = smoke.run_flow(
        "http://127.0.0.1:1",
        Path(__file__),
        exempt=[1, 2],
        flatten=flatten,
        var_dir=tmp_path,
    )
    # 取证信息必须回到调用方手里，`main` 靠它做 AC-R13-N4 的断言。
    assert data["_sha_before"] == data["_sha_after"]
    assert Path(data["_source_path"]).name == smoke.SOURCE_FILE_NAME
    return captured["payload"]


def test_config_patch_does_not_send_tag_choice(smoke, monkeypatch, tmp_path):
    """回归：`PATCH /config` 的 payload 里不能再有 `tag_choice`。"""
    payload = _fake_flow(smoke, monkeypatch, tmp_path, flatten=True)

    assert payload["exempt_pages_raw"] == "1,2"
    assert "tag_choice" not in payload
    # 处理项按 UI 的 group key 发，不按流水线项发（§6.1）。
    assert payload["process_items"] == [*DEFAULT_PROCESS_GROUPS, "g6_flatten"]


def test_no_flatten_leaves_the_group_out(smoke, monkeypatch, tmp_path):
    """`--no-flatten` 必须真的少发一个 group。

    只把参数解析出来、却不影响请求，是这类开关最常见的坏法：命令行上看
    着生效了，发出去的配置一模一样。
    """
    payload = _fake_flow(smoke, monkeypatch, tmp_path, flatten=False)

    assert "g6_flatten" not in payload["process_items"]
    assert payload["process_items"] == list(DEFAULT_PROCESS_GROUPS)


# --------------------------------------------------------------------------- #
# --exe：同一套独立校验也要能验打包产物
# --------------------------------------------------------------------------- #


def test_build_command_runs_run_py_in_source_mode(smoke, tmp_path):
    """源码态必须用当前解释器跑 `run.py`，否则验的是别的 Python。"""
    command = smoke.build_command(None, port=8899, var_dir=tmp_path / "var")

    assert command[:2] == [sys.executable, "run.py"]
    assert "--no-browser" in command
    assert command[command.index("--port") + 1] == "8899"
    assert command[command.index("--var-dir") + 1] == str(tmp_path / "var")


def test_build_command_runs_exe_when_given(smoke, tmp_path):
    """打包产物自带解释器：前面不能再拼 `sys.executable` 和 `run.py`。

    拼错了的表现是「测试通过了」，其实跑的还是源码，打包产物一次都没被验过。
    """
    exe = tmp_path / "PDFTool.exe"
    command = smoke.build_command(exe, port=8899, var_dir=tmp_path / "var")

    assert command[0] == str(exe)
    assert sys.executable not in command
    assert "run.py" not in command
    assert command[command.index("--port") + 1] == "8899"


def test_script_reports_missing_exe(tmp_path):
    """`--exe` 指向不存在的文件时要给人话，而不是抛 FileNotFoundError。"""
    source = build_source(tmp_path / "样本.pdf")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(source),
            "--exe",
            str(tmp_path / "没有这个.exe"),
            "--port",
            str(free_port()),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )

    assert result.returncode == 2
    assert "可执行文件不存在" in (result.stdout + result.stderr)
