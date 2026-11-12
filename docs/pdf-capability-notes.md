# PDF 处理能力说明（测试验证过的部分）

这份文档只写**有测试兜底**的行为结论。每条后面都跟着它的事实来源；没有测试的
推测、经验之谈、上游文档的转述一律不写进来。这样做的理由很实际：PDF 的坑几乎
都藏在「某个库在某个边界条件下的实际表现」里，而这类表现只能靠跑出来确认，
不能靠读文档推断。

以下编号与测试用例名一一对应，可以直接 `pytest tests/... -k 名字` 复现。

---

## 1. 线性化（Fast Web View）会重排对象编号，所以「零变化」不能比 `unparse()`

**结论**：qpdf 做线性化时要把首页对象排到文件前部，对象编号会被整体重排。同一个
从未被改动过的对象，保存前后的 `unparse()` 会从 `9 0 R` 变成 `6 0 R`——**编号变了，
内容没变**。

**影响**：任何「逐对象比对保存前后是否零变化」的断言，如果直接拿 `unparse()` 字符串
比较，在带线性化的流水线上永远不可能成立。它测的是 xref 号，不是「对象有没有被改」。

**做法**：改为解析内容本身生成规范化签名——间接引用展开成被引用对象的内容（页面
引用折算成页码）、字典按 key 排序、流取「字典部分 + 解码后字节的 SHA-256」。
签名只随内容变化，不随编号漂移。

**来源**：
- `tests/pdf_ops.py::object_signature`（函数 docstring 记录了 9→6 这个实测现象，
  以及为什么排除 `/Length`）
- `tests/integration/test_pipeline_nontag.py::test_exempt_page_objects_are_byte_identical`
  （豁免页在完整流水线跑完后签名不变）
- `tests/integration/test_pipeline_nontag.py::test_exempt_objects_change_when_not_exempt`
  （对照组：不豁免时签名必须变，证明签名确实敏感）

---

## 2. 备份包的「损坏」有三层，每层的表现不一样

**结论**：同样是「包坏了」，三种坏法在 Python 侧的可观测现象完全不同，不能用一个
`except BadZipFile` 兜住。

| 坏法 | 现象 | 谁来抓 |
|---|---|---|
| deflate 压缩流被翻了一个字节 | `testzip()` **抛 `zlib.error`**，不是返回坏条目名 | `except (BadZipFile, zlib.error)` |
| CRC 字段被改错（内容能解出来） | `testzip()` 返回该条目的文件名 | 判返回值 `is not None` |
| 内容被换但 CRC 被重算过 | `testzip()` **通过**，什么都没发生 | 只有逐条比 `sha256` 才能抓 |

**影响**：只写 `except zipfile.BadZipFile` 会漏掉第一类，让一个损坏的包以
`zlib.error` 的形式穿到上层，变成 500 而不是「这个包坏了，请重新导出」。

**来源**：
- `tests/fault_injection/test_restore_failures.py::corrupt_entry_payload`
  （注释写明「压缩流被破坏时连解压都做不到，报的是『已损坏』」）
- `tests/fault_injection/test_restore_failures.py::break_entry_crc`
  （本地头 + 中央目录各改一处 CRC）
- `tests/fault_injection/test_restore_failures.py::build_hash_mismatch`
  （注释：「CRC 由 zipfile 重算，所以 testzip 会通过；只有 sha256 能抓出来」）
- `tests/fault_injection/test_restore_failures.py::test_bad_package_reason_matches_the_actual_defect`
  （断言每种坏包给出的 reason 与真实缺陷对应，不是笼统的「包坏了」）
- 实现侧：`app/core/archive.py` 的 `except (zipfile.BadZipFile, zlib.error)`

---

## 3. 注释压平：旋转页上的像素差异也必须接近零

**结论**：把注释外观画进内容流再摘掉注释，渲染结果应当与处理前一致——**包括
页面带 `/Rotate 90` 的情况**。实测差异为 0，测试留 0.1% 余量给坐标取整。

**为什么专门测旋转页**：注释外观是画在「未旋转的页面坐标系」里的，而渲染输出是
旋转后的。压平实现如果拿渲染后的尺寸/方向去算变换矩阵，非旋转页看不出来，旋转页
会整体偏移或转置。这是最容易漏、也最容易在用户那里炸掉的一类回归。

**配套对照**：同一样本注释数从 9 降到 3，像素却没塌——证明是「先画上去再摘掉」，
不是「直接删掉」。

**来源**：
- `tests/integration/test_pipeline_nontag.py::test_rotated_page_flatten_keeps_pixels`
  （`diff / total < 0.001`，且 `non_white(after) > 1000` 保证对比本身有意义）
- `tests/integration/test_pipeline_nontag.py::test_flatten_reduces_annotations_not_content`
  （9 → 3，同阈值）
- `tests/integration/test_pipeline_nontag.py::test_appearance_actually_drawn_when_present`

---

## 4. 共享命名目标必须复制，不能就地改

**结论**：当同一个命名目标（`/Names /Dests` 里的一条）被「要改的书签」和「豁免页
上的链接」共用时，**不能就地修改那个目标对象**——改了书签就顺手把豁免页的链接也
改了，等于绕过了豁免。

