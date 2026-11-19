# Word Atlas 英语单词学习模块设计

## 意图

在现有 Python/Flask 服务中增加一个独立的 Word Atlas 学习模块，让用户可以搜索和学习目标站点 `https://0xlei.tech/words/#/atlas` 的全部 7298 个英语单词。词条页面参考 `library` 的信息组织：单词、英美音标、中文释义、英文释义、例句、词族、搭配、近义词、反义词、学习提示和插画。单词发音优先播放目标站点提供的英式/美式 MP3；例句沿用目标站点的浏览器 `SpeechSynthesis` 逻辑，按英式/美式选择 `en-GB`/`en-US` 声音。

## 目标

- 新增 `/words/` 学习界面，支持单词搜索、CEFR/主题筛选、列表分页、词条详情、atlas/classic 画风切换、英美音切换和例句 TTS。
- 提供单词列表、单个单词详情和批量单词详情接口。
- 用 SQLite 保存来源索引和完整词条 JSON，支持重复同步和增量 upsert，不破坏现有控制台数据。
- 提供管理员 JSON/JSONL 批量上传更新接口和管理员从目标站点同步接口。
- 支持把更新打包，通过现有 `/api/updates/upload` 与 `/api/updates/<id>/apply` 流程部署到线上服务器；部署后在服务器上执行同步填充数据。
- 在界面和文档中保留 Word Atlas 的 CC BY 4.0 来源说明。

## 非目标与反目标

- 不重写现有 Ogden `/learn_en/` 页面，也不把 7298 个词硬编码进 HTML。
- 不把所有图片和 MP3 二进制复制进 SQLite 或发布包；数据库保存来源地址，浏览器按需读取。
- 不实现账户级的间隔重复学习算法、学习进度跨设备同步或后台音频转码。
- 不依赖目标站点页面的 React bundle 作为本地运行时；只使用其公开数据和资源约定。

## 约束

- 现有项目使用 Flask、SQLite 和原生静态前端，无前端构建链；新增代码应保持这一结构。
- 目标数据由 `/words/atlas/index.json` 和分块 `/words/atlas/entries/<n>.json` 提供，索引声明 7298 个词、每块 100 个；插画按 `img/<id>.webp`、`classic/img/<id>.webp`，发音按 `audio/uk/<id>.mp3`、`audio/us/<id>.mp3`。
- 例句没有独立录音时，浏览器使用 Web Speech API；浏览器不支持时显示明确提示。
- 线上更新只能通过已有自更新接口或等价的管理员部署流程完成；更新前先完成本地测试和打包。

## 实现方案与取舍

### 方案 A：独立 Word Atlas 模块（采用）

新增 `dev_control/words.py`、SQLite 词条表、`words/` 静态页面和管理员同步/导入接口。词条内容以结构化列加 JSON 数组保存，图片和音频 URL 根据词条 ID 生成并在 API 返回。这样可以分页和批量查询，更新不需要重建页面，线上只需同步数据库内容。

### 方案 B：静态前端内嵌完整词库

把所有词条打包到静态 JavaScript 中。开发量较小，但文件体积大，后续同步要重新生成并发布代码，无法自然支持数据库更新和 API 查询，因此不采用。

### 方案 C：实时代理目标站点

每次请求都从目标站点读取。部署简单，但受外网和对方变更影响，无法稳定离线使用、无法保留同步结果，也不适合批量导入，因此不采用。

## 数据模型

新增 `word_entries` 表：

- `id`：来源词条 ID，主键；
- `word`、`zh`、`uk_ipa`、`us_ipa`、`cefr`、`topic`、`media`：用于搜索和筛选的标量字段；
- `senses_json`、`examples_json`、`collocations_json`、`family_json`、`synonyms_json`、`antonyms_json`：保持目标数据结构的 JSON 数组；
- `tip`、`source_url`、`source_updated_at`、`updated_at`：来源和同步元数据。

在初始化数据库时使用 `CREATE TABLE IF NOT EXISTS`，不改变既有表和数据。列表查询按 `word`、`cefr`、`topic` 过滤，返回分页结果；详情查询返回完整结构和派生资源 URL：`images.atlas`、`images.classic`、`audio.uk`、`audio.us`。

