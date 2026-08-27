"""妙笔散文日记 —— 把妙笔里的随笔用 AI 梳理成散文、批注、对话的小工具。

用法：双击「启动散文工具.bat」，或在命令行运行  pythonw app.py
依赖：仅 Python 标准库（tkinter / urllib）
"""

import datetime
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import annotator
import polisher
from glm_client import GLMClient, GLMError
from wp_client import WonderPenClient, WonderPenError

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"

DEFAULT_CONFIG = {
    "wonderpen_port": 8022,
    "wonderpen_token": "",
    "glm_api_key": "",
    "glm_model": "glm-5.2",
}

TEXT_FONT = ("Microsoft YaHei UI", 11)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("妙笔散文日记")
        root.geometry("1020x680")
        root.minsize(860, 560)

        self.config = load_config()
        self.queue: queue.Queue = queue.Queue()

        self.wp: WonderPenClient | None = None
        self.lib_key: str | None = None
        self.lib_title: str = ""
        self.items_by_id: dict[str, dict] = {}  # 文档树所有节点的元数据
        self.tree_items: list = []  # 文档树的原始嵌套结构
        self.last_source_ids: list[str] = []  # 最近一次梳理对应的源文档 id
        self.polished = False  # 预览区是否已是梳理结果

        # 预览区的形态：prose=散文(Markdown，可编辑) / annotate=批注(HTML，只读)
        self.preview_kind = "prose"
        self.pending_html = ""  # 批注文档的 HTML（写回妙笔时用，预览只是展示）

        self._build_ui()
        self.root.after(120, self._poll_queue)

        if self.config.get("wonderpen_token"):
            self._run_worker(self._task_connect)
        else:
            self._set_status("请先点击右上角「设置」，填写妙笔 API Token 和智谱 API Key。", "gray")

    # ------------------------------------------------------------------ 界面

    def _build_ui(self):
        # 顶部：连接状态 + 操作
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.dot = ttk.Label(top, text="●", foreground="gray")
        self.dot.pack(side="left")
        self.conn_label = ttk.Label(top, text=" 未连接", width=40, anchor="w")
        self.conn_label.pack(side="left", padx=(4, 12))
        ttk.Button(top, text="刷新文档", command=self._on_refresh).pack(side="right", padx=4)
        ttk.Button(top, text="设置", command=self._open_settings).pack(side="right", padx=4)

        # 主体：左文档树 / 右预览（可拖动分隔条）
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # 左：文档树（可多选）
        left = ttk.Frame(body, width=280)
        body.add(left, weight=1)
        ttk.Label(left, text="随笔文档（可多选）").pack(anchor="w", pady=(0, 4))
        self.tree = ttk.Treeview(left, selectmode="extended", show="tree")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="left", fill="y")

        # 右：标题 + 预览 + 按钮 + 状态
        right = ttk.Frame(body)
        body.add(right, weight=3)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        title_row = ttk.Frame(right)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(title_row, text="新文档标题：").pack(side="left")
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(title_row, textvariable=self.title_var)
        title_entry.pack(side="left", fill="x", expand=True, padx=4)

        self.preview = scrolledtext.ScrolledText(right, wrap="word", font=TEXT_FONT, undo=True)
        self.preview.grid(row=1, column=0, sticky="nsew")

        btn_row = ttk.Frame(right, padding=(0, 8, 0, 4))
        btn_row.grid(row=2, column=0, sticky="ew")
        self.polish_btn = ttk.Button(btn_row, text="✨ AI 梳理成散文", command=self._on_polish)
        self.polish_btn.pack(side="left")

        self.mode_var = tk.StringVar(value=annotator.MODE_LABELS[0])
        mode_combo = ttk.Combobox(
            btn_row,
            textvariable=self.mode_var,
            values=annotator.MODE_LABELS,
            state="readonly",
            width=10,
        )
        mode_combo.pack(side="left", padx=(10, 2))
        self.annotate_btn = ttk.Button(btn_row, text="📖 AI 批注", command=self._on_annotate)
        self.annotate_btn.pack(side="left")

        self.write_btn = ttk.Button(
            btn_row, text="写回妙笔", command=self._on_write_back, state="disabled"
        )
        self.write_btn.pack(side="left", padx=10)

        self.chat_btn = ttk.Button(btn_row, text="💬 对话", command=self._on_chat)
        self.chat_btn.pack(side="right")

        self.status_label = ttk.Label(right, text="就绪", foreground="gray", anchor="w")
        self.status_label.grid(row=3, column=0, sticky="ew", pady=(4, 0))

    # ------------------------------------------------------------------ 配置

    def _make_wp(self) -> WonderPenClient:
        return WonderPenClient(
            port=int(self.config.get("wonderpen_port", 8022)),
            token=self.config.get("wonderpen_token", ""),
        )

    # ------------------------------------------------------------------ 后台任务（在工作线程里跑，只许往 queue 放消息）

    def _run_worker(self, task):
        threading.Thread(target=self._worker_wrap, args=(task,), daemon=True).start()

    def _worker_wrap(self, task):
        try:
            task()
        except WonderPenError as e:
            self.queue.put(("error", str(e)))
        except GLMError as e:
            self.queue.put(("error", str(e)))
        except Exception as e:  # 兜底，避免线程内异常无声消失
            self.queue.put(("error", f"发生意外错误：{e!r}"))

    def _task_connect(self):
        self.queue.put(("status", "正在连接妙笔……"))
        wp = self._make_wp()
        libs = wp.list_libraries()
        if not libs:
            raise WonderPenError(
                "妙笔 API 没有可访问的文档库，请检查 妙笔 设置 → 高级 → API 里的「文档库访问范围」。"
            )
        lib = libs[0]
        self.wp = wp
        self.lib_key = lib.get("libKey")
        self.lib_title = lib.get("title", "")
        self._load_tree_data()
        self.queue.put(("connected", None))

    def _load_tree_data(self):
        data = self.wp.list_items(self.lib_key)
        self.items_by_id = {}
        self.tree_items = data.get("items", [])

        def walk(nodes):
            for node in nodes:
                self.items_by_id[node["id"]] = node
                walk(node.get("children", []))

        walk(self.tree_items)

    def _make_glm(self) -> GLMClient:
        return GLMClient(
            api_key=self.config.get("glm_api_key", ""),
            model=self.config.get("glm_model", "glm-5.2"),
        )

    def _task_polish(self, doc_metas):
        docs = []
        for i, meta in enumerate(doc_metas, 1):
            self.queue.put(("status", f"正在读取随笔 {i}/{len(doc_metas)}：{meta['title']}……"))
            item = self.wp.get_item(meta["id"], self.lib_key, fmt="markdown")
            docs.append({**meta, "content": item.get("content", "")})
        docs = polisher.sort_docs(docs)

        self.queue.put(("status", "正在请 GLM 梳理成散文，稍等……"))
        result = self._make_glm().chat(
            polisher.SYSTEM_PROMPT, polisher.build_user_message(docs)
        )
        self.queue.put(("polished", {"docs": docs, "text": result.strip()}))

    def _task_annotate(self, meta, mode):
        self.queue.put(("status", f"正在读取《{meta['title']}》……"))
        item = self.wp.get_item(meta["id"], self.lib_key, fmt="markdown")
        text = item.get("content", "")
        paras = annotator.split_paragraphs(text)
        if not paras:
            raise WonderPenError("这篇文章是空的，没有可批注的内容。")

        self.queue.put(
            ("status", f"共 {len(paras)} 段，GLM 正在逐段批注（{annotator.MODE_NAMES[mode]}），稍等……")
        )
        raw = self._make_glm().chat_messages(
            [
                {"role": "system", "content": annotator.MODE_PROMPTS[mode]},
                {"role": "user", "content": annotator.build_user_message(paras)},
            ],
            temperature=0.4,
        )
        notes = annotator.parse_annotations(raw)
        html = annotator.compose_html(paras, notes, fallback=raw)
        preview = annotator.compose_preview(paras, notes, fallback=raw)
        n = sum(len(v) for v in notes.values())
        self.queue.put(
            (
                "annotated",
                {
                    "meta": meta,
                    "html": html,
                    "preview": preview,
                    "count": n,
                    "mode": annotator.MODE_NAMES[mode],
                },
            )
        )

    def _task_open_chat(self, meta):
        """读取文章 + 顺手抓一下编辑器选区（如果用户正开着这篇并选中了文字）。"""
        self.queue.put(("status", f"正在读取《{meta['title']}》……"))
        item = self.wp.get_item(meta["id"], self.lib_key, fmt="markdown")
        selection = ""
        try:
            state = self.wp.get_editor_state()
            doc = state.get("document") or {}
            if doc.get("itemId") == meta["id"]:
                selection = (state.get("selection") or {}).get("text", "")
        except Exception:
            pass  # 选区只是锦上添花，失败不影响聊天
        self.queue.put(
            ("chat_ready", {"meta": meta, "content": item.get("content", ""), "selection": selection})
        )

    def _task_export_chat(self, meta, dialog_text):
        """把聊天记录写入《原题 · 对话》：已存在则追加，否则建在原文档后面。"""
        self.queue.put(("status", "正在把对话写入妙笔……"))
        chat_title = f"{meta['title']} · 对话"
        target_id = None
        for node in self.items_by_id.values():
            t = node.get("rendered_title") or node.get("title", "")
            if t == chat_title:
                target_id = node["id"]
                break

        stamp = datetime.datetime.now()
        new_block = f"---\n\n**{stamp:%m月%d日 %H:%M}**\n\n{dialog_text.strip()}\n"

        if target_id:
            old = self.wp.get_item(target_id, self.lib_key, fmt="markdown").get("content", "")
            self.wp.update_item(target_id, old.rstrip() + "\n\n" + new_block, fmt="markdown",
                                lib_key=self.lib_key)
            self.queue.put(("chat_exported", chat_title))
        else:
            self.wp.create_item(
                title=chat_title,
                content=new_block,
                fmt="markdown",
                related_item_id=meta["id"],
                where="after",
                lib_key=self.lib_key,
            )
            self.queue.put(("chat_exported", chat_title))

    def _task_write_back(self, title, content, after_doc_id, fmt):
        self.queue.put(("status", "正在写入妙笔……"))
        self.wp.create_item(
            title=title,
            content=content,
            fmt=fmt,
            related_item_id=after_doc_id,
            where="after",
            lib_key=self.lib_key,
        )
        self._load_tree_data()
        self.queue.put(("written", title))

    # ------------------------------------------------------------------ 事件

    def _on_refresh(self):
        if not self.config.get("wonderpen_token"):
            self._open_settings()
            return
        self._run_worker(self._task_connect)

    def _selected_metas(self) -> list[dict]:
        ids = self.tree.selection()
        metas = []
        for i in ids:
            node = self.items_by_id.get(i)
            if node:
                metas.append(
                    {
                        "id": node["id"],
                        "title": node.get("rendered_title") or node.get("title", ""),
                        "rendered_title": node.get("rendered_title") or node.get("title", ""),
                        "created_at_ms": node.get("created_at_ms"),
                    }
                )
        return metas

    def _check_ready(self) -> list[dict] | None:
        """批注 / 对话 / 梳理 共用的前置检查，返回选中的文档元数据。"""
        if self.wp is None:
            self._set_status("还没连接妙笔。", "red")
            return None
        if not self.config.get("glm_api_key"):
            messagebox.showinfo(
                "需要智谱 API Key",
                "还没有填写智谱 API Key。\n\n请到 open.bigmodel.cn 申请（有免费额度），"
                "然后在弹出的设置里填入。",
            )
            self._open_settings()
            return None
        metas = self._selected_metas()
        if not metas:
            self._set_status("请先在左侧选择一篇随笔。", "red")
            return None
        return metas

    def _on_polish(self):
        metas = self._check_ready()
        if not metas:
            return
        self._set_buttons_busy(True)
        self._run_worker(lambda: self._task_polish(metas))

    def _on_annotate(self):
        metas = self._check_ready()
        if not metas:
            return
        if len(metas) > 1:
            self._set_status("批注一次只针对一篇，已取第一篇《%s》。" % metas[0]["title"], "gray")
        meta = metas[0]
        mode = annotator.LABEL_TO_MODE.get(self.mode_var.get(), "interpret")
        self._set_buttons_busy(True)
        self._run_worker(lambda: self._task_annotate(meta, mode))

    def _on_chat(self):
        metas = self._check_ready()
        if not metas:
            return
        meta = metas[0]
        for win in self.root.winfo_children():
            if isinstance(win, ChatWindow) and win.doc_id == meta["id"]:
                win.lift()
                win.focus_force()
                return
        self._set_status("正在准备对话……", "gray")
        self._run_worker(lambda: self._task_open_chat(meta))

    def _on_write_back(self):
        title = self.title_var.get().strip()
        if not title:
            self._set_status("标题不能为空。", "red")
            return
        if not self.last_source_ids:
            self._set_status("找不到源文档位置，请重新生成后再写回。", "red")
            return

        if self.preview_kind == "annotate":
            content, fmt = self.pending_html, "html"
            question = f"在妙笔中新建批注文档「{title}」？\n（原文档不会被修改）"
        else:
            content = self.preview.get("1.0", "end").strip()
            fmt = "markdown"
            question = f"在妙笔中新建文档「{title}」？\n（原文档不会被修改）"

        if not content:
            self._set_status("没有可写回的内容。", "red")
            return
        if not messagebox.askyesno("写回妙笔", question):
            return
        self.write_btn.configure(state="disabled")
        self._run_worker(lambda: self._task_write_back(title, content, self.last_source_ids[-1], fmt))

    def _set_buttons_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.polish_btn.configure(state=state)
        self.annotate_btn.configure(state=state)

    # ------------------------------------------------------------------ 设置对话框

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.resizable(False, False)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        fields = [
            ("妙笔 API 端口", "wonderpen_port", "8022"),
            ("妙笔 API Token", "wonderpen_token", "wp-…（妙笔 设置 → 高级 → API）"),
            ("智谱 API Key", "glm_api_key", "open.bigmodel.cn 申请"),
            ("模型", "glm_model", "glm-5.2 / glm-5.3 等"),
        ]
        vars = {}
        for i, (label, key, hint) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=5, padx=(0, 10))
            var = tk.StringVar(value=str(self.config.get(key, "")))
            ttk.Entry(frame, textvariable=var, width=46).grid(row=i, column=1, pady=5)
            ttk.Label(frame, text=hint, foreground="gray").grid(row=i, column=2, sticky="w", padx=6)
            vars[key] = var

        def save():
            for key, var in vars.items():
                value = var.get().strip()
                if key == "wonderpen_port":
                    try:
                        value = int(value)
                    except ValueError:
                        messagebox.showerror("设置", "端口必须是数字，如 8022", parent=win)
                        return
                self.config[key] = value
            save_config(self.config)
            win.destroy()
            self._run_worker(self._task_connect)

        btns = ttk.Frame(frame)
        btns.grid(row=len(fields), column=0, columnspan=3, pady=(14, 0))
        ttk.Button(btns, text="保存并连接", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=4)

    # ------------------------------------------------------------------ 队列回调（主线程）

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                handler = getattr(self, f"_on_q_{kind}")
                handler(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _set_status(self, text, color="gray"):
        self.status_label.configure(text=text, foreground=color)

    def _on_q_status(self, text):
        self._set_status(text, "#444")

    def _on_q_error(self, text):
        self._set_status(text, "red")
        self._set_buttons_busy(False)
        self.write_btn.configure(
            state="normal" if (self.polished and self.last_source_ids) else "disabled"
        )
        messagebox.showerror("出错了", text)

    def _on_q_connected(self, _):
        self.dot.configure(foreground="#2e9e44")
        self.conn_label.configure(text=f" 已连接：{self.lib_title} 库（{len(self.items_by_id)} 篇文档）")
        self._render_tree()
        self._set_status("已连接。左侧选随笔 → 「梳理成散文」/「AI 批注」/「对话」。", "gray")

    def _render_tree(self):
        self.tree.delete(*self.tree.get_children())

        def insert(nodes, parent=""):
            for node in nodes:
                title = node.get("rendered_title") or node.get("title", "（无标题）")
                self.tree.insert(parent, "end", iid=node["id"], text=" 📄 " + title)
                insert(node.get("children", []), node["id"])

        insert(self.tree_items)
        # 展开全部节点
        for iid in self.items_by_id:
            if self.tree.exists(iid) and self.tree.get_children(iid):
                self.tree.item(iid, open=True)

    def _on_q_polished(self, payload):
        docs, text = payload["docs"], payload["text"]
        self.last_source_ids = [d["id"] for d in docs]
        self.title_var.set(polisher.suggest_title(docs))
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.polished = True
        self.preview_kind = "prose"
        self._set_buttons_busy(False)
        self.write_btn.configure(state="normal")
        self._set_status(
            "梳理完成，下方可自由修改。满意后点「写回妙笔」。", "#2e9e44"
        )

    def _on_q_annotated(self, payload):
        meta = payload["meta"]
        self.last_source_ids = [meta["id"]]
        self.title_var.set(f"{meta['title']} · 批注")
        self.pending_html = payload["html"]
        self.preview_kind = "annotate"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", payload["preview"])
        self.preview.configure(state="disabled")  # 预览只读；写回妙笔后可在妙笔里改
        self.polished = True
        self._set_buttons_busy(False)
        self.write_btn.configure(state="normal")
        n = payload["count"]
        if n:
            self._set_status(
                f"批注完成（{payload['mode']}，共 {n} 条）。点「写回妙笔」生成《{meta['title']} · 批注》，"
                "批注会带下划线。",
                "#2e9e44",
            )
        else:
            self._set_status(
                "AI 这次没按段落返回批注，已把它的整体点评附在文末。可再试一次或直接写回。", "#b3660a"
            )

    def _on_q_chat_ready(self, payload):
        ChatWindow(self, payload["meta"], payload["content"], payload["selection"])
        if payload["selection"]:
            self._set_status(f"对话已打开（附上了你在妙笔里选中的片段）。", "gray")
        else:
            self._set_status(f"对话已打开：《{payload['meta']['title']}》。", "gray")

    def _on_q_chat_exported(self, chat_title):
        self._run_worker(self._task_connect)  # 刷新树，让新文档出现
        self._set_status(f"对话已写入妙笔：《{chat_title}》（再次导出会追加到同一篇）。", "#2e9e44")

    def _on_q_written(self, title):
        self._render_tree()
        self.write_btn.configure(state="normal")
        if self.preview_kind == "annotate":
            self._set_status(
                f"已写入妙笔：《{title}》。原文下面带下划线的，就是 AI 批注。", "#2e9e44"
            )
        else:
            self._set_status(f"已写入妙笔：《{title}》。可以继续选下一篇随笔。", "#2e9e44")


class ChatWindow(tk.Toplevel):
    """围绕一篇文章的对话窗。AI 回复可一键导出到妙笔《原题 · 对话》。"""

    def __init__(self, app: App, meta: dict, content: str, selection: str):
        super().__init__(app.root)
        self.app = app
        self.doc_id = meta["id"]
        self.doc_title = meta["title"]
        self.title(f"对话：{self.doc_title}")
        self.geometry("560x620")
        self.minsize(460, 480)

        self.messages = [
            {
                "role": "system",
                "content": annotator.chat_system_prompt(self.doc_title, content, selection),
            }
        ]
        self.busy = False
        self.q: queue.Queue = queue.Queue()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        hello = (
            f"已就绪，聊《{self.doc_title}》吧。"
            + ("你在妙笔里选中的那段我也看到了，可以直接问它。" if selection else "")
            + "\n（Enter 发送，Shift+Enter 换行；聊完可点「导出妙笔」保存整段对话）"
        )
        self._append("ai", hello)
        self.after(120, self._poll)

    def _build_ui(self):
        self.chat_view = scrolledtext.ScrolledText(self, wrap="word", font=TEXT_FONT, state="disabled")
        self.chat_view.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self.chat_view.tag_configure("user_tag", foreground="#1a5fb4")
        self.chat_view.tag_configure("ai_tag", foreground="#33302a")
        self.chat_view.tag_configure("sys_tag", foreground="#8d8577")

        input_row = ttk.Frame(self, padding=(10, 0, 10, 4))
        input_row.pack(fill="x")
        input_row.columnconfigure(0, weight=1)
        self.input_box = tk.Text(input_row, font=TEXT_FONT, height=4, wrap="word", undo=True)
        self.input_box.grid(row=0, column=0, sticky="ew")
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Shift-Return>", lambda e: None)  # 放行默认换行

        btn_col = ttk.Frame(input_row)
        btn_col.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.send_btn = ttk.Button(btn_col, text="发送", command=self._on_send)
        self.send_btn.pack(fill="x", pady=(0, 4))
        self.export_btn = ttk.Button(btn_col, text="导出妙笔", command=self._on_export, state="disabled")
        self.export_btn.pack(fill="x")

        self.chat_status = ttk.Label(self, text="", foreground="gray", anchor="w", padding=(10, 0, 8, 6))
        self.chat_status.pack(fill="x")

    # ------------------------------------------------------------------ 聊天逻辑

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ai":
                    self._append("ai", payload)
                    self._set_busy(False)
                elif kind == "err":
                    self._append("sys", f"出错了：{payload}")
                    self._set_busy(False)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _on_enter(self, event):
        self._on_send()
        return "break"  # 阻止回车换行

    def _on_send(self):
        if self.busy:
            return
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return
        self.input_box.delete("1.0", "end")
        self._append("user", text)
        self.messages.append({"role": "user", "content": text})
        self._set_busy(True)

        messages = list(self.messages)

        def worker():
            try:
                reply = self.app._make_glm().chat_messages(messages, temperature=0.7)
                self.messages.append({"role": "assistant", "content": reply})
                self.q.put(("ai", reply))
            except (GLMError, WonderPenError) as e:
                self.q.put(("err", str(e)))
            except Exception as e:
                self.q.put(("err", repr(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_export(self):
        dialog = self._dialog_text()
        if not dialog.strip():
            return
        meta = {"id": self.doc_id, "title": self.doc_title}
        self.export_btn.configure(state="disabled")
        self.app._run_worker(lambda: self.app._task_export_chat(meta, dialog))

    def _dialog_text(self) -> str:
        """把本次对话拼成导出用的 Markdown（不含开场白）。"""
        lines = []
        for m in self.messages:
            if m["role"] == "user":
                lines.append(f"**我**：{m['content']}")
            elif m["role"] == "assistant" and not m.get("hello"):
                lines.append(f"**AI**：{m['content']}")
        return "\n\n".join(lines)

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.send_btn.configure(state="disabled" if busy else "normal")
        self.chat_status.configure(text="AI 正在回复……" if busy else "")
        if not busy and any(m["role"] == "user" for m in self.messages):
            self.export_btn.configure(state="normal")

    def _append(self, who: str, text: str):
        tag = {"user": "user_tag", "ai": "ai_tag", "sys": "sys_tag"}[who]
        name = {"user": "我", "ai": "AI", "sys": "·"}[who]
        self.chat_view.configure(state="normal")
        if self.chat_view.index("end-1c") != "1.0":
            self.chat_view.insert("end", "\n\n")
        self.chat_view.insert("end", f"{name}：{text}", tag)
        self.chat_view.configure(state="disabled")
        self.chat_view.see("end")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
