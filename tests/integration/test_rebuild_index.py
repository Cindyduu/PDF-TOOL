"""阶段 C：index.json 必须是可重建的派生数据，job.json 才是事实来源。

覆盖：
- 删除 index.json 后，历史列表能从各任务目录重建且内容一致；
- index.json 损坏（非法 JSON / 结构错误）时自动重建而不是报错；
- 重建结果按创建时间倒序；
- 输出文件缺失、需要复核等状态如实反映在索引里；
- 改动 job.json 后重建能反映最新状态（证明没有第二份真相）。
"""

from __future__ import annotations

from app.core import atomic
from tests.helpers import make_job, make_result, write_job


def _seed_three_jobs(ctx) -> tuple[str, list[dict]]:
    gid = ctx.storage.ensure_active()
    jobs = []
    for index, day in enumerate(("01", "02", "03")):
        job = make_job(
            gid,
            created_at=f"2026-03-{day}T00:00:00Z",
            status="completed",
            original_name=f"报告{index}.pdf",
            output_name=f"报告{index}_已处理.pdf",
            results=[make_result("metadata")],
        )
        write_job(ctx, job, with_original=True, output=b"%PDF-1.7\n%out\n")
        jobs.append(job)
    return gid, jobs


def test_index_rebuild_after_deletion_matches(ctx) -> None:
    gid, jobs = _seed_three_jobs(ctx)

    first = ctx.storage.rebuild_index(gid)
    assert ctx.storage.index_path(gid).is_file()
    assert len(first["jobs"]) == 3

    # 创建时间倒序：最新的排最前
    assert [entry["id"] for entry in first["jobs"]] == [
        jobs[2]["id"],
        jobs[1]["id"],
        jobs[0]["id"],
    ]

    ctx.storage.index_path(gid).unlink()

    rebuilt = ctx.storage.read_index(gid)
    assert [entry["id"] for entry in rebuilt["jobs"]] == [entry["id"] for entry in first["jobs"]]
    assert all(entry["output_available"] for entry in rebuilt["jobs"])
    assert all(entry["source_available"] for entry in rebuilt["jobs"])
    assert all(entry["review_required"] is False for entry in rebuilt["jobs"])
    assert ctx.storage.index_path(gid).is_file()


def test_corrupt_index_is_rebuilt(ctx) -> None:
    gid, jobs = _seed_three_jobs(ctx)
    index_path = ctx.storage.index_path(gid)
    index_path.write_text("{ not valid json", encoding="utf-8")

    rebuilt = ctx.storage.read_index(gid)
    assert len(rebuilt["jobs"]) == 3
    assert {entry["id"] for entry in rebuilt["jobs"]} == {job["id"] for job in jobs}
    # 已用合法内容覆盖坏文件
    assert isinstance(atomic.read_json(index_path), dict)


def test_index_with_wrong_shape_is_rebuilt(ctx) -> None:
    gid, jobs = _seed_three_jobs(ctx)
    ctx.storage.index_path(gid).write_text('{"jobs": "not-a-list"}', encoding="utf-8")

    rebuilt = ctx.storage.read_index(gid)
    assert [entry["id"] for entry in rebuilt["jobs"]][0] == jobs[2]["id"]


def test_rebuild_reflects_job_json_changes(ctx) -> None:
    gid = ctx.storage.ensure_active()
    job = make_job(gid, created_at="2026-03-01T00:00:00Z", status="completed")
    write_job(ctx, job, with_original=True)

    first = ctx.storage.rebuild_index(gid)
    assert first["jobs"][0]["status"] == "completed"
    assert first["jobs"][0]["output_available"] is False

    job["status"] = "completed_with_warnings"
    job["output"] = {"file_name": "out.pdf", "sha256": "1" * 64, "size": 10, "page_count": 1}
    write_job(ctx, job, output=b"%PDF-1.7\n")
    ctx.storage.job_file(gid, job["id"], "original.pdf").unlink()

    rebuilt = ctx.storage.rebuild_index(gid)
    entry = rebuilt["jobs"][0]
    assert entry["status"] == "completed_with_warnings"
    assert entry["output_available"] is True
    assert entry["source_available"] is False


def test_rebuild_flags_review_required(ctx) -> None:
    gid = ctx.storage.ensure_active()
    job = make_job(
        gid,
        created_at="2026-03-01T00:00:00Z",
        status="completed_with_warnings",
        results=[make_result("link_color", "review_required")],
    )
    write_job(ctx, job)

    rebuilt = ctx.storage.rebuild_index(gid)
    assert rebuilt["jobs"][0]["review_required"] is True


def test_rebuild_skips_broken_job_json(ctx) -> None:
    gid, jobs = _seed_three_jobs(ctx)
    broken = ctx.storage.job_file(gid, jobs[0]["id"], "job.json")
    broken.write_text("{ half written", encoding="utf-8")

    rebuilt = ctx.storage.rebuild_index(gid)
    ids = [entry["id"] for entry in rebuilt["jobs"]]
    assert jobs[0]["id"] not in ids
    assert len(ids) == 2


def test_read_index_without_rebuild_returns_empty(ctx) -> None:
    gid = ctx.storage.ensure_active()
    payload = ctx.storage.read_index(gid, rebuild_if_missing=False)
    assert payload["jobs"] == []
    assert payload["rebuilt_at"] is None
