"""Memory monitoring helpers extracted from power_flow_fixed.ipynb cell 1."""

import os, time, threading, psutil
import matplotlib.pyplot as plt

_mem_ts = []
_mem_mb = []
_monitor_flag = {"running": False}

def _total_rss_mb():
    p = psutil.Process(os.getpid())
    total = p.memory_info().rss
    for child in p.children(recursive=True):
        try:
            total += child.memory_info().rss
        except Exception:
            pass
    return total / 1024 / 1024

def _monitor_memory(interval=0.5):
    t0 = time.time()
    while _monitor_flag["running"]:
        _mem_ts.append(time.time() - t0)
        _mem_mb.append(_total_rss_mb())
        time.sleep(interval)

def start_memory_monitor():
    _mem_ts.clear()
    _mem_mb.clear()
    _monitor_flag["running"] = True
    th = threading.Thread(target=_monitor_memory, daemon=True)
    th.start()
    return th

def stop_memory_monitor(th):
    _monitor_flag["running"] = False
    th.join(timeout=2)

def plot_memory_curve(title="长时序连续运行内存监控结果"):
    plt.figure(figsize=(10, 5))
    plt.plot(_mem_ts, _mem_mb, linewidth=2)
    plt.xlabel("运行时长 / s")
    plt.ylabel("总内存占用 / MB")
    plt.title(title)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.show()


__all__ = [name for name in globals() if not name.startswith("__")]
