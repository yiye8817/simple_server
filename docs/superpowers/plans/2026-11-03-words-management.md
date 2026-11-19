# Words 管理、导入与接口调试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Word Atlas 学习页中加入管理员新建/批量导入单词、单词读取接口调试能力，并把入口接入控制台首页。

**Architecture:** 复用现有 `dev_control.words` 的词条校验、事务写入和读取接口；新增一个管理员专用 `POST /api/words` 创建接口。`words/index.html`、`words/app.js` 和 `words/style.css` 增加同页管理区，使用控制台已有 `localStorage.devControlToken`；`templates/control.html` 增加侧栏和首页快捷入口。

**Tech Stack:** Python 3、Flask、SQLite、原生 HTML/CSS/JavaScript、unittest。

**Spec:** `docs/superpowers/specs/2026-11-03-words-management-design.md`

## Global Constraints

- 公开读取接口继续无需登录；创建和批量导入必须管理员 Bearer token。
- 词条输入沿用 `validate_entry` 支持的词光紧凑结构，未知字段保留在 `raw_json`。
- 单个创建重复 ID 返回 409，不覆盖已有词条；非法批次不得改变已有数据。
- 不改变现有列表、详情、批量读取接口响应格式，不新增前端构建依赖。
- 页面调试器展示请求 URL、状态码和原始 JSON；不把 token 拼接到 URL。

## Review Focus

- 未登录或普通用户调用创建接口：必须分别返回 401/403；由 Task 1 的权限测试覆盖。
- 创建缺少 `id`/`w`、列表字段类型错误：必须返回 400 且没有插入；由 Task 1 的原子性测试覆盖。
- 重复 ID 创建：必须返回 409 且保留原词条；由 Task 1 覆盖。
- 批量 JSON/JSONL 导入错误：页面必须显示后端错误，不能误报成功；由 Task 2 的页面字符串/行为测试和现有导入测试覆盖。
- 调试器查询参数包含中文、空结果和 API 错误：必须正确编码并展示 JSON；由 Task 2 的静态标记测试和现有读取接口测试覆盖。

### Task 1: 管理员单词创建接口

**Files:**
- Modify: `dev_control/words.py`（在现有读取路由和导入路由附近新增 `POST /api/words`）
- Test: `tests/test_words.py`
- Modify: `dev_control/apis.py`（登记 `POST /api/words`）

**Interfaces:**
- Consumes: `validate_entry(payload)`, `upsert_entries(entries, replace=False, ...)`, `require_user(admin=True)`。
- Produces: `POST /api/words`；成功 `201 {item: ...}`，重复 `409 {error: ...}`，非法请求 `400`。

- [ ] **Step 1: Write failing tests**
  - 增加测试：未认证返回 401，普通用户返回 403。
  - 增加测试：管理员用 `LIBRARY` 创建返回 201，随后 `GET /api/words/library` 可读；缺少 `id` 的请求返回 400 且数据库没有新增行。
  - 增加测试：再次创建同 ID 返回 409，原 `tip` 保持不变。
- [ ] **Step 2: Run focused tests to verify failure**
  - Run: `python3 -m unittest tests.test_words.WordCreateApiTest -v`
  - Expected: `POST /api/words` 当前返回 405/404，新增测试失败。
- [ ] **Step 3: Implement minimal endpoint**
  - 用 `request.get_json(silent=True)` 检查对象；先 `validate_entry`，再查询 ID；不存在时调用 `upsert_entries` 单条写入并用 `_get_word` 返回完整 `item`。
  - 使用 `@require_user(admin=True)`；捕获 `ValueError` 为 400，数据库冲突路径返回 409。
- [ ] **Step 4: Run focused tests**
  - Run: `python3 -m unittest tests.test_words.WordCreateApiTest -v`
  - Expected: PASS。
- [ ] **Step 5: Add OpenAPI path and rerun API checks**
  - `POST /api/words` 说明 JSON 词光对象、管理员权限、201/400/409 响应。
  - Run: `python3 -m unittest tests.test_apis tests.test_words.WordCreateApiTest -v`

### Task 2: 首页入口与 `/words/` 管理/调试工作区

**Files:**
- Modify: `templates/control.html`（侧栏、首页快捷卡片、标题映射和 `openView`）
- Modify: `words/index.html`（管理与调试面板结构）
- Modify: `words/app.js`（认证、创建、导入、调试请求和结果渲染）
- Modify: `words/style.css`（管理区和调试区样式）
- Test: `tests/test_words.py`

**Interfaces:**
- Consumes: `POST /api/words`、`POST /api/words/import`、`GET /api/words`、`GET /api/words/<word_id>`、`localStorage.devControlToken`。
- Produces: 控制台点击 Words 后进入 `/words/`；登录后可见单条创建、批量导入、单个/列表调试控件。

- [ ] **Step 1: Write failing static/page tests**
  - 断言 `/` 页面源代码包含 `words` 入口、标题和 `/words/`。
  - 断言 `/words/` 页面包含创建表单、JSON/JSONL 导入控件、单词 ID 调试输入、列表查询字段和结果容器。
  - 断言 `app.js` 包含 `devControlToken`、`/api/words`、`/api/words/import`、`/api/words/`、错误处理和状态码展示相关标记。
- [ ] **Step 2: Run focused tests to verify failure**
  - Run: `python3 -m unittest tests.test_words.WordManagementPageTest -v`
  - Expected: 页面尚无这些管理/调试标记，测试失败。
- [ ] **Step 3: Implement home entry**
  - 在“内容与工具”导航增加 `data-view="words"`，在首页常用管理增加 Words 快捷卡片；新增 `v-words` iframe、标题映射和 `/words/` lazy load。
- [ ] **Step 4: Implement page management markup and behavior**
  - 管理区默认折叠或登录提示；登录后用 `localStorage.devControlToken` 发送 `Authorization: Bearer ...`。
  - 单条创建发送 JSON；文件导入发送 multipart；显示成功计数或错误 JSON。
  - 调试器构造 `URLSearchParams`，分别请求单个和列表接口，把状态码、请求 URL、响应 JSON 写入 `<pre>`，所有动态文本转义。
- [ ] **Step 5: Add CSS and run page tests**
  - 管理区在窄屏下单列排列，沿用现有词光色板和按钮样式。
  - Run: `python3 -m unittest tests.test_words.WordManagementPageTest -v`
  - Expected: PASS。

### Task 3: 全量回归与静态验证

**Files:**
- Modify: `README.md`（补充单个创建和页面管理/调试使用说明）
- Test: `tests/test_words.py`、`tests/test_apis.py`（仅在需要时补充断言）

- [ ] **Step 1: Update README**
  - 说明 `/words/` 首页入口、词光 JSON 示例、`POST /api/words`、现有批量导入和调试区的认证要求。
- [ ] **Step 2: Run complete test suite**
  - Run: `python3 -m unittest discover -s tests -v`
  - Expected: 全部通过；如已有环境问题，记录具体失败。
- [ ] **Step 3: Run Python compilation**
  - Run: `python3 -m py_compile dev_control/*.py`
  - Expected: exit code 0。
- [ ] **Step 4: Inspect diff and route smoke checks**
  - 用 Flask test client 检查 `/`、`/words/`、`/words/app.js` 返回 200；确认新增接口的状态码和 OpenAPI 路径。
