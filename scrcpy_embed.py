# -*- coding: utf-8 -*-
"""
scrcpy_embed.py —— 多设备 scrcpy 镜像嵌入 PyQt5 窗口显示（Windows）

功能特性
--------
1. 「一键投屏」按钮：枚举当前所有设备（USB + 无线），全部用
   scrcpy -s <设备号> 启动，每 3 秒启动 1 台（无线设备 serial 为
   IP:端口，同样用 -s 选择）；「连接」「配对」按钮保持可点击；
2. 画面按「每行 5 个」的网格排列，每台默认固定为 330x720 竖屏，外围灰色
   边框、画面贴合边框输出（超出窗口部分滚动查看）；勾选「横屏」后按
   720x330 横屏显示；
3. 无线投屏：无线IP / 端口 / 配对端口 三个输入框分开填写；
   「连接」= adb connect IP:端口；「配对」= adb pair IP:配对端口
   （配对码弹框输入，配对成功后把端口填为无线调试端口再点「连接」）；
4. 勾选「联动操作」后，鼠标在第一个画面上的点击 / 拖拽会按相对位置
   同步到其他所有设备（从第 1 台 → 其他台）；其他设备上的操作只作用于
   本设备，不会反向同步；
5. 提供声音开关、设备刷新、运行日志、意外断连提示。

运行环境
--------
* Windows 10/11，Python 3.8+
* 依赖：pip install PyQt5
* 手机开启 USB 调试（adb devices 可见）

使用方法
--------
python scrcpy_embed.py

原理简述
--------
每个设备启动一个独立 scrcpy 子进程（--serial=<设备号>，窗口标题唯一），
用 FindWindowW 找到各自窗口后 SetParent 嵌入对应的 PyQt 容器；
容器缩放时 MoveWindow 同步尺寸。

联动原理：用轮询（GetAsyncKeyState + GetCursorPos）监听第一个嵌入窗口
上的鼠标按下 / 抬起，换算成相对坐标后，通过 adb shell input（tap/swipe）
把点击与拖拽注入到其他设备。不直接用 PostMessage 往 scrcpy 窗口注入鼠标
消息——实测目标 scrcpy 进程会除零崩溃（exit=0xC0000094）。

注意：
* 本机 PyQt5 5.15 + Python 3.14 组合下，控件样式表/调色板与事件覆盖叠加
  会触发栈溢出崩溃（0xC0000409），因此代码中一律使用默认样式；
* 不要对嵌入的 scrcpy 窗口调用 SetWindowLong 改样式（实测崩溃）。
"""

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

if os.name != "nt":
    sys.exit("本示例仅支持 Windows（依赖 Win32 API SetParent 嵌入窗口）。")

# scrcpy 安装目录（默认取你本机的路径，界面里也可以改）
DEFAULT_SCRCPY_DIR = r"./scrcpy"
# 无线投屏默认手机 IP（端口 / 配对端口随手机无线调试会话变化，界面可改）
DEFAULT_WIFI_IP = ""
DEFAULT_WIFI_PORT = ""
DEFAULT_WIFI_PAIR_PORT = ""
# 每行最多显示几个手机屏幕
GRID_COLUMNS = 5
# 每台设备画面的固定分辨率（宽 x 高，竖屏）
CELL_WIDTH = 450
CELL_HEIGHT = 1000
# 横屏分辨率（勾选「横屏」复选框时使用）
LANDSCAPE_WIDTH = 825
LANDSCAPE_HEIGHT = 370
# 每台设备外围的边框：灰色，用 QPainter 原生绘制（不使用 QSS/调色板，
# 本机 PyQt5 5.15 + Python 3.14 组合下这些会触发栈溢出崩溃）
FRAME_LINE = 2        # 边框线宽（像素）
FRAME_MARGIN = 0      # 边框与画面之间的内边距（0 = 画面贴合边框）
FRAME_COLOR = (140, 140, 140)  # 灰色边框

# ------------------------- Win32 封装 -------------------------
user32 = ctypes.windll.user32

SW_HIDE = 0
SW_SHOW = 5
VK_LBUTTON = 0x01
MK_LBUTTON = 0x0001
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202

