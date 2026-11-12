/* 下载文件名的规则层：纯函数，不碰 DOM、不发请求。
 *
 * 为什么单独一个模块：这套规则必须能被测试直接调用。项目里没有 Node 工具链，
 * 也不该为了几条字符串规则引一个——测试在真实页面里 import 这个模块调函数，
 * 既覆盖了规则本身，也顺带证明模块在浏览器里真的加载得起来。
 *
 * 取舍（和界面文案一一对应）：
 * - 能猜的静默修：首尾空格、主名末尾的点、缺 .pdf、扩展名写成了别的
 * - 猜不出的直接报错：空名、路径分隔符、非法字符、Windows 保留名、超长
 * 不把非法字符替换成下划线——那会让用户以为名字改成功了，实际存下来的是
 * 另一个名字，事后在磁盘上找不到。
 */

/** Windows 文件名里不允许出现的字符（不含 / 与 \，那两个单独给更清楚的提示）。 */
const ILLEGAL_CHARS = '<>:"|?*';

/** Windows 保留设备名：主名与之相同（不分大小写）时系统会拒绝创建。 */
const RESERVED_NAMES = new Set([
  'CON',
  'PRN',
  'AUX',
  'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
]);

/** 主名长度上限，与后端 naming.MAX_FILENAME_CHARS 保持一致。 */
export const MAX_STEM_CHARS = 150;

function fail(name, error) {
  return { ok: false, name, error, notice: '' };
}

/**
 * 校验并规范化用户输入的文件名。
 *
 * @param {string} input 用户在输入框里打的字
 * @returns {{ok: boolean, name: string, error: string, notice: string}}
 *   `ok=false` 时 `error` 是给用户看的原因；`ok=true` 时 `name` 是最终建议名，
 *   `notice` 是自动修正的说明（没修正过就是空串）。
 */
export function validateName(input) {
  const raw = String(input ?? '');
  // 从别的窗口粘贴时常带进控制字符，直接丢掉：既不可见也没法进文件名
  const stripped = raw.replace(/[\u0000-\u001f\u007f]/g, '');
  const trimmed = stripped.trim();
  if (!trimmed) return fail('', '文件名不能为空');

  if (/[\\/]/.test(trimmed)) {
    return fail(trimmed, '文件名不能包含路径分隔符（\\ 或 /）');
  }
  const illegal = [...new Set([...trimmed].filter((ch) => ILLEGAL_CHARS.includes(ch)))];
  if (illegal.length) {
    return fail(trimmed, `文件名不能包含 ${illegal.join(' ')}`);
  }

  const notices = [];
  if (stripped !== trimmed) notices.push('已去掉首尾空格');

  const dot = trimmed.lastIndexOf('.');
  // 只有开头一个点（".pdf"）说明用户只填了扩展名，没有可用的主名
  if (dot === 0) return fail(trimmed, '文件名不能只有扩展名');
  let stem = dot > 0 ? trimmed.slice(0, dot) : trimmed;
  let ext = dot > 0 ? trimmed.slice(dot + 1) : '';

  const stemBefore = stem;
  stem = stem.replace(/[. ]+$/, '');
  if (stem !== stemBefore) notices.push('已去掉文件名末尾的点或空格');
  if (!stem) return fail(trimmed, '文件名不能只有扩展名');

  if (RESERVED_NAMES.has(stem.toUpperCase())) {
    return fail(trimmed, `「${stem}」是 Windows 保留名，请换一个`);
  }
  if (stem.length > MAX_STEM_CHARS) {
    return fail(trimmed, `文件名过长（${stem.length} 字），最多 ${MAX_STEM_CHARS} 字`);
  }

  if (ext.toLowerCase() !== 'pdf') {
    notices.push(ext ? '扩展名已改为 .pdf（下载的是 PDF 文件）' : '已自动补上 .pdf');
    ext = 'pdf';
  }

  return { ok: true, name: `${stem}.${ext}`, error: '', notice: notices.join('；') };
}