**做法**：给书签换一份目标副本（`objgen` 与原件不同），原件留在命名树里一个字节
不动，同时产生一条「共用同一个目标」的 warning 让用户知道这里做过取舍。

**反向约束**：如果共用者全部是豁免引用，则目标必须一动不动，reason 为 `all_exempt`。

**来源**：
- `tests/unit/test_ops_bookmarks.py::test_shared_named_dest_is_copied_not_mutated`
  （原件仍是 2.5；书签的 slot 换成 `objgen` 不同的副本；豁免页注释的 `/Dest`
  仍指向原名 `sharedTarget`；计数 `checked=3, modified=2, exempt=1`）
- `tests/unit/test_ops_bookmarks.py::test_shared_named_dest_is_not_touched_when_only_exempt_refs`
- `tests/integration/test_pipeline_nontag.py::test_shared_named_dest_is_not_cross_modified`
  （端到端同样的性质）

---

## 5. 扫描件（无文本层）加标签会失败，但不拖累其他八项

**结论**：没有文本层的 PDF 无法生成有意义的标签树。此时标签这一项报
`status="failed"` / `reason_code="tag_no_text"`，整份任务降级为
`completed_with_warnings`，其余八项照常产出，输出文件仍是有效 PDF。

**关键细节**：失败时**不许留下空的 `/StructTreeRoot`**。留下半棵标签树比没有更糟——
阅读器会认为「这文件是有标签的」，然后读到一棵空树。

**来源**：
- `tests/integration/test_tags_in_pipeline.py::test_tag_failure_does_not_block_the_other_eight`
  （断言其余八项 `status != "failed"`、输出 `verify_pdf_file().ok is True`、
  `pdf.Root.get("/StructTreeRoot") is None`）

---

## 6. 只有 `/MarkInfo` 不算「已加标签」

**结论**：判断一份 PDF 是否真的带标签，不能看 `/MarkInfo`。检测分四类结果：

| 实际结构 | 判定 | 默认选择 |
|---|---|---|
| 无 `/StructTreeRoot`、无 `/MarkInfo` | `untagged` | 生成 |
| 有 `/MarkInfo` 但无 `/StructTreeRoot` | `markinfo_only` | 重新生成 |
| 有 `/StructTreeRoot` 但 ParentTree / StructParents 不完整 | `markinfo_only` | 重新生成 |
| `/StructTreeRoot` 完整且 ParentTree 自洽 | `tagged` | 保留 |

`/MarkInfo /Marked true` 只是一个声明，很多工具会写它但没写结构树（`markinfo_only.pdf`），
也有结构树存在但 ParentTree 指向已删页面的情况（`broken_struct.pdf`）。两者都不能
当成「已加标签」直接保留——保留的结果是用户拿到一份自称有标签、实际读屏软件读不出
结构的文件。

**来源**：
- `tests/integration/test_tag_detection_branches.py::BRANCHES`（五组样本 → 状态 + 默认选择）
- `tests/integration/test_tag_detection_branches.py::test_markinfo_only_is_not_trusted`
- `tests/integration/test_tag_detection_branches.py::test_broken_struct_tree_is_treated_as_markinfo_only`
  （`has_struct_tree is True` 但状态仍是 `markinfo_only`）
- `tests/integration/test_tag_detection_branches.py::test_detect_tags_agrees_with_api`
  （直接调用与 API 结果逐字段一致，防止两条路径判据漂移）

---

## 7. 线性化「做了但没做成」必须降级为失败

**结论**：`process()` 改动了文档不等于线性化成功。真正的判据是**写盘之后**再打开
检查：`/Linearized` 标记、`/L` 声明长度与实际文件大小一致、`/H` hint 存在。任一项
不满足就把结果从 `adjusted` 降级成 `failed`（`reason_code="linearize_failed"`，
`counts.modified=0`），并把不通过的原因带进 warnings。

**为什么**：线性化失败在肉眼上完全没有痕迹——文件能打开、页面正常、只是不满足
Fast Web View。如果状态栏还写着「已调整」，用户会以为做完了。

**来源**：
- `tests/unit/test_ops_linearize.py::test_check_rejects_plain_file`
  （普通文件必须同时报出「线性化」「/L」「/H」三类问题）
- `tests/unit/test_ops_linearize.py::test_inspect_reports_declared_length_and_hint`
  （`declared_length == size`、`length_matches`、`has_hint`）
- `tests/unit/test_ops_linearize.py::test_finalize_downgrades_when_check_fails`
  （`status="failed"`、`modified=0`、`changed=False`、warnings 非空）
- `tests/unit/test_ops_linearize.py::test_linearized_document_keeps_pages_and_content`
  （线性化不得改变页数与内容）

---

## 附：怎么复现上面这些结论

```powershell
# 只跑本文档引用的用例
python -m pytest tests/unit/test_ops_linearize.py tests/unit/test_ops_bookmarks.py `
    tests/integration/test_pipeline_nontag.py tests/integration/test_tags_in_pipeline.py `
    tests/integration/test_tag_detection_branches.py `
    tests/fault_injection/test_restore_failures.py -q

# 全量
python -m pytest tests/ -q
```

样本文件在 `tests/fixtures/pdfs/`，由 `tests/fixtures/make_fixtures.py` 生成。
改样本之后要重新生成，否则上面这些用例会在新的样本上失去意义。
