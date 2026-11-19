# Words 管理、导入与接口调试设计

## 意图

让控制台首页能直接进入现有 `/words/` 词光页面，并在同一页面完成管理员新增单词、批量导入和读取接口调试。公开访客继续使用搜索与词条详情；管理能力复用现有 Bearer 登录令牌。

## 目标

- 在首页控制台侧栏和概览快捷区增加 `Words / 词光` 入口，打开 `/words/`。
- 增加管理员单词创建接口 `POST /api/words`，使用现有词光紧凑字段结构（`id`、`w`、`uk`、`us`、`senses`、`ex`、`col`、`fam`、`syn`、`ant`、`m` 等）。重复 ID 返回冲突，不覆盖已有词条。
- 在 `/words/` 页面增加登录后可见的单词管理区：单个 JSON 创建、JSON/JSONL 文件批量导入、导入结果提示。
- 增加读取接口调试区：调用 `GET /api/words/<word_id>` 获取单个词条，调用 `GET /api/words` 获取词条列表，展示请求地址、状态码和原始 JSON。
- 将新增接口登记到 OpenAPI，并为前端管理功能补充必要的回归测试。

## 非目标

- 不改变现有公开列表、详情、批量读取接口的响应格式。
- 不重写现有词条校验、同步器、学习页视觉风格或数据库结构。
- 不新增账户体系；管理员身份继续由现有 Bearer token 判定。
- 不把管理表单开放给未登录用户，也不在浏览器保存新的长期凭据。

## 约束

- 项目使用 Flask、SQLite 和原生 HTML/CSS/JavaScript，无前端构建链。
- `validate_entry` 是词光结构的唯一校验入口，未知字段继续保存在 `raw_json`。
- 单个创建和批量导入必须使用一次数据库事务；非法批次不得改变已有数据。
- 页面从控制台跳转时复用 `localStorage.devControlToken`，未登录时管理区显示登录提示；公开读取仍无需登录。

## 方案与取舍

1. **同页管理区（采用）**：在 `/words/` 的公开学习区下方加入可折叠管理与调试面板，新增 `POST /api/words`，批量导入继续调用已有 `POST /api/words/import`。改动集中，操作路径短，学习页仍可公开访问。
2. **独立 `/words/manage/` 页面**：权限边界清晰，但需要重复导航、样式和登录状态处理；用户需要在两个页面间切换。
3. **只增强 `/apis/`**：只提供接口，不提供 `/words/` 内的业务操作，无法满足页面管理和调试的直接使用场景。

## 已确定的行为

- `POST /api/words` 仅管理员可调用，请求体必须是单个词光 JSON 对象；校验成功且 ID 不存在时返回 `201` 和 `item`，ID 已存在返回 `409`，格式错误返回 `400`。
- 单个创建使用 `upsert_entries(..., replace=False)` 的事务能力，但在写入前显式检查 ID，确保“新建”不会静默覆盖。
- 批量管理区提供 JSON 文本框和文件选择，两者都发送到现有 multipart `POST /api/words/import`；页面展示 `inserted`、`updated`、`unchanged`、`deleted`、`errors`。
- 列表调试器支持 `q`、`topic`、`cefr`、`page`、`page_size`，默认请求 `/api/words?page=1&page_size=40`；单词调试器请求 `/api/words/<id>`。响应原样格式化展示，便于复制排查。
- 首页入口以控制台侧栏“词光 Words”和首页快捷卡片出现；点击后打开同一站点 `/words/`，不把 Bearer token 拼接到 URL。

## 验证

- 新增测试先证明 `POST /api/words` 的未认证、格式错误、重复 ID 和成功创建行为，再实现接口。
- 页面测试断言首页出现 `/words/` 入口，词光页面包含创建、导入、单词/列表调试控件和相应接口文案。
- 运行 `python3 -m unittest discover -s tests -v` 与 `python3 -m py_compile dev_control/*.py`。
