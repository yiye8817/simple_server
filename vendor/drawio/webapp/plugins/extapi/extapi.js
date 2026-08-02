/**
 * 外部 API 集成插件（External API Integration Plugin）
 *
 * 功能：
 *  - 配置外部服务的 Base URL 与 X-API-Key（保存在 localStorage）
 *  - 通过文件浏览器浏览目标 URL 下的文件夹/文件
 *  - 打开已存在的文件并由 DRAWIO 渲染、编辑
 *  - 将编辑结果通过接口保存回原文件（覆盖），支持“click here to save”一键保存
 *  - 自动保存（可开关、可设置时间间隔）
 *  - 新建 DRAWIO 文件：直接在“已选服务器路径”下创建
 *  - 多 TAB：同时打开多个 DRAWIO 文件，标签切换
 *  - 分享：调用 /share 接口生成公开链接
 *
 * 对应接口（见 “外部 API·上传到指定文件夹.md”）：
 *  - GET    /api/external/folder-items              列目录
 *  - GET    /api/external/files/<file_id>/content   读文件文本
 *  - POST   /api/external/file                      写/新建文本文件（overwrite 控制覆盖）
 *  - POST   /api/external/files/<file_id>/share     开启分享，返回 share_url / download_url
 *
 * 为规避浏览器跨域(CORS)与自定义请求头预检问题，所有请求统一走本地服务的反向代理
 * `/__ext_proxy__?u=<目标完整URL>`，由 server.py 注入 X-API-Key 转发到目标服务。
 *
 * 本插件通过全局 window.Draw.loadPlugin 注册，无需重新编译 app.min.js。
 */