## API

公开读取接口：

- `GET /api/words?q=&topic=&cefr=&page=1&page_size=40`：分页搜索和筛选；
- `GET /api/words/<word_id>`：单个完整词条；
- `POST /api/words/batch`：请求体 `{"ids":[...],"words":[...]}`，最多 2000 个 ID/单词，返回按输入顺序的词条。

管理员写入接口：

- `POST /api/words/import`：multipart 字段 `file`（JSON 数组或 JSONL），默认 upsert；支持 `replace=true` 清理本次数据集中缺失的词条；返回新增、更新、跳过和错误数量；
- `POST /api/words/sync`：从配置的目标站点索引和 entries 分块抓取并 upsert，返回索引版本、总词数和新增/更新数量；支持 `dry_run=true` 只校验不写库。

同步器使用 Python 标准库 HTTP 客户端、超时和有限并发；每块先校验 ID/字段，再在单次数据库事务中写入。来源地址和资源 URL 可配置，但默认使用目标站点固定地址。

## 学习界面

`/words/` 使用原生 HTML/CSS/JavaScript，保持项目无需前端构建的部署方式。布局包括：顶部搜索、CEFR/主题筛选、词条列表、词条详情抽屉、画风切换、英美音切换和每条例句的 TTS 按钮。详情 URL 支持 `#/atlas?w=library` 形式，便于直接打开指定词条。图片和音频使用 API 返回的 URL；例句按钮调用 `SpeechSynthesisUtterance`，优先匹配英式或美式英语声音。

## 验证

新增单元测试覆盖：数据库建表、单词详情、分页搜索、批量查询、JSON/JSONL upsert、非法数据拒绝和同步器的分块合并（使用本地测试响应，不依赖公网）。现有完整 `python -m unittest discover -s tests -v` 继续通过。部署前用测试客户端验证 API，再用浏览器或 `curl` 验证 `/words/`、单词详情和资源链接。

## 部署

本地完成测试后，用现有 `package.sh` 生成版本归档，使用线上管理员 Bearer token 上传至 `http://8.138.86.215:8080/api/updates/upload`，调用 apply 并等待服务重启。服务恢复后调用线上 `/api/words/sync`，确认词数约 7298、`library` 详情、英美音频 URL 和画风 URL 均可用。若当前会话没有线上管理员凭据，则保留可审阅的归档和精确部署命令，不能猜测或尝试密码。

## 已落定的构建决策

- 解析器以当前目标站点公开 schema 为基线，同时在 `raw_json` 中保留未映射字段；缺少必需的 ID/单词字段或类型错误会拒绝该批次。
- 同步先抓取并校验索引和全部分块，校验总数、chunk 覆盖范围、ID 唯一性及 entry 与 index 的对应关系；全部成功后才开启一次数据库事务。单块请求使用有限并发和最多两次重试，任何失败都保留旧库并返回错误。
- `replace=true` 只在一次完整、成功且无重复 ID 的批次上生效，作用域为默认 source 的整个 `word_entries`；重复 ID 直接报错，不静默选取某一条。
- 列表接口默认 `page=1`、`page_size=40`，最大 100，按词典序稳定排序；`q` 对英文单词和中文摘要做大小写不敏感的包含匹配。响应固定包含 `items`、`total`、`page`、`page_size`、`pages`；批量查询按输入首次出现顺序返回，重复去重，未知 ID 放入 `missing`。
- 同步在一个 HTTP 请求中完成，服务端设置合理的总超时并使用当前 Flask 的 threaded 配置；不引入后台任务表。线上同步前端显示处理中，超时或失败时保留旧数据。
- API 默认返回目标站点绝对资源 URL，资源基础地址由应用配置覆盖；本次不做本地资源代理或镜像，浏览器加载失败时前端显示占位图/音频错误提示。
- `updated_at` 是本地同步时间，`source_updated_at` 保存索引版本或来源时间字符串；使用规范化 JSON 的 SHA-256 内容哈希判断 unchanged，重复同步不会虚增更新计数。
