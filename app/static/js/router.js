/* hash 路由。
 *
 * 为什么用 hash 而不是 history.pushState：这个工具是 `python run.py` 起在
 * 127.0.0.1 上的，没有前端服务器替我们兜底 rewrite。刷新 `#/result/job-x`
 * 时浏览器只会请求 `/`，页面自己从 hash 恢复状态——不需要后端配合，
 * 也不会出现刷新变 404。前进/后退同理，交给浏览器的 hashchange。
 */

export const ROUTES = [
  { name: 'home', pattern: /^\/home\/?$/ },
  { name: 'config', pattern: /^\/config\/([^/]+)\/?$/, keys: ['jobId'] },
  { name: 'processing', pattern: /^\/processing\/([^/]+)\/?$/, keys: ['jobId'] },
  { name: 'result', pattern: /^\/result\/([^/]+)\/?$/, keys: ['jobId'] },
  { name: 'history', pattern: /^\/history\/?$/ },
  { name: 'backup', pattern: /^\/backup\/?$/ },
];

export const DEFAULT_ROUTE = 'home';

function parseHash(hash) {
  const path = String(hash || '').replace(/^#/, '') || '/home';
  for (const route of ROUTES) {
    const match = route.pattern.exec(path);
    if (!match) continue;
    const params = {};
    (route.keys || []).forEach((key, index) => {
      params[key] = decodeURIComponent(match[index + 1]);
    });
    return { name: route.name, params, path };
  }
  return { name: DEFAULT_ROUTE, params: {}, path, unknown: path };
}

export function navigate(path) {
  const target = path.startsWith('#') ? path : `#${path}`;
  location.hash = target;
}

export function createRouter(handler) {
  let current = null;

  function run() {
    const route = parseHash(location.hash);
    const signature = `${route.name}:${route.path}`;
    if (signature === current) return;
    current = signature;
    handler(route);
  }

  window.addEventListener('hashchange', run);

  // 首屏：没有 hash 时用 replaceState 补一个，不触发 hashchange，
  // 避免"启动时渲染两次"导致的重复请求。
  if (!location.hash) {
    history.replaceState(null, '', '#/home');
  }
  run();

  return {
    current: () => parseHash(location.hash),
    refresh: () => {
      current = null;
      run();
    },
  };
}
