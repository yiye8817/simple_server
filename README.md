# Dev Control Server

统一后端项目，包含：

- 用户管理与 Bearer token 认证（管理员、普通用户）；
- 与 `clients/device_agent/agent.py` 匹配的设备注册、心跳、长轮询、任务结果、服务归档及文件 blob API；
- 服务器文件管理 API，文件限制在 `data/files/`，写入采用临时文件 + 原子替换；
- 当前项目内置的 drawio，地址 `/drawio/`，其“外部 API”插件已改成直接访问本后端；
- 原 `web_server` 可维护源码和模板位于 `vendor/web_server/`，drawio 完整资源位于 `vendor/drawio/webapp/`。
- 原端口工具与 FRP 工具由同一进程挂载在 `/tools/ports/`、`/tools/frp/`；首次访问附加 `?access_token=<登录token>`。

## 启动
n`run.sh` 首次运行会自动创建 `.venv` 并安装 `requirements.txt`；依赖文件未变化时不会重复安装。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ADMIN_PASSWORD='请替换为强密码' python run.py
```

默认地址 `http://127.0.0.1:5000`，默认管理员为 `admin`。生产环境必须设置 `ADMIN_PASSWORD`，并建议设置：

```bash
export DEVICE_AGENT_OPEN_ENROLL=0
export DEVICE_AGENT_ENROLL_TOKEN='随机注册密钥'
export SERVER_FILES_ROOT='/srv/dev-control/files'
export DATA_DIR='/srv/dev-control/data'
```

## 使用 drawio 保存到服务器

1. `POST /api/auth/login`，请求 JSON 为 `{"username":"admin","password":"..."}`，复制返回的 `token`。
2. 打开 `/drawio/`，进入 `Extras -> External API Settings`（外部 API 设置）。
3. Base URL 使用当前站点地址，API Key 填入上述 token。
4. 使用插件的 Open/New/Save 操作；文件会写入 `SERVER_FILES_ROOT`。

## 主要 API

- 用户：`/api/auth/login`、`/api/auth/users`
- 文件：`GET /api/files`、`GET|PUT /api/files/content`、`POST /api/files/directory`
- 设备管理：`GET /api/devices`、`POST /api/devices/<id>/tasks`
- Agent：`/api/agent/enroll`、`register`、`heartbeat`、`poll`、`tasks/<id>/result`
- 大模型接口：`GET|POST /api/llm/providers`、`PATCH|DELETE /api/llm/providers/<id>`、`POST /api/llm/providers/<id>/check`
- 完整大模型配置（仅管理员）：`GET /api/llm/providers/<id>/config`。响应包含 API Key，禁止公开管理员 token。
- 场景黑白名单：`GET /api/access-policies/scenarios`、`GET /api/access-policies/scenarios/<name>`、`POST /api/access-policies/evaluate`
- 文件批量删除：`POST /api/files/bulk-delete`，请求体为 `{"paths":["a.txt","folder"]}`。
- 用户统计：`GET /api/user-management/users`、`GET /api/user-management/me/stats`
- 用户访问 Token：`GET|POST /api/user-management/users/<id>/tokens`、`DELETE /api/user-management/users/<id>/tokens/<token_id>`。完整 Token 仅本人或管理员可读取。
- 服务器自更新：`GET /api/updates`、`POST /api/updates/upload`、`POST /api/updates/<id>/apply`、`POST /api/updates/<id>/rollback`。仅管理员可用。

Agent 启动示例：

```bash
clients/device_agent/run.sh --server-url http://127.0.0.1:5000
```

## 打包
n项目版本保存在根目录 `VERSION`。自更新页面上传安装包时可指定新版本号，并在应用后读取包内的新 `VERSION`。

```bash
./package.sh
# 或指定输出文件
./package.sh /tmp/simple-server.tar.gz
```

安装包排除运行数据、虚拟环境、Git 元数据、缓存和 secret 文件；解压后直接执行 `./run.sh`。

## 测试

```bash
python -m unittest discover -s tests -v
```

