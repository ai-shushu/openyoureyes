"""有手机在线时不让电脑自动睡眠（睡了手机就全断了）；没有手机在线就恢复正常。只防「闲置睡眠」，合盖、手动睡眠照常。

  macOS   caffeinate -i（系统自带）
  Windows SetThreadExecutionState(ES_SYSTEM_REQUIRED)
  Linux   systemd-inhibit（有就用）
"""
import os
import subprocess
import sys
import threading

_lock = threading.Lock()
_state = {"on": False, "proc": None}


def hold(on):
    """on=True 阻止闲置睡眠；False 恢复。重复调用无副作用。"""
    if os.environ.get("T9_NO_POWER") == "1":
        return
    with _lock:
        if on == _state["on"]:
            return
        _state["on"] = on
        try:
            if os.name == "nt":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                # 这个设置跟着线程走：由同一个后台线程反复调用（见 serve.py 的 _power_loop）
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
                return
            p = _state["proc"]
            if p and p.poll() is None:
                p.terminate()
            _state["proc"] = None
            if not on:
                return
            pid = str(os.getpid())
            if sys.platform == "darwin":
                cmd = ["caffeinate", "-i", "-w", pid]
            else:
                cmd = ["systemd-inhibit", "--what=idle:sleep", "--who=Open Your Eyes", "--why=手机在线", "sleep", "infinity"]
            _state["proc"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass   # 没有对应工具就算了，不影响拍照
