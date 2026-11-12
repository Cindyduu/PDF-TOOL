"""下载时用户自报文件名（R12）。

验收点：
- 带了 `name` 就用它当 `Content-Disposition` 的文件名，没带就完全保持原样；
- 用户的名字一定过一遍安全化：补后缀、替换非法字符、剔控制字符；
- 给不出可用名字（空、只有目录、只有空白）时退回任务默认名，不会凭空
  冒出一个 document.pdf；
- 名字只进响应头，响应体字节一个不差。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.integration.test_state_machine import run_to_end
from tests.integration.test_upload import assert_error

ENDPOINT = "/api/jobs"


def fetch(client, job_id: str, **params):
    return client.get(f"{ENDPOINT}/{job_id}/file", params={"kind": "output", **params})


def encoded_name(response) -> str:
    """取 RFC 5987 那一段，它就是浏览器实际采用的文件名。"""
    header = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in header, header
    return unquote(header.split("filename*=UTF-8''")[1])


@pytest.fixture
def done_job(client, fixture_pdfs: Path) -> dict:
    return run_to_end(client, fixture_pdfs)


def test_custom_name_becomes_the_download_name(client, done_job: dict):
    response = fetch(client, done_job["id"], name="测试改名.pdf")
    assert response.status_code == 200, response.text
    assert encoded_name(response) == "测试改名.pdf"
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert response.content.startswith(b"%PDF-")


def test_name_without_suffix_gets_pdf_appended(client, done_job: dict):
    assert encoded_name(fetch(client, done_job["id"], name="报告")) == "报告.pdf"


def test_illegal_characters_are_replaced_not_echoed(client, done_job: dict):
    response = fetch(client, done_job["id"], name="ab:c*d?.pdf")
    assert encoded_name(response) == "ab_c_d_.pdf"


def test_no_name_keeps_the_generated_file_name(client, done_job: dict):
    response = fetch(client, done_job["id"])
    assert encoded_name(response) == done_job["output"]["file_name"]


@pytest.mark.parametrize("bad", ["", "   ", "///", "../../etc/", "C:\\temp\\"])
def test_unusable_name_falls_back_to_the_generated_file_name(
    client, done_job: dict, bad: str
):
    """这些输入剥完之后不剩主名，不能变成 document.pdf 糊弄用户。"""
    response = fetch(client, done_job["id"], name=bad)
    assert response.status_code == 200, response.text
    assert encoded_name(response) == done_job["output"]["file_name"]


def test_response_body_is_byte_identical_with_or_without_a_name(
    client, done_job: dict
):
    plain = fetch(client, done_job["id"])
    renamed = fetch(client, done_job["id"], name="完全不同的名字.pdf")
    assert renamed.content == plain.content
    assert len(plain.content) == done_job["output"]["size"]


def test_control_characters_cannot_smuggle_a_header(client, done_job: dict):
    response = fetch(client, done_job["id"], name="evil\r\nX-Injected: 1.pdf")
    assert response.status_code == 200, response.text
    assert "x-injected" not in {key.lower() for key in response.headers}
    name = encoded_name(response)
    assert "\r" not in name and "\n" not in name


def test_overlong_name_is_refused_at_the_protocol_layer(client, done_job: dict):
    """名字长度在进清洗逻辑之前就被参数校验挡住，不用等到内存里截断。"""
    payload = assert_error(
        fetch(client, done_job["id"], name="长" * 600),
        "invalid_config",
        status=422,
    )
    assert payload["details"]["errors"]


def test_source_download_also_accepts_a_name(client, done_job: dict):
    response = client.get(
        f"{ENDPOINT}/{done_job['id']}/file",
        params={"kind": "source", "name": "原始件.pdf"},
    )
    assert response.status_code == 200, response.text
    assert encoded_name(response) == "原始件.pdf"