# 声明参数 / 返回值类型，避免 64 位下窗口句柄被截断
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SetParent.restype = wintypes.HWND
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.MoveWindow.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL,
]
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def find_window_by_title(title: str) -> int:
    """按完整标题查找顶层窗口，返回句柄（找不到返回 0）。"""
    return user32.FindWindowW(None, title) or 0


def set_parent(child: int, parent: int) -> None:
    """把 child 窗口过继给 parent 窗口。"""
    user32.SetParent(child, parent)


def move_window(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """移动并缩放窗口，宽高不小于 0。"""
    user32.MoveWindow(hwnd, x, y, max(w, 0), max(h, 0), True)


def show_window(hwnd: int, cmd: int) -> None:
    user32.ShowWindow(hwnd, cmd)


def get_client_rect_screen(hwnd: int):
    """返回窗口客户区在屏幕上的 (x, y, 宽, 高)。"""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, rect.right - rect.left, rect.bottom - rect.top


# ------------------------- 输出读取线程 -------------------------
class OutputReader(QThread):
    """后台读取 scrcpy 的 stdout / stderr，逐行通过信号发回主线程。"""

    line_ready = pyqtSignal(str)

    def __init__(self, stream, parent=None):
        super().__init__(parent)
        self._stream = stream

    def run(self) -> None:
        try:
            for raw in iter(self._stream.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.line_ready.emit(text)
        finally:
            self._stream.close()


# ------------------------- 灰色边框控件 -------------------------
class BorderWidget(QWidget):
    """带灰色边框的容器：用 QPainter 原生绘制，画面贴合边框。

    不用样式表 / 调色板（本机 PyQt5 5.15 + Python 3.14 组合下会触发
    栈溢出崩溃 0xC0000409），所以直接画一个矩形边框。
    """

    def __init__(self, color=FRAME_COLOR, width=FRAME_LINE, parent=None):
        super().__init__(parent)
        self._color = QColor(*color)
        self._width = width

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(self._color)
        pen.setWidth(self._width)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        # 边框画在控件最外缘（线宽 2 时覆盖 0-1 与 w-2..w-1 像素），
        # 容器内缩 FRAME_LINE 后四边边框都可见，画面贴紧边框
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        painter.end()


# ------------------------- 单设备嵌入容器 -------------------------
class ScrcpyContainer(QWidget):
    """把一台设备的 scrcpy 窗口嵌入进来的容器控件。"""

    log = pyqtSignal(str)      # 运行日志
    stopped = pyqtSignal()     # scrcpy 进程意外退出时发出（用于恢复按钮状态）

    def __init__(self, scrcpy_dir: str, window_title: str, serial: str = None,
                 landscape: bool = False, parent=None):
        super().__init__(parent)
        self.scrcpy_dir = scrcpy_dir
        self.window_title = window_title  # 该设备独有的窗口标题（用于 FindWindow）
        self.serial = serial
        self.landscape = landscape        # True=横屏 720x330，False=竖屏 330x720
        # 按横/竖屏选择显示尺寸
        self._w = LANDSCAPE_WIDTH if landscape else CELL_WIDTH
        self._h = LANDSCAPE_HEIGHT if landscape else CELL_HEIGHT
        self.process = None
        self.hwnd = 0
        self._readers = []

        self._hint = QLabel("正在连接设备…\n画面将嵌入到这里", self)
        self._hint.setAlignment(Qt.AlignCenter)

        # 轮询查找本设备的 scrcpy 窗口
        self._find_timer = QTimer(self)
        self._find_timer.setInterval(300)
        self._find_timer.timeout.connect(self._try_attach)

        # 监视进程是否意外退出（如拔掉数据线）
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(1000)
        self._watch_timer.timeout.connect(self._check_process)

        # 尺寸守护：SDL 会在视频尺寸确定后把窗口改成视频原始尺寸
        # （曾导致画面只占容器一部分、被裁切），这里每 500ms 把窗口
        # 纠正回容器尺寸，保证画面始终完整铺满。
        self._size_timer = QTimer(self)
        self._size_timer.setInterval(500)
        self._size_timer.timeout.connect(self._enforce_size)

        # 固定显示尺寸（竖屏 330x720 / 横屏 720x330），画面铺满容器
        self.setFixedSize(self._w, self._h)

    # ---------- 对外接口 ----------
    def start(self, audio=True) -> bool:
        """启动本设备的 scrcpy 子进程并开始寻找窗口。"""
        if self.is_running():
            return False
        exe = os.path.join(self.scrcpy_dir, "scrcpy.exe")
        if not os.path.isfile(exe):
            self.log.emit(f"[错误] 未找到 scrcpy.exe：{exe}")
            return False

        cmd = [
            exe,
            f"--window-title={self.window_title}",
            "--window-borderless",
            "--window-x=-10000",  # 先放到屏幕外，嵌入后由容器接管位置，避免闪现
            "--window-y=-10000",
        ]
        if self.serial:
            cmd.append(f"--serial={self.serial}")
        # 固定视频分辨率：长边最大 720（对更高分辨率的设备统一降采样）
        cmd.append(f"--max-size={max(self._w, self._h)}")
        # 让 scrcpy 按容器尺寸初始化自身窗口，渲染输出铺满整个窗口，
        # 避免视频只占窗口一角、其余区域显示未渲染的灰底
        cmd.append(f"--window-width={self._w}")
        cmd.append(f"--window-height={self._h}")
        if not audio:
            cmd.append("--no-audio")

        env = dict(os.environ)
        env["PATH"] = self.scrcpy_dir + os.pathsep + env.get("PATH", "")

        self.hwnd = 0
        self.process = subprocess.Popen(
            cmd,
            cwd=self.scrcpy_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        self._readers = [
            OutputReader(self.process.stdout),
            OutputReader(self.process.stderr),
        ]
        for reader in self._readers:
            reader.line_ready.connect(self.log)
            reader.start()

        self.log.emit(f"[启动] scrcpy pid={self.process.pid}，等待窗口出现…")
        self._find_timer.start()
        self._watch_timer.start()
        self._size_timer.start()
        return True

    def stop(self) -> None:
        """停止 scrcpy 并解除嵌入。"""
        self._find_timer.stop()
        self._watch_timer.stop()
        self._size_timer.stop()

        if self.process is not None:
            proc = self.process
            self.process = None
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        for reader in self._readers:
            reader.wait(500)
        self._readers = []

        if self.hwnd:
            try:
                show_window(self.hwnd, SW_HIDE)
                set_parent(self.hwnd, 0)
            except Exception:
                pass
            self.hwnd = 0

        self._hint.show()
        self.log.emit("[停止] scrcpy 已退出")

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    # ---------- 内部实现 ----------
    def _try_attach(self) -> None:
        """轮询查找本设备的 scrcpy 窗口并执行嵌入。"""
        if self.process is None:
            self._find_timer.stop()
            return

        # 进程已经退出（多半是设备没连上）
        if self.process.poll() is not None:
            self._handle_process_exit(self.process.poll())
            return

        hwnd = find_window_by_title(self.window_title)
        if not hwnd:
            return  # 窗口还没创建，继续等

        self._find_timer.stop()
        self.hwnd = hwnd

        # 经典嵌入三步：SetParent 过继 → MoveWindow 铺满 → ShowWindow 显示。
        # 注意：不要在此基础上再调用 SetWindowLong 修改子窗口样式（如改成
        # WS_CHILD）——实测会导致进程崩溃（0xC0000409）。保留原样式即可，
        # 输入、裁剪、缩放行为都正常。
        parent_hwnd = int(self.winId())  # 容器自身的原生窗口句柄
        set_parent(hwnd, parent_hwnd)
        self._relayout()
        show_window(hwnd, SW_SHOW)

        self._hint.hide()
        self.update()  # 立即刷新容器，清掉提示文字的残留像素
        self._enforce_size()  # 立刻纠正一次（SDL 可能随后再次自调尺寸）
        self.log.emit(f"[成功] 已嵌入 scrcpy 窗口（hwnd=0x{hwnd:x}）")

    def _check_process(self) -> None:
        """进程意外退出（拔线 / 崩溃）时清理状态。"""
        if self.process is not None and self.process.poll() is not None:
            self._handle_process_exit(self.process.poll())

    def _enforce_size(self) -> None:
        """尺寸守护：把 scrcpy 窗口强制纠正回容器尺寸。"""
        if not self.hwnd or self.process is None:
            return
        ratio = self.devicePixelRatioF()
        cw = int(self.width() * ratio)
        ch = int(self.height() * ratio)
        rect = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if abs(w - cw) > 2 or abs(h - ch) > 2:
            move_window(self.hwnd, 0, 0, cw, ch)
            self.update()  # 顺带刷新容器，清掉可能残留的旧像素

    def _handle_process_exit(self, code: int) -> None:
        self._find_timer.stop()
        self._watch_timer.stop()
        self._size_timer.stop()
        self.process = None
        self.hwnd = 0
        self._hint.show()
        self.log.emit(f"[提示] scrcpy 进程已退出（exit={code}），请检查设备连接。")
        self.stopped.emit()

    def _relayout(self) -> None:
        """让 scrcpy 窗口填满容器（Qt5 高 DPI 下逻辑尺寸要乘缩放比）。"""
        if not self.hwnd:
            return
        ratio = self.devicePixelRatioF()
        w = int(self.width() * ratio)
        h = int(self.height() * ratio)
        move_window(self.hwnd, 0, 0, w, h)

    # ---------- Qt 事件 ----------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._hint.setGeometry(0, 0, self.width(), self.height())
        self._relayout()

    def closeEvent(self, event) -> None:
        self.stop()
        super().closeEvent(event)


# ------------------------- 主窗口 -------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("scrcpy 多设备镜像")
        self.resize(1280, 820)

        self._devices = []          # 当前 adb 设备列表
        self.containers = []        # 每个设备一个容器
        self._active_count = 0      # 仍在运行的 scrcpy 数量
        self._drag = False          # 联动鼠标拖拽状态机
        self._drag_start = (0.0, 0.0)  # 联动按下时的相对坐标
        self._device_sizes = {}     # 设备物理分辨率缓存（wm size）

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---- 控制栏 ----
        bar = QHBoxLayout()

        bar.addWidget(QLabel("scrcpy 目录:"))
        self.path_edit = QLineEdit(DEFAULT_SCRCPY_DIR)
        self.path_edit.setMinimumWidth(220)
        bar.addWidget(self.path_edit)

        self.refresh_btn = QPushButton("刷新设备")
        bar.addWidget(self.refresh_btn)

        self.audio_check = QCheckBox("声音")
        self.audio_check.setChecked(True)
        bar.addWidget(self.audio_check)

        self.sync_check = QCheckBox("联动操作（第1台→其他）")
        self.sync_check.setChecked(True)
        bar.addWidget(self.sync_check)

        self.landscape_check = QCheckBox("横屏")  # 勾选=横屏 720x330，默认竖屏 330x720
        self.landscape_check.setChecked(False)
        bar.addWidget(self.landscape_check)

        # 一键投屏：全部设备逐台启动（每 3 秒 1 台，scrcpy -s <设备号>）
        self.all_btn = QPushButton("一键投屏")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        bar.addWidget(self.all_btn)
        bar.addWidget(self.stop_btn)

        self.devices_label = QLabel("设备: 0 台")
        bar.addWidget(self.devices_label)
        bar.addStretch(1)
        layout.addLayout(bar)

        # ---- 无线投屏栏 ----
        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("无线IP:"))
        self.ip_edit = QLineEdit(DEFAULT_WIFI_IP)  # 无线调试 IP
        self.ip_edit.setMinimumWidth(120)
        self.ip_edit.setPlaceholderText("如 192.168.21.84")
        bar2.addWidget(self.ip_edit)
        bar2.addWidget(QLabel("端口:"))
        self.port_edit = QLineEdit(DEFAULT_WIFI_PORT)  # 无线调试端口（连接用）
        self.port_edit.setMinimumWidth(80)
        self.port_edit.setPlaceholderText("无线调试端口")
        bar2.addWidget(self.port_edit)
        bar2.addWidget(QLabel("配对端口:"))
        self.pair_edit = QLineEdit(DEFAULT_WIFI_PAIR_PORT)  # 配对码端口（配对用）
        self.pair_edit.setMinimumWidth(80)
        self.pair_edit.setPlaceholderText("配对码端口")
        bar2.addWidget(self.pair_edit)
        self.connect_btn = QPushButton("连接")
        bar2.addWidget(self.connect_btn)
        self.pair_btn = QPushButton("配对")
        bar2.addWidget(self.pair_btn)
        self.wifi_hint = QLabel("连接=adb connect IP:端口；配对=adb pair IP:配对端口（弹框输配对码）")
        bar2.addWidget(self.wifi_hint)
        bar2.addStretch(1)
        layout.addLayout(bar2)

        # ---- 多屏网格（每行 GRID_COLUMNS 个） ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        grid_host = QWidget()
        self._grid = QGridLayout(grid_host)
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(6, 6, 6, 6)
        self._grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)  # 固定尺寸格子，左上对齐
        scroll.setWidget(grid_host)
        layout.addWidget(scroll, 1)

        # ---- 日志 ----
        layout.addWidget(QLabel("运行日志:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        layout.addWidget(self.log_view)

        # ---- 信号连接 ----
        self.refresh_btn.clicked.connect(self.refresh_devices)
        self.connect_btn.clicked.connect(self.on_wifi_connect)
        self.pair_btn.clicked.connect(self.on_pair)
        self.all_btn.clicked.connect(self.on_all_start)
        self.stop_btn.clicked.connect(self.on_stop)

        # 联动轮询（约 16ms 一次）
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(16)
        self._sync_timer.timeout.connect(self._sync_mouse)
        self._sync_timer.start()

        self.refresh_devices()

    # ---------- 设备枚举 ----------
    def _adb_path(self) -> str:
        """返回可用的 adb.exe 路径（优先取界面填写的 scrcpy 目录）。"""
        scrcpy_dir = self.path_edit.text().strip().strip('"')
        adb = os.path.join(scrcpy_dir, "adb.exe")
        if not os.path.isfile(adb):
            adb = "adb"
        return adb

    def _list_devices(self) -> list:
        """用 adb devices 枚举已连接设备（含 USB 与无线），返回 serial 列表。"""
        adb = self._adb_path()
        try:
            result = subprocess.run(
                [adb, "devices"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.append_log(f"[设备] 无法执行 {adb} devices：{exc}")
            return []
        devices = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    # ---------- 无线投屏 ----------
    def _wifi_connect(self, target: str) -> bool:
        """adb connect 无线设备，等待其进入 device 状态，返回是否成功。"""
        adb = self._adb_path()
        # 若该地址已在列表（含 offline 状态，说明会话过期），先断开再重连
        try:
            raw = subprocess.run(
                [adb, "devices"], capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
        except Exception:
            raw = ""
        if target in raw:
            self.append_log(f"[无线] {target} 已在设备列表（可能 offline），先断开再重连")
            try:
                subprocess.run(
                    [adb, "disconnect", target], capture_output=True, text=True,
                    timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass

        self.append_log(f"[无线] adb connect {target}")
        try:
            result = subprocess.run(
                [adb, "connect", target],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.append_log(f"[无线] 连接失败：{exc}")
            return False
        output = (result.stdout + result.stderr).strip()
        self.append_log(f"[无线] {output or '（无输出）'}")

        # 等待设备进入 device 状态（刚连接时可能短暂 offline），最多等 5 秒
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if target in self._list_devices():
                return True
            QApplication.processEvents()
            time.sleep(0.4)
        return target in self._list_devices()

    def on_wifi_connect(self):
        """点击「连接」：adb connect IP:端口 并刷新设备列表。"""
        ip = self.ip_edit.text().strip().strip('"')
        port = self.port_edit.text().strip().strip('"')
        if not ip or not port:
            QMessageBox.warning(self, "格式错误", "请输入无线 IP 和端口，例如 IP:192.168.21.84 端口:40823")
            return
        target = f"{ip}:{port}"
        self._wifi_connect(target)
        self.refresh_devices()

    def on_pair(self):
        """点击「配对」：adb pair IP:配对端口，配对码通过弹框输入。"""
        ip = self.ip_edit.text().strip().strip('"')
        pair_port = self.pair_edit.text().strip().strip('"')
        if not ip or not pair_port:
            QMessageBox.warning(self, "格式错误", "请输入无线 IP 和配对端口（手机「无线调试-使用配对码配对设备」页面可看到）")
            return
        target = f"{ip}:{pair_port}"
        code, ok = QInputDialog.getText(
            self, "输入配对码",
            f"请输入 {target} 的配对码（手机无线调试页面显示）：",
        )
        if not ok or not code.strip():
            self.append_log("[配对] 已取消")
            return
        code = code.strip()

        self.append_log(f"[配对] adb pair {target}")
        adb = self._adb_path()
        try:
            result = subprocess.run(
                [adb, "pair", target],
                input=code + "\n",  # adb pair 从 stdin 读取配对码
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.append_log(f"[配对] 失败：{exc}")
            return
        output = (result.stdout + result.stderr).strip()
        self.append_log(f"[配对] {output or '（无输出）'}")
        if "Successfully paired" in output:
            self.append_log("[配对] 配对成功！请把「端口」填为无线调试端口后点「连接」")

    def refresh_devices(self):
        self._devices = self._list_devices()
        self.devices_label.setText(f"设备: {len(self._devices)} 台")
        if self._devices:
            self.append_log(f"[设备] 发现 {len(self._devices)} 台：{', '.join(self._devices)}")
        else:
            self.append_log("[设备] 未发现设备，请开启手机 USB 调试并授权")

    # ---------- 启动 / 停止 ----------
    def _start_containers(self, devices: list):
        """为设备列表创建镜像容器，然后每 3 秒启动一台 scrcpy。

        所有设备统一用 scrcpy --serial=<设备号>（无线设备 serial 为
        IP:端口，同样用 --serial 选择，与用户指定命令一致）。
        """
        scrcpy_dir = self.path_edit.text().strip().strip('"')
        if not os.path.isfile(os.path.join(scrcpy_dir, "scrcpy.exe")):
            QMessageBox.warning(self, "路径错误", f"未找到 scrcpy.exe：\n{scrcpy_dir}")
            return
        if not devices:
            QMessageBox.warning(self, "没有设备", "未发现可投屏的设备，请先连接并授权。")
            return

        self._clear_containers()
        self.containers = []
        self._active_count = 0

        # 1) 先为所有设备创建容器并放入网格（画面区显示"正在连接设备…"）
        for serial in devices:
            # 每个设备用独立窗口标题，避免 FindWindow 匹配到别的窗口
            title = f"scrcpy_embedded_{serial}"
            container = ScrcpyContainer(scrcpy_dir, title, serial=serial,
                                        landscape=self.landscape_check.isChecked())
            container.log.connect(
                lambda text, c=container: self.append_log(f"[{c.serial}] {text}")
            )
            container.stopped.connect(
                lambda c=container: self.on_container_exited(c)
            )
            self.containers.append(container)
            self._add_cell(serial, container)

        self.append_log(f"[一键] 已创建 {len(devices)} 台设备画面，每 3 秒启动 1 台：{', '.join(devices)}")
        # 2) 每 3 秒启动一台（QTimer 链）
        self._pending_starts = list(devices)
        self._stagger_timer = QTimer(self)
        self._stagger_timer.setInterval(3000)
        self._stagger_timer.timeout.connect(self._stagger_next)
        self._stagger_timer.start()
        self._stagger_next()  # 立即启动第一台，之后每 3 秒一台

        # 「连接」「配对」按钮保持可点击（用户可随时操作）
        self.all_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _stagger_next(self):
        """3 秒定时器：启动队列中的下一台 scrcpy。"""
        if not self._pending_starts:
            self._stagger_timer.stop()
            return
        serial = self._pending_starts.pop(0)
        container = None
        for c in self.containers:
            if c.serial == serial:
                container = c
                break
        if container is None:
            self._stagger_next()
            return
        if container.start(audio=self.audio_check.isChecked()):
            self._active_count += 1
        if self._pending_starts:
            self.append_log(f"[一键] {serial} 已启动，3 秒后启动下一台…")
        else:
            self.append_log(f"[一键] {serial} 已启动，全部 {len(self.containers)} 台已启动完毕")

    def on_all_start(self):
        """「一键投屏」：全部设备逐台投屏（每 3 秒启动 1 台）。

        1) 输入框填了 IP:端口时，先 adb connect 把该无线设备拉入列表
           （已在列表则会先断开再重连，清除 offline 状态）；
        2) 枚举当前所有已连接设备（USB + 无线），全部用
           scrcpy -s <设备号> 启动（无线设备 serial 为 IP:端口）；
        3) 每 3 秒启动 1 台，例如：
           scrcpy -s 9dfbc63f → 3 秒 → scrcpy -s 192.168.137.161:42579
           → 3 秒 → scrcpy -s 192.168.137.197:37671
        """
        # 1) 输入框 IP:端口（可选）：把还没连接的无线设备拉进来
        ip = self.ip_edit.text().strip().strip('"')
        port = self.port_edit.text().strip().strip('"')
        target = f"{ip}:{port}" if ip and port else ""
        if target:
            self._wifi_connect(target)
        else:
            self.append_log("[一键] 未填写无线 IP/端口，直接投当前已连接的所有设备")

        # 2) 全部设备一起投：USB（无冒号）+ 所有无线（有冒号）
        devices = self._list_devices()
        if not devices:
            QMessageBox.warning(self, "没有设备", "未发现可投屏的设备（USB 或无线），请先连接并授权。")
            return

        usb = [d for d in devices if ":" not in d]
        wifi = [d for d in devices if ":" in d]
        if usb and wifi:
            self.append_log(f"[一键] 先投有线 {len(usb)} 台：{', '.join(usb)}，"
                            f"再投无线 {len(wifi)} 台：{', '.join(wifi)}")
        else:
            self.append_log(f"[一键] 共投 {len(devices)} 台：{', '.join(devices)}")
        self._start_containers(devices)

    def on_stop(self):
        self._clear_containers()
        self.all_btn.setEnabled(True)
        # 连接/配对按钮保持可点击
        self.stop_btn.setEnabled(False)

    def on_container_exited(self, container):
        """某台设备意外断连（scrcpy 退出）。"""
        self._active_count = max(0, self._active_count - 1)
        if self._active_count == 0:
            self.all_btn.setEnabled(True)
            # 连接/配对按钮保持可点击
            self.stop_btn.setEnabled(False)

    def _grid_columns(self) -> int:
        """每行显示数量：勾选横屏时 3 台/行，竖屏时 5 台/行。"""
        if self.landscape_check.isChecked():
            self._grid.setHorizontalSpacing(8)   # 横屏间距
            return 3
        self._grid.setHorizontalSpacing(38)      # 竖屏间距
        return GRID_COLUMNS
        #return 3 if self.landscape_check.isChecked() else GRID_COLUMNS

    def _add_cell(self, serial: str, container: QWidget):
        """把容器放进网格，固定尺寸、左上对齐。

        每行数量跟随横/竖屏：横屏 3 台、竖屏 5 台。
        每个设备外加一个灰色边框（BorderWidget），画面贴合边框输出。
        """
        idx = self._grid.count()
        row, col = divmod(idx, self._grid_columns())
        cell = QWidget()
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(4, 4, 4 , 4) #竖屏时外边距
        lay.setSpacing(4)
        label = QLabel(serial)
        label.setAlignment(Qt.AlignCenter)
        lay.addWidget(label)

        # 灰色边框：QPainter 绘制，画面嵌入在边框内部并贴合
        border = BorderWidget()
        border.setFixedSize(
            container._w + 2 * (FRAME_MARGIN + FRAME_LINE),
            container._h + 2 * (FRAME_MARGIN + FRAME_LINE),
        )
        blay = QVBoxLayout(border)
        # 容器内缩一个线宽：四边边框都露出来，画面与边框之间无多余空隙（贴合）
        blay.setContentsMargins(FRAME_MARGIN + FRAME_LINE, FRAME_MARGIN + FRAME_LINE,
                                FRAME_MARGIN + FRAME_LINE, FRAME_MARGIN + FRAME_LINE)
        blay.setSpacing(0)
        blay.addWidget(container, 0, Qt.AlignLeft | Qt.AlignTop)

        lay.addWidget(border)
        self._grid.addWidget(cell, row, col, Qt.AlignLeft | Qt.AlignTop)

    def _clear_containers(self):
        # 停止逐台启动定时器
        if getattr(self, "_stagger_timer", None) is not None:
            try:
                self._stagger_timer.stop()
            except Exception:
                pass
            self._stagger_timer = None
        self._pending_starts = []
        for c in self.containers:
            try:
                c.stop()
            except Exception:
                pass
        self.containers = []
        self._active_count = 0
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    # ---------- 联动：第 1 台 → 其他台 ----------
    def _device_size(self, serial: str):
        """查询设备物理分辨率（adb shell wm size），结果缓存。

        勾选横屏时返回旋转后的显示尺寸（宽高互换，如 2400x1080），
        保证 adb input 注入坐标与手机当前显示方向一致。
        """
        if serial in self._device_sizes:
            w, h = self._device_sizes[serial]
        else:
            w, h = (1080, 2400)
            adb = self._adb_path()
            try:
                out = subprocess.run(
                    [adb, "-s", serial, "shell", "wm", "size"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).stdout
                mm = re.search(r"(\d+)x(\d+)", out)
                if mm:
                    w, h = int(mm.group(1)), int(mm.group(2))
                    self._device_sizes[serial] = (w, h)
            except Exception:
                pass
        if self.landscape_check.isChecked():
            # 横屏联动：手机已旋转 90°，显示方向宽高互换（如 2400x1080），
            # adb input 的注入坐标按当前显示方向计算，因此交换宽高
            return (h, w)
        return (w, h)

    def _inject_touch(self, serial: str, x: int, y: int, x2: int = None, y2: int = None):
        """后台线程用 adb input 往设备注入点击 / 滑动（不阻塞 UI）。

        无线与 USB 设备通用（adb 已连接的设备均可）。
        """
        def work():
            try:
                adb = self._adb_path()
                if x2 is None:
                    subprocess.run(
                        [adb, "-s", serial, "shell", "input", "tap", str(x), str(y)],
                        capture_output=True,
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    subprocess.run(
                        [adb, "-s", serial, "shell", "input", "swipe",
                         str(x), str(y), str(x2), str(y2), "250"],
                        capture_output=True,
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _sync_mouse(self):
        """轮询第一个窗口上的鼠标操作，把点击 / 拖拽同步到其他设备。

        按下时记录起点，抬起时用 adb shell input 注入：
        位置没变 = tap（点击），位置变了 = swipe（滑动）。
        说明：早期版本用 PostMessage 往目标 scrcpy 窗口注入鼠标消息，
        实测目标 scrcpy 进程会除零崩溃（exit=0xC0000094），因此改为
        直接往手机注入输入（不经过 scrcpy 窗口），无线/有线都可靠。
        """
        if not self.sync_check.isChecked():
            self._drag = False
            return

        src = None
        targets = []
        for c in self.containers:
            if c.hwnd and c.is_running():
                if src is None:
                    src = c          # 第一个已连接的屏幕作为源
                else:
                    targets.append(c)
        if src is None or not targets:
            self._drag = False
            return

        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        ox, oy, sw, sh = get_client_rect_screen(src.hwnd)
        if sw <= 0 or sh <= 0:
            self._drag = False
            return

        # 归一化到源窗口（限制在 [0,1]）
        rx = min(max((pt.x - ox) / sw, 0.0), 1.0)
        ry = min(max((pt.y - oy) / sh, 0.0), 1.0)

        if not self._drag:
            if down and ox <= pt.x < ox + sw and oy <= pt.y < oy + sh:
                self._drag = True
                self._drag_start = (rx, ry)  # 记录按下位置
        else:
            if not down:
                self._drag = False
                sx, sy = self._drag_start
                moved = abs(rx - sx) > 0.02 or abs(ry - sy) > 0.02
                for t in targets:
                    tw_, th_ = self._device_size(t.serial)
                    x1 = int(sx * tw_)
                    y1 = int(sy * th_)
                    if moved:
                        self._inject_touch(t.serial, x1, y1,
                                           int(rx * tw_), int(ry * th_))
                    else:
                        self._inject_touch(t.serial, x1, y1)
            # 拖动过程中不逐帧注入（抬起时一次性 swipe 完成手势）

    # ---------- 日志 ----------
    def append_log(self, text: str):
        self.log_view.appendPlainText(text)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        self._sync_timer.stop()
        self._clear_containers()
        super().closeEvent(event)


def main():
    # 高 DPI 支持（必须在 QApplication 创建之前设置）
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    win = MainWindow()
    win.showMaximized()  # 打开后最大化显示
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
