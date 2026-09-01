#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClipNote - 智能剪贴板历史管理器
================================

一个零第三方依赖的剪贴板历史工具，基于 Python 标准库 (tkinter + sqlite3) 编写。

功能：
  * 后台自动监听剪贴板，复制过的文本自动入历史
  * 相同内容自动去重（合并并刷新时间，避免刷屏）
  * 主窗口列表展示历史，双击 / 选中后回车一键复制回剪贴板
  * 实时搜索过滤（不区分大小写）
  * 星标收藏重要内容，可切换"只看收藏"
  * 单条删除 / 一键清空
  * 数据持久化到 SQLite，重启不丢
  * 导出全部历史为 JSON 备份

运行方式：
    python clipnote.py            # 启动图形界面
    python clipnote.py --selftest # 无界面自测（数据库 / 去重 / 搜索逻辑）

平台：Windows / macOS / Linux（需自带 Tk 的 Python 3.8+）
数据目录：~/.clipnote/clipnote.db
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

# tkinter 延迟导入：保证 --selftest 在无 Tk 的 Python 环境也能运行
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    HAS_TK = True
except ImportError:
    tk = ttk = messagebox = None
    HAS_TK = False

APP_NAME = "ClipNote"
APP_VERSION = "1.0.0"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".clipnote")
DB_PATH = os.path.join(DATA_DIR, "clipnote.db")
EXPORT_PATH = os.path.join(DATA_DIR, "clipnote_export.json")

POLL_INTERVAL_MS = 800       # 剪贴板轮询间隔（毫秒）
MAX_HISTORY = 500            # 最多保留多少条历史
MAX_ITEM_CHARS = 2000        # 单条内容最多保存多少字符（超长截断）
PREVIEW_CHARS = 80           # 列表中预览文本的长度
DEDUP_WINDOW_SECONDS = 30    # 去重时间窗口：窗口内相同内容视为一次


