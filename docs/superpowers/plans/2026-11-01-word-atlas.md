# Word Atlas 英语单词模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 Flask 服务中加入可同步、可批量更新的 Word Atlas 单词数据库 API 和 `/words/` 学习界面，并通过现有自更新接口部署到线上。

**Architecture:** 新增 `dev_control/words.py` 负责 SQLite 词条存储、公开查询、管理员导入和目标站点同步；`dev_control/learning.py` 挂载独立 `words/` 静态资源。数据库保存结构化字段、JSON 数组和原始数据哈希，图片/音频使用目标站点绝对 URL，前端使用原生 JavaScript 与浏览器 SpeechSynthesis。

**Tech Stack:** Python 3、Flask 3、sqlite3、Python 标准库 `urllib`/`json`/`concurrent.futures`、原生 HTML/CSS/JavaScript、unittest。

**Spec:** `docs/superpowers/specs/2026-11-01-word-atlas-design.md`

## Global Constraints

- 使用 `CREATE TABLE IF NOT EXISTS`，不重置现有 SQLite 数据。
- 默认目标源是 `https://0xlei.tech/words/atlas/`，索引声明 7298 个词、每块 100 个。
- 同步先完整抓取和校验，全部成功后一次事务写入；失败保留旧库。
- 列表默认 `page=1`、`page_size=40`，最大 100；按词典序稳定排序。
- 公开读取，导入和同步要求管理员 Bearer token。
- 图片和音频默认返回目标站点绝对 URL，不将二进制复制进 SQLite。
- 例句使用 `en-GB`/`en-US` 浏览器 SpeechSynthesis；单词优先使用目标站点 MP3。
- 部署使用 `package.sh` 与现有 `/api/updates/upload`、`/api/updates/<id>/apply` 流程。

## Review Focus

- 目标索引与 entries 分块缺块、重复 ID、ID 不匹配时：测试确认整个同步失败且旧数据不变（Task 3）。
- JSON/JSONL 导入字段缺失、非法 JSON、重复 ID 和 `replace=true` 误删风险：测试确认批次拒绝或只在完整成功后替换（Task 3）。
- 列表分页、中文/英文包含搜索、越界页和批量未知/重复 ID：测试确认稳定 envelope、顺序和 `missing`（Task 2）。
- 资源 URL 中的词条 ID 只能使用安全来源 ID，详情不存在返回 404：测试确认 URL 构造和错误状态（Task 2）。
- 线上没有管理员凭据或更新接口不可达时：部署脚本不猜密码，输出可执行的人工上传命令并把阻塞写入报告（Task 6）。

### Task 1: 数据库与词条序列化边界

**Files:**
- Modify: `dev_control/db.py`（新增 `word_entries` 表和索引）
- Create: `dev_control/words.py`（纯数据校验、序列化、资源 URL 辅助函数）
- Test: `tests/test_words.py`

**Interfaces:**
- Produces `validate_entry(payload) -> dict`、`entry_from_row(row) -> dict`、`asset_urls(entry_id, base_url) -> dict`，供 API 和同步器使用。

- [ ] **Step 1: Write the failing tests**
  - 测试 `create_app` 后存在 `word_entries` 表。
  - 测试一个 `library` 样例被规范化为 `id/word/zh/uk_ipa/us_ipa/cefr/topic/senses/examples/tip/collocations/family/synonyms/antonyms/raw_json`。
  - 测试缺少 `id` 或 `w`、非列表 senses/examples、非法 ID 时抛出 `ValueError`。
  - 测试 `asset_urls("library", ...)` 返回 atlas/classic 图片和 uk/us 音频 URL。
- [ ] **Step 2: Run tests to verify failure**
  - Run: `python -m unittest tests.test_words.WordDataTest -v`
  - Expected: 表或函数不存在导致失败。
- [ ] **Step 3: Implement minimal schema and helpers**
  - 在 `SCHEMA` 中加入 `word_entries` 标量列、JSON 文本列、`content_hash`、`source_updated_at`、`updated_at`，并为 `word`、`cefr`、`topic` 建索引。
  - 在 `dev_control/words.py` 中实现严格字段校验、规范化 JSON 和安全 URL 构造；未知字段保留到 `raw_json`。
