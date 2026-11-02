"""自测用的假界面窗口：标题里带「灵犀」，用来验证 close_ui.py 的关闭逻辑。

用法：python run/ui_window.py <后缀> normal|stubborn
  normal   —— 收到 WM_CLOSE 就正常退出
  stubborn —— 故意不理 WM_CLOSE，用来验证强杀兜底路径
"""
from __future__ import annotations

import sys
import tkinter as tk

suffix = sys.argv[1] if len(sys.argv) > 1 else "自测窗口"
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"

root = tk.Tk()
root.title("宇之语 · 灵犀 · " + suffix)
root.geometry("320x120")
if mode == "stubborn":
    # 假装有未保存内容：忽略关闭请求
    root.protocol("WM_DELETE_WINDOW", lambda: None)
root.after(30000, root.destroy)          # 兜底：30 秒后自己消失，别留垃圾
root.mainloop()