def now_str() -> str:
    """当前时间字符串，用于存储。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 数据层：SQLite
# ---------------------------------------------------------------------------

class HistoryDB:
    """剪贴板历史存储，负责建库、增删查、去重。"""

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT NOT NULL,
                starred    INTEGER NOT NULL DEFAULT 0,
                source     TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_updated ON items(updated_at)")
        self.conn.commit()

    def add(self, content: str, source: str = "") -> bool:
        """
        新增一条历史。若相同内容在去重窗口内已存在，则刷新其时间并返回 False（未新增）。
        超长内容按 MAX_ITEM_CHARS 截断。
        """
        content = content.strip()
        if not content:
            return False
        if len(content) > MAX_ITEM_CHARS:
            content = content[:MAX_ITEM_CHARS]

        now = now_str()
        row = self.conn.execute(
            "SELECT id, updated_at FROM items WHERE content = ? ORDER BY updated_at DESC LIMIT 1",
            (content,),
        ).fetchone()

        if row:
            # 时间窗口内重复 -> 合并，只刷新时间戳
            try:
                prev_ts = time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                prev_ts = 0
            try:
                cur_ts = time.mktime(time.strptime(now, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                cur_ts = time.time()
            if cur_ts - prev_ts < DEDUP_WINDOW_SECONDS:
                self.conn.execute(
                    "UPDATE items SET updated_at = ? WHERE id = ?", (now, row[0])
                )
                self.conn.commit()
                return False
            # 超过窗口的重复 -> 更新并置顶
            self.conn.execute(
                "UPDATE items SET updated_at = ?, source = ? WHERE id = ?",
                (now, source, row[0]),
            )
            self.conn.commit()
            return True

        self.conn.execute(
            "INSERT INTO items (content, starred, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (content, 0, source, now, now),
        )
        self.conn.commit()

        # 超过上限则删除最旧的（保留星标项）
        excess = self.conn.execute(
            "SELECT COUNT(*) - ? FROM items", (MAX_HISTORY,)
        ).fetchone()[0]
        if excess > 0:
            self.conn.execute(
                """
                DELETE FROM items WHERE starred = 0 AND id IN (
                    SELECT id FROM items WHERE starred = 0
                    ORDER BY updated_at ASC LIMIT ?
                )
                """,
                (excess,),
            )
            self.conn.commit()
        return True

    def all(self, starred_only: bool = False) -> list:
        """取历史，最新在前。可选只看收藏。"""
        sql = "SELECT id, content, starred, source, created_at, updated_at FROM items"
        if starred_only:
            sql += " WHERE starred = 1"
        sql += " ORDER BY updated_at DESC"
        return self.conn.execute(sql).fetchall()

    def search(self, keyword: str, starred_only: bool = False) -> list:
        """按关键字过滤，最新在前。"""
        sql = "SELECT id, content, starred, source, created_at, updated_at FROM items WHERE content LIKE ?"
        params: list = [f"%{keyword}%"]
        if starred_only:
            sql += " AND starred = 1"
        sql += " ORDER BY updated_at DESC"
        return self.conn.execute(sql, params).fetchall()

    def toggle_star(self, item_id: int) -> int:
        """切换星标状态，返回新状态（0/1）。"""
        cur = self.conn.execute(
            "SELECT starred FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if cur is None:
            return 0
        new = 0 if cur[0] else 1
        self.conn.execute(
            "UPDATE items SET starred = ?, updated_at = ? WHERE id = ?",
            (new, now_str(), item_id),
        )
        self.conn.commit()
        return new

    def delete(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()

    def clear(self, keep_starred: bool = True) -> None:
        if keep_starred:
            self.conn.execute("DELETE FROM items WHERE starred = 0")
        else:
            self.conn.execute("DELETE FROM items")
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def export_json(self, path: str = EXPORT_PATH) -> str:
        rows = self.all()
        data = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "exported_at": now_str(),
            "count": len(rows),
            "items": [
                {
                    "id": r[0],
                    "content": r[1],
                    "starred": bool(r[2]),
                    "source": r[3],
                    "created_at": r[4],
                    "updated_at": r[5],
                }
                for r in rows
            ],
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

class ClipNoteApp:
    """主界面：搜索框 + 历史列表 + 操作按钮。"""

    def __init__(self, root, db: HistoryDB):
        self.root = root
        self.db = db
        self._last_clip = None          # 上次看到的剪贴板内容
        self._starred_only = False      # 是否只看收藏
        self._current_items = []        # 当前列表展示的行 (id, content, starred, ...)
        self._polling = True

        root.title(f"{APP_NAME} v{APP_VERSION} - 剪贴板历史")
        root.geometry("720x520")
        root.minsize(560, 360)

        # 样式
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._build_toolbar()
        self._build_list()
        self._build_statusbar()

        # 快捷键
        root.bind("<Control-f>", lambda e: self.search_var.focus_set())
        root.bind("<Control-l>", lambda e: self.clear_search())
        root.bind("<Return>", lambda e: self.copy_selected())
        root.bind("<Delete>", lambda e: self.delete_selected())
        root.bind("<Control-a>", lambda e: self.toggle_star_selected())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 开始轮询剪贴板
        self.refresh_list()
        self._poll_clipboard()

    # ---- 界面构建 ----

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(side=tk.TOP, fill=tk.X)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh_list())
        search_entry = ttk.Entry(bar, textvariable=self.search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        search_entry.insert(0, "")
        search_entry.config(width=1)  # 交给 pack 控制宽度

        ttk.Button(bar, text="搜索", command=self.search_var.focus_set).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        self.star_btn = ttk.Button(bar, text="只看收藏", command=self.toggle_star_filter)
        self.star_btn.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar, text="复制", command=self.copy_selected).pack(side=tk.LEFT)
        ttk.Button(bar, text="收藏", command=self.toggle_star_selected).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="删除", command=self.delete_selected).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="清空", command=self.clear_history).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(bar, text="导出", command=self.export_history).pack(side=tk.LEFT, padx=(4, 0))

    def _build_list(self):
        frame = ttk.Frame(self.root, padding=(8, 4, 8, 4))
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        columns = ("content", "time", "star")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("content", text="内容", anchor=tk.W)
        self.tree.heading("time", text="时间", anchor=tk.W)
        self.tree.heading("star", text="★", anchor=tk.CENTER)
        self.tree.column("content", width=460, anchor=tk.W)
        self.tree.column("time", width=150, anchor=tk.W, stretch=False)
        self.tree.column("star", width=40, anchor=tk.CENTER, stretch=False)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", lambda e: self.copy_selected())

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(bar, text=f"数据: {DB_PATH}").pack(side=tk.RIGHT)

    # ---- 剪贴板监听 ----

    def _poll_clipboard(self):
        """主线程轮询剪贴板，有变化则入库并刷新列表。"""
        if not self._polling:
            return
        try:
            content = self.root.clipboard_get()
        except tk.TclError:
            content = None  # 剪贴板为空或包含非文本数据

        if content and content != self._last_clip:
            self._last_clip = content
            self.db.add(content)
            if self._starred_only is False:
                self.refresh_list()
        elif not content:
            self._last_clip = None

        self.root.after(POLL_INTERVAL_MS, self._poll_clipboard)

    # ---- 列表刷新 ----

    def refresh_list(self):
        keyword = self.search_var.get().strip()
        if keyword:
            rows = self.db.search(keyword, self._starred_only)
        else:
            rows = self.db.all(self._starred_only)
        self._current_items = rows

        # 记住当前选中项
        selected = self.tree.selection()
        sel_id = None
        if selected:
            try:
                sel_id = self.tree.item(selected[0], "values")[3]
            except (IndexError, tk.TclError):
                sel_id = None

        self.tree.delete(*self.tree.get_children())
        for r in rows:
            item_id, content, starred, source, created_at, updated_at = r
            preview = content.replace("\n", " ").replace("\r", " ")
            if len(preview) > PREVIEW_CHARS:
                preview = preview[:PREVIEW_CHARS] + "…"
            star = "★" if starred else " "
            self.tree.insert(
                "", tk.END,
                values=(preview, updated_at, star, item_id),
            )

        # 恢复选中
        if sel_id is not None:
            for child in self.tree.get_children():
                if self.tree.item(child, "values")[3] == sel_id:
                    self.tree.selection_set(child)
                    self.tree.see(child)
                    break

        self.status_var.set(f"共 {len(rows)} 条{'（已过滤）' if keyword or self._starred_only else ''}")

    # ---- 操作 ----

    def _selected_row(self):
        selected = self.tree.selection()
        if not selected:
            return None
        try:
            vals = self.tree.item(selected[0], "values")
            return int(vals[3])
        except (IndexError, ValueError):
            return None

    def copy_selected(self):
        item_id = self._selected_row()
        if item_id is None:
            return
        row = self.db.conn.execute(
            "SELECT content FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(row[0])
        self.root.update()  # 保证剪贴板立即生效
        self.status_var.set("已复制到剪贴板 ✓")

    def toggle_star_selected(self):
        item_id = self._selected_row()
        if item_id is None:
            return
        new = self.db.toggle_star(item_id)
        self.status_var.set("已收藏 ★" if new else "已取消收藏")
        self.refresh_list()

    def delete_selected(self):
        item_id = self._selected_row()
        if item_id is None:
            return
        if messagebox.askyesno(APP_NAME, "确定删除这条历史吗？"):
            self.db.delete(item_id)
            self.refresh_list()

    def clear_history(self):
        keep = messagebox.askyesno(
            APP_NAME, "清空全部历史？\n\n选择「是」= 保留收藏项\n选择「否」= 连收藏一起清空"
        )
        if keep is None:
            return
        self.db.clear(keep_starred=keep)
        self.refresh_list()
        self.status_var.set("历史已清空")

    def export_history(self):
        path = self.db.export_json()
        self.status_var.set(f"已导出到 {path}")
        messagebox.showinfo(APP_NAME, f"导出成功：\n{path}")

    def toggle_star_filter(self):
        self._starred_only = not self._starred_only
        self.star_btn.config(text="只看收藏 ✓" if self._starred_only else "只看收藏")
        self.refresh_list()

    def clear_search(self):
        self.search_var.set("")
        self.refresh_list()

    # ---- 生命周期 ----

    def on_close(self):
        self._polling = False
        self.db.close()
        self.root.destroy()


# ---------------------------------------------------------------------------
# 无界面自测
# ---------------------------------------------------------------------------

def run_selftest() -> int:
    """headless 自测：验证数据库 / 去重 / 搜索 / 收藏逻辑，不启动 GUI。"""
    import tempfile

    print(f"{APP_NAME} v{APP_VERSION} 自测开始...")
    tmp = tempfile.mkdtemp(prefix="clipnote_test_")
    db = HistoryDB(os.path.join(tmp, "test.db"))
    failures = 0

    def check(name, cond):
        nonlocal failures
        status = "PASS" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"  [{status}] {name}")

    # 1. 空库
    check("初始历史为空", db.count() == 0)

    # 2. 添加
    db.add("hello world")
    check("添加后为 1 条", db.count() == 1)

    # 3. 去重窗口内相同内容不新增
    db.add("hello world")
    check("窗口内重复合并为 1 条", db.count() == 1)

    # 4. 不同内容新增
    db.add("第二段内容")
    db.add("第三段内容")
    check("添加不同内容后为 3 条", db.count() == 3)

    # 5. 空内容 / 纯空白被忽略
    db.add("   ")
    db.add("")
    check("空白内容被忽略", db.count() == 3)

    # 6. 搜索
    found = db.search("第二")
    check("搜索『第二』命中 1 条", len(found) == 1 and "第二段" in found[0][1])

    # 7. 收藏
    target = db.all()[0][0]
    new = db.toggle_star(target)
    check("收藏切换为 1", new == 1)
    starred = db.all(starred_only=True)
    check("只看收藏命中 1 条", len(starred) == 1 and starred[0][0] == target)

    # 8. 删除
    db.delete(target)
    check("删除后为 2 条", db.count() == 2)

    # 9. 导出
    path = db.export_json(os.path.join(tmp, "out.json"))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    check("导出 JSON 条数正确", data["count"] == db.count())

    # 10. 超长截断
    db.add("A" * 5000)
    rows = db.all()
    check("超长内容被截断", len(rows[0][1]) <= MAX_ITEM_CHARS)

    db.close()
    print(f"\n结果: {'全部通过 ✓' if failures == 0 else f'{failures} 项失败 ✗'}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - 智能剪贴板历史管理器")
    parser.add_argument("--selftest", action="store_true", help="运行无界面自测")
    parser.add_argument("--db", default=DB_PATH, help="自定义数据库路径")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(run_selftest())

    if not HAS_TK:
        print(
            "错误：当前 Python 环境缺少 tkinter，无法启动图形界面。\n"
            "请使用自带 Tk 的 Python（如 python.org 官方安装包）运行，"
            "或先执行 --selftest 验证逻辑。",
            file=sys.stderr,
        )
        sys.exit(2)

    root = tk.Tk()
    db = HistoryDB(args.db)
    ClipNoteApp(root, db)
    root.mainloop()


if __name__ == "__main__":
    main()
