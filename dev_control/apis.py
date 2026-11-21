from pathlib import Path
from flask import Blueprint, jsonify, render_template, request
from .security import require_user

bp=Blueprint("apis",__name__)

def op(summary, *, body=None, params=None, auth=True, desc=""):
    item={"summary":summary,"responses":{"200":{"description":"成功"},"400":{"description":"请求无效"},"401":{"description":"未认证"},"404":{"description":"不存在"}}}
    if desc: item["description"]=desc
    if params: item["parameters"]=params
    if body is not None: item["requestBody"]={"required":True,"content":{"application/json":{"schema":{"type":"object"},"example":body}}}
    item["security"]=[{"bearerAuth":[]}] if auth else []
    return item

def q(name, desc="", required=False, typ="string"):
    return {"name":name,"in":"query","required":required,"schema":{"type":typ},"description":desc}

def p(name, typ="string"):
    return {"name":name,"in":"path","required":True,"schema":{"type":typ}}

def spec():
    version=(Path(__file__).resolve().parent.parent/"VERSION").read_text(encoding="utf-8").strip() or "1.0.0"
    paths={
        "/api/health":{"get":op("健康检查",auth=False)},
        "/api/openapi.json":{"get":op("下载 OpenAPI 接口文件",params=[q("download","设为 1 时作为附件下载")])},
        "/api/auth/login":{"post":op("登录",body={"username":"admin","password":"your-password"},auth=False)},
        "/api/auth/logout":{"post":op("注销当前会话")},
        "/api/auth/users":{"get":op("列出用户（管理员）"),"post":op("创建用户（管理员）",body={"username":"alice","password":"alice-pass","role":"user"})},
        "/api/auth/users/{user_id}":{"patch":op("更新用户（管理员）",body={"active":True,"role":"user","password":"new-password"},params=[p("user_id","integer")])},
        "/api/files":{"get":op("列出目录",params=[q("path","相对路径，空为根目录")]),"delete":op("删除文件或目录",params=[q("path","要删除的路径",True)])},
        "/api/files/content":{"get":op("读取或下载文件",params=[q("path","文件路径",True),q("download","1 表示附件下载")]),"put":op("上传或覆盖文件",params=[q("path","目标路径",True)],desc="请求体为原始文件字节")},
        "/api/files/directory":{"post":op("创建目录",body={"path":"folder/sub","parents":True})},
        "/api/files/bulk-delete":{"post":op("批量删除",body={"paths":["a.txt","folder"]})},
        "/api/notes":{"get":op("列出当前用户小记"),"post":op("新建小记",body={"title":"备忘","content":"内容"})},
        "/api/notes/{id}":{"get":op("读取小记",params=[p("id","integer")]),"patch":op("编辑小记",body={"title":"新标题","content":"新内容"},params=[p("id","integer")]),"delete":op("删除小记",params=[p("id","integer")])},
        "/api/user-management/users":{"get":op("用户统计列表")},
        "/api/user-management/me/stats":{"get":op("当前用户统计")},
        "/api/user-management/users/{user_id}/tokens":{"get":op("列出访问 Token",params=[p("user_id","integer")]),"post":op("创建访问 Token",body={"label":"API Token"},params=[p("user_id","integer")])},
        "/api/user-management/users/{user_id}/tokens/{token_id}":{"delete":op("撤销访问 Token",params=[p("user_id","integer"),p("token_id","integer")])},
        "/api/llm/providers":{"get":op("列出大模型接口"),"post":op("添加大模型接口",body={"name":"OpenAI","base_uri":"https://api.openai.com/v1","api_style":"openai","api_key":"sk-..."})},
        "/api/llm/providers/{pid}":{"patch":op("更新大模型接口",body={"enabled":True},params=[p("pid","integer")]),"delete":op("删除大模型接口",params=[p("pid","integer")])},
        "/api/llm/providers/{pid}/check":{"post":op("检测大模型接口",params=[p("pid","integer")])},
        "/api/llm/providers/{pid}/config":{"get":op("读取完整配置（管理员）",params=[p("pid","integer")])},
        "/api/hermes/status":{"get":op("Hermes 后端状态")},
        "/api/hermes/catalog":{"get":op("Hermes Skill 与常用功能")},
        "/api/hermes/conversations":{"get":op("列出 Hermes 对话",params=[q("q","按标题或消息内容搜索")]),"post":op("新建 Hermes 对话",body={"title":"故障排查"})},
        "/api/hermes/conversations/{conversation_id}":{"get":op("读取 Hermes 对话内容",params=[p("conversation_id","integer")])},
        "/api/hermes/upload":{"post":op("上传文件给 Hermes",desc="multipart/form-data，字段 files 可重复")},
        "/api/hermes/chat":{"post":op("发送 Hermes 消息",body={"conversation_id":1,"message":"检查在线设备","attachments":[]})},
        "/api/access-policies/scenarios":{"get":op("列出场景名单"),"post":op("新建场景",body={"name":"deploy","description":"部署目标","whitelist":["prod-a"],"blacklist":[]})},
        "/api/access-policies/scenarios/{name}":{"get":op("按名称读取场景",params=[p("name")])},
        "/api/access-policies/scenarios/{sid}":{"patch":op("更新场景",body={"enabled":True},params=[p("sid","integer")]),"delete":op("删除场景",params=[p("sid","integer")])},
        "/api/access-policies/evaluate":{"post":op("评估名单",body={"scenario":"deploy","value":"prod-a"})},
        "/api/devices":{"get":op("列出设备")},
        "/api/devices/{device_id}/tasks":{"get":op("设备任务",params=[p("device_id","integer")]),"post":op("下发任务",body={"kind":"exec","payload":{"command":"uname -a","timeout":60,"shell":True}},params=[p("device_id","integer")])},
        "/api/agent/enroll":{"post":op("设备注册",body={"device_uid":"dev-1","hostname":"box"},auth=False)},
        "/api/agent/install-info":{"get":op("Agent 安装信息",auth=False)},
        "/api/updates":{"get":op("更新历史（管理员）")},
        "/api/updates/{id}/download":{"get":op("下载更新包（管理员）",params=[p("id","integer")])},
        "/api/updates/{id}":{"delete":op("删除更新版本（管理员）",params=[p("id","integer")],
                                          desc="删除更新包、暂存文件和回滚备份，保留当前运行代码。")},
        "/api/typing/word":{"get":op("获取练习单词",desc="返回英文单词、中文释义和音标")},
        "/api/typing/history":{"get":op("打字历史")},
        "/api/typing/results":{"post":op("提交打字结果",body={"session_id":"s1","word":"hello","typed":"hello","correct":True,"timed_out":False,"elapsed_ms":800})},
        "/api/xiaobu-words/progress":{"get":op("列出当前账号的小步英语同步设备")},
        "/api/xiaobu-words/progress/{device_id}":{
            "get":op("读取设备的详细学习进度",params=[p("device_id"),q("kind","记录类型"),q("after","上一页返回的 next_after",typ="integer"),q("limit","每页记录数，最大 50",typ="integer")]),
            "put":op("同步设备的学习记录",params=[p("device_id")],body={"schema_version":1,"device_name":"Android Pad","revision":1,"records":[{"kind":"course","key":"body-front","data":{"last_activity":0,"puzzle":{"phase":"BUILD","solved":["head"]}}}]}),
        },
    }
    paths.update({
        "/api/words": {
            "get": op("搜索 Word Atlas 单词", auth=False, params=[q("q", "英文或中文包含搜索"), q("topic", "主题"), q("cefr", "CEFR 级别"), q("page", "页码", typ="integer"), q("page_size", "每页数量，最大100", typ="integer")]),
            "post": op("新建 Word Atlas 单词（管理员）", body={"id": "example", "w": "example", "uk": "/ɪɡˈzɑːmpəl/", "us": "/ɪɡˈzæmpəl/", "senses": [{"pos": "n.", "zh": "例子", "en": "A representative case."}], "ex": [{"en": "This is an example.", "zh": "这是一个例子。"}]})
        },
        "/api/words/{word_id}": {"get": op("获取单个 Word Atlas 词条", auth=False, params=[p("word_id")])},
        "/api/words/batch": {
            "get": op("按逗号分隔单词批量获取 Word Atlas 词条", auth=False, params=[q("words", "逗号或换行分隔的单词，例如 library,abandon", True), q("ids", "逗号或换行分隔的词条 ID")]),
            "post": op("批量获取 Word Atlas 词条", auth=False, body={"ids": ["library"], "words": ["spring"]})
        },
        "/api/words/import": {"post": op("管理员批量导入 Word Atlas 词条", params=[q("replace", "1 表示完整替换当前来源词库")], desc="multipart/form-data，字段 file 为 JSON 数组或 JSONL；可用 replace=true 替换完整词库")},
        "/api/words/sync": {"post": op("管理员同步 Word Atlas 来源词库", body={"source_base": "https://0xlei.tech/words/atlas/", "dry_run": False}, desc="完整抓取并校验目标站点索引及 entries 分块后原子更新")},
        "/api/words/assets/sync": {"post": op("管理员同步 Word Atlas 图片和音频到本地", body={"ids": ["library"], "dry_run": False}, desc="将词条的 atlas/classic 图片及 UK/US 音频下载到服务器 DATA_DIR/word_assets；省略 ids 时处理全部词条，已存在文件跳过")},
    })
    paths["/api/words/import"]["post"]["requestBody"] = {"required": True, "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}
    paths["/api/words"]["post"]["responses"]["201"] = {"description": "词条已创建"}
    paths["/api/words"]["post"]["responses"]["409"] = {"description": "词条 ID 已存在"}
    paths["/api/words/sync"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {"type": "object", "properties": {"source_base": {"type": "string", "format": "uri"}, "dry_run": {"type": "boolean"}}}
    update_download = paths["/api/updates/{id}/download"]["get"]
    update_download["responses"]["200"] = {"description": "更新包附件", "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}}
    update_download["responses"]["403"] = {"description": "需要管理员权限"}
    paths["/api/updates/{id}"]["delete"]["responses"]["403"] = {"description": "需要管理员权限"}
    android_device = [p("device_id", "integer")]
    android_file = [*android_device, p("file_id", "integer")]
    paths.update({
        "/api/android/pairing-codes": {"post": op("生成 10 分钟一次性配对码")},
        "/api/android/pair": {"post": op("Android 配对", auth=False, body={"code": "012345abcdef", "device_uid": "random-installation-uuid", "name": "Tablet", "capabilities": {}})},
        "/api/android/devices": {"get": op("当前用户的 Android 设备（管理员可查看全部）")},
        "/api/android/devices/{device_id}": {"get": op("Android 设备、策略和应用清单", params=android_device)},
        "/api/android/devices/{device_id}/policy": {"put": op("替换远程规则并递增版本", params=android_device, body={"apps": {"org.example.reader": {"allowed": False, "visible": True}}})},
        "/api/android/devices/{device_id}/usage": {"get": op("设备当地日期的每小时使用时长", params=[*android_device, q("date", "YYYY-MM-DD，设备 IANA 时区", True)], desc="时长为整数毫秒，bucket_start 为 UTC Unix 毫秒。缺少授权时保留历史，并返回 permission_granted=false；首次未同步为 null。")},
        "/api/android/devices/{device_id}/files": {"get": op("已发送文件及实际任务状态", params=android_device), "post": op("上传文件 / 安装 APK", params=[*android_device, q("filename", "原始文件名（不含路径）", True), q("open", "0/1，接收后打开"), q("install", "仅 APK 可设为 1")], desc="原始字节流，至多 512 MiB；SHA256 校验，下载保留 30 天。返回 201。")},
        "/api/android/devices/{device_id}/files/{file_id}/open": {"post": op("再次请求在设备打开已发文件", params=android_file)},
        "/api/android/agent/state": {"get": op("读取配对设备策略")},
        "/api/android/agent/inventory": {"post": op("原子更新应用及权限清单", body={"name": "Tablet", "timezone": "Asia/Shanghai", "capabilities": {"usage_access": True, "accessibility_enabled": True, "install_permission": False}, "apps": []}, desc="应用清单至多 2000 项；所有 Android agent API 仅接受已配对 Android 设备。")},
        "/api/android/agent/usage": {"post": op("绝对每小时用量快照", body={"timezone": "Asia/Shanghai", "permission_granted": True, "generated_at": 1792890000000, "window_start": 1792717200000, "window_end": 1792890000000, "buckets": []}, desc="所有时间字段为 Unix 毫秒，duration_ms 为 0..3600000 整数。窗口起点 UTC 整点、至多 72 小时、至多 10000 行，原子替换窗口；缺少授权不删除历史。")},
        "/api/android/agent/recover-tasks": {"get": op("恢复本设备已领取未完成的 Android 任务")},
        "/api/agent/register": {"post": op("更新设备在线状态", body={"name": "Tablet"})},
        "/api/agent/heartbeat": {"post": op("设备心跳", body={})},
        "/api/agent/poll": {"get": op("领取待执行任务", params=[q("wait", "长轮询秒数，0..25")])},
        "/api/agent/tasks/{task_id}/result": {"post": op("上报任务结果", params=[p("task_id", "integer")], body={"claim_token": "task-claim", "status": "waiting_user", "result": {"state": "pending_user_action", "message": "请在平板确认"}}, desc="Android 状态仅 done/error/waiting_user；waiting_user 非终态，允许后续同一 claim_token 上报 done/error。")},
        "/api/agent/fs-blobs/{token}": {"get": op("下载本设备 blob", params=[p("token")])},
    })
    agenda = "/api/android/devices/{device_id}/schedule"
    rule_body = {"title": "课程名称", "notes": "", "weekdays": [1, 3], "start_minute": 540, "end_minute": 600, "valid_from": None, "valid_until": None}
    paths.update({
        agenda: {"get": op("课程规则、当前版本与实际设备确认", params=android_device, desc="返回 revision/ack_revision/ack_at/timezone/through_date/rules；ack_at 为 Unix 秒，仅设备完成持久化后确认。")},
        agenda+"/rules": {"post": op("创建每周课程（相同有效内容自动去重）", params=android_device, body=rule_body, desc="每台设备至多128条有效规则；weekdays须为排序不重复的1..7；时间为当地分钟，不支持跨夜；日期YYYY-MM-DD，起始默认设备今天。")},
        agenda+"/rules/{rule_id}": {
            "put": op("修改尚未开始的课程并保留历史", params=[*android_device,p("rule_id","integer")], body={**rule_body,"expected_version":1}),
            "delete": op("停止未来课程并保留已开始与已完成任务", params=[*android_device,p("rule_id","integer")], body={"expected_version":1})},
        agenda+"/occurrences": {"get": op("日期任务或全部逾期未完成任务", params=[*android_device,q("date","YYYY-MM-DD；与overdue=1二选一"),q("overdue","1表示逾期"),q("after_id","上一页next_after"),q("limit","1..200，默认200")], desc="返回 occurrences/total_count/next_after/revision；按开始或逾期时间和ID排序，显式分页。")},
        agenda+"/occurrences/{occurrence_id}": {"put": op("手动完成、重新打开或取消单次课程", params=[*android_device,p("occurrence_id","integer")], body={"status":"completed","expected_version":0}, desc="status为pending/completed/cancelled；显式pending持久保留单次重新打开，后续每周编辑只覆盖未来未开始的pending；版本冲突409返回当前occurrence。")},
        "/api/android/agent/schedule": {"get": op("滚动56日与全部历史未完成任务的版本化快照", params=[q("after_id","首0，后续为next_after"),q("known_revision","无缓存首请求省略；后续页须传第一页revision")], desc="每页200项严格ID升序。返回revision/timezone/week_start/through_date/generated_ms/total_count/unchanged/occurrences/next_after；每项occurrence额外包含其生成时的IANA timezone，历史保留原时区；快照timezone仍为设备当前时区。所有*_ms为Unix毫秒。末页next_after=null；版本相同首请求unchanged=true。跨页版本变化409应丢弃暂存并保留旧缓存。超过10000项或16MiB显式413，不得覆盖旧缓存。")},
        "/api/android/agent/schedule/ack": {"post": op("完整快照持久化后确认版本", body={"revision":1}, desc="不允许确认新于服务器的版本；旧确认不会覆盖更新的确认。")},
        "/api/android/agent/schedule/completions": {"post": op("设备完成或撤销完成，按设备与UUID幂等", body={"operation_id":"4b520570-2ec0-4fe4-87ee-611647931de5","occurrence_id":1,"expected_version":0,"completed":True}, desc="成功返回operation_id/occurrence/revision/duplicate。重试返回当前canonical occurrence；UUID不可换载荷。过期版本、已取消或撤销后会被已移除/不适用规则立即取消时409返回occurrence；未知或外部ID404。设备不能取消任务。")},
    })
    for route, methods in paths.items():
        if '/schedule' in route:
            for item in methods.values():
                item['responses']['409'] = {'description': '版本或操作冲突，请保留有效缓存并刷新'}
                item['responses']['413'] = {'description': '超出课程或缓存上限'}
    for path, methods in paths.items():
        if path.startswith("/api/android/agent/") or (path.startswith("/api/agent/") and path not in ("/api/agent/enroll", "/api/agent/install-info")):
            for item in methods.values():
                item["security"] = [{"deviceToken": []}]
        for method, item in methods.items():
            if method == "post" and "/api/android/devices/" in path:
                item["responses"]["201"] = {"description": "文件或任务已创建"}
    upload = paths["/api/android/devices/{device_id}/files"]["post"]
    upload["requestBody"] = {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
    tags={}
    for path,methods in paths.items():
        tag="其他"
        if path.startswith("/api/auth") or path.startswith("/api/user-management"): tag="用户与认证"
        elif path.startswith("/api/files"): tag="文件"
        elif path.startswith("/api/notes"): tag="小记"
        elif path.startswith("/api/llm"): tag="大模型"
        elif path.startswith("/api/hermes"): tag="Hermes"
        elif path.startswith("/api/access-policies"): tag="黑白名单"
        elif path.startswith("/api/android"): tag="Android 桌面"
        elif path.startswith("/api/device") or path.startswith("/api/agent"): tag="设备"
        elif path.startswith("/api/updates"): tag="更新"
        elif path.startswith("/api/typing"): tag="打字游戏"
        elif path.startswith("/api/xiaobu-words"): tag="小步英语"
        elif path.startswith("/api/words"): tag="Word Atlas"
        elif path.startswith("/api/openapi") or path=="/api/health": tag="系统"
        for item in methods.values():
            item["tags"]=[tag]
        tags[tag]=True
    return {
        "openapi":"3.0.3",
        "info":{"title":"Dev Control API","version":version,"description":"统一控制台 HTTP 接口。浏览器测试页可直接调用本站接口；下载 OpenAPI JSON 可导入 Apifox / Postman。"},
        "servers":[{"url":"/","description":"当前站点"}],
        "tags":[{"name":n} for n in tags],
        "components":{"securitySchemes":{"bearerAuth":{"type":"http","scheme":"bearer","bearerFormat":"token"},"deviceToken":{"type":"apiKey","in":"header","name":"X-Device-Token"}}},
        "security":[{"bearerAuth":[]}],
        "paths":paths,
    }

@bp.get("/apis/")
def page():
    return render_template("apis.html")

@bp.get("/api/openapi.json")
@require_user()
def openapi():
    resp=jsonify(spec())
    if request.args.get("download")=="1":
        resp.headers["Content-Disposition"]='attachment; filename="openapi.json"'
    return resp
