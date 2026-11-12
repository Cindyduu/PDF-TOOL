# 开发计划：下载时选择保存位置与改文件名（R12）

状态：**待验收**（未写一行代码）
版本：草案 1
日期：2026-11-06

---

## 1. 问题与目标

现在结果页的「下载处理后的 PDF」是一个普通链接：

```html
<a id="download-output" href="/api/jobs/{id}/file?kind=output" download="xxx_已处理_...pdf">
```

点击后文件直接落到浏览器默认下载目录，名字是后端生成的
`原名_已处理_时间戳_任务ID.pdf`。用户既不能选文件夹，也不能改名字，
想去桌面或项目目录得下载完再手动搬一次。

目标：点下载时先弹出一个小面板，能改文件名、能在支持的浏览器里选文件夹。

## 2. 一个必须先说清的技术事实

**网页没法凭空指定保存路径。** 浏览器出于安全不允许 JS 往任意磁盘路径写文件。
能同时满足「选位置 + 改名」的只有两条路：

| 方案 | 原理 | 优点 | 缺点 |
| --- | --- | --- | --- |
| A. File System Access API（`showSaveFilePicker`） | 浏览器弹**系统原生另存为窗口**，用户选文件夹并确认文件名，JS 拿到句柄后写入 | 真·任意位置、真·改名；无需后端改动；流式写入不占内存 | 只有 Chromium 系（Edge / Chrome / 360 极速模式）支持；Firefox、Safari 没有 |
| B. 后端弹 Windows 原生对话框 | 本地 Python 服务用 `ctypes` 调 `GetSaveFileNameW`，用户选好后服务端把文件复制过去 | 所有浏览器一致 | 窗口会弹在浏览器**后面**（抢焦点是出了名的难）；打包态要跨线程跑 Win32 消息循环，一旦卡住就是整个服务无响应；e2e 完全测不了 |
| C. 纯 `<a download>` | 只改文件名，位置由浏览器设置决定 | 全兼容、零风险 | 满足不了「选位置」 |

**选定 A + C 组合**：支持的浏览器走 A（真正的选位置），不支持的自动降级到 C
（能改名，位置由浏览器决定），并在面板上**明说**当前是哪种。

B 方案被否掉的理由不是"麻烦"，而是它把风险从"功能不好用"变成了"服务可能挂住"——
这个工具的核心价值是稳，不值得为了兼容 Firefox 去动服务进程的消息循环。

## 3. 交互设计（文案级）

点「下载处理后的 PDF」→ 弹出面板：

```
┌─ 保存处理后的 PDF ───────────────────────────┐
│ 原文件：text_blocks.pdf                      │
│ 大小：1.2 MB                                 │
│                                              │
│ 文件名  [ text_blocks_已处理_20261106-… .pdf ]│  ← 预填 + 全选
│         已自动补上 .pdf                       │  ← 自动修正提示（灰）
│                                              │
│ 点击保存后可选择文件夹并确认文件名。           │  ← 有 picker 时的说明
│ 当前浏览器不支持选择保存位置，将保存到浏览器    │  ← 无 picker 时的说明
│ 默认下载目录（文件名仍可修改）。                │
│                                              │
│              [ 保存 ]  [ 取消 ]               │
└──────────────────────────────────────────────┘
```

- 输入框预填 `output.file_name` 并**全选**，想改名直接打字覆盖。
- 回车 = 保存，Esc = 取消（`<dialog>` 原生行为）。
- 保存按钮在文件名不合法时**禁用**，旁边红字说明原因。
- 取消（含系统另存为窗口里点取消）→ 静默关闭，不报错、不留半截文件。

## 4. 文件名规则

前端只负责给浏览器一个「建议文件名」，不涉及服务端存储，所以规则以
**用户能看懂**为准，不照搬后端 `sanitize_filename` 的静默改写：

| 情况 | 处理 | 理由 |
| --- | --- | --- |
| 首尾空格、主名尾部的点 | **自动去掉** + 灰字提示 | 用户看不见，静默修比报错友好；Windows 本来也会吃掉 |
| 不以 `.pdf` 结尾（大小写不敏感） | **自动补** + 灰字提示 | 少一步操作，且不会存出打不开的文件 |
| 空 / 全空格 | 报错「文件名不能为空」，禁用保存 | 没有合理猜测空间 |
| 含 `\ / : * ? " < > \|` | 报错并列出具体字符，禁用保存 | 静默替换成 `_` 会让用户以为名字改成功了 |
| `CON` `PRN` `AUX` `NUL` `COM1-9` `LPT1-9`（主名） | 报错「这是 Windows 保留名」 | 系统会直接拒绝创建，不如提前说 |
| 主名超过 150 字符 | 报错并显示当前长度 | 后端上限也是 150，超了会被截断 |
| 中文、空格、emoji、括号 | **允许** | 没有理由禁止 |