- [ ] **Step 4: Run tests to verify pass**
  - Run: `python -m unittest tests.test_words.WordDataTest -v`
  - Expected: PASS。

### Task 2: 公开查询 API

**Files:**
- Modify: `dev_control/__init__.py`（注册 blueprint）
- Modify: `dev_control/apis.py`（登记 OpenAPI 路径）
- Modify: `dev_control/words.py`
- Test: `tests/test_words.py`

**Interfaces:**
- `GET /api/words?q=&topic=&cefr=&page=1&page_size=40`
- `GET /api/words/<word_id>`
- `POST /api/words/batch` with `{"ids":[],"words":[]}`

- [ ] **Step 1: Write failing API tests**
  - 预置两个词条，断言列表响应含 `items,total,page,page_size,pages`，默认 page size 为 40。
  - 断言英文和中文包含搜索、topic/cefr 筛选、稳定词典序和空页行为。
  - 断言详情返回完整 arrays 和 `images/audio`，不存在 ID 返回 404。
  - 断言 batch 去重后按输入顺序返回，未知项进入 `missing`。
- [ ] **Step 2: Run tests to verify failure**
  - Run: `python -m unittest tests.test_words.WordApiTest -v`
  - Expected: 404 或 blueprint 未注册。
- [ ] **Step 3: Implement query handlers**
  - 使用参数校验限制 page/page_size；q 对 `word` 和 `zh` 做大小写不敏感包含匹配；详情和 batch 共用 `entry_from_row`。
  - 读取接口不要求登录，写入接口留给 Task 3。
- [ ] **Step 4: Run focused and existing API tests**
  - Run: `python -m unittest tests.test_words.WordApiTest tests.test_apis -v`
  - Expected: PASS。

### Task 3: JSON/JSONL 导入与原子同步

**Files:**
- Modify: `dev_control/words.py`
- Modify: `dev_control/apis.py`
- Test: `tests/test_words.py`

**Interfaces:**
- `POST /api/words/import` admin multipart `file`, optional `replace=true`
- `POST /api/words/sync` admin JSON optional `source_base`, `dry_run`
- Internal `fetch_source_snapshot(base_url) -> (index, entries)` and `upsert_entries(entries, replace=False, dry_run=False) -> counts`

- [ ] **Step 1: Write failing import and sync tests**
  - 登录管理员后上传 JSON 数组和 JSONL，断言新增/更新/unchanged 计数及数据库内容。
  - 上传非法 JSON、重复 ID、缺字段，断言 400 且已有数据不变。
  - 用 patch 的本地 HTTP 响应模拟两块 entries，断言 sync 合并后一次写入。
  - 模拟缺块、总数不符、entry ID 与 index 不符，断言 sync 失败且旧库完整保留。
  - 断言 `replace=true` 仅在完整成功后删除缺失词条。
- [ ] **Step 2: Run tests to verify failure**
  - Run: `python -m unittest tests.test_words.WordImportSyncTest -v`
  - Expected: endpoint 404 或同步器未实现。
- [ ] **Step 3: Implement importer and crawler**
  - 解析 multipart 文件为 JSON 数组或逐行 JSON；规范化并去重校验后，单事务 upsert，使用规范化 JSON SHA-256 统计 unchanged。
  - 用标准库 HTTP 请求、每块超时/最多两次重试和有限并发抓取 index/entries；校验 chunk 覆盖、总数、唯一 ID 与 index 对应；全部成功后再写库。
  - `replace=true` 仅在完整快照成功后清理该 source 的缺失行；dry_run 只返回统计。
- [ ] **Step 4: Run focused tests and full suite**
  - Run: `python -m unittest tests.test_words.WordImportSyncTest -v`
  - Run: `python -m unittest discover -s tests -v`
  - Expected: 全部 PASS。

### Task 4: Word Atlas 学习界面

