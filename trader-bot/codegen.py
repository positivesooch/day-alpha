#!/usr/bin/env python3
"""Day Trader Code: the separate window that makes the code for Day Trader Bot.

    python3 trader-bot/codegen.py
    python3 trader-bot/gui.py --codegen
    DayTraderBot.exe --codegen

The code is saved in the bot's data folder and shown here. Type it into
Day Trader Bot to open that window. It expires after 15 minutes. It is
not a broker password. Standard library only.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import access  # noqa: E402


def main(home=None):
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("Day Trader Code needs tkinter (python3-tk on Debian/Ubuntu).", file=sys.stderr)
        return 1
    import bot
    home = os.path.abspath(home or bot.default_home())
    os.makedirs(os.path.join(home, "trader", "state"), exist_ok=True)

    root = tk.Tk()
    root.title("Day Trader Code")
    root.minsize(420, 320)
    root.configure(background="#e8edf2")
    card = tk.Frame(root, background="#ffffff", padx=28, pady=22,
                    highlightthickness=1, highlightbackground="#c5ced6")
    card.place(relx=0.5, rely=0.5, anchor="center")

    tk.Label(card, text="Day Trader Code", background="#ffffff",
             font=("TkDefaultFont", 16, "bold")).pack()
    tk.Label(card, text="Type this code into Day Trader Bot to open it.\n"
                        "It is not your broker password. A new code replaces this one.",
             background="#ffffff", justify="center").pack(pady=(8, 12))
    code_var = tk.StringVar(value="------")
    tk.Label(card, textvariable=code_var, background="#ffffff",
             font=("TkFixedFont", 28, "bold"), fg="#0a58ca").pack()
    left_var = tk.StringVar()
    tk.Label(card, textvariable=left_var, background="#ffffff", fg="#555").pack(pady=(6, 10))

    def show(doc, left):
        code_var.set(doc["code"] if doc and left > 0 else "------")
        if left > 0:
            mins, secs = divmod(left, 60)
            left_var.set("Valid for %d:%02d. Then press Generate." % (mins, secs))
        else:
            left_var.set("No current code. Press Generate.")

    def refresh():
        doc, left = access.status(home)
        show(doc, left)
        if root.winfo_exists():
            root.after(1000, refresh)

    def generate():
        doc = access.issue(home)
        show(doc, access.TTL_SECONDS)

    def copy_code():
        code = code_var.get()
        if not code or code == "------":
            return
        root.clipboard_clear()
        root.clipboard_append(code)

    row = ttk.Frame(card)
    row.pack(pady=(4, 0))
    ttk.Button(row, text="Generate", command=generate).pack(side="left", padx=(0, 8))
    ttk.Button(row, text="Copy", command=copy_code).pack(side="left")
    doc, left = access.status(home)
    if left <= 0:
        generate()
    else:
        show(doc, left)
    root.after(1000, refresh)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