`\ /` 报错而不是取 basename，是因为用户输入路径通常是误操作（以为这里是选位置的地方），
静默截成文件名会让他更困惑。

## 5. 范围

**做**

- 结果页单文件下载：改文件名 + 选位置（支持的浏览器）
- 文件名规则模块与校验
- PRD 增补 R12 及验收条目

**不做（本次）**

- **批量下载**（`全部下载`）：一次弹 N 个另存为窗口是灾难，保持现状。批量页每个文件
  的单独「下载」链接也保持现状——要改名就去结果页。
- **历史页下载链接**：同上，保持一行一个直链。
- 记住"上次保存到哪个目录"：浏览器自己会记住（同一会话内），不做持久化。
- 后端**新增**接口：不新增路由。但下载端点要加一个可选查询参数（见下）。

> **修正（S2 实测推翻原判断）**：原本以为「同源下载时 `<a download="新名">` 的
> `download` 属性优先于服务端 `Content-Disposition`」，所以零后端改动也够。
> 实测在 Chrome 里是**反过来的**：只要响应带了 `Content-Disposition: attachment`，
> 浏览器就采信 header 里的 `filename*`，`download` 属性被忽略——测试里
> `download.suggested_filename` 拿到的仍是服务端生成的 `xxx_已处理_....pdf`。
>
> 所以改为：`GET /api/jobs/{job_id}/file` 增加可选参数 `name`，有值时用它构造
> `Content-Disposition`，无值或给不出可用名字时行为与原来**完全一致**。名字只进
> 响应头，不参与任何路径拼接；`sanitize_filename` 会剔掉控制字符与路径分隔符，
> 没有响应头注入面。前端 `downloadWithName()` 同时把名字写进 URL 和 `download`
> 属性，两种浏览器行为都能覆盖。

## 6. 分步执行（每步一次提交，每步先加测试再跑）

### S1 文件名规则模块

- 新增 `app/static/js/filename.js`，纯函数、无 DOM 依赖：
  - `validateName(raw)` → `{ ok, name, error, notice }`
  - 内部处理第 4 节全部规则
- 新增 `tests/e2e/test_ui_save_as.py`：用 Playwright 在真实页面里
  `await import('/static/js/filename.js')` 直接调纯函数，参数化覆盖上表每一行
  （约 12 组），**不需要 Node 工具链**。
- 验收：`pytest tests/e2e/test_ui_save_as.py -k filename` 全绿。

### S2 「另存为」面板（只做 UI 与校验，不碰落盘）

- 新增 `app/static/js/save-as.js`：构建 `<dialog>`、绑定校验、返回
  `Promise<string | null>`（确认的文件名 / 取消）
- 改 `app/static/js/views/result.js` 第 137–157 行的按钮区：
  - `#download-output` **仍是 `<a>`**，保留 `href` 与 `download`（右键「链接另存为」、
    JS 失效时仍能下载，是渐进增强而非两套逻辑）
  - 新增 click 拦截：`preventDefault()` → 打开面板 → 用户确认后才落盘
- `app/static/css/app.css` 补 dialog 样式，跟现有卡片风格一致
- **后端配合改名**（S2 实测后追加）：`app/api/jobs.py::get_file` 加可选
  `name` 查询参数（`max_length=512`）；`app/core/naming.py` 加
  `preferred_download_name(requested, default)`，把用户名字清洗成可用文件名，
  给不出主名时退回默认名
- 测试：面板出现、预填名等于 `output.file_name`、非法名禁用保存、取消后无下载
- 新增 `tests/integration/test_download_name.py`（13 条）：自定义名生效、补后缀、
  非法字符替换、控制字符进不了响应头、空/纯目录名退回默认、**有无 `name`
  时响应体字节完全相同**、超长名 422、`kind=source` 同样支持
- `tests/unit/test_naming.py` 追加 `preferred_download_name` 参数化用例（15 组）
- 验收：`pytest tests/e2e/test_ui_save_as.py tests/integration/test_download_name.py` 全绿

### S3 真正落盘（picker 主路径 + 降级）