**Files:**
- Create: `words/index.html`
- Create: `words/app.js`
- Create: `words/style.css`
- Modify: `dev_control/learning.py`（挂载 `/words/`）
- Test: `tests/test_words.py`

**Interfaces:**
- Browser route `/words/` and hash route `#/atlas?w=library`.
- Frontend consumes the Task 2 API and asset URL contract.

- [ ] **Step 1: Write failing static route tests**
  - 断言 `/words/` 返回 200，包含搜索、`library` hash 支持、atlas/classic、UK/US、例句 TTS 文案和 CC BY 4.0 来源。
  - 断言 CSS/JS 静态资源可读取。
- [ ] **Step 2: Run tests to verify failure**
  - Run: `python -m unittest tests.test_words.WordPageTest -v`
  - Expected: 404 或静态文件不存在。
- [ ] **Step 3: Implement the page**
  - 用原生 JS 做列表加载、筛选分页、详情抽屉、hash 路由、画风切换、本地 accent 偏好和错误状态。
  - 单词播放 `<audio>` 目标 MP3；例句用 SpeechSynthesisUtterance，按当前 accent 选择声音，缺失时显示提示。
  - 使用响应式 CSS，详情布局参考目标站点 `library`，但不复制其 React bundle。
- [ ] **Step 4: Run route tests and manual browser smoke check**
  - Run: `python -m unittest tests.test_words.WordPageTest -v`
  - 启动临时 Flask 服务，打开 `/words/#/atlas?w=library`，确认列表、详情、音频按钮和例句 TTS 控件可见。

### Task 5: 文档、接口说明与打包前验证

**Files:**
- Modify: `README.md`（新增 Word Atlas API/同步/许可证和部署说明）
- Modify: `dev_control/apis.py`（补齐 OpenAPI 描述）
- Test: `tests/test_apis.py`

- [ ] **Step 1: Write failing OpenAPI assertions**
  - 断言 `/api/openapi.json` 包含五个 Word Atlas 路径及管理员/公开安全说明。
- [ ] **Step 2: Run test to verify failure**
  - Run: `python -m unittest tests.test_apis.ApisTest.test_word_atlas_paths -v`
  - Expected: 路径缺失。
- [ ] **Step 3: Implement docs and OpenAPI entries**
  - 同步实际参数、响应 envelope、资源 URL 和导入格式，说明来源为目标站点 CC BY 4.0。
- [ ] **Step 4: Run complete verification**
  - Run: `python -m unittest discover -s tests -v`
  - Run: `python -m py_compile dev_control/*.py`
  - Expected: 全部 PASS，无编译错误。

### Task 6: 打包、线上部署和线上词库同步

**Files:**
- Create: `scripts/deploy_word_atlas.sh`（只封装已授权的 URL/token 环境变量，不保存密钥）
- Generated: `dist/simple-server-<version>.tar.gz`

- [ ] **Step 1: Build the release archive**
  - Run: `VERSION=1.0.14 ./package.sh /tmp/simple-server-word-atlas.tar.gz`
  - Expected: 归档包含新代码和 `words/`，不包含 `data/` 或密钥。
- [ ] **Step 2: Upload through the existing update API**
  - Require `DEPLOY_TOKEN` from the authorized operator; send multipart archive to `http://8.138.86.215:8080/api/updates/upload`.
  - Expected: 201 with staged update and diff containing Word Atlas files.
- [ ] **Step 3: Apply and wait for restart**
  - POST `/api/updates/<id>/apply` with `{"restart":true}`, poll `/api/health` until 200.
  - Expected: health status `ok` and new version.
- [ ] **Step 4: Populate production database**
  - POST `/api/words/sync` with the same authorized token.
  - Expected: approximately 7298 entries, `library` detail includes senses/examples, UK/US audio URLs and both image URLs.
- [ ] **Step 5: Run production smoke checks**
  - GET `/words/`, `/api/words?q=library`, `/api/words/library`, `/api/words/batch` and verify 200/expected payloads.
  - If no authorized token is available in the session or the server is unreachable, stop before mutation and report exact commands and the missing credential.
