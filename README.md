# Dev Control Server

统一后端项目，包含：

- 用户管理与 Bearer token 认证（管理员、普通用户）；
- 与 `clients/device_agent/agent.py` 匹配的设备注册、心跳、长轮询、任务结果、服务归档及文件 blob API；
- 服务器文件管理 API，文件限制在 `data/files/`，写入采用临时文件 + 原子替换；
- 当前项目内置的 drawio，地址 `/drawio/`，其“外部 API”插件已改成直接访问本后端；
- 原 `web_server` 可维护源码和模板位于 `vendor/web_server/`，drawio 完整资源位于 `vendor/drawio/webapp/`。
- 原端口工具与 FRP 工具由同一进程挂载在 `/tools/ports/`、`/tools/frp/`；首次访问附加 `?access_token=<登录token>`。

## 启动

`run.sh` 首次运行会自动创建 `.venv` 并安装 `requirements.txt`；依赖文件未变化时不会重复安装。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ADMIN_PASSWORD='请替换为强密码' python run.py
```

默认监听 `0.0.0.0:5000`。本机可用 `http://127.0.0.1:5000`，局域网其他机器使用运行日志中的 `http://<本机局域网IP>:5000`；若系统防火墙开启，请放行 TCP 5000。默认管理员为 `admin`；生产环境必须设置 `ADMIN_PASSWORD`，并建议设置：

```bash
export DEVICE_AGENT_OPEN_ENROLL=0
export DEVICE_AGENT_ENROLL_TOKEN='随机注册密钥'
export SERVER_FILES_ROOT='/srv/dev-control/files'
export DATA_DIR='/srv/dev-control/data'
export HERMES_LOCAL_ENABLED=1
# 可选：本机 Hermes CLI 的绝对路径；默认自动查找 ~/.hermes/hermes-agent/venv/bin/hermes
export HERMES_LOCAL_COMMAND='/home/yiye/.hermes/hermes-agent/venv/bin/hermes'
# 若本机 Hermes 暴露 OpenAI 兼容 HTTP 服务，再设置 HERMES_BASE_URL；网页不会直接访问此地址
export HERMES_API_KEY='可选的 Hermes API Key'
export HERMES_MODEL='hermes'
```

## 小步英语 Android 学习进度

启动更新后的服务，在控制台「用户管理」为学习账号创建访问 Token。在 APK 的 **Learning history → Progress sync** 填写设备可访问的服务地址和 Token。同一局域网可使用 `http://<服务器 IP>:5000`，公网连接请使用 HTTPS。

- `GET /api/xiaobu-words/progress` 列出当前账号的同步设备和最近同步时间。
- `GET /api/xiaobu-words/progress/<device_id>` 返回逐条进度。可用 `kind` 筛选，以 `next_after` 作为下一页的 `after`。返回 `null` 表示已到末页。也可在 `/apis/` 选择「小步英语」测试接口。
- `PUT /api/xiaobu-words/progress/<device_id>` 由 APK 调用。服务按账号、设备和记录键归档。同一批次可以重试，旧版本不会覆盖新进度。时间字段使用 Unix 毫秒。

记录类型包括 `course` 地图课程，`self_word`、`self_practice` 自学词卡和练习，`review_word`、`review_practice`、`review_session` 错题和间隔复习，以及 `town_session`、`town_mastery`、`town_round`、`town_reading` 单词小镇。服务器启动时自动创建相应 SQLite 表。APK 将记录上传到服务器，各设备的本地学习状态独立保存。

## 使用 drawio 保存到服务器

1. `POST /api/auth/login`，请求 JSON 为 `{"username":"admin","password":"..."}`，复制返回的 `token`。
2. 打开 `/drawio/`，进入 `Extras -> External API Settings`（外部 API 设置）。
3. Base URL 使用当前站点地址，API Key 填入上述 token。
4. 使用插件的 Open/New/Save 操作；文件会写入 `SERVER_FILES_ROOT`。

## 主要 API