- `save-as.js` 的 `saveFile(url, name)` → `Promise<'picker'|'download'|'cancelled'>`：
  - 有 `window.showSaveFilePicker` → 调它拿 handle（`suggestedName` = 用户确认名，
    `types` 限定 `.pdf`）→ `fetch(url)` → `response.body.pipeTo(writable)` 流式写入，
    大文件不进内存 → 失败时 `abort()`，不留半个文件
  - 没有 → 创建 `<a download="新名">` 点击
  - `AbortError`（用户点取消）→ 返回 `'cancelled'`，静默
  - **`NotAllowedError` / `SecurityError`**（用户激活过期、跨域 iframe、策略禁用）
    → 不报错，直接降级成普通下载。理由：这两种情况下用户已经点过一次「保存」，
    再让他白点一次比降级更糟；名字仍然是对的
  - 位置提示按能力动态给：能选位置说「会弹出系统窗口」，不能就直说「存到默认下载目录」
- 顺序上**先 fetch 成功再 createWritable**：fetch 挂了不会在磁盘上留下空文件
- 测试（6 条，全部用 `page.evaluate` 打桩 `showSaveFilePicker`）：
  - 桩断言 `suggestedName`、`types[0].accept` 含 `.pdf`、写入字节数与
    `/api/jobs/{id}/file` 的响应体**完全一致**且以 `%PDF-` 开头、且不再触发浏览器下载
  - 桩抛 `AbortError` → 无错误提示、未建写入流
  - 桩抛 `NotAllowedError` → 降级下载，`suggested_filename` 仍是新名字
  - 桩置 `undefined`（模拟 Firefox/Safari）→ `expect_download()` 断言新名字
  - 位置提示文案随能力变化
  - `page.route` 让文件请求 500 → 页面报「保存失败」且没写入任何字节
- 验收：`pytest tests/e2e/test_ui_save_as.py` 全绿（13 条）

### S4 PRD 同步 + 全量回归

- `PRD.md`：新增 `### R12 下载时选择保存位置与改文件名`，含 AC-R12-N1..N5、E1..E3；
  版本 2.0 → 2.1；附录 A 加修订记录
- `tests/unit/test_prd_matrix.py`：`REQUIREMENT_IDS` 扩到 `R12`，docstring 同步
- 验收：全量 `pytest` 通过（当前基线 **793 passed**，预计增至 **810+**）

## 7. 会动到的现有测试（先说清楚，不藏）

| 用例 | 影响 | 处理 |
| --- | --- | --- |
| `tests/e2e/test_ui_flow.py::test_full_flow_from_upload_to_download` | `page.click("#download-output")` 不再直接触发下载，要先过面板 | 已改为「点开 → 等面板 → 点保存 → expect_download」，`.pdf` 与 `%PDF-` 断言保留。只会更严 |
| `tests/e2e/test_ui_flow.py::test_preview_failure_shows_notice_but_keeps_download_enabled` | 断言 `#download-output` 的 `href` 含 `/api/jobs/{id}/file?kind=output` | **不用改**——元素仍是带 `href` 的 `<a>` |
| `tests/e2e/test_ui_batch.py`、`history` 相关 | 批量/历史下载不动 | 不用改 |

不删测试、不 skip、不 xfail、不降低任何断言强度。

## 8. 已知限制（会写进 PRD）

1. Firefox / Safari 用户**无法选位置**，只能改名字后落到默认下载目录。面板会明说。
2. Chrome 若判定「用户激活已过期」（面板关掉到调 picker 之间拖太久），会自动降级成
   普通下载——文件仍按新名字落到默认目录，只是没让你选位置。这条无法在自动化里
   验到（系统窗口点不了），只能靠手工验收确认主路径真的弹窗。
3. 磁盘写满或中途拔盘时，系统另存为可能留下一个不完整的文件，工具无法代为清理。
4. 打包产物（`dist/PDFTool`）需重新打包才包含新前端；本次验收以**源码态 8765** 为准。
5. 分享包 `D:\xindi.du\pdftool\` 是旧版，功能验收通过后再决定是否重新出包（本次不做）。

## 9. 验收方式

1. 每完成一步：跑该步测试 → 给你看结果 → 提交
2. 四步全完：跑全量 `pytest`（带 `--basetemp`）→ 给你看 `N passed`
3. 手工验收（8765 源码态，Edge 打开）：
   - 处理一个 PDF → 点下载 → 面板出现且预填名正确
   - 改成 `测试改名` → 点保存 → 系统另存为窗口弹出、名字显示 `测试改名.pdf` → 存到桌面 → 确认文件能打开
   - 文件名填 `a<b` → 保存按钮变灰并提示原因
   - 打开系统另存为窗口后点取消 → 回到页面、无报错