(function()
{
	'use strict';

	var PROXY_PATH = null;
	var CONFIG_KEY = 'extApiConfig';
	var BLANK_DIAGRAM = '<mxfile><diagram id="0" name="第 1 页">' +
		'<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" ' +
		'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="850" pageHeight="1100" ' +
		'math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/></root>' +
		'</mxGraphModel></diagram></mxfile>';

	// ---------------------------------------------------------------------
	// 配置读写
	// ---------------------------------------------------------------------
	function defaultConfig()
	{
		return {baseUrl: window.location.origin, apiKey: '', rootPath: '/', autosave: false, autosaveInterval: 10};
	}

	function loadConfig()
	{
		try
		{
			var raw = localStorage.getItem(CONFIG_KEY);
			if (raw != null)
			{
				var c = JSON.parse(raw);
				var d = defaultConfig();
				for (var k in d) { if (c[k] === undefined) { c[k] = d[k]; } }
                c.apiKey = localStorage.getItem("devControlToken") || c.apiKey || "";
                if (!c.baseUrl) { c.baseUrl = window.location.origin; }
				return c;
			}
		}
		catch (e) { /* ignore */ }

        var initial = defaultConfig();
        initial.apiKey = localStorage.getItem("devControlToken") || "";
        return initial;
	}

	function saveConfig(cfg)
	{
		try { localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg)); }
		catch (e) { /* ignore */ }
	}

	// ---------------------------------------------------------------------
	// API 客户端（统一走本地代理）
	// ---------------------------------------------------------------------
	function ExtApiClient(cfg) { this.cfg = cfg; }

	ExtApiClient.prototype.base = function()
	{
		return (this.cfg.baseUrl || '').replace(/\/+$/, '');
	};

	ExtApiClient.prototype.request = function(method, apiPath, body)
	{
		var target = this.base() + apiPath;
		var url = target;
		var headers = {'Authorization': 'Bearer ' + (this.cfg.apiKey || '')};
		var opts = {method: method, headers: headers};

		if (body != null)
		{
			headers['Content-Type'] = 'application/json';
			opts.body = (typeof body === 'string') ? body : JSON.stringify(body);
		}

		return fetch(url, opts).then(function(resp)
		{
			return resp.text().then(function(text)
			{
				if (!resp.ok)
				{
					var msg = text;
					try { var j = JSON.parse(text); msg = j.error || j.message || text; }
					catch (e) { /* ignore */ }
					throw new Error('HTTP ' + resp.status + ': ' + msg);
				}

				try { return JSON.parse(text); }
				catch (e) { return text; }
			});
		});
	};

	ExtApiClient.prototype.listFolder = function(opts)
	{
		opts = opts || {};
		var qs = [];

		if (opts.folderId != null && opts.folderId !== '')
		{
			qs.push('folder_id=' + encodeURIComponent(opts.folderId));
		}
		else
		{
			qs.push('path=' + encodeURIComponent(opts.path != null ? opts.path : '/'));
		}

		return this.request('GET', '/api/external/folder-items?' + qs.join('&'));
	};

	ExtApiClient.prototype.readFile = function(fileId)
	{
		return this.request('GET', '/api/external/files/' + encodeURIComponent(fileId) + '/content');
	};

	ExtApiClient.prototype.writeFile = function(params)
	{
		var body = {name: params.name, content: params.content, overwrite: !!params.overwrite};

		if (params.folderId != null && params.folderId !== '') { body.folder_id = params.folderId; }
		else if (params.path != null) { body.path = params.path; }

		if (params.encoding != null) { body.encoding = params.encoding; }

		return this.request('POST', '/api/external/file', body);
	};

	ExtApiClient.prototype.share = function(fileId)
	{
		return this.request('POST', '/api/external/files/' + encodeURIComponent(fileId) + '/share', {});
	};

	// ---------------------------------------------------------------------
	// 响应结构归一化（不同后端字段名可能略有差异，做容错处理）
	// ---------------------------------------------------------------------
	function normalizeItems(resp)
	{
		var arr = null;

		if (Array.isArray(resp)) { arr = resp; }
		else if (resp != null)
		{
			arr = resp.items || resp.children || resp.files || resp.data ||
				resp.entries || resp.results || null;

			if (arr == null && (resp.folders != null || resp.dirs != null))
			{
				arr = (resp.folders || resp.dirs || []).concat(resp.files || []);
			}
		}

		if (!Array.isArray(arr)) { arr = []; }

		return arr.map(function(it)
		{
			var type = (it.type || it.kind || '').toString().toLowerCase();
			var isDir = it.is_dir === true || it.is_folder === true ||
				it.isDir === true || it.directory === true ||
				type === 'dir' || type === 'folder' || type === 'directory' ||
				(it.children != null && it.files == null && it.size == null && it.file_id == null);

			return {
				id: it.id != null ? it.id : (isDir ? it.folder_id : it.file_id),
				folderId: it.folder_id != null ? it.folder_id : (isDir ? it.id : null),
				fileId: it.file_id != null ? it.file_id : (!isDir ? it.id : null),
				name: it.name || it.filename || it.title || it.path || '(未命名)',
				isDir: isDir,
				path: it.path || null,
				raw: it
			};
		});
	}

	function extractContent(resp)
	{
		if (resp == null) { return ''; }
		if (typeof resp === 'string') { return resp; }
		return resp.content != null ? resp.content :
			(resp.data != null ? resp.data : (resp.text != null ? resp.text : ''));
	}

	function pickFileId(resp)
	{
		if (resp == null) { return null; }
		if (resp.file_id != null) { return resp.file_id; }
		if (resp.id != null) { return resp.id; }
		if (resp.file != null) { return resp.file.id != null ? resp.file.id : resp.file.file_id; }
		if (resp.data != null) { return resp.data.id != null ? resp.data.id : resp.data.file_id; }
		return null;
	}

	function isDrawioName(name) { return /\.(drawio|xml|dio)$/i.test(name || ''); }

	function ensureDrawioExt(name)
	{
		if (name == null || name === '') { return 'untitled.drawio'; }
		return /\.[^./\\]+$/.test(name) ? name : (name + '.drawio');
	}

	// ---------------------------------------------------------------------
	// 主插件入口
	// ---------------------------------------------------------------------
	function installPlugin(ui)
	{
		var cfg = loadConfig();
		var client = new ExtApiClient(cfg);

		// 多 TAB 状态
		ui.extApiTabs = [];           // [{fileId,name,folderId,path,data,modified}]
		ui.extApiActiveTab = null;    // 当前激活的 tab
		ui.extApiCurrentFolder = null; // 最近浏览/选择的文件夹（用于“NEW 直接创建”）

		var tabBar = null;

		function refreshClient() { cfg = loadConfig(); client = new ExtApiClient(cfg); }

		function alertError(e)
		{
			var msg = (e && e.message) ? e.message : String(e);
			ui.showError('错误', msg, mxResources.get('ok') || '确定');
		}

		function requireConfig()
		{
			refreshClient();
			if (!cfg.baseUrl) { showConfigDialog(); return false; }
			return true;
		}

		// =====================================================================
		// 标签栏（多 TAB）
		// =====================================================================
		function injectTabStyles()
		{
			if (document.getElementById('geExtApiStyle') != null) { return; }
			var css = '' +
				'.geExtApiTabBar{display:none;align-items:flex-end;gap:2px;height:32px;' +
				'background:light-dark(#eef0f2,#2a2a2a);border-bottom:1px solid light-dark(#d0d0d0,#444);' +
				'padding:0 8px;overflow-x:auto;overflow-y:hidden;font-size:13px;white-space:nowrap;}' +
				'.geExtApiTab{display:inline-flex;align-items:center;padding:4px 8px;' +
				'background:light-dark(#dfe3e6,#3a3a3a);color:light-dark(#333,#ddd);' +
				'border:1px solid light-dark(#c4c9cd,#555);border-bottom:none;' +
				'border-radius:7px 7px 0 0;margin-bottom:-1px;cursor:pointer;max-width:220px;}' +
				'.geExtApiTab.active{background:light-dark(#fff,#1e1e1e);font-weight:600;}' +
				'.geExtApiTab .label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}' +
				'.geExtApiTab .dot{color:#e67700;margin-left:6px;font-weight:bold;}' +
				'.geExtApiTab .close{margin-left:8px;color:#999;padding:0 2px;border-radius:3px;}' +
				'.geExtApiTab .close:hover{color:#fff;background:#d33;}' +
				/* 新增一行网格：标签栏在最顶部，其余整体下移 */
				'body.geExtApiTabs{grid-template-rows:min-content min-content min-content 1fr min-content !important;}' +
				'body.geExtApiTabs>.geExtApiTabBar{display:flex;grid-column:1 / -1 !important;grid-row:1 !important;}' +
				'body.geExtApiTabs>.geMenubarContainer,body.geExtApiTabs>.geSimpleMainMenu{grid-row:2 !important;}' +
				'body.geExtApiTabs>.geToolbarContainer{grid-row:3 !important;}' +
				'body.geExtApiTabs>.geSidebarContainer,body.geExtApiTabs>.geHsplit,' +
				'body.geExtApiTabs>.geDiagramContainer{grid-row:4 !important;}' +
				'body.geExtApiTabs>.geTabContainer{grid-row:5 !important;}';
			var style = document.createElement('style');
			style.id = 'geExtApiStyle';
			style.textContent = css;
			document.head.appendChild(style);
		}

		function ensureTabBar()
		{
			injectTabStyles();
			if (tabBar == null)
			{
				tabBar = document.createElement('div');
				tabBar.className = 'geExtApiTabBar';
				document.body.appendChild(tabBar);
			}
			return tabBar;
		}

		function updateBodyTabsClass()
		{
			ensureTabBar();
			if (ui.extApiTabs.length > 0) { document.body.classList.add('geExtApiTabs'); }
			else { document.body.classList.remove('geExtApiTabs'); }

			// 通知 drawio 重新计算画布尺寸
			try { ui.refresh(); ui.editor.graph.sizeDidChange(); } catch (e) { /* ignore */ }
		}

		function renderTabs()
		{
			var bar = ensureTabBar();
			bar.innerHTML = '';

			ui.extApiTabs.forEach(function(tab)
			{
				var el = document.createElement('div');
				el.className = 'geExtApiTab' + (tab === ui.extApiActiveTab ? ' active' : '');

				var label = document.createElement('span');
				label.className = 'label';
				label.textContent = tab.name;
				el.appendChild(label);

				if (tab.modified)
				{
					var dot = document.createElement('span');
					dot.className = 'dot';
					dot.textContent = '●';
					el.appendChild(dot);
				}

				var close = document.createElement('span');
				close.className = 'close';
				close.textContent = '×';
				close.title = '关闭';
				close.onclick = function(evt) { evt.stopPropagation(); closeTab(tab); };
				el.appendChild(close);

				el.onclick = function() { if (tab !== ui.extApiActiveTab) { switchToTab(tab); } };
				bar.appendChild(el);
			});

			updateBodyTabsClass();
		}

		// 把当前编辑器内容快照写回激活 tab（切换前调用，避免丢失未保存编辑）
		function captureActiveTab()
		{
			var f = ui.getCurrentFile();
			if (f != null && f.extApiServerFile && ui.extApiActiveTab != null)
			{
				try
				{
					ui.extApiActiveTab.data = ui.getFileData(true);
					ui.extApiActiveTab.modified = f.isModified();
					// 标记为未修改，避免 openLocalFile 弹出“放弃更改”确认
					f.setModified(false);
				}
				catch (e) { /* ignore */ }
			}
		}

		// 构造一个带“服务器保存行为”的 LocalFile（temp 文件）
		function makeServerFile(tab)
		{
			var file = new LocalFile(ui, tab.data || BLANK_DIAGRAM, tab.name, true);

			file.extApiServerFile = true;
			file.extApiTab = tab;
			file.isRenamable = function() { return true; };
			file.isAutosaveOptional = function() { return true; };
			// 覆盖 LocalFile.isAutosave（其默认要求 File System Access 句柄），
			// 改为完全由我们的配置开关决定。
			file.isAutosave = function() { return !this.inConflictState && cfg.autosave === true; };
			file.autosaveDelay = Math.max(1, cfg.autosaveInterval || 10) * 1000;
			file.maxAutosaveDelay = file.autosaveDelay * 2;

			var doSave = function(success, error)
			{
				var desc = file.extApiTab;
				try { file.updateFileData(); } catch (e) { /* ignore */ }
				var data = file.getData();

				client.writeFile({
					name: desc.name,
					content: data,
					folderId: desc.folderId,
					path: (desc.folderId == null) ? desc.path : null,
					overwrite: true,
					encoding: 'utf-8'
				})
				.then(function(resp)
				{
					var fid = pickFileId(resp);
					if (fid != null) { desc.fileId = fid; }
					file.setModified(false);
					desc.modified = false;
					try { file.contentChanged(); } catch (e) { /* ignore */ }
					renderTabs();
					ui.editor.setStatus(mxUtils.htmlEntities(
						(mxResources.get('allChangesSaved') || '已保存') + '：' + desc.name));
					if (success != null) { success(); }
				})
				.catch(function(e)
				{
					if (error != null) { error({error: e}); }
					else { alertError(e); }
				});
			};

			// 覆盖实例方法：Ctrl+S / 自动保存 / “click here to save” 全部走服务器
			file.save = function(revision, success, error) { doSave(success, error); };
			file.saveAs = function(title, success, error) { doSave(success, error); };
			file.saveFile = function(title, revision, success, error) { doSave(success, error); };

			return file;
		}

		function loadTab(tab)
		{
			// 直接用 fileLoaded 加载，避免 openLocalFile 在“当前画布非空”时弹出
			// “在新窗口打开”的对话框而无法就地替换内容。
			var file = makeServerFile(tab);
			ui.fileLoaded(file, true);
			ui.extApiActiveTab = tab;

			if (tab.modified) { file.setModified(true); }

			// 按配置开启/关闭自动保存
			try { ui.editor.setAutosave(cfg.autosave === true); } catch (e) { /* ignore */ }

			renderTabs();
		}

		function switchToTab(tab)
		{
			captureActiveTab();
			loadTab(tab);
			ui.editor.setStatus('已切换到：' + mxUtils.htmlEntities(tab.name));
		}

		function closeTab(tab)
		{
			var doClose = function()
			{
				var idx = ui.extApiTabs.indexOf(tab);
				if (idx < 0) { return; }
				ui.extApiTabs.splice(idx, 1);

				if (tab === ui.extApiActiveTab)
				{
					ui.extApiActiveTab = null;
					if (ui.extApiTabs.length > 0)
					{
						var next = ui.extApiTabs[Math.min(idx, ui.extApiTabs.length - 1)];
						loadTab(next);
					}
					else
					{
						// 没有标签了：恢复为一个空白本地文件
						ui.fileLoaded(new LocalFile(ui, BLANK_DIAGRAM, ui.defaultFilename || 'untitled.drawio', true), true);
						updateBodyTabsClass();
					}
				}
				renderTabs();
			};

			if (tab.modified)
			{
				ui.confirm('“' + tab.name + '” 有未保存的更改，仍要关闭吗？', doClose, null,
					'关闭', mxResources.get('cancel') || '取消');
			}
			else { doClose(); }
		}

		function findTabByFileId(fileId)
		{
			if (fileId == null) { return null; }
			for (var i = 0; i < ui.extApiTabs.length; i++)
			{
				if (ui.extApiTabs[i].fileId === fileId) { return ui.extApiTabs[i]; }
			}
			return null;
		}

		// =====================================================================
		// 配置对话框
		// =====================================================================
		function showConfigDialog(onSaved)
		{
			refreshClient();

			var div = document.createElement('div');
			div.innerHTML = '<h3 style="margin-top:0;">外部服务配置</h3>';

			function textRow(labelText, value, placeholder, type)
			{
				var wrap = document.createElement('div');
				wrap.style.cssText = 'margin-bottom:12px;';
				var label = document.createElement('div');
				label.style.cssText = 'margin-bottom:4px;font-size:12px;color:#666;';
				label.textContent = labelText;
				var input = document.createElement('input');
				input.type = type || 'text';
				input.value = (value != null) ? value : '';
				input.placeholder = placeholder || '';
				input.style.cssText = 'width:100%;box-sizing:border-box;padding:6px 8px;' +
					'border:1px solid #ccc;border-radius:4px;';
				wrap.appendChild(label); wrap.appendChild(input);
				div.appendChild(wrap);
				return input;
			}

			var urlInput = textRow('Base URL（例如 https://example.com）', cfg.baseUrl, 'https://your-server.com');
			var keyInput = textRow('X-API-Key', cfg.apiKey, '你的 API Key', 'password');
			var rootInput = textRow('根目录 path（默认 /）', cfg.rootPath || '/', '/');

			// 自动保存设置
			var asWrap = document.createElement('div');
			asWrap.style.cssText = 'margin-bottom:12px;display:flex;align-items:center;gap:8px;';
			var asChk = document.createElement('input');
			asChk.type = 'checkbox';
			asChk.checked = cfg.autosave === true;
			var asLbl = document.createElement('span');
			asLbl.textContent = '启用自动保存，间隔';
			asLbl.style.fontSize = '13px';
			var asInt = document.createElement('input');
			asInt.type = 'number';
			asInt.min = '1';
			asInt.value = cfg.autosaveInterval || 10;
			asInt.style.cssText = 'width:64px;padding:4px 6px;border:1px solid #ccc;border-radius:4px;';
			var asUnit = document.createElement('span');
			asUnit.textContent = '秒';
			asUnit.style.fontSize = '13px';
			asWrap.appendChild(asChk); asWrap.appendChild(asLbl);
			asWrap.appendChild(asInt); asWrap.appendChild(asUnit);
			div.appendChild(asWrap);

			var btnBar = document.createElement('div');
			btnBar.style.cssText = 'text-align:right;margin-top:16px;';

			var cancelBtn = mxUtils.button(mxResources.get('cancel') || '取消', function() { ui.hideDialog(); });
			cancelBtn.style.marginRight = '8px';

			var saveBtn = mxUtils.button(mxResources.get('save') || '保存', function()
			{
				var newCfg = {
					baseUrl: urlInput.value.trim(),
					apiKey: keyInput.value.trim(),
					rootPath: rootInput.value.trim() || '/',
					autosave: asChk.checked,
					autosaveInterval: Math.max(1, parseInt(asInt.value, 10) || 10)
				};
				saveConfig(newCfg);
				refreshClient();
				ui.hideDialog();

				// 立即对当前已打开的服务器文件生效
				var f = ui.getCurrentFile();
				if (f != null && f.extApiServerFile)
				{
					f.autosaveDelay = newCfg.autosaveInterval * 1000;
					f.maxAutosaveDelay = f.autosaveDelay * 2;
					try { ui.editor.setAutosave(newCfg.autosave === true); } catch (e) { /* ignore */ }
				}

				if (onSaved != null) { onSaved(); }
			});
			saveBtn.className = 'geBtn gePrimaryBtn';

			btnBar.appendChild(cancelBtn);
			btnBar.appendChild(saveBtn);
			div.appendChild(btnBar);

			ui.showDialog(div, 440, 340, true, true);
			urlInput.focus();
		}

		// =====================================================================
		// 文件浏览器对话框
		// mode: 'open' 选择要打开的文件；'pick-folder' 选择目标文件夹
		// =====================================================================
		function showBrowser(mode, onChoose)
		{
			if (!requireConfig()) { return; }

			var stack = [{name: '根目录', path: cfg.rootPath || '/', folderId: null}];

			var div = document.createElement('div');
			div.style.cssText = 'height:100%;display:flex;flex-direction:column;';

			var title = document.createElement('h3');
			title.style.cssText = 'margin:0 0 8px 0;';
			title.textContent = (mode === 'open') ? '从服务器打开文件' : '选择目标文件夹';
			div.appendChild(title);

			var crumb = document.createElement('div');
			crumb.style.cssText = 'font-size:12px;color:#666;margin-bottom:8px;' +
				'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
			div.appendChild(crumb);

			var list = document.createElement('div');
			list.style.cssText = 'flex:1;overflow:auto;border:1px solid #e0e0e0;' +
				'border-radius:4px;min-height:240px;';
			div.appendChild(list);

			var btnBar = document.createElement('div');
			btnBar.style.cssText = 'text-align:right;margin-top:12px;';
			div.appendChild(btnBar);

			function currentNode() { return stack[stack.length - 1]; }
			function renderCrumb() { crumb.textContent = stack.map(function(n) { return n.name; }).join('  /  '); }
			function setLoading() { list.innerHTML = '<div style="padding:16px;color:#999;">加载中…</div>'; }

			function makeRow(label, icon, onClick, onPick)
			{
				var rowEl = document.createElement('div');
				rowEl.style.cssText = 'display:flex;align-items:center;padding:8px 10px;' +
					'cursor:pointer;border-bottom:1px solid #f0f0f0;';
				rowEl.onmouseover = function() { rowEl.style.background = '#f5f7ff'; };
				rowEl.onmouseout = function() { rowEl.style.background = '#fff'; };

				var ic = document.createElement('span');
				ic.textContent = icon;
				ic.style.cssText = 'margin-right:10px;font-size:14px;';
				var tx = document.createElement('span');
				tx.textContent = label;
				tx.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
				rowEl.appendChild(ic); rowEl.appendChild(tx);

				if (onPick != null)
				{
					var pickBtn = mxUtils.button('选择', function(evt) { evt.stopPropagation(); onPick(); });
					pickBtn.style.cssText = 'padding:2px 8px;font-size:12px;';
					rowEl.appendChild(pickBtn);
				}

				rowEl.onclick = onClick;
				return rowEl;
			}

			function load()
			{
				renderCrumb();
				setLoading();
				var node = currentNode();

				// 记住当前浏览到的文件夹，供“NEW 直接创建”使用
				ui.extApiCurrentFolder = {name: node.name, path: node.path, folderId: node.folderId};

				client.listFolder({path: node.path, folderId: node.folderId}).then(function(resp)
				{
					var items = normalizeItems(resp);
					list.innerHTML = '';

					if (stack.length > 1)
					{
						list.appendChild(makeRow('.. 返回上一级', '↩', function() { stack.pop(); load(); }));
					}

					var dirs = items.filter(function(i) { return i.isDir; });
					var files = items.filter(function(i) { return !i.isDir; });

					dirs.forEach(function(it)
					{
						var childNode = {
							name: it.name,
							path: it.path || ((node.path || '/').replace(/\/$/, '') + '/' + it.name),
							folderId: it.folderId != null ? it.folderId : it.id
						};
						list.appendChild(makeRow(it.name, '📁', function()
						{
							stack.push(childNode); load();
						}, (mode === 'pick-folder') ? function()
						{
							ui.hideDialog(); onChoose(childNode);
						} : null));
					});

					if (mode === 'open')
					{
						files.forEach(function(it)
						{
							var isDio = isDrawioName(it.name);
							var rowEl = makeRow(it.name, isDio ? '📄' : '·', function()
							{
								ui.hideDialog();
								onChoose({
									fileId: it.fileId != null ? it.fileId : it.id,
									name: it.name,
									folderId: node.folderId,
									path: node.path
								});
							});
							if (!isDio) { rowEl.style.opacity = '0.55'; }
							list.appendChild(rowEl);
						});
					}

					if (list.childNodes.length === 0 ||
						(stack.length > 1 && list.childNodes.length === 1))
					{
						var empty = document.createElement('div');
						empty.style.cssText = 'padding:16px;color:#999;';
						empty.textContent = '（此文件夹为空）';
						list.appendChild(empty);
					}
				})
				.catch(function(e)
				{
					list.innerHTML = '';
					var err = document.createElement('div');
					err.style.cssText = 'padding:16px;color:#c00;font-size:12px;';
					err.textContent = '加载失败：' + ((e && e.message) || e);
					list.appendChild(err);
				});
			}

			if (mode === 'pick-folder')
			{
				var pickHereBtn = mxUtils.button('选择当前文件夹', function()
				{
					var node = currentNode();
					ui.hideDialog();
					onChoose({name: node.name, path: node.path, folderId: node.folderId});
				});
				pickHereBtn.className = 'geBtn gePrimaryBtn';
				pickHereBtn.style.marginRight = '8px';
				btnBar.appendChild(pickHereBtn);
			}

			btnBar.appendChild(mxUtils.button(mxResources.get('cancel') || '取消', function() { ui.hideDialog(); }));

			ui.showDialog(div, 480, 470, true, true);
			load();
		}

		// =====================================================================
		// 打开 / 保存 / 新建 / 分享
		// =====================================================================
		function openFromServer()
		{
			showBrowser('open', function(sel)
			{
				var existing = findTabByFileId(sel.fileId);
				if (existing != null) { switchToTab(existing); return; }

				ui.spinner.spin(document.body, '正在打开…');
				client.readFile(sel.fileId).then(function(resp)
				{
					ui.spinner.stop();
					var content = extractContent(resp);
					if (content == null || content === '') { content = BLANK_DIAGRAM; }

					captureActiveTab();
					var tab = {
						fileId: sel.fileId, name: sel.name,
						folderId: sel.folderId, path: sel.path,
						data: content, modified: false
					};
					ui.extApiTabs.push(tab);
					ui.extApiCurrentFolder = {folderId: sel.folderId, path: sel.path, name: sel.name};

					try { loadTab(tab); ui.editor.setStatus('已从服务器打开：' + mxUtils.htmlEntities(sel.name)); }
					catch (e) { alertError(e); }
				})
				.catch(function(e) { ui.spinner.stop(); alertError(e); });
			});
		}

		// 保存当前激活文件回服务器
		function saveCurrent()
		{
			if (!requireConfig()) { return; }
			var f = ui.getCurrentFile();
			if (f != null && f.extApiServerFile) { f.save(false); }
			else { saveAsToServer(); }
		}

		// 另存为：选择文件夹 + 命名后写入，并作为新 tab 打开
		function saveAsToServer()
		{
			if (!requireConfig()) { return; }

			showBrowser('pick-folder', function(folder)
			{
				var f = ui.getCurrentFile();
				var defaultName = (f != null && f.getTitle != null) ? f.getTitle() : 'diagram.drawio';

				promptName('另存到服务器', ensureDrawioExt(defaultName), function(name)
				{
					name = ensureDrawioExt(name);
					var data = ui.getFileData(true);
					ui.spinner.spin(document.body, '正在保存…');

					client.writeFile({
						name: name, content: data,
						folderId: folder.folderId,
						path: (folder.folderId == null) ? folder.path : null,
						overwrite: true, encoding: 'utf-8'
					})
					.then(function(resp)
					{
						ui.spinner.stop();
						captureActiveTab();
						var tab = {
							fileId: pickFileId(resp), name: name,
							folderId: folder.folderId, path: folder.path,
							data: data, modified: false
						};
						ui.extApiTabs.push(tab);
						loadTab(tab);
						ui.editor.setStatus('已保存到服务器：' + mxUtils.htmlEntities(name));
					})
					.catch(function(e) { ui.spinner.stop(); alertError(e); });
				});
			});
		}

		// NEW：直接在“已选服务器路径”下新建（仅需输入文件名）
		function newToServerDirect()
		{
			if (!requireConfig()) { return; }

			var folder = ui.extApiCurrentFolder || {folderId: null, path: cfg.rootPath || '/', name: '根目录'};

			promptName('在 [' + (folder.name || folder.path || '/') + '] 新建 DRAWIO 文件',
				'untitled.drawio', function(name)
			{
				name = ensureDrawioExt(name);
				ui.spinner.spin(document.body, '正在创建…');

				client.writeFile({
					name: name, content: BLANK_DIAGRAM,
					folderId: folder.folderId,
					path: (folder.folderId == null) ? folder.path : null,
					overwrite: false, encoding: 'utf-8'
				})
				.then(function(resp)
				{
					ui.spinner.stop();
					captureActiveTab();
					var tab = {
						fileId: pickFileId(resp), name: name,
						folderId: folder.folderId, path: folder.path,
						data: BLANK_DIAGRAM, modified: false
					};
					ui.extApiTabs.push(tab);
					loadTab(tab);
					ui.editor.setStatus('已在服务器新建：' + mxUtils.htmlEntities(name));
				})
				.catch(function(e) { ui.spinner.stop(); alertError(e); });
			});
		}

		// 先选择文件夹（设为当前路径），再新建
		function newToServerPick()
		{
			if (!requireConfig()) { return; }
			showBrowser('pick-folder', function(folder)
			{
				ui.extApiCurrentFolder = folder;
				newToServerDirect();
			});
		}

		// 分享当前文件
		function shareCurrent()
		{
			if (!requireConfig()) { return; }

			var tab = ui.extApiActiveTab;
			if (tab == null)
			{
				ui.showError('提示', '请先从服务器打开或保存一个文件后再分享。', mxResources.get('ok') || '确定');
				return;
			}

			var doShare = function()
			{
				ui.spinner.spin(document.body, '正在生成分享链接…');
				client.share(tab.fileId).then(function(resp)
				{
					ui.spinner.stop();
					showShareDialog(resp);
				})
				.catch(function(e) { ui.spinner.stop(); alertError(e); });
			};

			if (tab.fileId != null) { doShare(); return; }

			// 尚无 file_id：先保存以获取，再尝试通过列目录解析
			var f = ui.getCurrentFile();
			ui.spinner.spin(document.body, '正在保存…');
			var afterSave = function()
			{
				ui.spinner.stop();
				if (tab.fileId != null) { doShare(); }
				else { resolveFileId(tab, function() { doShare(); }); }
			};

			if (f != null && f.extApiServerFile) { f.save(false, afterSave, function(e) { ui.spinner.stop(); alertError(e && e.error || e); }); }
			else { ui.spinner.stop(); ui.showError('提示', '当前文件未关联服务器，请先“另存到服务器”。', mxResources.get('ok') || '确定'); }
		}

		// 通过列目录按文件名解析 file_id（兜底）
		function resolveFileId(tab, cb)
		{
			client.listFolder({path: tab.path, folderId: tab.folderId}).then(function(resp)
			{
				var items = normalizeItems(resp);
				for (var i = 0; i < items.length; i++)
				{
					if (!items[i].isDir && items[i].name === tab.name)
					{
						tab.fileId = items[i].fileId != null ? items[i].fileId : items[i].id;
						break;
					}
				}
				if (tab.fileId != null) { cb(); }
				else { alertError(new Error('无法解析文件 ID，分享失败。')); }
			})
			.catch(function(e) { alertError(e); });
		}

		function showShareDialog(resp)
		{
			var shareUrl = resp && (resp.share_url || resp.shareUrl || resp.url);
			var downloadUrl = resp && (resp.download_url || resp.downloadUrl);

			var div = document.createElement('div');
			div.innerHTML = '<h3 style="margin-top:0;">分享链接</h3>';

			function linkRow(labelText, value)
			{
				if (!value) { return; }
				var wrap = document.createElement('div');
				wrap.style.cssText = 'margin-bottom:12px;';
				var label = document.createElement('div');
				label.style.cssText = 'margin-bottom:4px;font-size:12px;color:#666;';
				label.textContent = labelText;

				var rowEl = document.createElement('div');
				rowEl.style.cssText = 'display:flex;gap:6px;';
				var input = document.createElement('input');
				input.type = 'text';
				input.readOnly = true;
				input.value = value;
				input.style.cssText = 'flex:1;padding:6px 8px;border:1px solid #ccc;border-radius:4px;';
				input.onclick = function() { input.select(); };

				var copyBtn = mxUtils.button('复制', function()
				{
					input.select();
					try { document.execCommand('copy'); } catch (e) { /* ignore */ }
					if (navigator.clipboard) { navigator.clipboard.writeText(value).catch(function() {}); }
					copyBtn.textContent = '已复制';
					setTimeout(function() { copyBtn.textContent = '复制'; }, 1500);
				});

				var openBtn = mxUtils.button('打开', function() { window.open(value, '_blank'); });

				rowEl.appendChild(input); rowEl.appendChild(copyBtn); rowEl.appendChild(openBtn);
				wrap.appendChild(label); wrap.appendChild(rowEl);
				div.appendChild(wrap);
			}

			linkRow('公开分享地址（share_url）', shareUrl);
			linkRow('文件下载地址（download_url）', downloadUrl);

			if (!shareUrl && !downloadUrl)
			{
				var pre = document.createElement('pre');
				pre.style.cssText = 'white-space:pre-wrap;font-size:12px;background:#f6f6f6;padding:8px;border-radius:4px;';
				pre.textContent = (typeof resp === 'string') ? resp : JSON.stringify(resp, null, 2);
				div.appendChild(pre);
			}

			var btnBar = document.createElement('div');
			btnBar.style.cssText = 'text-align:right;margin-top:12px;';
			var closeBtn = mxUtils.button(mxResources.get('close') || '关闭', function() { ui.hideDialog(); });
			closeBtn.className = 'geBtn gePrimaryBtn';
			btnBar.appendChild(closeBtn);
			div.appendChild(btnBar);

			ui.showDialog(div, 460, 260, true, true);
		}

		// 简单的名称输入对话框
		function promptName(titleText, defaultValue, onOk)
		{
			var div = document.createElement('div');
			var h = document.createElement('h3');
			h.style.cssText = 'margin-top:0;';
			h.textContent = titleText;
			div.appendChild(h);

			var input = document.createElement('input');
			input.type = 'text';
			input.value = defaultValue || '';
			input.style.cssText = 'width:100%;box-sizing:border-box;padding:6px 8px;' +
				'border:1px solid #ccc;border-radius:4px;';
			div.appendChild(input);

			var btnBar = document.createElement('div');
			btnBar.style.cssText = 'text-align:right;margin-top:16px;';

			var cancelBtn = mxUtils.button(mxResources.get('cancel') || '取消', function() { ui.hideDialog(); });
			cancelBtn.style.marginRight = '8px';

			var okBtn = mxUtils.button(mxResources.get('ok') || '确定', function()
			{
				var v = input.value.trim();
				if (v === '') { return; }
				ui.hideDialog();
				onOk(v);
			});
			okBtn.className = 'geBtn gePrimaryBtn';

			btnBar.appendChild(cancelBtn); btnBar.appendChild(okBtn);
			div.appendChild(btnBar);

			ui.showDialog(div, 380, 170, true, true);
			input.focus(); input.select();
			input.addEventListener('keydown', function(e) { if (e.keyCode === 13) { okBtn.click(); } });
		}

		// =====================================================================
		// 注册动作、菜单、Ctrl+S/“click here to save” 接管
		// =====================================================================
		ui.actions.addAction('extApiConfig', function() { showConfigDialog(); });
		ui.actions.addAction('extApiOpen', function() { openFromServer(); });
		ui.actions.addAction('extApiNew', function() { newToServerDirect(); });
		ui.actions.addAction('extApiNewPick', function() { newToServerPick(); });
		ui.actions.addAction('extApiSave', function() { saveCurrent(); });
		ui.actions.addAction('extApiSaveAs', function() { saveAsToServer(); });
		ui.actions.addAction('extApiShare', function() { shareCurrent(); });

		// 让 Ctrl+S 与状态栏“点此保存”在服务器文件激活时回写服务器
		function wrapAction(name)
		{
			var a = ui.actions.get(name);
			if (a == null) { return; }
			var orig = a.funct;
			a.funct = function()
			{
				var f = ui.getCurrentFile();
				if (f != null && f.extApiServerFile && loadConfig().baseUrl)
				{
					saveCurrent();
				}
				else if (orig != null) { orig.apply(this, arguments); }
			};
		}
		wrapAction('save');
		wrapAction('saveAs');

		// 资源文本
		mxResources.parse('extApiConfig=外部服务配置…');
		mxResources.parse('extApiOpen=从服务器打开…');
		mxResources.parse('extApiNew=新建到服务器（当前路径）');
		mxResources.parse('extApiNewPick=新建到服务器（选择文件夹）…');
		mxResources.parse('extApiSave=保存到服务器');
		mxResources.parse('extApiSaveAs=另存到服务器…');
		mxResources.parse('extApiShare=分享…');
		mxResources.parse('extApiMenu=外部服务');

		var menuItems = ['extApiOpen', 'extApiNew', 'extApiNewPick', '-',
			'extApiSave', 'extApiSaveAs', 'extApiShare', '-', 'extApiConfig'];

		ui.menus.put('extApi', new Menu(mxUtils.bind(ui.menus, function(menu, parent)
		{
			this.addMenuItems(menu, menuItems, parent);
		})));

		function addToMenubar()
		{
			try
			{
				if (ui.menubar != null && ui.menubar.addMenu != null)
				{
					var elt = ui.menubar.addMenu(mxResources.get('extApiMenu'), ui.menus.get('extApi').funct);
					if (elt != null)
					{
						elt.style.fontWeight = 'bold';
						if (ui.menubar.container != null) { ui.menubar.container.appendChild(elt); }
					}
					return true;
				}
			}
			catch (e) { /* ignore */ }
			return false;
		}

		function appendToMenu(key)
		{
			var menu = ui.menus.get(key);
			if (menu == null) { return; }
			var oldFunct = menu.funct;
			menu.funct = function(menu2, parent)
			{
				oldFunct.apply(this, arguments);
				this.addMenuItems(menu2, ['-'].concat(menuItems), parent);
			};
		}

		var addedBar = addToMenubar();
		appendToMenu('extras');
		appendToMenu('file');

		if (!addedBar) { addFloatingButton(); }

		function addFloatingButton()
		{
			var btn = document.createElement('div');
			btn.title = '外部服务';
			btn.textContent = '外部服务 ▾';
			btn.style.cssText = 'position:fixed;top:8px;right:12px;z-index:10000;' +
				'background:#1f6feb;color:#fff;padding:6px 12px;border-radius:6px;' +
				'font-size:13px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.2);';
			var menuOpen = false;
			var panel = document.createElement('div');
			panel.style.cssText = 'position:fixed;top:40px;right:12px;z-index:10000;' +
				'background:#fff;border:1px solid #ddd;border-radius:6px;display:none;' +
				'box-shadow:0 2px 8px rgba(0,0,0,0.2);min-width:200px;overflow:hidden;color:#333;';

			function item(label, fn)
			{
				var it = document.createElement('div');
				it.textContent = label;
				it.style.cssText = 'padding:8px 14px;cursor:pointer;font-size:13px;';
				it.onmouseover = function() { it.style.background = '#f0f4ff'; };
				it.onmouseout = function() { it.style.background = '#fff'; };
				it.onclick = function() { panel.style.display = 'none'; menuOpen = false; fn(); };
				panel.appendChild(it);
			}

			item('从服务器打开…', openFromServer);
			item('新建到服务器（当前路径）', newToServerDirect);
			item('新建到服务器（选择文件夹）…', newToServerPick);
			item('保存到服务器', saveCurrent);
			item('另存到服务器…', saveAsToServer);
			item('分享…', shareCurrent);
			item('外部服务配置…', function() { showConfigDialog(); });

			btn.onclick = function() { menuOpen = !menuOpen; panel.style.display = menuOpen ? 'block' : 'none'; };
			document.body.appendChild(btn);
			document.body.appendChild(panel);
		}

		ensureTabBar();

		// 仅在 URL 带 ?extapiTest=1 时暴露内部入口，供自动化测试使用（生产环境不激活）
		if (typeof urlParams !== 'undefined' && urlParams['extapiTest'] === '1')
		{
			window.__extApi = {
				ui: ui,
				open: openFromServer,
				save: saveCurrent,
				saveAs: saveAsToServer,
				newDirect: newToServerDirect,
				newPick: newToServerPick,
				share: shareCurrent,
				config: showConfigDialog,
				tabs: function() { return ui.extApiTabs; },
				activeTab: function() { return ui.extApiActiveTab; }
			};
		}

		if (window.console != null) { console.log('[extapi] 外部 API 集成插件已加载（v2）'); }
	}

	// ---------------------------------------------------------------------
	// 自举：等待 drawio 的全局插件入口 window.Draw.loadPlugin 就绪
	// ---------------------------------------------------------------------
	var tries = 0;
	function bootstrap()
	{
		if (window.Draw != null && typeof window.Draw.loadPlugin === 'function')
		{
			window.Draw.loadPlugin(installPlugin);
			return;
		}
		if (tries++ < 600) { setTimeout(bootstrap, 100); }
	}

	bootstrap();
})();