- 用户：`/api/auth/login`、`/api/auth/users`
- 文件：`GET /api/files`、`GET|PUT /api/files/content`、`POST /api/files/directory`
- 小记：`GET|POST /api/notes`、`GET|PATCH|DELETE /api/notes/<id>`
- 小步英语：`GET /api/xiaobu-words/progress`、`GET|PUT /api/xiaobu-words/progress/<device_id>`
- OpenAPI：`GET /api/openapi.json`，页面 `/apis/` 可下载接口文件并在线测试
- 设备管理：`GET /api/devices`、`POST /api/devices/<id>/tasks`
- Agent：`/api/agent/enroll`、`register`、`heartbeat`、`poll`、`tasks/<id>/result`
- 大模型接口：`GET|POST /api/llm/providers`、`PATCH|DELETE /api/llm/providers/<id>`、`POST /api/llm/providers/<id>/check`
- Hermes 远程操作：页面 `/hermes/`（已集成到首页顶部 Tab），API `GET /api/hermes/status`、`GET /api/hermes/catalog`、`GET|POST /api/hermes/conversations`、`GET /api/hermes/conversations/<id>`、`POST /api/hermes/upload`、`POST /api/hermes/chat`。对话和上传文件会在本地持久化；默认由服务器本机 Hermes CLI 执行，只有配置 `HERMES_BASE_URL` 时才改用 OpenAI 兼容 HTTP 转发。局域网浏览器只需访问 Dev Control 的局域网地址。
- 完整大模型配置（仅管理员）：`GET /api/llm/providers/<id>/config`。响应包含 API Key，禁止公开管理员 token。
- 场景黑白名单：`GET /api/access-policies/scenarios`、`GET /api/access-policies/scenarios/<name>`、`POST /api/access-policies/evaluate`
- 文件批量删除：`POST /api/files/bulk-delete`，请求体为 `{"paths":["a.txt","folder"]}`。
- 用户统计：`GET /api/user-management/users`、`GET /api/user-management/me/stats`
- 用户访问 Token：`GET|POST /api/user-management/users/<id>/tokens`、`DELETE /api/user-management/users/<id>/tokens/<token_id>`。完整 Token 仅本人或管理员可读取。
- 服务器自更新（仅管理员）：`GET /api/updates`、`POST /api/updates/upload`、`GET /api/updates/<id>/download`、`DELETE /api/updates/<id>`、`POST /api/updates/<id>/apply`、`POST /api/updates/<id>/rollback`。

Agent 启动示例：

```bash
clients/device_agent/run.sh --server-url http://127.0.0.1:5000
```

## 打包

项目版本保存在根目录 `VERSION`。每次执行 `./package.sh` 会自动把补丁号加一（例如 `1.0.6` → `1.0.7`），并写入 `VERSION` 与安装包文件名。也可用 `VERSION=1.2.0 ./package.sh` 指定版本。自更新页面会从安装包文件名读取版本号（如 `simple-server-1.2.3.tar.gz`），并在应用后读取包内的新 `VERSION`。

管理员可在控制台「服务器更新」下载原始更新包或删除历史版本。删除时同时清理暂存文件和回滚备份，当前运行代码会保留。侧栏「GitHub 仓库」可直接打开项目源码。

端口扫描会优先使用系统 `nmap`；服务器未安装 `nmap` 时自动使用内置 TCP 扫描器，无需额外安装。打字练习词库包含中文释义，答对后使用浏览器语音合成播报英文单词。

```bash
./package.sh
# 或指定版本和输出文件
VERSION=1.2.0 ./package.sh /tmp/simple-server-1.2.0.tar.gz
```

安装包排除运行数据、虚拟环境、Git 元数据、缓存和 secret 文件；解压后直接执行 `./run.sh`。

## 测试

```bash
python -m unittest discover -s tests -v
```

## Android 桌面管理

首页新增「Android 桌面」Tab，独立页面为 `/android/`，沿用控制台登录。普通用户只管理自己的配对设备，管理员可管理全部设备。已有 Linux Agent 的开放注册保留，但不能重置 Android 设备身份；Android 设备不能使用通用命令接口执行 shell。

在网页点击「配对新设备」，然后在托管桌面的管理设置填写服务器地址和一次性配对码。配对码为 12 位十六进制、10 分钟有效；服务器仅保存哈希并原子消费。设备使用独立 `X-Device-Token`，用户界面使用 Bearer token。请使用设备能访问的局域网地址；支持 HTTP 开发连接及使用系统证书校验的 HTTPS。设备断开管理后重新配对会使用新的随机身份，旧记录的历史仍留在所属用户下。

