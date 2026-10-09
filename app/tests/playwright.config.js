// Run from sva/app:  npx playwright test -c tests/playwright.config.js [spec]
// Two projects: "root" (PUBLIC_BASE_PATH empty, :PORT) and "prefixed" (/app through a strip-prefix proxy on :PORT+1,
// like the site Ingress rewrite). Before app/main.py exists (A1) a static server stands in; specs then mock /api with
// e2e/mockapi.js. Set SVA_E2E_LIVE=1 to make specs skip the mocks and hit the real app.
const fs = require('fs');
const path = require('path');

const SVA = path.resolve(__dirname, '..', '..');
const PY = path.join(SVA, '.venv', 'bin', 'python');
const E2E = path.join(SVA, '.e2e');
const hasApp = fs.existsSync(path.join(SVA, 'app', 'main.py'));
// SVA_E2E_PORT: app port (proxy = port + 1); give each parallel worktree its own, servers are reused by port.
const PORT = +(process.env.SVA_E2E_PORT || 8300), PROXY = PORT + 1;

const appEnv = {
  MODE: 'dev',
  DATA_BACKEND: 'duckdb',
  ASSET_DIR: path.join(SVA, 'fixtures', 'sva-out'),
  INDEX_DUCKDB: path.join(E2E, 'index.duckdb'),
  APP_DUCKDB: path.join(E2E, 'app.duckdb'),
  RUNS_DIR: path.join(E2E, 'runs'),
  LIB_DIR: path.join(SVA, 'app', 'static', 'lib'),
  PORT: String(PORT),
};

module.exports = {
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: { trace: 'retain-on-failure', viewport: { width: 1440, height: 900 } },
  projects: [
    { name: 'root', use: { baseURL: `http://127.0.0.1:${PORT}/` } },
    { name: 'prefixed', use: { baseURL: `http://127.0.0.1:${PROXY}/app/` } },
  ],
  webServer: [
    ...(hasApp ? [
    {
      command: `rm -rf "${E2E}" && mkdir -p "${E2E}" && "${PY}" fixtures/gen_fixture.py --no-media --out "${E2E}/fx" --index "${E2E}/index.duckdb" && "${PY}" -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT}`,
      cwd: SVA, env: appEnv, url: `http://127.0.0.1:${PORT}/health`, reuseExistingServer: !process.env.CI, timeout: 60_000,
    }] : [{
      command: `"${PY}" -m http.server ${PORT} --bind 127.0.0.1 --directory app/static`,
      cwd: SVA, url: `http://127.0.0.1:${PORT}/`, reuseExistingServer: !process.env.CI, timeout: 30_000,
    }]),
    {
      command: `"${PY}" app/tests/prefix_proxy.py --listen ${PROXY} --prefix /app --upstream ${PORT}`,
      cwd: SVA, url: `http://127.0.0.1:${PROXY}/app/`, reuseExistingServer: !process.env.CI, timeout: 30_000,
    },
  ],
};
