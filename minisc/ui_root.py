"""Общий скрытый Tk-корень для всплывающих окон (громкость, перемотка).

Каждое окно раньше создавало свой собственный `tk.Tk()` в отдельном потоке.
Tkinter не рассчитан на несколько параллельно работающих корней/mainloop'ов в
одном процессе — когда два таких окна открыты одновременно, они делят общее
состояние интерпретатора Tcl, и это ломает вещи вроде обновления `StringVar`
на экране во втором окне. Поэтому все окна теперь — `Toplevel` одного и того
же скрытого корня, живущего в одном выделенном потоке.
"""

import threading
import tkinter as tk
from typing import Callable

_root: 'tk.Tk | None' = None
_lock = threading.Lock()


def _ensure_root() -> tk.Tk:
    global _root
    with _lock:
        if _root is not None:
            return _root

        ready = threading.Event()
        holder: dict = {}

        def start() -> None:
            root = tk.Tk()
            root.withdraw()
            holder["root"] = root
            ready.set()
            root.mainloop()

        threading.Thread(target=start, daemon=True, name="minisc-ui").start()
        ready.wait()
        _root = holder["root"]
        return _root


def open_window(build: Callable[[tk.Tk], None]) -> None:
    """Планирует создание окна (через `build(root)`) на общем UI-потоке."""
    root = _ensure_root()
    root.after(0, build, root)