- 应用列表独立设置「允许使用」和「桌面可见」，保存时替换规则并递增版本；未配置应用默认允许且可见。恢复入口、系统关键应用、安装器和设备报告的受保护应用不能隐藏或停用。跨通知/最近任务入口的限制依赖本地无障碍权限，网页如实显示授权状态；不声称 OS 级冻结。
- 原始字节上传 `POST /api/android/devices/<id>/files?filename=<name>&open=1&install=0`，最多 512 MiB。使用随机 blob、SHA256、同设备鉴权，下载期限为 30 天；`install=1` 仅适用于 APK。安装结果区分处理中、等待本地确认、实际安装成功及失败。文件打开仅表示已请求兼容查看器处理，不证明文档被阅读；`.doc` 需要本地 Office 查看器。
- 用量页面按设备 IANA 时区选择日期，展示应用总时长和每小时明细；夏令时日可能有 23 或 25 小时。数值是前台交互毫秒，不含后台服务时长。UTC 小时汇总按小时起点的本地日期归属；非整小时偏移时区的日期边界精度为一个统计桶。
- Agent 每分钟上报绝对用量快照：`window_start` 对齐 UTC 整点，窗口至多 72 小时，最多 10000 行，单应用单小时 `duration_ms` 为 0..3600000。窗口内原子替换，重发不会叠加，窗口外历史保留。未获权限时保留已有历史并返回 `permission_granted=false`，首次未同步为 `null`。应用清单最多 2000 项。
- `GET /api/android/agent/recover-tasks` 只恢复本设备已经领取、尚未完成的四类 Android 任务。`waiting_user` 不是完成状态；后续使用原领取令牌报告 `done`/`error`。终态结果不可回退，相同终态重试是幂等的。

接口与认证方式可从 `/apis/` 下载 OpenAPI。新增 SQLite 表由 `create_app` 初始化时自动创建，不重置原有数据、用户或密码。升级生效需按原部署流程重启服务；开发验证应使用独立临时 `DATA_DIR`，不要指向生产数据。

### 每周课程与完成记录

控制台侧栏「课程日程」（`/android/schedule/`）选择已配对 Android 设备后，可添加每周规则、按日期查看任务、处理全部逾期任务，并手动完成、重新打开或取消单次任务。侧栏继续提供原有页面；重复的顶部功能栏已移除。

课程使用设备上报的 IANA 时区，支持周一至周日与可选学期起止日期，不支持跨夜。服务器生成接下来 56 天及所有历史未完成任务。首次添加包含正在上课的任务，跳过已经结束的课；重叠课程分别保留。修改和删除规则只调整尚未开始、未完成的未来任务；已开始、已完成与已取消的快照保留，避免历史被重写。DST 不存在的当地时间跳过，重复时间采用较早时刻。逾期任务不会自动完成。

管理 API 位于 `/api/android/devices/<id>/schedule`：GET 规则与同步状态，POST `/rules` 添加，PUT/DELETE `/rules/<rule_id>` 携带 `expected_version`，GET `/occurrences?date=YYYY-MM-DD` 或 `?overdue=1`，PUT `/occurrences/<id>` 设置 `pending/completed/cancelled`。任务列表返回 `total_count` 和 `next_after`，可用 `after_id`、`limit=1..200` 继续加载。所有管理请求沿用原有用户认证和设备归属检查。

平板 API 为 GET `/api/android/agent/schedule`、POST `/ack` 与 POST `/completions`，沿用 `X-Device-Token`。每页至多 200 项，ID 严格递增；首次无缓存省略 `known_revision`，已有缓存则传缓存版本，后续分页必须传第一页的版本与 `next_after`。跨页修改返回 409，客户端保留原有完整缓存并重新同步。超出 10000 项或 16 MiB 时返回明确的 413。只有完整快照持久化后才可 `/ack`；页面根据 `ack_revision` 是否等于当前 `revision` 显示「已同步」或「等待设备同步」。所有日程 `_ms` 字段为 Unix 毫秒，`ack_at` 仍为 Unix 秒。

完成操作携带规范 UUID `operation_id`、`occurrence_id`、`expected_version` 和布尔值 `completed`。操作按设备与 UUID 去重，同一个 UUID 不可换载荷，重试不重复修改；旧版本或取消任务返回 409 与当前快照，未知/其他设备 ID 返回 404。设备可完成或撤销完成，单次取消由管理端操作。规则、任务、确认及幂等记录存于原 SQLite 中新增的 `android_schedule_*` 表，不修改账号、设备令牌或原策略。每台设备最多 128 条有效规则。

仅 `weekly_schedule: true` 的客户端会收到合并的 `android.schedule.sync` 提醒；旧客户端的策略、文件、使用统计继续工作。服务重启使用原 `DATA_DIR` 即可建表，无须重配对或重置账号。测试：`.venv/bin/python -m unittest discover -s tests -v`，只创建临时测试数据库。


单次任务额外返回 `timezone`（生成时的 IANA 时区），用于解释其 `local_date` 与开始/结束时间。已经开始、完成、取消的历史快照保留原时区，未开始的普通待办随当前设备时区重新生成并递增版本；快照外层 `timezone` 仍表示设备当前时区。管理界面按任务自己的时区显示时间及来源。

服务器单次「重新打开」会保存独立覆盖，普通同步不会因为原每周规则已删除或不再适用而再次取消它。后续显式修改/删除每周规则，会覆盖该规则下未来未开始的待办；已经开始或完成的任务继续保留。设备撤销完成时，如果未来任务不再适用且没有服务器的单次覆盖，返回 409 与当前已完成任务，避免先成功再被下一次同步取消。

从早期不含任务时区/覆盖字段的日程表启动时采用加列迁移，旧任务以当前设备时区作为可用来源补齐，并提升任务版本和日程版本以刷新缓存。早期表没有保留原时区，无法还原当时已经发生的设备时区变化；新生成任务完整保留来源。迁移不修改账号、密码或设备令牌。

## Word Atlas 英语单词学习

访问 `/words/` 打开英语单词学习界面，也可以直接使用 `#/atlas?w=library` 打开 `library` 词条。页面提供英文/中文搜索、CEFR 与主题筛选、atlas/classic 画风、英式/美式单词音频，以及使用浏览器 `SpeechSynthesis` 播报的例句。

Word Atlas 接口的读取不要求登录：

- `GET /api/words?q=&topic=&cefr=&page=1&page_size=40`：分页搜索，`page_size` 最大 100；
- `GET /api/words/<word_id>`：完整词条、释义、例句、搭配、词族及 `assets.images`/`assets.audio`；
- `POST /api/words/batch`：请求体为 `{"ids":[...],"words":[...]}`，最多 2000 项，未知项在 `missing` 中返回。

首页控制台侧栏的「词光 Words」会打开 `/words/`。页面下方的管理区使用当前控制台登录令牌，支持按词光结构新建单词、上传 JSON/JSONL 批量导入，并可直接调试单个词条和分页列表接口。单词 JSON 沿用词光字段，例如：

```json
{"id":"library","w":"library","uk":"/ˈlaɪbrəri/","us":"/ˈlaɪbreri/","senses":[{"pos":"n.","zh":"图书馆","en":"A place for books."}],"ex":[{"en":"I went to the library.","zh":"我去了图书馆。"}],"col":[],"fam":[],"syn":[],"ant":[],"m":0}
```

单个新建、批量写入和来源同步只允许管理员：

- `POST /api/words`：请求体为单个词光 JSON；新 ID 返回 201，重复 ID 返回 409，不覆盖已有词条；
- `POST /api/words/import`：multipart 的 `file` 支持 JSON 数组或 JSONL；`?replace=true` 仅在完整上传成功后删除缺失词条；
- `POST /api/words/sync`：从 `https://0xlei.tech/words/atlas/` 完整抓取索引和 entries 分块，校验成功后原子更新；可传 `{"dry_run":true}` 只检查不写入。
- `POST /api/words/assets/sync`：管理员将词条的 atlas/classic 图片及 UK/US 音频下载到服务器 `DATA_DIR/word_assets`；请求体可传 `{"ids":["library"]}` 只同步指定词条，省略 `ids` 同步全部词条，已存在文件会跳过。同步完成后详情接口和 `/words/` 页面优先使用 `/words/assets/...` 本地资源，缺失资源继续使用词光源站地址。

词条、插画和音频来源于 [0xLei Word Atlas](https://0xlei.tech/words/#/atlas)，授权为 CC BY 4.0；本服务保存词条数据和资源地址，不复制目标站点的图片或音频二进制文件。

线上更新可使用 `scripts/deploy_word_atlas.sh /tmp/simple-server-word-atlas.tar.gz`。目标地址为 HTTP 时，脚本默认拒绝明文发送 Bearer token；只有确认网络链路受信后才设置 `ALLOW_INSECURE_DEPLOY=1`。更新完成后由管理员调用 `/api/words/sync`，不要把令牌写入脚本或归档。

### Android 应用市场与前台守护

应用市场默认不可使用、桌面不可见；必须由设备所有者在“Android 管理 → 应用规则”中明确放行。普通未指定应用仍保持原先默认。
每台设备的 `foreground_kill_enabled` 默认为 `true`，可在应用规则上方切换后保存。关闭该开关仅停止强停机制，并不自动放行市场。显式允许应用会豁免该应用的前台强停。
守护针对未放行的荣耀/华为应用市场及官方 B站HD。家长同签名占位设置页豁免。平板通过已授权的受限 ADB 服务执行真实强停，无授权时仍保留无障碍拦截；重启设备后需重新启动授权服务。
设备私有保存服务器地址、配对信息与单独的无凭据表单草稿；主动解除配对保留地址/草稿，但撤销令牌和服务器策略。更换配对需要本机锁屏身份验证。
