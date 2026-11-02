# -*- coding: utf-8 -*-
"""
GameDock — 本地游戏卡带坞 (Phase 1, 纯本地不联网)
风格参考 Steam dock: 底部深色底条 + 竖版封面卡带 + 悬停放大亮圈 + 上方详情面板
数据来源: Steam appmanifest / Epic manifests / 注册表(带黑名单过滤)
运行: pythonw GameDock.pyw   (依赖 PyQt5 + Pillow)
"""

import os
import re
import sys
import json
import time
import ctypes
import threading
import subprocess
import traceback
import urllib.request
from ctypes import wintypes
from winreg import (OpenKey, QueryValueEx, EnumKey, CloseKey,
                    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER,
                    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY)

from PyQt5.QtCore import (Qt, QTimer, QPoint, QRect, QRectF, QSize, QEvent,
                          QPointF)
from PyQt5.QtGui import (QPixmap, QPainter, QColor, QPainterPath, QFont,
                         QFontMetrics, QGuiApplication, QImage, QBrush,
                         QLinearGradient, QCursor, QPen, QRadialGradient,
                         QIcon, QPolygonF, QKeyEvent)
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QHBoxLayout, QVBoxLayout,
                             QScrollArea, QFrame, QPushButton, QMenu, QLineEdit,
                             QSpinBox, QSystemTrayIcon, QFileDialog)

APP_NAME = "GameDock"
CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GameDock")
HIDDEN_FILE = os.path.join(CFG_DIR, "hidden.txt")
COVER_DIR = os.path.join(CFG_DIR, "covers")   # 联网补拉的竖版封面缓存 (拉一次, 永久离线用)
GENRES_FILE = os.path.join(CFG_DIR, "genres.json")   # 联网拉取的游戏类型缓存
SORT_FILE = os.path.join(CFG_DIR, "sort.txt")   # 用户选择的排序方式
CUSTOM_FILE = os.path.join(CFG_DIR, "custom.txt")   # 自定义拖拽排序的 gid 顺序
SCALE_FILE = os.path.join(CFG_DIR, "scale.txt")   # 卡片缩放百分比 (50~150, 步进5)
LANG_FILE = os.path.join(CFG_DIR, "lang.txt")     # 右键菜单语言 (zh/en/ja)

# ---------------- Xbox 手柄 (XInput, ctypes 直连, 零新依赖) ----------------
class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong),
                ("Gamepad", _XINPUT_GAMEPAD)]


_xinput_dll = None
for _xn in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
    try:
        _xinput_dll = ctypes.WinDLL(_xn)
        break
    except OSError:
        pass
if _xinput_dll is not None:
    _xinput_get_state = _xinput_dll.XInputGetState
    _xinput_get_state.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
    _xinput_get_state.restype = ctypes.c_ulong
else:
    _xinput_get_state = None

# wButtons 位
GP_DPAD_LEFT = 0x0004
GP_DPAD_RIGHT = 0x0008
GP_DPAD_UP = 0x0001
GP_DPAD_DOWN = 0x0002
GP_START = 0x0010            # ☰ 菜单键
GP_A = 0x1000
GP_B = 0x2000
GP_X = 0x4000
GP_Y = 0x8000
GP_LTHUMB_DZ = 7849          # XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE


def xinput_poll(idx=0):
    """读一个手柄。返回 (connected, wButtons, sThumbLX, sThumbLY) ; 无 XInput 时返回 None。"""
    if _xinput_get_state is None:
        return None
    st = _XINPUT_STATE()
    if _xinput_get_state(idx, ctypes.byref(st)) != 0:
        return (False, 0, 0, 0)
    g = st.Gamepad
    return (True, g.wButtons, g.sThumbLX, g.sThumbLY)

# ---------------- 右键菜单多语言 (仅菜单文案, 其他 UI 不动) ----------------
# key: (简体中文, English, 日本語)
MENU_STR = {
    "cat":     ("分类", "Category", "カテゴリ"),
    "all":     ("全部", "All", "すべて"),
    "not_av":  ("（暂未接入）", " (not available yet)", "（未対応）"),
    "sort":    ("排序", "Sort by", "並べ替え"),
    "s_name":  ("名称", "Name", "名前"),
    "s_last":  ("最近游玩", "Recently Played", "最近のプレイ"),
    "s_time":  ("游玩时长", "Playtime", "プレイ時間"),
    "s_genre": ("游戏类型", "Genre", "ジャンル"),
    "s_custom": ("自定义", "Custom", "カスタム"),
    "update":  ("更新", "Update", "更新"),
    "u_cover": ("封面资源（联网）", "Covers (online)", "カバー画像（オンライン）"),
    "u_genre": ("游戏类型（联网）", "Genres (online)", "ジャンル（オンライン）"),
    "hidden":  ("已隐藏的游戏 (%d)", "Hidden Games (%d)", "非表示のゲーム (%d)"),
    "none":    ("（空）", "(empty)", "（なし）"),
    "restore": ("全部恢复显示", "Restore All", "すべて表示に戻す"),
    "search":  ("搜索游戏", "Search Games", "ゲームを検索"),
    "size":    ("调整尺寸", "Card Size", "サイズ調整"),
    "launch":  ("启动", "Startup", "起動"),
    "autostart": ("开机自启", "Launch at Boot", "自動起動"),
    "boot_show": ("开机自动显示", "Show on Boot", "起動時に表示"),
    "boot_hide": ("隐藏至托盘", "Hide to Tray", "トレイに格納"),
    "imp_cover": ("导入封面…", "Import Cover…", "カバーを読み込む…"),
    "reset_cover": ("恢复默认封面", "Reset Cover", "カバーをリセット"),
    "lang":    ("语言", "Language", "言語"),
    "rescan":  ("⟳  重新扫描", "⟳  Rescan", "⟳  再スキャン"),
    "quit":    ("退出 GameDock", "Quit GameDock", "GameDock を終了"),
}
MENU_LANGS = (("简体中文", "zh"), ("English", "en"), ("日本語", "ja"))


def load_lang():
    try:
        with open(LANG_FILE, "r", encoding="utf-8") as f:
            m = f.read().strip()
        if m in ("zh", "en", "ja"):
            return m
    except Exception:
        pass
    return "zh"


def save_lang(m):
    try:
        with open(LANG_FILE, "w", encoding="utf-8") as f:
            f.write(m)
    except Exception:
        pass


_LANG = load_lang()          # 当前菜单语言 (set_lang 切换)


def _t(key, *args):
    """取当前语言的菜单文案, 支持 %-格式化参数。"""
    i = {"zh": 0, "en": 1, "ja": 2}.get(_LANG, 0)
    s = MENU_STR[key][i]
    return s % args if args else s


# ---------------- 开机自启 (HKCU Run, 用户级无需管理员) ----------------
# 模式: off=不开机自启 / show=开机直接显示 / hide=开机只留托盘图标
STARTUP_FILE = os.path.join(CFG_DIR, "startup.txt")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "GameDock"


def load_startup():
    try:
        with open(STARTUP_FILE, "r", encoding="utf-8") as f:
            m = f.read().strip()
        if m in ("off", "show", "hide"):
            return m
    except Exception:
        pass
    return "off"


def save_startup(mode):
    try:
        with open(STARTUP_FILE, "w", encoding="utf-8") as f:
            f.write(mode)
    except Exception:
        pass


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except Exception:
        return False


def set_autostart(on, tray=False):
    """写/删 HKCU Run 自启项。tray=True 时带 --tray 参数 (开机只出托盘)。"""
    try:
        import winreg
        if on:
            if getattr(sys, "frozen", False):
                # PyInstaller 打包: 自启直接指向 exe 本体 (--tray 由 argv 解析)
                cmd = '"%s"%s' % (os.path.abspath(sys.argv[0]),
                                  " --tray" if tray else "")
            else:
                py = sys.executable.replace("python.exe", "pythonw.exe")
                script = os.path.abspath(sys.argv[0])
                cmd = '"%s" "%s"%s' % (py, script, " --tray" if tray else "")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        log_exc("set_autostart")


def make_tray_icon():
    """程序图标 / 托盘图标: 优先用用户自绘 GD 图 (gd_icon.png, 已预裁透明边),
    文件缺失/损坏时兜底为代码绘制的亮橙渐变字标。"""
    try:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gd_icon.png")
        if os.path.isfile(src):
            img = QImage(src)
            if not img.isNull():
                pm = QPixmap.fromImage(img)
                icon = QIcon()
                for px in (16, 24, 32, 48, 64, 128, 256):
                    icon.addPixmap(pm.scaled(px, px, Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation))
                return icon
    except Exception:
        log_exc("make_tray_icon(user png)")
    icon = QIcon()

    def glyph_path(font, ch, tx, ty):
        """构建字形路径并平移到 字形bbox左上角=(tx,ty)"""
        path = QPainterPath()
        path.addText(0, 0, font, ch)
        br = path.boundingRect()
        return path.translated(tx - br.left(), ty - br.top())

    # ---- 设计稿常量 (512 基准) ----
    # 字形尺寸 440: G bbox 299x318, D bbox 329x308
    # D 相对 G 字形偏移 (+70, +30); 并集 399x338 居中 -> G(56,87) D(126,117)
    GX_T, GY_T = 56.0, 87.0      # G 字形左上 (512 基准)
    DX_T, DY_T = 126.0, 117.0    # D 字形左上
    GLYPH_PX = 440               # 字号 (em px)
    RADIUS = 100                 # 底圆角

    for px in (16, 24, 32, 48, 64, 128, 256):
        k = px / 512.0
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        # 底: 亮橙渐变圆角方 (铺满)
        bg = QLinearGradient(0, 0, 0, px)
        bg.setColorAt(0, QColor(255, 172, 64))
        bg.setColorAt(1, QColor(255, 74, 14))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(0, 0, px, px, RADIUS * k, RADIUS * k)
        # 字体: Segoe UI Black, 字号随画布缩放
        f = QFont("Segoe UI")
        f.setWeight(QFont.Black)
        f.setPixelSize(max(6, round(GLYPH_PX * k)))
        dpath = glyph_path(f, "D", DX_T * k, DY_T * k)
        gpath = glyph_path(f, "G", GX_T * k, GY_T * k)
        # 渐变 (跟背景同方向, 铺满画布)
        ggrad = QLinearGradient(0, 0, 0, px)
        ggrad.setColorAt(0, QColor(255, 255, 255))
        ggrad.setColorAt(1, QColor(255, 214, 150))
        dgrad = QLinearGradient(0, 0, 0, px)
        dgrad.setColorAt(0, QColor(96, 48, 20))
        dgrad.setColorAt(1, QColor(32, 13, 5))
        # 柔影 (多重低透明硬影近似高斯)
        for off, a in ((3 * k, 50), (6 * k, 34), (9 * k, 20)):
            sh = QColor(120, 30, 0, a)
            p.fillPath(dpath.translated(0, off), sh)
            p.fillPath(gpath.translated(0, off), sh)
        # D 后 (深棕渐变) -> G 前 (白渐变)
        p.fillPath(dpath, QBrush(dgrad))
        p.fillPath(gpath, QBrush(ggrad))
        p.end()
        icon.addPixmap(pm)
    return icon

# 类型归一化: Steam 商店 schinese 类型名 -> 大类
GENRE_MAP = {
    "角色扮演": "RPG", "RPG": "RPG",
    "动作": "动作", "冒险": "冒险", "射击": "射击", "第一人称射击": "射击",
    "枪械射击": "射击", "格斗": "格斗", "策略": "策略", "模拟": "模拟",
    "竞速": "竞速", "体育": "体育", "休闲": "休闲", "独立": "独立",
    "大型多人在线": "网游", "大型多人在线游戏": "网游", "免费开玩": "免费开玩",
}
GENRE_ORDER = ["RPG", "动作", "冒险", "射击", "格斗", "策略", "模拟",
               "竞速", "体育", "休闲", "独立", "网游", "免费开玩"]

# ---------------- 黑名单: 反作弊/运行库/启动器等 ----------------
BLACKLIST = [
    "visual c++", "redistributable", "vcredist", "directx", "runtime", "runtimes",
    "sdk", "driver", "anti-cheat", "anticheat", "easy anti", "battleye", "punkbuster",
    "launcher", "steam", "epic games", "gog galaxy", "ubisoft connect", "ubisoft club",
    "origin", "ea app", "battle.net", "riot client", "microsoft edge", "webview",
    ".net", "python", "java ", "jdk", "visual studio", "office", "windows ",
    "update", "installer", "setup", "uninstall", "documentation", "redistrib",
    "physx", "openal", "nvidia", "geforce", "amd software", "radeon", "realtek",
    "intel(", "intel ", "certificate", "speech", "msxml", "silverlight",
    "discord", "wallpaper engine", "helper", "service", "组件", "运行库",
    "扩展", "支持库", "语言包", "修正", "补丁", "验证", "插件", "测试版",
]


# ================= 数据模型 =================

class GameEntry:
    __slots__ = ("source", "gid", "name", "launch", "is_steam", "uninstall",
                 "icon_exe", "icon_ico", "art", "meta",
                 "lastplayed", "playtime", "genre", "catalog_id", "base_art")

    def __init__(self):
        self.source = ""
        self.gid = ""
        self.name = ""
        self.launch = None
        self.is_steam = False
        self.uninstall = None
        self.lastplayed = 0        # 最近游玩 unix 时间戳 (0 = 从没玩过)
        self.playtime = 0          # 累计游玩分钟
        self.genre = ""            # 大类 (RPG/动作/...), 空串 = 未分类
        self.icon_exe = None
        self.icon_ico = None
        self.art = None
        self.meta = ""
        self.catalog_id = ""       # Epic CatalogItemId (封面查找用)
        self.base_art = None       # 被手动导入封面压掉前的平台封面 (恢复用)


def load_hidden():
    try:
        with open(HIDDEN_FILE, "r", encoding="utf-8") as f:
            return set(x.strip() for x in f if x.strip())
    except Exception:
        return set()


def save_hidden(hidden):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(HIDDEN_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(hidden)))
    except Exception:
        pass


def norm_name(s):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "").lower())


# ================= 扫描 =================

def fmt_size_bytes(s):
    try:
        b = int(s)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    gb = b / 1073741824.0
    if gb >= 1.0:
        return "%.1f GB" % gb
    return "%d MB" % (b / 1048576.0)


# ================= 封面管线 (整体逻辑) =================
#
# 目标: 每款已安装游戏在卡片上都显示完整竖版封面, 纯本地优先, 联网仅按需。
#
# 数据源优先级 (scan_steam 里逐款执行, 新装游戏重新扫描即自动接入):
#   1. 联网缓存  %APPDATA%\GameDock\covers\S_<appid>.jpg
#      (用户点右键菜单『更新封面资源（联网）』时拉取, 拉一次永久离线用)
#   2. Steam 本地缓存  <Steam>\appcache\librarycache\<appid>\...
#      新版结构: <appid>/<SHA1哈希>/标准名.jpg, 文件名规则见 find_steam_art:
#      竖版 = library_600x900.jpg / portrait.png / library_capsule*.jpg
#      (注意: 新版客户端的竖版封面叫 Capsule, 300x450, 没有 Portrait 这个分类)
#      横版 = header.jpg / library_header*.jpg (只做最后兜底, 由 cover_crop 裁竖)
#   3. exe 图标 / 首字母色块  (Card.__init__ 里的最后兜底)
#
# 什么时候需要联网: Steam 新客户端显示封面直接走 CDN 不落盘,
# 所以部分游戏本地只有横版头图。右键菜单『更新封面资源（联网）』
# 会对这些游戏从 Steam 官方 CDN 拉 library_600x900 (简中优先),
# 存入 covers 缓存后, 之后每次启动都是纯本地读取。
#
# 相关函数: find_steam_art (找本地图) / fetch_cover (联网拉一张) /
#           has_portrait (竖版校验) / Dock.update_covers (菜单入口, 后台线程)


def steam_cache_dir():
    """Steam 的 librarycache 目录 (读注册表 SteamPath), 找不到返回 None。"""
    try:
        with OpenKey(HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except Exception:
        return None
    if not os.path.isdir(steam):
        return None
    return os.path.join(steam, "appcache", "librarycache")


def parse_localconfig(path):
    """解析 Steam localconfig.vdf 的 Software/Valve/Steam/apps 段,
    返回 {appid: (LastPlayed, Playtime分钟)}。
    只抓 'appid' { "LastPlayed" "..." "Playtime" "..." } 这种平铺子块。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except Exception:
        return out
    ai = txt.find('"apps"')               # 注意: 键名是 apps 不是 games
    if ai < 0:
        return out
    i = txt.find("{", ai)
    if i < 0:
        return out
    depth, j, end = 0, i, i
    while j < len(txt):                    # 找 apps 块的收口大括号
        if txt[j] == "{":
            depth += 1
        elif txt[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
        j += 1
    block = txt[i:end]
    for m in re.finditer(r'"(\d{3,12})"\s*\{', block):
        start = m.end() - 1              # 从 appid 的 '{' 起做配对, 取完整子块
        depth, k = 0, start
        while k < len(block):
            if block[k] == "{":
                depth += 1
            elif block[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        inner = block[start:k + 1]
        lp = re.search(r'"LastPlayed"\s*"(\d+)"', inner)
        pt = re.search(r'"Playtime"\s*"(\d+)"', inner)
        if lp or pt:
            out[m.group(1)] = (int(lp.group(1)) if lp else 0,
                               int(pt.group(1)) if pt else 0)
    return out


def steam_playdata():
    """汇总所有 Steam 账号的游玩数据。
    多账号时: LastPlayed 取最新, Playtime 累加。"""
    merged = {}
    try:
        with OpenKey(HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except Exception:
        return merged
    ud = os.path.join(steam, "userdata")
    if not os.path.isdir(ud):
        return merged
    for acc in os.listdir(ud):
        lc = os.path.join(ud, acc, "config", "localconfig.vdf")
        for appid, (lp, pt) in parse_localconfig(lc).items():
            old = merged.get(appid, (0, 0))
            merged[appid] = (max(old[0], lp), old[1] + pt)
    return merged


def load_genres():
    try:
        with open(GENRES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_genres(d):
    try:
        os.makedirs(os.path.dirname(GENRES_FILE), exist_ok=True)
        with open(GENRES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_custom_order():
    """自定义拖拽排序: 按用户拖好的顺序存 gid 列表。"""
    try:
        with open(CUSTOM_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


def save_custom_order(order):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(order))
    except Exception:
        log_exc("save_custom_order")


def load_sort_mode():
    try:
        with open(SORT_FILE, "r", encoding="utf-8") as f:
            m = f.read().strip()
        if m in ("name", "lastplayed", "playtime", "genre", "custom"):
            return m
    except Exception:
        pass
    return "name"


def save_sort_mode(m):
    try:
        with open(SORT_FILE, "w", encoding="utf-8") as f:
            f.write(m)
    except Exception:
        pass


def load_scale():
    try:
        v = int(open(SCALE_FILE, "r", encoding="utf-8").read().strip())
        return max(50, min(150, v))
    except Exception:
        return 100


def save_scale(pct):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(SCALE_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(pct)))
    except Exception:
        log_exc("save_scale")


def has_portrait(path):
    """判断图片是否为竖版 (高 > 宽); 解码失败返回 False。"""
    try:
        img = QImage(path)
        return not img.isNull() and img.height() > img.width()
    except Exception:
        return False


def fetch_genres_once(appid):
    """从 Steam 商店 API 拉一款游戏的类型 (schinese), 归一化成大类。失败返回 None。"""
    try:
        u = "https://store.steampowered.com/api/appdetails?appids=%s&l=schinese" % appid
        req = urllib.request.Request(u, headers={"User-Agent": "GameDock/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        d = (j or {}).get(appid) or {}
        if not d.get("success"):
            return None
        for g in (d.get("data", {}).get("genres") or []):
            desc = (g.get("description") or "").strip()
            if desc:
                return GENRE_MAP.get(desc, desc)
    except Exception:
        pass
    return None


def scan_steam():
    out = []
    try:
        import winreg
        with OpenKey(HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
    except Exception:
        return out
    if not os.path.isdir(steam):
        return out

    libs = [steam]
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libs.append(m.group(1).replace("\\\\", "\\"))
        except Exception:
            pass

    cache = os.path.join(steam, "appcache", "librarycache")
    pdata = steam_playdata()     # LastPlayed / Playtime (本地, 不联网)
    genres = load_genres()       # 类型缓存 (联网拉过就有)
    for lib in dict.fromkeys(libs):
        sa = os.path.join(lib, "steamapps")
        if not os.path.isdir(sa):
            continue
        for fn in os.listdir(sa):
            if not fn.startswith("appmanifest_") or not fn.endswith(".acf"):
                continue
            try:
                with open(os.path.join(sa, fn), "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            m_id = re.search(r'"appid"\s+"(\d+)"', txt)
            m_name = re.search(r'"name"\s+"([^"]+)"', txt)
            if not m_id or not m_name:
                continue
            name = m_name.group(1)
            low = name.lower()
            if any(x in low for x in ("redistribut", "proton", "steam linux",
                                      "steamworks", "steam runtime")):
                continue
            appid = m_id.group(1)
            g = GameEntry()
            g.source = "Steam"
            g.gid = "S|" + appid
            g.name = name
            g.is_steam = True
            g.launch = "steam://rungameid/" + appid
            g.uninstall = "steam://uninstall/" + appid
            g.art = find_steam_art(cache, appid)
            cp = os.path.join(COVER_DIR, "S_%s.jpg" % appid)
            if os.path.isfile(cp) and has_portrait(cp):
                g.art = cp    # 联网补拉的完整竖版优先
            m_sod = re.search(r'"SizeOnDisk"\s+"(\d+)"', txt)
            sz = fmt_size_bytes(m_sod.group(1)) if m_sod else None
            g.meta = "Steam" + (" · " + sz if sz else "")
            g.lastplayed, g.playtime = pdata.get(appid, (0, 0))
            g.genre = genres.get(appid, "")
            out.append(g)
    return out


def find_steam_art(cache, appid):
    """在 Steam 本地缓存里找竖版封面。

    新版 Steam 结构: librarycache/{appid}/{哈希}/标准名.jpg。
    关键: 新版的竖版封面叫 library_capsule*.jpg (300x450), 不是只有
    library_600x900; 横版 header/hero 只是最后兜底。选图时真解码验证竖版,
    拿不到竖版才退横版 (由调用方裁切)。语言后缀 (_schinese) 简中优先。
    """
    if not os.path.isdir(cache):
        return None
    d = os.path.join(cache, appid)
    first_any = None           # 万一全程没竖版, 至少回退到第一张能用的图
    if os.path.isdir(d):
        bases = [d]
        try:
            bases += [os.path.join(d, s) for s in sorted(os.listdir(d))
                      if os.path.isdir(os.path.join(d, s))]
        except Exception:
            pass
        probes = ["library_600x900.jpg", "library_600x900_2x.jpg", "portrait.png",
                  "library_capsule.jpg", "header.jpg", "library_header.jpg"]
        for b in bases:
            for n in probes:
                p = os.path.join(b, n)
                if os.path.isfile(p):
                    if has_portrait(p):
                        return p
                    if first_any is None:
                        first_any = p
        for prefix in ("library_600x900", "portrait", "library_capsule",
                       "header", "library_header"):
            for b in bases:
                try:
                    hits = [f for f in os.listdir(b)
                            if f.startswith(prefix)
                            and f.lower().endswith((".jpg", ".png"))]
                except Exception:
                    hits = []
                if hits:
                    hits.sort(key=lambda f: ("schinese" not in f, f))
                    p = os.path.join(b, hits[0])
                    if has_portrait(p):
                        return p
                    if first_any is None:
                        first_any = p
    # 旧版平铺缓存: 文件直接躺在 librarycache 根
    try:
        for prefix in (appid + "_library_600x900", appid + "_portrait",
                       appid + "_library", appid + "_header"):
            hits = [f for f in os.listdir(cache) if f.startswith(prefix)]
            if hits:
                hits.sort()
                p = os.path.join(cache, hits[0])
                if os.path.isfile(p):
                    if has_portrait(p):
                        return p
                    if first_any is None:
                        first_any = p
    except Exception:
        pass
    return first_any


def fetch_cover(appid):
    """从 Steam 官方 CDN 拉竖版封面 (国内直连), 存入 COVER_DIR。
    成功返回路径; CDN 没有竖版/网络失败返回 None。仅由用户点菜单触发。"""
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
    except Exception:
        pass
    urls = [
        # 2023+ 新版资产路径 (Cloudflare 与 Akamai 双镜像, 简中优先)
        "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/%s/library_600x900_schinese.jpg" % appid,
        "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/%s/library_600x900.jpg" % appid,
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%s/library_600x900_schinese.jpg" % appid,
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%s/library_600x900.jpg" % appid,
        # 旧版路径 (老游戏可能只有这里)
        "https://cdn.cloudflare.steamstatic.com/steam/apps/%s/library_600x900_schinese.jpg" % appid,
        "https://cdn.cloudflare.steamstatic.com/steam/apps/%s/library_600x900.jpg" % appid,
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "GameDock/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) < 5000:
                continue
            img = QImage.fromData(data)
            if img.isNull() or img.height() <= img.width():
                continue
            out = os.path.join(COVER_DIR, "S_%s.jpg" % appid)
            with open(out, "wb") as f:
                f.write(data)
            return out
        except Exception:
            continue
    return None


# ---- Epic 封面: EGL 本地库数据库(IndexedDB)里缓存着每款游戏的 keyImages 图源 ----

_EPIC_URL_MAP = None


def epic_cover_url_map():
    """纯本地: 从 EGL webcache IndexedDB 提取 CatalogItemId -> [竖版URL, 任意URL]。
    EGL 把商店 catalog 数据 (含 keyImages) 缓存在 IndexedDB; 这里不做 LevelDB
    级解析, 直接按字节流配对: 'catalogItemId' 标记与其后 4KB 内出现的图片 URL
    属于同一条记录 (实测相邻几百字节)。"""
    global _EPIC_URL_MAP
    if _EPIC_URL_MAP is not None:
        return _EPIC_URL_MAP
    _EPIC_URL_MAP = {}
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "EpicGamesLauncher", "Saved")
    if not os.path.isdir(base):
        return _EPIC_URL_MAP
    cid_re = re.compile(rb'catalogItemId"[ \x00-\x1f]{0,4}([0-9a-f]{32})')
    url_re = re.compile(rb'https[!-~]{8,600}\.(?:jpg|png)')
    for dirpath, dirs, files in os.walk(base):
        if "IndexedDB" not in dirpath:
            continue
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 200 * 1024 * 1024:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except Exception:
                continue
            if b"Diesel" not in data and b"epicgames" not in data:
                continue
            cids = list(cid_re.finditer(data))
            if not cids:
                continue
            for m in url_re.finditer(data):
                s = m.start()
                cur = None
                for c in cids:          # 回溯最近的 catalogItemId
                    if c.end() > s:
                        break
                    cur = c
                if cur is None or s - cur.end() > 4096:
                    continue
                url = m.group().decode("ascii", "replace")
                tall = b"Tall" in data[max(0, s - 120):s]
                ent = _EPIC_URL_MAP.setdefault(cur.group(1).decode("ascii"),
                                               [None, None])
                if tall and not ent[0]:
                    ent[0] = url
                elif not tall and not ent[1]:
                    ent[1] = url
    return _EPIC_URL_MAP


def epic_cover_local(cid):
    """已缓存过的 Epic 封面路径; 没有返回 None。"""
    if not cid:
        return None
    p = os.path.join(COVER_DIR, "E_%s.jpg" % cid)
    return p if os.path.isfile(p) else None


def fetch_epic_cover(cid):
    """用 EGL 本地缓存的官方 CDN 图源拉封面 (竖版优先), 存 COVER_DIR。
    成功返回路径; 本地没有图源记录或下载失败返回 None。"""
    ent = epic_cover_url_map().get(cid)
    if not ent:
        return None
    url = ent[0] or ent[1]
    if not url:
        return None
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
    except Exception:
        pass
    for u in (url, ent[0], ent[1]):
        if not u:
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "GameDock/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) < 3000:
                continue
            img = QImage.fromData(data)
            if img.isNull():
                continue
            out = os.path.join(COVER_DIR, "E_%s.jpg" % cid)
            with open(out, "wb") as f:
                f.write(data)
            return out
        except Exception:
            continue
    return None


# ---- WeGame 封面: WeGame 官网海报 CDN (官方商店同款海报, 拉一次永久离线) ----

_WG_POSTER_BASE = "https://wegame.gtimg.com/g.55555-r.c4663/poster/"
WEGAME_POSTERS = {
    # 游戏名关键词 -> 海报文件名 (均已实测 200)
    "英雄联盟": "lol.png",
    "穿越火线": "cf.png",
    "地下城与勇士": "dnf.png",
    "无畏契约": "val.png",
    "黑神话": "heishenhua.png",
    "和平精英": "heping.png",
    "流放之路": "poe.png",
}


def wegame_cover_local(name):
    """已缓存的 WeGame 官方海报路径; 没有返回 None。"""
    if not name:
        return None
    p = os.path.join(COVER_DIR, "W_%s.jpg" % name)
    return p if os.path.isfile(p) else None


def fetch_wegame_cover(name):
    """按游戏名从 WeGame 官网海报 CDN 拉官方封面, 存 COVER_DIR。
    海报 PNG 四角自带圆角透明 -> 铺深底压平成 JPG。成功返回路径。"""
    if not name:
        return None
    nl = name.lower()
    slug = None
    for k, s in WEGAME_POSTERS.items():
        if k in nl or (nl in k.lower() and len(nl) >= 2):
            slug = s
            break
    if not slug:
        return None
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
    except Exception:
        pass
    try:
        req = urllib.request.Request(_WG_POSTER_BASE + slug,
                                     headers={"User-Agent": "GameDock/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        img = QImage.fromData(data)
        if img.isNull():
            return None
        canvas = QImage(img.size(), QImage.Format_RGB32)   # 铺深底压平
        canvas.fill(QColor(10, 12, 18))
        p = QPainter(canvas)
        p.drawImage(0, 0, img)
        p.end()
        out = os.path.join(COVER_DIR, "W_%s.jpg" % name)
        canvas.save(out, "JPG", 90)
        return out
    except Exception:
        return None


def scan_epic():
    out = []
    base = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if not os.path.isdir(base):
        return out
    for fn in os.listdir(base):
        if not fn.endswith(".item"):
            continue
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        name = j.get("DisplayName")
        if not name:
            continue
        cats = " ".join(j.get("AppCategories") or [])
        if "game" not in cats.lower():
            continue
        loc = j.get("InstallLocation") or ""
        exe = j.get("LaunchExecutable") or ""
        app = j.get("AppName") or fn[:-5]
        cid = j.get("CatalogItemId") or ""
        g = GameEntry()
        g.source = "Epic"
        g.gid = "E|" + app
        g.name = name
        g.catalog_id = cid
        cp = epic_cover_local(cid)          # 已缓存的官方竖版优先
        if cp:
            g.art = cp
        if loc and exe:
            full = os.path.join(loc, exe)
            if os.path.isfile(full):
                g.launch = full
                g.icon_exe = full
        g.uninstall = "appwiz.cpl"
        sz = fmt_size_bytes(j.get("SizeOnDisk"))
        g.meta = "Epic" + (" · " + sz if sz else "")
        out.append(g)
    return out


def scan_registry():
    out = []
    paths = [r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"]
    hives = [(HKEY_LOCAL_MACHINE, KEY_WOW64_64KEY, paths),
             (HKEY_LOCAL_MACHINE, KEY_WOW64_32KEY, paths),
             (HKEY_CURRENT_USER, 0, paths[:1])]
    for hive, access, plist in hives:
        for p in plist:
            try:
                rk = OpenKey(hive, p, 0, KEY_READ | access)
            except Exception:
                continue
            i = 0
            while True:
                try:
                    key_name = EnumKey(rk, i)
                except OSError:
                    break
                i += 1
                try:
                    k = OpenKey(rk, key_name)
                except Exception:
                    continue
                try:
                    vals = {}
                    for vn in ("DisplayName", "InstallLocation", "DisplayIcon",
                               "UninstallString", "EstimatedSize", "Publisher"):
                        try:
                            vals[vn] = QueryValueEx(k, vn)[0]
                        except Exception:
                            vals[vn] = None
                finally:
                    CloseKey(k)
                name = vals.get("DisplayName")
                if not name:
                    continue
                low = name.lower()
                if any(b in low for b in BLACKLIST):
                    continue
                loc = vals.get("InstallLocation") or ""
                icon = vals.get("DisplayIcon") or ""
                if not loc and not icon:
                    continue
                g = GameEntry()
                g.source = "本地"
                g.gid = "R|" + key_name
                g.name = name
                g.uninstall = vals.get("UninstallString") or None
                g.icon_ico = parse_icon_ico(icon)
                g.icon_exe = parse_icon_exe(icon)
                if not g.icon_exe and loc and os.path.isdir(loc):
                    g.icon_exe = guess_exe(loc)
                if g.icon_exe and os.path.isfile(g.icon_exe):
                    g.launch = g.icon_exe
                parts = ["本地"]
                sz = fmt_size_kb(vals.get("EstimatedSize"))
                if sz:
                    parts.append(sz)
                if vals.get("Publisher"):
                    parts.append(vals["Publisher"])
                g.meta = " · ".join(parts)
                out.append(g)
            CloseKey(rk)
    return out


def _reg_uninstall_entries():
    """遍历卸载表, 产出 (key_name, vals_dict)。专用扫描器共用, 不做任何过滤。"""
    paths = [r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"]
    hives = [(HKEY_LOCAL_MACHINE, KEY_WOW64_64KEY, paths),
             (HKEY_LOCAL_MACHINE, KEY_WOW64_32KEY, paths),
             (HKEY_CURRENT_USER, 0, paths[:1])]
    for hive, access, plist in hives:
        for p in plist:
            try:
                rk = OpenKey(hive, p, 0, KEY_READ | access)
            except Exception:
                continue
            i = 0
            while True:
                try:
                    key_name = EnumKey(rk, i)
                except OSError:
                    break
                i += 1
                try:
                    k = OpenKey(rk, key_name)
                except Exception:
                    continue
                try:
                    vals = {}
                    for vn in ("DisplayName", "InstallLocation", "DisplayIcon",
                               "UninstallString", "EstimatedSize", "Publisher"):
                        try:
                            vals[vn] = QueryValueEx(k, vn)[0]
                        except Exception:
                            vals[vn] = None
                finally:
                    CloseKey(k)
                yield key_name, vals
            try:
                CloseKey(rk)
            except Exception:
                pass


def _child_env():
    """子进程环境: 剔除 Qt 相关变量。

    GameDock 打包成 exe 后, PyInstaller 会给本进程注入 Qt 插件路径类
    环境变量 (QT_PLUGIN_PATH / QT_QPA_PLATFORM_PLUGIN_PATH 等);
    子进程若是别的 Qt 程序 (如 MuMuManager) 继承后会加载到我们打包的
    插件, 版本不匹配 -> 弹 'no Qt platform plugin could be
    initialized' 报错框。所以所有 subprocess 都要传这份干净环境。
    """
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku.startswith(("QT_", "QSG_", "QV4_", "QML2_")):
            continue
        env[k] = v
    return env


def _parse_json_stream(raw):
    """容忍式 JSON 流解析: 依次抠出 {..}/[..] (MuMuManager info 输出可能是
    单对象、数组或多个对象连排)。返回 dict 列表。"""
    dec = json.JSONDecoder()
    objs, i, n = [], 0, len(raw)
    while i < n:
        while i < n and raw[i] not in "{[":
            i += 1
        if i >= n:
            break
        try:
            obj, j = dec.raw_decode(raw, i)
            objs.append(obj)
            i = j
        except Exception:
            i += 1
    flat = []
    for o in objs:
        if isinstance(o, list):
            flat.extend(x for x in o if isinstance(x, dict))
        elif isinstance(o, dict):
            flat.append(o)
    return flat


def _launcher_exe_in(dirpath):
    """在游戏目录两层内找一个像启动器的 exe (给 WeGame 游戏当卡片图标)。"""
    bad = ("uninst", "setup", "crash", "report", "render", "ux", "helper",
           "update", "service", "daemon", "install")
    good = ("client", "launcher", "game", "tcls")
    cands = []
    base_depth = dirpath.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(dirpath):
        if root.count(os.sep) - base_depth > 2:
            dirs[:] = []
            continue
        for fn in files:
            low = fn.lower()
            if not low.endswith(".exe"):
                continue
            if any(b in low for b in bad):
                continue
            if not any(x in low for x in good):
                continue
            full = os.path.join(root, fn)
            try:
                cands.append((os.path.getsize(full), full))
            except OSError:
                pass
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def scan_mumu():
    """MuMu 模拟器: 注册表定位安装目录 -> 官方 MuMuManager 枚举实例。

    每个实例一张卡 (名字用实例名, 如「明日方舟-MuMu模拟器12」),
    启动走官方命令 control -v <index> launch。零猜测, 全部实测可得。
    """
    out = []
    roots = []
    for _key, vals in _reg_uninstall_entries():
        name = (vals.get("DisplayName") or "").lower()
        if "mumu" not in name:
            continue
        icon = _strip_icon_path(vals.get("DisplayIcon") or "") or ""
        us = vals.get("UninstallString") or ""
        # 实测: InstallLocation 常为空, icon 落在 overlay\ 子目录, 卸载串在根目录
        # -> 各候选沿目录向上回溯 (最多 4 级), 找到含 shell\MuMuManager.exe 的根
        cands = [vals.get("InstallLocation") or "",
                 os.path.dirname(icon) if icon else "",
                 os.path.dirname(us) if us else ""]
        for cand in cands:
            cand = (cand or "").strip().strip('"')
            if not cand or not os.path.isdir(cand):
                continue
            cur = os.path.normpath(cand)
            root = None
            for _ in range(4):
                if os.path.isfile(os.path.join(cur, "shell", "MuMuManager.exe")):
                    root = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
            if root:
                roots.append(root)
                break
    seen = set()
    for root in roots:
        rl = os.path.normpath(root).lower()
        if rl in seen:
            continue
        seen.add(rl)
        mgr = os.path.join(root, "shell", "MuMuManager.exe")
        try:
            cp = subprocess.run([mgr, "info", "-v", "all"],
                                capture_output=True, timeout=8,
                                cwd=root, env=_child_env(),
                                creationflags=0x08000000)   # CREATE_NO_WINDOW
            insts = _parse_json_stream(
                cp.stdout.decode("utf-8", "replace"))
        except Exception:
            continue
        player = os.path.join(root, "shell", "MuMuPlayer.exe")
        brand_ico = os.path.join(root, "overlay", "shortcut.ico")
        for inst in insts:
            idx = str(inst.get("index", "0"))
            name = (inst.get("name") or "").strip() or ("MuMu模拟器%s" % idx)
            g = GameEntry()
            g.source = "MuMu"
            g.gid = "M|%s|%s" % (rl, idx)
            g.name = name
            g.launch = [mgr, "control", "-v", idx, "launch"]
            if os.path.isfile(brand_ico):
                g.icon_ico = brand_ico       # 官方橙色联名图标
            if os.path.isfile(player):
                g.icon_exe = player
            g.meta = "MuMu 模拟器"
            out.append(g)
    return out


def scan_wegame():
    """WeGame 平台游戏: 卸载表里路径落在 WeGameApps 下的条目 (英雄联盟等)。

    启动 = 拉起 WeGame 客户端 (直启游戏 exe 会被反作弊拦, 必须经 WeGame 环境)。
    """
    out = []
    wg_exe = None
    entries = []
    for key_name, vals in _reg_uninstall_entries():
        name = vals.get("DisplayName") or ""
        if not name:
            continue
        low = name.lower()
        us = vals.get("UninstallString") or ""
        loc = vals.get("InstallLocation") or ""
        icon = _strip_icon_path(vals.get("DisplayIcon") or "") or ""
        if low == "wegame":                    # 本体: 记客户端 exe 路径
            if icon.lower().endswith(".exe") and os.path.isfile(icon):
                wg_exe = icon
            continue
        if "wegameapps" not in " ".join((us, loc, icon)).lower():
            continue
        if any(b in low for b in BLACKLIST):
            continue
        entries.append((key_name, name, us, loc, icon))
    if not entries:
        return out
    seen_dir = set()
    for key_name, name, us, loc, icon in entries:
        g = GameEntry()
        g.source = "WeGame"
        g.gid = "W|" + key_name
        g.name = name
        g.launch = [wg_exe] if wg_exe else None
        g.uninstall = us or None
        gdir = None
        for cand in (loc, os.path.dirname(us) if us else "",
                     os.path.dirname(icon) if icon else ""):
            cand = (cand or "").strip().strip('"')
            if cand and os.path.isdir(cand):
                gdir = cand
                break
        # 同一游戏常有多条卸载条目 (64/32 位 + HKCU), 按目录去重
        dk = (name.lower(),
              os.path.normpath(gdir).lower() if gdir else key_name)
        if dk in seen_dir:
            continue
        seen_dir.add(dk)
        if gdir:
            g.icon_exe = _launcher_exe_in(gdir)
        cp = wegame_cover_local(name)          # WeGame 官方海报 (缓存过就挂)
        if cp:
            g.art = cp
        g.meta = "WeGame"
        out.append(g)
    return out


def fmt_size_kb(v):
    try:
        kb = int(v)
    except (TypeError, ValueError):
        return None
    if kb <= 0:
        return None
    gb = kb / 1048576.0
    if gb >= 1.0:
        return "%.1f GB" % gb
    return "%d MB" % (kb / 1024.0)


def _strip_icon_path(s):
    if not s:
        return None
    s = s.strip()
    if s.startswith('"'):
        i = s.find('"', 1)
        if i > 0:
            return s[1:i]
        return None
    m = re.match(r"^(.*?\.ico)\s*,?\s*-?\d*\s*$", s, re.I)
    if m:
        return m.group(1)
    m = re.match(r"^([^,]+\.exe)\s*,\s*-?\d+\s*$", s, re.I)
    if m:
        return m.group(1)
    return s if (s.lower().endswith(".exe") or s.lower().endswith(".ico")) else None


def parse_icon_exe(s):
    p = _strip_icon_path(s)
    if p and p.lower().endswith(".exe") and os.path.isfile(p):
        return p
    return None


def parse_icon_ico(s):
    p = _strip_icon_path(s)
    if p and p.lower().endswith(".ico") and os.path.isfile(p):
        return p
    return None


def guess_exe(dirpath):
    try:
        cands = []
        for fn in os.listdir(dirpath):
            if not fn.lower().endswith(".exe"):
                continue
            low = fn.lower()
            if low.startswith(("unins", "setup", "uninst")) or "crash" in low or "unity" in low:
                continue
            full = os.path.join(dirpath, fn)
            if os.path.isfile(full):
                cands.append((os.path.getsize(full), full))
        if not cands:
            return None
        cands.sort(reverse=True)
        return cands[0][1]
    except Exception:
        return None


def custom_cover_path(gid):
    """用户手动导入封面的落盘路径: covers\\M_<gid>.jpg (gid 确定性映射, 重扫不丢)。"""
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(gid))
    return os.path.join(COVER_DIR, "M_%s.jpg" % safe[:100])


def apply_custom_covers(games):
    """手动导入的封面永远压过各平台自动封面; 被压掉的原封面存进 base_art 备用。"""
    for g in games:
        try:
            cp = custom_cover_path(g.gid)
            if os.path.isfile(cp) and g.art != cp:
                g.base_art = g.art
                g.art = cp
        except Exception:
            pass


def scan_all(hidden=None):
    """收各游戏平台的已安装游戏。

    Steam (appmanifest) / Epic (manifest) / WeGame (WeGameApps 卸载条目) /
    MuMu (MuMuManager 枚举实例)。刻意不扫注册表卸载列表全集: 那里混着驱动、
    反作弊、编辑器(TILED 之类)等非游戏条目, 无法可靠过滤 ——
    2026-10-31 UN-G 明确要求只显示游戏; 新平台一律走专用窄扫描。
    hidden=None 时返回全量(含已隐藏, 供右键菜单列出名字)。
    """
    games = []
    for fn in (scan_steam, scan_epic, scan_wegame, scan_mumu):
        try:
            games.extend(fn())
        except Exception:
            pass
    seen = set()
    result = []
    for g in games:
        key = norm_name(g.name)
        if key in seen:
            continue
        seen.add(key)
        result.append(g)
    apply_custom_covers(result)
    if hidden is not None:
        result = [g for g in result if g.gid not in hidden]
    result.sort(key=lambda x: x.name.lower())
    return result


# ================= 图标提取 (ctypes) =================

class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_icon_cache = {}


def hicon_to_qimage(hicon):
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return None
    try:
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = 0
        gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmi), ctypes.byref(bmi))
        # 先取 BITMAP 宽高
        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]
        bm = BITMAP()
        if not gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        w, h = bm.bmWidth, bm.bmHeight
        if w <= 0 or h <= 0:
            return None
        bmi2 = BITMAPINFOHEADER()
        bmi2.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi2.biWidth = w
        bmi2.biHeight = -h
        bmi2.biPlanes = 1
        bmi2.biBitCount = 32
        bmi2.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = gdi32.CreateCompatibleDC(None)
        ok = gdi32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bmi2), 0)
        gdi32.DeleteDC(hdc)
        if not ok:
            return None
        img = QImage(buf, w, h, w * 4, QImage.Format_ARGB32)
        return img.copy()
    finally:
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)


def get_icon(path):
    if not path:
        return None
    key = path.lower()
    if key in _icon_cache:
        return _icon_cache[key]
    img = None
    try:
        if path.lower().endswith(".ico"):
            hicon = ctypes.windll.user32.LoadImageW(
                None, path, 1, 64, 64, 0x10)  # LR_LOADFROMFILE
            if hicon:
                img = hicon_to_qimage(hicon)
                ctypes.windll.user32.DestroyIcon(hicon)
        else:
            sfi = SHFILEINFOW()
            res = ctypes.windll.shell32.SHGetFileInfoW(
                path, 0, ctypes.byref(sfi), ctypes.sizeof(sfi), 0x100)
            if res:
                img = hicon_to_qimage(sfi.hIcon)
                ctypes.windll.user32.DestroyIcon(sfi.hIcon)
    except Exception:
        img = None
    _icon_cache[key] = img
    return img


# ================= 图像工具 =================

def cover_crop(path, w, h, radius=10, ybias=0.5):
    """载入图片, 按 KeepAspectRatioByExpanding 缩放后裁剪到 w×h, 圆角。

    ybias: 裁切窗口的垂直位置比例 (0=最顶, 0.5=居中)。
    Steam 海报的主体人物偏上半部, 详情面板取 ybias=0.25 保住头部。
    """
    pm = QPixmap(path)
    if pm.isNull():
        return None
    scaled = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = int((scaled.height() - h) * ybias)
    y = max(0, min(y, scaled.height() - h))
    crop = scaled.copy(x, y, w, h)
    return rounded(crop, radius)


def rounded(pm, radius):
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def letter_tile(letter, w, h, radius=10):
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, radius, radius)
    p.setClipPath(path)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0, QColor(0x24, 0x2D, 0x3D))
    grad.setColorAt(1, QColor(0x10, 0x14, 0x1E))
    p.fillPath(path, QBrush(grad))
    p.setPen(QColor(0xE8, 0xEE, 0xF8))
    f = QFont("Microsoft YaHei UI", min(52, w // 3))
    f.setBold(True)
    p.setFont(f)
    p.drawText(out.rect(), Qt.AlignCenter, letter)
    p.end()
    return rounded(out, radius)


# ================= 卡片 =================

CARD_W, CARD_H = 164, 234
HOVER_W, HOVER_H = 180, 256
HOLDER_W, HOLDER_H = 196, 264


def cover_crop_pixmap(pm, w, h, radius=10):
    """对已有 QPixmap 按 KeepAspectRatioByExpanding 缩放并中心裁剪, 圆角。"""
    if pm is None or pm.isNull():
        return None
    scaled = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    return rounded(scaled.copy(x, y, w, h), radius)


def draw_glow_frame(pm, radius=10, color=None, width=3):
    """在 pixmap 上画一圈亮边 (悬停态)。"""
    if color is None:
        color = QColor(0x4F, 0xC3, 0xFF, 235)
    out = QPixmap(pm)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(width / 2.0, width / 2.0,
                        out.width() - width, out.height() - width, radius, radius)
    pen = p.pen()
    pen.setColor(color)
    pen.setWidthF(width)
    p.setPen(pen)
    p.drawPath(path)
    p.end()
    return out


def draw_name_band(pm, name, band_h=32):
    """卡片底部画渐变暗带 + 游戏名 (参考用户截图的卡带样式)。"""
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, pm.height() - band_h * 2, 0, pm.height())
    grad.setColorAt(0.0, QColor(0, 0, 0, 0))
    grad.setColorAt(1.0, QColor(2, 4, 8, 205))
    p.fillRect(0, pm.height() - band_h * 2, pm.width(), band_h * 2, QBrush(grad))
    f = QFont("Microsoft YaHei UI", 10)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(0xF2, 0xF6, 0xFC))
    fm = QFontMetrics(f)
    text = fm.elidedText(name, Qt.ElideRight, pm.width() - 16)
    p.drawText(QRect(8, pm.height() - band_h + 2, pm.width() - 14, band_h - 6),
               Qt.AlignLeft | Qt.AlignVCenter, text)
    p.end()
    return pm


def make_hover_canvases(pm, name=""):
    """预渲染正常/悬停两态 pixmap。

    关键设计: 两个状态都是 HOVER_W×HOVER_H, 卡片控件几何尺寸永不变化,
    悬停时只换 pixmap —— 完全避开 QScrollArea 布局里的动态 resize
    (那是 PyQt5 原生 qFatal 崩溃的源头)。
    """
    if pm is None or pm.isNull():
        return None, None

    # 悬停态: 满格 + 亮边 + 游戏名
    hover = draw_glow_frame(cover_crop_pixmap(pm, HOVER_W, HOVER_H))
    draw_name_band(hover, name, 34)

    # 正常态: 透明画布, 小尺寸封面居底居中 (视觉上 = 悬停时向四周放大)
    small = QPixmap(pm)
    draw_name_band(small, name, 28)
    normal = QPixmap(HOVER_W, HOVER_H)
    normal.fill(Qt.transparent)
    p = QPainter(normal)
    p.drawPixmap((HOVER_W - CARD_W) // 2, HOVER_H - CARD_H, small)
    p.end()

    return normal, hover


class Card(QLabel):
    BTN_QSS = {
        "play": ("QPushButton{background:rgba(46,196,143,235);color:#061410;"
                 "font-weight:700;border:none;border-radius:7px;"
                 "padding:0 4px;font-size:11px;}"
                 "QPushButton:hover{background:rgba(59,217,162,255);}"
                 "QPushButton:disabled{background:rgba(35,48,66,200);color:#566;}"),
        "un": ("QPushButton{background:rgba(10,13,20,190);color:#FF9A9A;"
               "border:1px solid rgba(255,255,255,42);border-radius:7px;"
               "padding:0 4px;font-size:11px;}"
               "QPushButton:hover{background:rgba(255,138,138,60);}"
               "QPushButton:disabled{color:#566;}"),
        "hide": ("QPushButton{background:rgba(10,13,20,190);color:#C3CCDC;"
                 "border:1px solid rgba(255,255,255,42);border-radius:7px;"
                 "padding:0 4px;font-size:11px;}"
                 "QPushButton:hover{background:rgba(255,255,255,58);}"),
    }

    def __init__(self, game, dock):
        super().__init__()
        self.game = game
        self.dock = dock
        self._base_art = getattr(game, "base_art", None) or game.art   # 恢复默认封面用
        self.setAlignment(Qt.AlignBottom | Qt.AlignHCenter)
        self.setCursor(Qt.PointingHandCursor)

        pm = None
        if game.art:
            pm = cover_crop(game.art, CARD_W, CARD_H)
        if pm is None:
            ic = get_icon(game.icon_exe or game.icon_ico)
            if ic is not None:
                pm = rounded(QPixmap.fromImage(ic).scaled(
                    CARD_W, CARD_H, Qt.KeepAspectRatio, Qt.SmoothTransformation), 10)
        if pm is None:
            pm = letter_tile(game.name[:1] or "?", CARD_W, CARD_H)

        self.pm_normal, self.pm_hover = make_hover_canvases(pm, game.name)
        if self.pm_normal is None:
            self.pm_normal = letter_tile("?", HOVER_W, HOVER_H)
            self.pm_hover = self.pm_normal
        self.setPixmap(self.pm_normal)
        self.setFixedSize(HOVER_W, HOVER_H)  # 永不改变

        # 悬停按钮行: 直接浮在卡片上 (代替原详情面板)
        # 布局: 卸载(小) · 游玩(主, 居中大) · 隐藏(小) —— 两侧缩小防误触
        # 全部尺寸随卡片缩放系数同比缩放; 游玩按钮吃行内剩余宽度, 任何缩放下不溢出
        f = dock.card_scale / 100.0
        mx = max(3, int(6 * f))            # 行左右边距
        spacing = max(2, int(5 * f))       # 按钮间距
        bh = max(18, int(27 * f))          # 按钮高
        small_w = max(22, int(44 * f))     # 卸载/隐藏宽
        avail = HOVER_W - 2 * mx           # 行内可用宽度
        play_w = max(30, avail - 2 * small_w - 2 * spacing)   # 游玩 = 剩余
        self.btn_row = QWidget(self)
        self.btn_row.setStyleSheet("background:transparent;")
        bl = QHBoxLayout(self.btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(spacing)
        self.b_un = QPushButton("卸载")
        self.b_play = QPushButton("游玩")
        self.b_hide = QPushButton("隐藏")
        for b in (self.b_un, self.b_play, self.b_hide):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(bh)
            bl.addWidget(b, 0)
        self.b_play.setFixedWidth(play_w)   # 主按钮保持大
        self.b_un.setFixedWidth(small_w)
        self.b_hide.setFixedWidth(small_w)
        fs_main = max(8, int(round(11 * f)))     # 主按钮字号
        fs_small = max(7, int(round(10 * f)))    # 小按钮字号
        self.b_un.setStyleSheet(
            self.BTN_QSS["un"].replace("font-size:11px", "font-size:%dpx" % fs_small))
        self.b_play.setStyleSheet(
            self.BTN_QSS["play"].replace("font-size:11px", "font-size:%dpx" % fs_main))
        self.b_hide.setStyleSheet(
            self.BTN_QSS["hide"].replace("font-size:11px", "font-size:%dpx" % fs_small))
        self.b_play.setEnabled(bool(game.launch))
        self.b_un.setEnabled(bool(game.uninstall))
        self.b_play.clicked.connect(self._on_play)
        self.b_un.clicked.connect(self._on_un)
        self.b_hide.clicked.connect(self._on_hide)
        # 名字暗带 (高约34px) 上方一行, 稍加宽以容纳三键; 位置/行高同比缩放
        self.btn_row.setGeometry(mx, HOVER_H - int(67 * f),
                                 avail, max(20, int(29 * f)))
        self.btn_row.hide()

        # 长按拖拽排序: 按住 1000ms 进入拖拽 (光标变抓手); 快速点按仍是启动。
        # 1 秒是两个手势的分界线 (用户 2026-11-01 定): 满秒 = 移动卡片,
        # 按下立刻滑 (不满秒) = 纯滚动, 永不进换位。
        self._drag = False
        self._scrolling = False       # 按住左右拖 = 整条滚动 (触摸屏式)
        self._grab = QPoint(0, 0)     # 按点在卡内偏移, 拖拽时保持手感
        self._press_t = QTimer(self)
        self._press_t.setSingleShot(True)
        self._press_t.setInterval(1000)
        self._press_t.timeout.connect(self._drag_start)

    # ---- 长按拖拽 ----

    def reload_art(self):
        """封面被替换/恢复后原地重渲染两态画布 (不动几何, 不重建整行)。"""
        try:
            pm = None
            if self.game.art:
                pm = cover_crop(self.game.art, CARD_W, CARD_H)
            if pm is None:
                ic = get_icon(self.game.icon_exe or self.game.icon_ico)
                if ic is not None:
                    pm = rounded(QPixmap.fromImage(ic).scaled(
                        CARD_W, CARD_H, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation), 10)
            if pm is None:
                pm = letter_tile(self.game.name[:1] or "?", CARD_W, CARD_H)
            self.pm_normal, self.pm_hover = make_hover_canvases(pm, self.game.name)
            if self.pm_normal is None:
                self.pm_normal = letter_tile("?", HOVER_W, HOVER_H)
                self.pm_hover = self.pm_normal
            if not self._drag:
                self.setPixmap(self.pm_hover if self.underMouse()
                               else self.pm_normal)
        except Exception:
            log_exc("Card.reload_art")

    def _drag_start(self):
        try:
            if not self.isVisible():
                return
            self._drag = True
            self.dock._card_drag = True
            self.setPixmap(self.pm_hover)
            self.btn_row.hide()
            self.raise_()
            self.setCursor(Qt.ClosedHandCursor)
            self.dock.drag_begin(self)
            self.dock._dragging_card = self   # 仅鼠标路径记录 (边缘自动滚用)
        except Exception:
            log_exc("Card._drag_start")

    def _on_play(self):
        try:
            launch_game(self.game)
        except Exception:
            log_exc("Card._on_play")

    def _on_un(self):
        try:
            uninstall_game(self.game)
        except Exception:
            log_exc("Card._on_un")

    def _on_hide(self):
        try:
            self.dock.hide_game(self.game)
        except Exception:
            log_exc("Card._on_hide")

    def enterEvent(self, e):
        try:
            # 手柄焦点让位给鼠标: 鼠标一碰到别的卡就交出焦点
            if getattr(self.dock, "_gp_focus_i", None) is not None:
                self.dock._gp_set_focus(None)
            if not self.dock._card_drag:      # 拖拽进行中不弹别的卡的按钮
                self.setPixmap(self.pm_hover)
                self.btn_row.show()
                self.btn_row.raise_()
        except Exception:
            log_exc("Card.enterEvent")
        super().enterEvent(e)

    # ---- 手柄焦点 ----

    def set_gfocus(self, on):
        """手柄选中态: 复用悬停视觉 (亮圈 + 按钮行), 不动几何。"""
        try:
            if self._drag:
                return
            if on:
                self.setPixmap(self.pm_hover)
                self.btn_row.show()
                self.btn_row.raise_()
            else:
                self.setPixmap(self.pm_normal)
                self.btn_row.hide()
                self.set_gp_hints(False)
        except Exception:
            log_exc("Card.set_gfocus")

    def set_gp_hints(self, on):
        """手柄连着且本卡被选中时, 三个按钮浮出手柄键位提示。"""
        try:
            if on:
                self.b_play.setText("A 游玩" if self.b_play.isEnabled() else "游玩")
                self.b_un.setText("X 卸载" if self.b_un.isEnabled() else "卸载")
                self.b_hide.setText("B 隐藏")
            else:
                self.b_play.setText("游玩")
                self.b_un.setText("卸载")
                self.b_hide.setText("隐藏")
        except Exception:
            log_exc("Card.set_gp_hints")

    def leaveEvent(self, e):
        try:
            if not self._drag:
                self.setPixmap(self.pm_normal)
                self.btn_row.hide()
        except Exception:
            log_exc("Card.leaveEvent")
        super().leaveEvent(e)

    def contextMenuEvent(self, e):
        try:
            e.accept()          # 不再 super(): 防事件冒泡到主窗二次开菜单顶掉本卡菜单
            self.dock.open_menu(e.globalPos(), self)   # 带上卡片: 菜单可针对本卡操作
        except Exception:
            log_exc("Card.contextMenu")

    def mousePressEvent(self, e):
        try:
            if e.button() == Qt.LeftButton:
                self._grab = e.pos()
                self._press_t.start()
        except Exception:
            log_exc("Card.mousePress")
        super().mousePressEvent(e)
        # 卡片上的按压就地消化, 不冒泡到 Dock: QLabel 默认 ignore 会把事件
        # 传给 Dock.mousePressEvent -> 顺手武装了拖动滚动, 之后换位拖动时
        # Dock.mouseMove 每帧又滚一遍 (真机: 抓手模式下整条跟着动, 2026-11-01)。
        e.accept()

    def mouseMoveEvent(self, e):
        try:
            if self._drag:
                self.dock.drag_move(self)
            elif self._scrolling:
                self.dock.scroll_drag(e)   # 按住拖动: 整条跟手
            elif self._press_t.isActive() and (
                    (e.pos() - self._grab).manhattanLength() > 60):
                # 未满 1 秒就大幅移动 = 明确想滚 -> 转整条滚动, 且永不进换位。
                # 小幅移动 (手抖) 不打断长按计时。
                self._press_t.stop()
                self._scrolling = True
                self.dock.scroll_begin(e)
            # 不冒泡: 否则事件传给 Dock.mouseMove, 换位/滚动会被 Dock 再滚一遍
            e.accept()
            return
        except Exception:
            log_exc("Card.mouseMove")
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        try:
            if self._scrolling:
                self._scrolling = False
                self.dock.scroll_end()
            if e.button() == Qt.LeftButton:
                self._press_t.stop()
                if self._drag:
                    self._drag = False
                    self.dock._card_drag = False
                    self.setCursor(Qt.PointingHandCursor)
                    self.dock.drop_card(self)
                    e.accept()
                    return                    # 拖拽落下不触发启动
                # 单击不启动 (防长按拖动误触); 双击才启动
            e.accept()                        # 不冒泡, 同 mousePress/mouseMove
        except Exception:
            log_exc("Card.mouseRelease")
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        try:
            if e.button() == Qt.LeftButton and not self._drag:
                self._press_t.stop()
                launch_game(self.game)
        except Exception:
            log_exc("Card.mouseDoubleClick")
        super().mouseDoubleClickEvent(e)


# 游戏启动后的手柄静默截止时刻 (time.time() 秒)。
# 游戏从拉起到窗口盖住 dock 有延迟 (Steam 还要先过客户端), 这段窗口期
# 用户已在游戏里按手柄, 没有遮挡判据可依 -> 靠启动冷却兜底 (2026-10-31)。
_LAUNCH_MUTE_UNTIL = 0.0

# 遮挡检测用的 API 签名 (POINT 按值传, 不声明会按指针传 -> 崩溃)
_user32 = ctypes.windll.user32
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND


def launch_game(g):
    global _LAUNCH_MUTE_UNTIL
    try:
        if isinstance(g.launch, (list, tuple)):
            # 命令行启动动作 (MuMu: MuMuManager control -v N launch / WeGame: 拉客户端)
            exe = g.launch[0]
            subprocess.Popen(list(g.launch), cwd=os.path.dirname(exe),
                             close_fds=True, env=_child_env())
        elif g.is_steam:
            os.startfile(g.launch)  # steam:// 协议
        elif g.launch and os.path.isfile(g.launch):
            subprocess.Popen([g.launch], cwd=os.path.dirname(g.launch),
                             close_fds=True, env=_child_env())
        else:
            _info("没找到可执行文件，可以卸载或隐藏这条。")
            return
        _LAUNCH_MUTE_UNTIL = time.time() + 10.0   # 启动后手柄静默 10s
    except Exception as ex:
        _info("启动失败: %s" % ex)


def uninstall_game(g):
    try:
        if not g.uninstall:
            return
        if g.uninstall.startswith("steam://") or g.uninstall == "appwiz.cpl":
            os.startfile(g.uninstall)
        else:
            subprocess.Popen(g.uninstall, shell=True, env=_child_env())
    except Exception as ex:
        _info("卸载启动失败: %s" % ex)


def _info(msg):
    from PyQt5.QtWidgets import QMessageBox
    box = QMessageBox()
    box.setWindowTitle(APP_NAME)
    box.setText(msg)
    box.setWindowFlags(box.windowFlags() | Qt.WindowStaysOnTopHint)
    box.show()          # 非模态! exec_ 的模态循环曾在遮罩/菜单开着时弹出,
    box.raise_()        # 把整个输入链锁死 (真机事故 2026-10-31)。非模态框
    box.activateWindow()  # 永远不会拦输入, 最坏情况只是被盖住。


def log_exc(where):
    """把异常写进 %APPDATA%\\GameDock\\error.log, 防止 PyQt 因未捕获异常直接崩进程。"""
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(os.path.join(CFG_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write("---- %s @ %s ----\n" % (where, __import__("datetime").datetime.now()))
            traceback.print_exc(file=f)
    except Exception:
        pass


# ================= 顶部工具栏 =================

class TopBar(QWidget):
    """搜索框: 玻璃质感胶囊, 悬在卡带坞正上方居中, 贴着卡片。

    背景手写 paintEvent (样式表背景在 无边框+半透明+Tool 窗口 上不可靠,
    DetailPanel 同款画法已真机验证)。
    """

    def __init__(self, dock):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.dock = dock
        self.setFixedSize(280, 44)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 5, 12, 5)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索游戏")
        self.search.setStyleSheet(
            "QLineEdit{background:transparent;border:none;color:#F4F8FE;"
            "font-size:13px;padding:0 2px;}")
        from PyQt5.QtGui import QPalette
        pal = self.search.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(255, 255, 255, 125))
        self.search.setPalette(pal)
        self.search.textChanged.connect(self._on_search)
        lay.addWidget(self.search)

    def paintEvent(self, e):
        """液态玻璃胶囊 (Liquid Glass 风): 玻璃基底 + 顶部镜面高光
        + 底部内侧反光 + 边缘透镜亮边。"""
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            w, h = self.width(), self.height()
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                21, 21)

            # 1) 玻璃基底: 顶部微亮 -> 深色半透明 (壁纸透过)
            base = QLinearGradient(0, 0, 0, h)
            base.setColorAt(0.00, QColor(255, 255, 255, 62))
            base.setColorAt(0.18, QColor(255, 255, 255, 14))
            base.setColorAt(0.45, QColor(18, 26, 38, 125))
            base.setColorAt(1.00, QColor(8, 12, 20, 190))
            p.fillPath(path, QBrush(base))

            # 2) 顶部镜面高光: 径向柔光 (液态玻璃的 specular)
            spec = QRadialGradient(QPointF(w * 0.5, -h * 0.38), h * 1.2)
            spec.setColorAt(0.00, QColor(255, 255, 255, 105))
            spec.setColorAt(0.45, QColor(255, 255, 255, 30))
            spec.setColorAt(1.00, QColor(255, 255, 255, 0))
            p.fillPath(path, QBrush(spec))

            # 3) 底部内侧反光: 光在玻璃下缘的回弹
            bot = QLinearGradient(0, h * 0.5, 0, h)
            bot.setColorAt(0.00, QColor(255, 255, 255, 0))
            bot.setColorAt(1.00, QColor(255, 255, 255, 30))
            p.fillPath(path, QBrush(bot))

            # 4) 边缘透镜效应: 外圈细亮边 + 内环 (光在玻璃边缘的折射感)
            p.setPen(QPen(QColor(255, 255, 255, 85), 1.2))
            p.drawPath(path)
            inner = QPainterPath()
            inner.addRoundedRect(QRectF(self.rect()).adjusted(1.8, 1.8, -1.8, -1.8),
                                 19, 19)
            p.setPen(QPen(QColor(255, 255, 255, 42), 1))
            p.drawPath(inner)
            p.end()
        except Exception:
            log_exc("TopBar.paintEvent")

    def show_at(self, global_pos):
        """右键菜单唤出: 以光标为中心弹出胶囊, 越界则收回屏内。"""
        try:
            av = QGuiApplication.primaryScreen().availableGeometry()
            x = global_pos.x() - self.width() // 2
            y = global_pos.y() - self.height() // 2
            x = max(av.left() + 8, min(x, av.right() - self.width() - 8))
            y = max(av.top() + 8, min(y, av.bottom() - self.height() - 8))
            self.move(x, y)
            self.show()
            self.raise_()
            self.activateWindow()
            self.search.setFocus()
        except Exception:
            log_exc("TopBar.show_at")

    def close_and_clear(self):
        """收起搜索框并清空关键词 (textChanged -> set_search("") 自动恢复全部)。"""
        try:
            self.hide()
            if self.search.text():
                self.search.clear()
        except Exception:
            log_exc("TopBar.close_and_clear")

    def keyPressEvent(self, e):
        try:
            if e.key() == Qt.Key_Escape:
                self.dock.dismiss_overlays()
                return
        except Exception:
            log_exc("TopBar.keyPress")
        super().keyPressEvent(e)

    def _on_search(self, text):
        try:
            self.dock.set_search(text)
        except Exception:
            log_exc("TopBar._on_search")


# ================= 主窗口 =================

class _Shield(QWidget):
    """全屏透明遮罩: 右键菜单 / 搜索胶囊弹出时垫在弹层底下。
    点到它 = 点到了弹层以外的地方 (含桌面壁纸), 一律收起弹层。
    弹层自己在遮罩之上, 点击不受影响。"""

    def __init__(self, dock):
        # StaysOnTop 必须有: 不置顶的话层级压不住, 桌面壁纸上的点击根本
        # 到不了遮罩 (真机实锤过两次: 搜索框/尺寸框点桌面不消失都是这个根因)。
        # 遮罩只在弹层开着时显示, 平时不占位, 置顶无害。
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool |
                         Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.dock = dock

    def paintEvent(self, ev):
        """必须铺一层 alpha=1 的底: 全透明 (alpha=0) 的分层窗口在 Windows
        上按像素命中测试会整体点击穿透——这就是'点桌面收不掉弹层'的真根因。
        alpha=1 肉眼不可见, 但 Windows 视为实心, 点击接得住。"""
        from PyQt5.QtGui import QPainter, QColor
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))

    def mousePressEvent(self, e):
        try:
            self.dock.dismiss_overlays()
        except Exception:
            log_exc("Shield.mousePress")


class ScaleBox(QWidget):
    """『调整尺寸』弹窗: 百分比框, 滚轮/上下箭头每下 5%, 改动防抖后实时生效。"""

    def __init__(self, dock):
        # Qt.Popup = 与右键菜单同类窗口: 点弹窗外任何地方 (含桌面壁纸)
        # Windows 层面自动关闭——真机验证过最可靠的"点外即关"机制。
        super().__init__(None, Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.dock = dock
        self.setFixedSize(196, 66)
        panel = QFrame(self)
        panel.setObjectName("scalePanel")
        panel.setGeometry(0, 0, 196, 66)
        panel.setStyleSheet("""
            #scalePanel { background: rgba(14,18,28,238);
              border:1px solid rgba(255,255,255,42); border-radius: 12px; }
            QLabel { color:#DFE7F4; font-size:13px; background:transparent; }
            QSpinBox { background: rgba(255,255,255,26); color:#FFFFFF;
              font-size:14px; border:1px solid rgba(255,255,255,50);
              border-radius:8px; padding:3px 8px; }
            QSpinBox::up-button, QSpinBox::down-button {
              width:20px; border:none; background:rgba(255,255,255,20);
              border-radius:5px; margin:2px; }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
              background:rgba(79,195,255,70); }
        """)
        lab = QLabel("卡片大小", panel)
        lab.setGeometry(14, 9, 100, 18)
        self.spin = QSpinBox(panel)
        self.spin.setRange(50, 150)
        self.spin.setSingleStep(5)
        self.spin.setSuffix(" %")
        self.spin.setAlignment(Qt.AlignCenter)
        self.spin.setGeometry(14, 32, 168, 26)
        self.spin.valueChanged.connect(self._on_val)
        self._syncing = False

    def hideEvent(self, e):
        """Popup 被点外自动关掉时 (不经 dismiss_overlays), 撤掉遮罩。"""
        try:
            self.dock.shield.hide()
        except Exception:
            log_exc("ScaleBox.hideEvent")
        super().hideEvent(e)

    def keyPressEvent(self, e):
        try:
            if e.key() == Qt.Key_Escape:
                self.dock.dismiss_overlays()
                return
        except Exception:
            log_exc("ScaleBox.keyPress")
        super().keyPressEvent(e)

    def sync_value(self, pct):
        """回显当前值, 不触发 valueChanged。"""
        self._syncing = True
        self.spin.setValue(pct)
        self._syncing = False

    def _on_val(self, v):
        try:
            if not self._syncing:
                self.dock.schedule_scale(v)
        except Exception:
            log_exc("ScaleBox._on_val")


class Dock(QWidget):
    def __init__(self):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnBottomHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hidden = load_hidden()
        self.games = []
        self._all_games = []          # 含已隐藏的全量, 供右键菜单列名字
        self._sources = ["全部"]

        av = QGuiApplication.primaryScreen().availableGeometry()
        self.dock_w = av.width()  # 铺满屏幕宽度, 以屏幕边缘为显示边缘
        self.dock_h = 300
        self.setGeometry(0, av.height() - self.dock_h + 2,
                         self.dock_w, self.dock_h)

        self.box = QFrame(self)
        self.box.setObjectName("dockBox")
        self.box.setGeometry(0, 0, self.dock_w, self.dock_h)
        self.box.setStyleSheet("""
            #dockBox { border: none; border-radius: 0px;
              background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0   rgba(6,9,15,0),
                stop:0.5 rgba(6,9,15,90),
                stop:1   rgba(4,6,10,215)); }
        """)

        lay = QHBoxLayout(self.box)
        lay.setContentsMargins(18, 14, 18, 12)

        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroller.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.scroller.viewport().installEventFilter(self)

        self.row = QWidget()
        self.row.setStyleSheet("background:transparent;")
        self.row_lay = QHBoxLayout(self.row)
        self.row_lay.setContentsMargins(0, 0, 0, 0)
        self.row_lay.setSpacing(0)  # 卡位自带透明边距
        self.row_lay.addStretch(1)
        self.scroller.setWidget(self.row)

        lay.addWidget(self.scroller, 1)

        # 右键菜单 (卡片和底条空白处都能唤出)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self.open_menu(self.mapToGlobal(p)))

        # 搜索框浮窗 (平时隐藏; 右键菜单里的『搜索游戏』唤出, 弹在鼠标位置)
        self.f_source = "全部"
        self.f_text = ""
        self.sort_mode = load_sort_mode()   # name / lastplayed / playtime / genre / custom
        self.custom_order = load_custom_order()   # 拖拽排序的 gid 顺序
        self._card_drag = False             # 有卡片正在被长按拖拽
        self._dragging_card = None          # 换位拖拽中的卡 (边缘自动滚用)
        self._edge_dir = 0                  # 拖到坞左右边缘的自动滚方向 (-1/0/+1)
        self._edge_t = QTimer(self)
        self._edge_t.setInterval(16)
        self._edge_t.timeout.connect(self._edge_tick)
        self._dragscroll_x = None           # 按住拖动滚动: 上一帧全局 x (None = 不在滚动)
        self._drag_base = None              # 拖拽中的基准槽位 [(卡, x)]
        self._drag_gap = None               # 当前让位槽下标
        self._display_gids = []             # 当前显示顺序 (排序后的)
        self.topbar = TopBar(self)
        self.topbar.hide()

        # 弹层状态: 右键菜单 / 搜索胶囊 + 全屏透明遮罩
        # (点遮罩 = 点了弹层以外的地方 -> 全部收起)
        self._menu_ref = None
        self._menu_pos = None
        self.shield = _Shield(self)
        # 全局点击过滤: 补上 Qt 窗口内的"点外收起" (遮罩只兜得住桌面那部分)
        QApplication.instance().installEventFilter(self)

        # 尽头撞壁回弹 (iOS rubber-band): 弹簧模拟, 只平移整行, 不动单卡几何
        self._ov = 0.0            # 溢出位移 (px, 正 = 内容被拉向右)
        self._ov_vel = 0.0        # 弹簧速度
        self._ov_timer = QTimer(self)
        self._ov_timer.setInterval(16)
        self._ov_timer.timeout.connect(self._ov_tick)

        # ---- Xbox 手柄 (XInput 轮询, 30ms) ----
        self._gp_on = False          # 手柄在位
        self._gp_prev_wB = 0         # 上一帧按键位 (沿检测)
        self._gp_focus_i = None      # 手柄焦点卡下标 (None = 未选中)
        self._gp_move = None         # 手柄移动模式 {"card","orig","gap"}
        self._gp_refocus_gid = None  # 重建后按 gid 找回焦点
        self._gp_dir = 0             # 当前按住的方向 (-1/0/+1)
        self._gp_hold_t = 0.0        # 该方向开始按住的时刻
        self._gp_last_step = 0.0     # 上次步进时刻
        self._gp_y_t = 0.0           # Y 按下时刻 (长按进移动模式)
        self._gp_mdir = 0            # 菜单导航: 当前按住方向 (0/+1/-1)
        self._gp_mhold = 0.0         # 菜单导航: 该方向开始时刻
        self._gp_mlast = 0.0         # 菜单导航: 上次步进时刻
        self._gp_sub_open = False    # 菜单导航: 二级菜单是否显式打开
        self._gp_lx_r = False        # 菜单导航: 上一帧摇杆右推 (沿检测)
        self._gp_lx_l = False        # 菜单导航: 上一帧摇杆左推 (沿检测)
        self._gp_timer = QTimer(self)
        self._gp_timer.setInterval(30)
        self._gp_timer.timeout.connect(self._gp_poll)
        self._gp_timer.start()

        # 卡片缩放 (调整尺寸弹窗): 防抖 250ms 后整体重渲染
        self.card_scale = load_scale()
        self._scale_pending = None
        self._scale_t = QTimer(self)
        self._scale_t.setSingleShot(True)
        self._scale_t.setInterval(250)
        self._scale_t.timeout.connect(self._scale_apply_tick)
        self.scalebox = ScaleBox(self)
        self.scalebox.hide()
        self.apply_scale(self.card_scale)

        # ---- 托盘图标: 左键单击切显示/隐藏, 右键出主菜单 ----
        self._tray_hidden = False       # 坞是否处于"藏进托盘"的主动隐藏态
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("GameDock")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        QTimer.singleShot(50, self.rescan)

    def _on_tray_activated(self, reason):
        try:
            if reason == QSystemTrayIcon.Trigger:       # 左键: 显示/隐藏切换
                if self.isVisible():
                    self.hide()
                    self._tray_hidden = True
                else:
                    self._tray_hidden = False
                    self.show()
                    self.raise_()
            elif reason == QSystemTrayIcon.Context:     # 右键: 主菜单
                self._menu_card = None          # 清掉上次卡片目标, 托盘菜单无单卡项
                m = self.build_menu()
                self._menu_ref = m
                m.aboutToHide.connect(
                    lambda: setattr(self, "_menu_ref", None))
                m.popup(QCursor.pos())
        except Exception:
            log_exc("Dock._on_tray_activated")

    def toggle_autostart(self):
        try:
            mode = load_startup()
            if mode == "off":
                save_startup("show")        # 默认开机自动显示
                set_autostart(True, tray=False)
            else:
                save_startup("off")
                set_autostart(False)
        except Exception:
            log_exc("Dock.toggle_autostart")

    def set_boot_mode(self, mode):
        """开机自启已开时选择: show=直接显示 / hide=隐藏至托盘; 同步注册表参数。"""
        try:
            if mode not in ("show", "hide"):
                return
            save_startup(mode)
            if load_startup() != "off" and autostart_enabled():
                set_autostart(True, tray=(mode == "hide"))
        except Exception:
            log_exc("Dock.set_boot_mode")

    # ---- 过滤 ----

    def set_source(self, src):
        try:
            self.f_source = src
            self._rebuild_cards()
        except Exception:
            log_exc("Dock.set_source")

    def set_sort(self, mode):
        try:
            if mode not in ("name", "lastplayed", "playtime", "genre", "custom"):
                return
            self.sort_mode = mode
            save_sort_mode(mode)
            self._rebuild_cards()
        except Exception:
            log_exc("Dock.set_sort")

    def set_search(self, text):
        try:
            self.f_text = (text or "").strip()
            self._rebuild_cards()
        except Exception:
            log_exc("Dock.set_search")

    def _filtered_games(self):
        out = []
        for g in self.games:
            if self.f_source != "全部" and g.source != self.f_source:
                continue
            if self.f_text and self.f_text.lower() not in g.name.lower():
                continue
            out.append(g)
        return out

    def _rebuild_cards(self):
        while self.row_lay.count() > 0:
            item = self.row_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        shown = self._filtered_games()
        m = self.sort_mode
        if m == "lastplayed":       # 最近启动在最前, 没玩过的垫底
            shown.sort(key=lambda g: (-g.lastplayed, g.name.lower()))
        elif m == "playtime":       # 玩得最久的在最前
            shown.sort(key=lambda g: (-g.playtime, g.name.lower()))
        elif m == "genre":          # RPG/动作/... 固定大类顺序, 类型内按名称
            order = {n: i for i, n in enumerate(GENRE_ORDER)}
            shown.sort(key=lambda g: (order.get(g.genre or "未分类",
                                                  len(GENRE_ORDER)),
                                      g.name.lower()))
        elif m == "custom":         # 用户拖出来的顺序, 没排过的垫底按名称
            pos = {gid: i for i, gid in enumerate(self.custom_order)}
            shown.sort(key=lambda g: (pos.get(g.gid, len(self.custom_order)),
                                      g.name.lower()))
        else:                       # 默认: 名称顺序
            shown.sort(key=lambda g: g.name.lower())
        self._display_gids = [g.gid for g in shown]   # 当前显示顺序, 拖拽以此为基准
        # 两侧弹性空白 -> 结果组居中显示
        self.row_lay.addStretch(1)
        for g in shown:
            self.row_lay.addWidget(Card(g, self))
        self.row_lay.addStretch(1)
        # 手柄: 重建后焦点清零, 若有找回标记 (隐藏/移动落下) 则定位回同一张
        self._gp_focus_i = None
        if getattr(self, "_gp_refocus_gid", None):
            gid = self._gp_refocus_gid
            self._gp_refocus_gid = None
            if gid in self._display_gids:
                self._gp_set_focus(self._display_gids.index(gid))

    # ---- 布局/滚动 ----

    # 按住左右拖 = 整条跟手滑动 (触摸屏式), 卡片上和空白处都可用。
    # 与长按排序的分界 (用户 2026-11-01 定): 按住不动满 1 秒 = 换位模式
    # (此后滚动完全不介入, 只有拖到左右边缘才自动滚); 未满 1 秒就大幅
    # 移动 (>60px) = 纯滚动, 永不进换位。
    def scroll_begin(self, e):
        try:
            self._dragscroll_x = e.globalX()
        except Exception:
            log_exc("Dock.scroll_begin")

    def scroll_drag(self, e):
        try:
            if self._dragscroll_x is None:
                return
            dx = e.globalX() - self._dragscroll_x
            self._dragscroll_x = e.globalX()
            if dx:
                bar = self.scroller.horizontalScrollBar()
                bar.setValue(max(0, min(bar.maximum(), bar.value() - dx)))
        except Exception:
            log_exc("Dock.scroll_drag")

    def scroll_end(self):
        self._dragscroll_x = None

    def mousePressEvent(self, e):
        try:
            if e.button() == Qt.LeftButton:
                self.scroll_begin(e)          # 空白处按下: 同样进入拖动滚动
        except Exception:
            log_exc("Dock.mousePress")
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        try:
            # 换位拖拽进行中: Dock 一律不滚 (双保险, 滚动只允许边缘自动滚)
            if self._card_drag:
                return
            if self._dragscroll_x is not None:
                self.scroll_drag(e)
        except Exception:
            log_exc("Dock.mouseMove")
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        try:
            self.scroll_end()
        except Exception:
            log_exc("Dock.mouseRelease")
        super().mouseReleaseEvent(e)

    def eventFilter(self, obj, ev):
        try:
            t = ev.type()
            # 滚轮: 只要事件落在坞内 (卡片/行/滚动区任何一层) 都接管。
            # 只盯 viewport 会漏——卡片上的滚轮事件传播不到 viewport (真机实测)。
            if t == QEvent.Wheel and isinstance(obj, QWidget):
                w, hit = obj, False
                while w is not None:
                    if w is self.scroller.viewport():
                        hit = True
                        break
                    w = w.parentWidget()
                if hit:
                    bar = self.scroller.horizontalScrollBar()
                    d = ev.angleDelta().y()
                    if d != 0:
                        # 真机取证 (wheel_log.txt, 已删): 一格滚轮 = 3 个完整 d=120 事件,
                        # 同一毫秒内三连发 (Card/QWidget/QWidget)。
                        # 双保险: a) 攒满 120 才动 (防拆分小事件)
                        #         b) 80ms 时间窗内不重复走格 (防同刻度连发)
                        acc = getattr(self, "_wheel_acc", 0)
                        if acc * d < 0:          # 方向反转 -> 丢弃旧攒量
                            acc = 0
                        acc += d
                        if abs(acc) < 120:       # 没攒满: 先不动
                            self._wheel_acc = acc
                            return True
                        self._wheel_acc = 0
                        import time as _t
                        now = _t.time()
                        if now - getattr(self, "_wheel_last", 0.0) < 0.08:
                            return True          # 同一刻度的重复事件 -> 吞掉
                        self._wheel_last = now
                        up = acc > 0
                        if up and bar.value() <= 0:
                            self._ov_kick(26.0)      # 左尽头再往上滚 -> 拉出界
                        elif not up and bar.value() >= bar.maximum() and bar.maximum() > 0:
                            self._ov_kick(-26.0)     # 右尽头再往下滚 -> 拉出界
                        else:
                            # 一步 = 一张卡位宽 (HOVER_W, 随卡片缩放同步), 并对齐卡位格点
                            step = max(1, HOVER_W)
                            if up:    # 上取整对齐后左移一格
                                nxt = -(-bar.value() // step) * step - step
                            else:     # 下取整对齐后右移一格
                                nxt = (bar.value() // step) * step + step
                            bar.setValue(max(0, min(bar.maximum(), nxt)))
                    return True
            # 全局点击过滤: 搜索框/尺寸框开着时, 点它以外的地方 -> 收起 (桌面由遮罩兜底)
            if t == QEvent.MouseButtonPress and (
                    self.topbar.isVisible() or self.scalebox.isVisible()):
                w = obj
                hit = False
                while w is not None:
                    if w is self.topbar or w is self.scalebox:
                        hit = True
                        break
                    w = w.parentWidget()
                if not hit:
                    self.dismiss_overlays()
                    return True   # 吞掉这一击, 防止误触到卡片
        except Exception:
            log_exc("Dock.eventFilter")
        return super().eventFilter(obj, ev)

    def _ov_kick(self, v):
        """在尽头继续滚: 给弹簧一个冲量 (带阻尼限幅, 最多拉出约 72px)。"""
        try:
            self._ov_vel += v
            if abs(self._ov_vel) > 44.0:
                self._ov_vel = 44.0 if self._ov_vel > 0 else -44.0
            if self._ov > 72.0:
                self._ov, self._ov_vel = 72.0, 0.0
            elif self._ov < -72.0:
                self._ov, self._ov_vel = -72.0, 0.0
            if not self._ov_timer.isActive():
                self._ov_timer.start()
        except Exception:
            log_exc("Dock._ov_kick")

    def _ov_tick(self):
        """16ms 一帧的弹簧积分: 位移回零, 带一点过冲 (Q 弹手感)。"""
        try:
            bar = self.scroller.horizontalScrollBar()
            acc = -0.18 * self._ov - 0.20 * self._ov_vel
            self._ov_vel += acc
            self._ov += self._ov_vel
            if abs(self._ov) < 0.4 and abs(self._ov_vel) < 0.4:
                self._ov = 0.0
                self._ov_vel = 0.0
                self._ov_timer.stop()
            # 整行平移; 基准位置 = -滚动条值, 动画结束自动归位
            self.row.move(-bar.value() + int(round(self._ov)), 0)
        except Exception:
            log_exc("Dock._ov_tick")

    def rescan(self):
        try:
            self._all_games = scan_all()  # 全量 (含已隐藏)
            self.games = [g for g in self._all_games if g.gid not in self.hidden]
            self._sources = ["全部"] + [s for s in ("Steam", "Epic", "WeGame",
                                                    "MuMu", "本地")
                                        if any(g.source == s for g in self.games)]
            if self.f_source != "全部" and self.f_source not in self._sources:
                self.f_source = "全部"
            self._rebuild_cards()
        except Exception:
            log_exc("Dock.rescan")

    # ---- 右键菜单 ----

    def open_menu(self, global_pos, card=None):
        """右键唤出菜单。菜单外任何点击 (含桌面壁纸) 都会经遮罩把它收起。
        card: 右键落在哪张卡上 (空白处/托盘为 None) -> 菜单出现针对该卡的项。"""
        try:
            self._menu_card = card
            if self._menu_ref is not None:
                m, self._menu_ref = self._menu_ref, None
                m.close()
            self.topbar.close_and_clear()
            self.scalebox.hide()
            if not self.isVisible() and not self._tray_hidden:   # 自愈 (托盘主动隐藏的不拉回)
                self.show()
            self.shield.setGeometry(
                QGuiApplication.primaryScreen().availableGeometry())
            self.shield.show()
            self.shield.raise_()
            menu = self.build_menu()
            self._menu_ref = menu
            self._menu_pos = QPoint(global_pos)
            # 任何菜单项被点: 先把菜单+遮罩干净收起, 再执行动作。
            # (动作若在弹层还开着时改卡片, 会和弹窗抓取打架 -> 真机宕机事故)
            menu.triggered.connect(
                lambda _a: QTimer.singleShot(0, self.dismiss_overlays))
            menu.aboutToHide.connect(self._on_menu_hidden)
            menu.popup(global_pos)
            menu.raise_()
        except Exception:
            log_exc("Dock.open_menu")

    def _on_menu_hidden(self):
        try:
            self._menu_ref = None
            if not self.topbar.isVisible() and not self.scalebox.isVisible():
                self.shield.hide()
        except Exception:
            log_exc("Dock._on_menu_hidden")

    def _on_menu_search(self):
        try:
            # 30ms 延迟: 保证『点菜单任意项先收层』的排队先执行完, 不然弹层会被收掉
            QTimer.singleShot(30, self.show_search)
        except Exception:
            log_exc("Dock._on_menu_search")

    def show_search(self):
        """菜单里的『搜索游戏』-> 在右键位置弹出搜索胶囊。"""
        try:
            self._menu_ref = None
            self.shield.setGeometry(
                QGuiApplication.primaryScreen().availableGeometry())
            self.shield.show()
            self.shield.raise_()
            self.topbar.show_at(self._menu_pos or QCursor.pos())
        except Exception:
            log_exc("Dock.show_search")

    def dismiss_overlays(self):
        """点在弹层以外: 收起菜单/搜索框 + 撤遮罩。自带自愈:
        若发现主窗口处于隐藏的烂状态, 直接拉回来。"""
        try:
            if self._menu_ref is not None:
                m, self._menu_ref = self._menu_ref, None
                m.close()
            self.topbar.close_and_clear()
            self.scalebox.hide()
            self.shield.hide()
            if not self.isVisible() and not self._tray_hidden:   # 自愈 (托盘主动隐藏的不拉回)
                self.show()
                with open(os.path.join(CFG_DIR, "error.log"), "a",
                          encoding="utf-8") as f:
                    f.write("---- HEAL @ %s ---- dismiss_overlays 拉回被隐藏的主窗口\n"
                            % __import__("datetime").datetime.now())
        except Exception:
            log_exc("Dock.dismiss_overlays")

    def build_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:rgba(12,16,24,242);color:#DFE7F4;"
            "border:1px solid rgba(255,255,255,36);border-radius:9px;padding:6px;}"
            "QMenu::item{padding:6px 26px 6px 14px;border-radius:6px;font-size:13px;}"
            "QMenu::item:selected{background:rgba(79,195,255,60);}"
            "QMenu::item:disabled{color:#5A657A;}"
            "QMenu::separator{height:1px;background:rgba(255,255,255,30);margin:5px 8px;}")

        # 一级分区: 分类 / 排序 / 已隐藏的游戏 (各带二级菜单)
        src_menu = menu.addMenu(_t("cat"))
        src_menu.setStyleSheet(menu.styleSheet())
        for src in self._sources:
            a = src_menu.addAction(_t("all") if src == "全部" else src)
            a.setCheckable(True)
            a.setChecked(src == self.f_source)
            a.triggered.connect(lambda checked=False, s=src: self.set_source(s))
        for s in ("Ubisoft Connect", "Xbox", "EA App", "GOG"):
            a = src_menu.addAction(s + _t("not_av"))
            a.setEnabled(False)

        # 排序 (一级分区第二项)
        sort_menu = menu.addMenu(_t("sort"))
        sort_menu.setStyleSheet(menu.styleSheet())
        for key, mode in (("s_name", "name"), ("s_last", "lastplayed"),
                          ("s_time", "playtime"), ("s_genre", "genre"),
                          ("s_custom", "custom")):
            a = sort_menu.addAction(_t(key))
            a.setCheckable(True)
            a.setChecked(self.sort_mode == mode)
            a.triggered.connect(lambda checked=False, md=mode: self.set_sort(md))

        # 已隐藏的游戏 (一级分区第三项)
        hid = menu.addMenu(_t("hidden", len(self.hidden)))
        hid.setStyleSheet(menu.styleSheet())
        entries = [g for g in self._all_games if g.gid in self.hidden]
        if not entries:
            a = hid.addAction(_t("none"))
            a.setEnabled(False)
        else:
            for g in entries:
                a = hid.addAction("✓ " + g.name)
                a.triggered.connect(
                    lambda checked=False, gid=g.gid: self.unhide_game(gid))
            hid.addSeparator()
            ca = hid.addAction(_t("restore"))
            ca.triggered.connect(self.unhide_all)

        # 重新扫描 (已隐藏的游戏下面) + 搜索游戏
        a1 = menu.addAction(_t("rescan"))
        a1.triggered.connect(self.rescan)
        sa = menu.addAction(_t("search"))
        sa.triggered.connect(self._on_menu_search)
        sz = menu.addAction(_t("size"))
        sz.triggered.connect(self._on_menu_scale)
        # 更新 (调整尺寸下面, 二级: 封面资源 / 游戏类型)
        upd = menu.addMenu(_t("update"))
        upd.setStyleSheet(menu.styleSheet())
        ua = upd.addAction(_t("u_cover"))
        ua.triggered.connect(self.update_covers)
        ub = upd.addAction(_t("u_genre"))
        ub.triggered.connect(self.update_genres)

        # 针对单卡的封面操作: 右键点在哪张卡上才出现 (空白处/托盘右键没有)
        card = getattr(self, "_menu_card", None)
        if card is not None:
            ia = menu.addAction(_t("imp_cover"))
            ia.triggered.connect(
                lambda checked=False, c=card: self._on_menu_import_cover(c))
            if os.path.isfile(custom_cover_path(card.game.gid)):
                ra = menu.addAction(_t("reset_cover"))
                ra.triggered.connect(
                    lambda checked=False, c=card: self._on_menu_reset_cover(c))

        # 启动 (更新下面): 开机自启 + 开机行为 (自启关着时两项灰色不可点)
        launch = menu.addMenu(_t("launch"))
        launch.setStyleSheet(menu.styleSheet())
        mode = load_startup()
        a0 = launch.addAction(_t("autostart"))
        a0.setCheckable(True)
        a0.setChecked(mode != "off")
        a0.triggered.connect(self.toggle_autostart)
        launch.addSeparator()
        b1 = launch.addAction(_t("boot_show"))
        b1.setCheckable(True)
        b1.setChecked(mode == "show")
        b1.setEnabled(mode != "off")
        b1.triggered.connect(lambda checked=False: self.set_boot_mode("show"))
        b2 = launch.addAction(_t("boot_hide"))
        b2.setCheckable(True)
        b2.setChecked(mode == "hide")
        b2.setEnabled(mode != "off")
        b2.triggered.connect(lambda checked=False: self.set_boot_mode("hide"))

        menu.addSeparator()

        # 语言 (退出上面)
        lang_menu = menu.addMenu(_t("lang"))
        lang_menu.setStyleSheet(menu.styleSheet())
        for label, code in MENU_LANGS:
            a = lang_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(_LANG == code)
            a.triggered.connect(lambda checked=False, c=code: self.set_lang(c))

        a2 = menu.addAction(_t("quit"))
        a2.triggered.connect(QApplication.instance().quit)
        return menu

    def set_lang(self, code):
        """切换菜单语言并持久化; 菜单每次右键现建, 无需刷新。"""
        try:
            global _LANG
            if code in ("zh", "en", "ja"):
                _LANG = code
                save_lang(code)
        except Exception:
            log_exc("Dock.set_lang")

    def _on_menu_scale(self):
        try:
            # 同 _on_menu_search: 让收层排队先跑完
            QTimer.singleShot(30, lambda: self.show_scalebox(
                self._menu_pos or QCursor.pos()))
        except Exception:
            log_exc("Dock._on_menu_scale")

    # ---- 单卡封面导入/恢复 (右键卡片 -> 导入封面) ----

    def _on_menu_import_cover(self, card):
        try:
            # 30ms 延迟: 让『点菜单先收层』的排队先跑完 (同 _on_menu_search)
            QTimer.singleShot(30, lambda: self.import_cover(card))
        except Exception:
            log_exc("Dock._on_menu_import_cover")

    def import_cover(self, card):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, _t("imp_cover"), "",
                "图片 (*.jpg *.jpeg *.png *.webp *.bmp);;所有文件 (*.*)")
            if not path:
                return
            img = QImage(path)
            if img.isNull():
                return
            os.makedirs(COVER_DIR, exist_ok=True)
            dst = custom_cover_path(card.game.gid)
            if not img.save(dst, "JPG", 92):
                return
            card.game.art = dst
            card.reload_art()
        except Exception:
            log_exc("Dock.import_cover")

    def _on_menu_reset_cover(self, card):
        try:
            QTimer.singleShot(30, lambda: self.reset_cover(card))
        except Exception:
            log_exc("Dock._on_menu_reset_cover")

    def reset_cover(self, card):
        """删掉手动导入的封面文件, 找回平台自动封面 (没有就退回图标/字母块)。"""
        try:
            dst = custom_cover_path(card.game.gid)
            if os.path.isfile(dst):
                os.remove(dst)
            base = getattr(card.game, "base_art", None)
            if base and os.path.isfile(base):
                card.game.art = base
            else:
                card.game.art = None
            card.game.base_art = None
            card._base_art = card.game.art
            card.reload_art()
        except Exception:
            log_exc("Dock.reset_cover")

    # ---- 封面更新 (右键菜单『更新封面资源』) ----

    def update_covers(self):
        """为本地缺竖版封面的 Steam 游戏联网拉图, 存 COVER_DIR (拉一次永久离线用)。
        网络在后台线程跑, 主线程 QTimer 轮询收尾。"""
        try:
            self.dismiss_overlays()    # 联网更新绝不和弹层共存 (真机宕机教训)
            if getattr(self, "_updating_covers", False):
                _info("封面正在更新中, 别急。")
                return
            os.makedirs(COVER_DIR, exist_ok=True)
            cache = steam_cache_dir()
            todo = []
            for g in self._all_games:
                if g.is_steam:
                    appid = g.gid.split("|", 1)[1]
                    if os.path.isfile(os.path.join(COVER_DIR, "S_%s.jpg" % appid)):
                        continue
                    cur = find_steam_art(cache, appid) if cache else None
                    if cur is not None and has_portrait(cur):
                        continue    # 本地已有竖版
                    todo.append(("S", appid, g.name))
                elif g.source == "Epic" and g.catalog_id:
                    if epic_cover_local(g.catalog_id):
                        continue
                    todo.append(("E", g.catalog_id, g.name))
                elif g.source == "WeGame":
                    if wegame_cover_local(g.name):
                        continue
                    todo.append(("W", g.name, g.name))
            if not todo:
                _info("所有游戏都有竖版封面了, 不用更新。")
                return
            self._updating_covers = True
            self._covers_todo = todo
            self._covers_result = None
            threading.Thread(target=self._covers_worker, daemon=True).start()
            self._cov_timer = QTimer(self)
            self._cov_timer.setInterval(300)
            self._cov_timer.timeout.connect(self._covers_poll)
            self._cov_timer.start()
        except Exception:
            log_exc("Dock.update_covers")

    def _covers_worker(self):
        """后台线程: 逐个拉图。只碰 QImage/文件/网络, 不碰任何控件。"""
        done, failed = 0, []
        try:
            for kind, key, name in self._covers_todo:
                ok = False
                if kind == "S":
                    ok = fetch_cover(key) is not None
                elif kind == "W":
                    ok = fetch_wegame_cover(key) is not None
                else:
                    ok = fetch_epic_cover(key) is not None
                if ok:
                    done += 1
                else:
                    failed.append(name)
        except Exception:
            log_exc("covers_worker")
        self._covers_result = (done, failed)

    def _covers_poll(self):
        try:
            r = getattr(self, "_covers_result", None)
            if r is None:
                return
            self._cov_timer.stop()
            self._updating_covers = False
            done, failed = r
            # 先收弹层再弹提示 (与 _genres_poll 同理, 防模态+弹层打架)
            self.dismiss_overlays()
            msg = "封面更新完成: 新增 %d 张" % done
            if failed:
                names = ", ".join(failed[:6]) + ("..." if len(failed) > 6 else "")
                msg += "\n以下 %d 款没拉到竖版 (Steam CDN 无图 / Epic 本地无图源记录), 保持现状:\n%s" % (len(failed), names)
            _info(msg)
            self.rescan()
        except Exception:
            log_exc("Dock._covers_poll")

    # ---- 游戏类型 (右键菜单『更新游戏类型』) ----

    def update_genres(self):
        """为没有类型数据的 Steam 游戏联网拉官方分类, 存 genres.json (拉一次永久离线)。"""
        try:
            self.dismiss_overlays()    # 联网更新绝不和弹层共存 (真机宕机教训)
            if getattr(self, "_updating_genres", False):
                _info("游戏类型正在更新中, 别急。")
                return
            saved = load_genres()
            todo = []
            for g in self._all_games:
                if not g.is_steam:
                    continue
                appid = g.gid.split("|", 1)[1]
                if saved.get(appid):
                    continue
                todo.append((appid, g.name))
            if not todo:
                _info("所有游戏都有类型数据了, 不用更新。")
                return
            self._updating_genres = True
            self._genres_todo = todo
            self._genres_result = None
            threading.Thread(target=self._genres_worker, daemon=True).start()
            self._gen_timer = QTimer(self)
            self._gen_timer.setInterval(300)
            self._gen_timer.timeout.connect(self._genres_poll)
            self._gen_timer.start()
        except Exception:
            log_exc("Dock.update_genres")

    def _genres_worker(self):
        """后台线程: 逐款拉类型, 每款成功即增量落盘, 中断也不丢进度。"""
        done, failed = 0, []
        try:
            saved = load_genres()
            for i, (appid, name) in enumerate(self._genres_todo):
                gr = fetch_genres_once(appid)
                if gr:
                    saved[appid] = gr
                    done += 1
                    save_genres(saved)      # 边拉边存
                else:
                    failed.append(name)
                if i % 5 == 4:
                    import time
                    time.sleep(0.4)         # 对商店 API 温柔一点
        except Exception:
            log_exc("genres_worker")
        self._genres_result = (done, failed)

    def _genres_poll(self):
        try:
            r = getattr(self, "_genres_result", None)
            if r is None:
                return
            self._gen_timer.stop()
            self._updating_genres = False
            done, failed = r
            # 先把菜单/遮罩全部收起, 再弹提示 —— 模态/弹窗绝不在弹层开着时出现
            self.dismiss_overlays()
            msg = "游戏类型更新完成: %d 款" % done
            if failed:
                names = ", ".join(failed[:6]) + ("..." if len(failed) > 6 else "")
                msg += "\n以下 %d 款拉取失败 (商店没数据或网络抖动):\n%s" % (len(failed), names)
            _info(msg)
            self.rescan()
        except Exception:
            log_exc("Dock._genres_poll")

    def drag_begin(self, card):
        """长按抓起: 记录当前可见卡的基准槽位 (散开让位/落点都以它计算)。"""
        try:
            self._drag_base = []
            for i in range(self.row_lay.count()):
                w = self.row_lay.itemAt(i).widget()
                if isinstance(w, Card):
                    self._drag_base.append((w, w.x()))
            self._drag_base.sort(key=lambda t: t[1])
            self._drag_gap = None
        except Exception:
            log_exc("Dock.drag_begin")

    def drag_move(self, card):
        """拖拽跟手: 被抓的卡跟光标, 其他卡像手机桌面一样滑开让出一个槽位。"""
        try:
            self._edge_check()          # 拖到坞左右边缘 -> 自动滚动整条
            base = self._drag_base
            if not base or base[0][0].dock is not self:
                return
            row = card.parentWidget()
            g = row.mapFromGlobal(QCursor.pos())
            card.move(g - card._grab)
            x = g.x()
            others = [(w, bx) for w, bx in base if w is not card]
            idx = 0
            for w, bx in others:
                if bx + w.width() // 2 < x:
                    idx += 1
            if idx == self._drag_gap:
                return
            self._drag_gap = idx
            # 让位: 排在让位槽前的卡不动, 后面的整体右移一格
            # (被抓卡原来的空槽正好被顺移填上)
            n = len(base)
            for m, (w, bx) in enumerate(others):
                slot = m if m < idx else m + 1
                w.move(base[slot][1], w.y())
        except Exception:
            log_exc("Dock.drag_move")

    # 换位拖到坞左右边缘时自动滚动整条 (手机桌面同款): 换位可以拖到任意远,
    # 不需要松手再分段拖 (分段后第二段会被判定成滚动, 2026-11-01 真机反馈)。
    EDGE_PX = 90          # 距屏幕左右边缘多少像素内开始自动滚

    def _edge_check(self):
        try:
            gp = QCursor.pos()
            g = self.mapToGlobal(QPoint(0, 0))
            if not (g.y() <= gp.y() <= g.y() + self.height()):
                self._edge_set(0)          # 拖出坞上下范围: 不滚
            elif gp.x() - g.x() < self.EDGE_PX:
                self._edge_set(-1)
            elif (g.x() + self.width()) - gp.x() < self.EDGE_PX:
                self._edge_set(1)
            else:
                self._edge_set(0)
        except Exception:
            log_exc("Dock._edge_check")

    def _edge_set(self, d):
        try:
            if d == self._edge_dir:
                return
            self._edge_dir = d
            if d:
                self._edge_t.start()
            else:
                self._edge_t.stop()
        except Exception:
            log_exc("Dock._edge_set")

    def _edge_tick(self):
        try:
            bar = self.scroller.horizontalScrollBar()
            bar.setValue(max(0, min(bar.maximum(), bar.value() + self._edge_dir * 12)))
            if self._dragging_card is not None:
                self.drag_move(self._dragging_card)   # 滚动中让位槽跟手更新
        except Exception:
            log_exc("Dock._edge_tick")

    def drop_card(self, card):
        """长按拖拽落下: 以【当前显示顺序】为基准 (类型/时长排序打下的底子),
        只叠加这一次拖动的插入位置, 存为自定义顺序并切到「自定义」。"""
        try:
            self._edge_set(0)
            self._dragging_card = None
            base = self._drag_base
            if base:
                x = card.parentWidget().mapFromGlobal(QCursor.pos()).x()
                order = [w.game.gid for w, bx in base]        # 屏幕上的实际顺序
                others = [(w, bx) for w, bx in base if w is not card]
                idx = 0
                for w, bx in others:
                    if bx + w.width() // 2 < x:
                        idx += 1
            else:                                             # 兜底 (理论不走)
                order = list(self._display_gids)
                idx = 0
            gids = [gid for gid in order if gid != card.game.gid]
            gids.insert(idx, card.game.gid)
            # 没显示出来的游戏 (搜索过滤中) 保持原相对顺序排在后面
            rest = [g.gid for g in self.games if g.gid not in set(gids)]
            self.custom_order = gids + rest
            save_custom_order(self.custom_order)
            self._drag_base = None
            self.set_sort("custom")     # 重建卡片 + 菜单勾选自动到「自定义」
        except Exception:
            log_exc("Dock.drop_card")

    # ---- Xbox 手柄 (XInput) ----

    def _gp_poll(self):
        """30ms 轮询: 沿检测 + 按住重复。所有分支各自消费按键。"""
        try:
            r = xinput_poll(0)
            if r is None or not r[0]:
                if self._gp_on:                    # 拔掉了 -> 清场
                    self._gp_on = False
                    self._gp_set_focus(None)
                    self._gp_move = None
                    self._gp_prev_wB = 0
                return
            self._gp_on = True
            _, wB, lx, ly = r
            if not self.isVisible() or self._tray_hidden:
                self._gp_prev_wB = wB
                return
            now = time.time()
            menu = QApplication.activePopupWidget()
            if menu is None:
                self._gp_sub_open = False          # 菜单链全关: 状态复位
            modal = QApplication.activeModalWidget()

            # ---- 游戏进行中静默 (2026-10-31 UN-G 需求) ----
            # dock 是沉底窗口, 游戏窗口 (全屏/窗口化) 盖住它后, 用户在游戏里
            # 按手柄 A/B/Y 不该被当成对卡片确认/卸载/隐藏。
            # 判据: a) 启动冷却 10s 内; b) dock 中心点最顶层窗口不是自己 (被盖住)。
            # 满足其一 -> 本帧手柄输入全部忽略, 并把按键记为"已按下",
            # 防静默结束瞬间沿检测误触发。弹层 (菜单/模态框) 开着时跳过检测。
            if menu is None and modal is None:
                mute = now < _LAUNCH_MUTE_UNTIL
                if not mute:
                    try:
                        gp = self.mapToGlobal(QPoint(self.width() // 2,
                                                     self.height() // 2))
                        hwnd = _user32.WindowFromPoint(
                            wintypes.POINT(gp.x(), gp.y()))
                        root = _user32.GetAncestor(hwnd, 2)   # GA_ROOT
                        if root and root != int(self.winId()):
                            mute = True
                    except Exception:
                        pass
                if mute:
                    self._gp_dir = 0
                    self._gp_y_t = 0.0
                    self._gp_prev_wB = wB
                    return

            def pressed(mask):
                return (wB & mask) and not (self._gp_prev_wB & mask)

            def released(mask):
                return (self._gp_prev_wB & mask) and not (wB & mask)

            try:
                if menu is not None:
                    # 菜单开着: 直接操纵 (setActiveAction/trigger/close)。
                    # 不用合成键盘事件 —— 真机实测弹出层抓键会吞掉它, 十字键/摇杆全部失灵。
                    self._gp_menu_nav(menu, wB, lx, ly, now, pressed)
                    return
                if modal is not None:
                    # 确认弹窗: 十字键 = Tab 换焦点按钮, A 确认, B 取消
                    for m in (GP_DPAD_LEFT, GP_DPAD_RIGHT, GP_DPAD_UP, GP_DPAD_DOWN):
                        if pressed(m):
                            self._gp_send(modal, Qt.Key_Tab)
                    if pressed(GP_A):
                        self._gp_send(modal, Qt.Key_Return)
                    if pressed(GP_B):
                        self._gp_send(modal, Qt.Key_Escape)
                    return
                if self._gp_move is not None:
                    # 移动模式: 方向挪卡, A/松Y 落下, B 取消
                    self._gp_direction(wB, lx, now)
                    if pressed(GP_A):
                        self._gp_drop(False)
                    elif pressed(GP_B):
                        self._gp_drop(True)
                    elif released(GP_Y):
                        self._gp_drop(False)
                    return
                if self.topbar.isVisible() or self.scalebox.isVisible():
                    self._gp_prev_wB = wB
                    return                          # 搜索/尺寸弹层开着: 不抢
                # 常规: 方向移动焦点, A/X/B 打在焦点卡上
                self._gp_direction(wB, lx, now)
                if pressed(GP_A):
                    if self._gp_focus_i is None:
                        self._gp_set_focus(0)
                    else:
                        c = self._gp_card_at(self._gp_focus_i)
                        if c is not None and c.b_play.isEnabled():
                            c._on_play()
                if pressed(GP_X) and self._gp_focus_i is not None:
                    c = self._gp_card_at(self._gp_focus_i)
                    if c is not None and c.b_un.isEnabled():
                        c._on_un()
                if pressed(GP_B) and self._gp_focus_i is not None:
                    c = self._gp_card_at(self._gp_focus_i)
                    if c is not None:
                        self._gp_refocus_gid = c.game.gid
                        c._on_hide()
                if pressed(GP_START):
                    c = (self._gp_card_at(self._gp_focus_i)
                         if self._gp_focus_i is not None else None)
                    self.open_menu(self.mapToGlobal(QPoint(self.width() // 2, 8)), c)
                if wB & GP_Y:
                    if self._gp_y_t == 0.0:
                        self._gp_y_t = now
                    elif self._gp_focus_i is not None and now - self._gp_y_t >= 0.5:
                        self._gp_y_t = 0.0
                        self._gp_move_begin()
                else:
                    self._gp_y_t = 0.0
            finally:
                self._gp_prev_wB = wB
        except Exception:
            log_exc("Dock._gp_poll")

    def _gp_menu_nav(self, menu, wB, lx, ly, now, pressed):
        """菜单手柄导航: 直接操纵 QMenu。
        十字键上下 / 左摇杆竖轴 = 移动高亮 (按住连走);
        十字键右 / 摇杆右推 = 进入子菜单 (显式动作, 高亮路过不弹);
        十字键左 / 摇杆左推 = 收起子菜单; A = 确认; B / ☰ = 取消。"""
        try:
            dz = GP_LTHUMB_DZ
            # 子菜单只在显式打开时保留: Qt 会在高亮落到子菜单动作上时自动弹出,
            # 非显式进入的一律立即收掉 —— 否则按住下键路过每个二级菜单都被劫持
            if isinstance(menu.parentWidget(), QMenu) and not self._gp_sub_open:
                menu.close()
                return
            d = 0
            if (wB & GP_DPAD_DOWN) or ly > dz:
                d = 1
            elif (wB & GP_DPAD_UP) or ly < -dz:
                d = -1
            if d != 0:
                if d != self._gp_mdir:
                    self._gp_mdir = d
                    self._gp_mhold = now
                    self._gp_menu_step(menu, d)       # 沿触发立即一步
                elif (now - self._gp_mhold > 0.35
                      and now - self._gp_mlast >= 0.22):
                    self._gp_mlast = now
                    self._gp_menu_step(menu, d)
            else:
                self._gp_mdir = 0
            # 左右: 十字键沿 / 摇杆横轴沿, 显式进/出子菜单
            sr = lx > dz
            sl = lx < -dz
            go_r = pressed(GP_DPAD_RIGHT) or (sr and not self._gp_lx_r)
            go_l = pressed(GP_DPAD_LEFT) or (sl and not self._gp_lx_l)
            self._gp_lx_r, self._gp_lx_l = sr, sl
            if go_r and not go_l:
                act = menu.activeAction()
                if act is not None and act.menu() is not None:
                    menu.setActiveAction(act)         # 弹出子菜单
                    self._gp_sub_open = True
            elif go_l and not go_r:
                if isinstance(menu.parentWidget(), QMenu):
                    menu.close()                      # 只收当前子菜单
                    self._gp_sub_open = False
            if pressed(GP_A):
                act = menu.activeAction()
                if act is not None and act.isEnabled():
                    if act.menu() is not None:
                        menu.setActiveAction(act)     # A 打开子菜单
                        self._gp_sub_open = True
                    else:
                        act.trigger()
                        # trigger() 不关菜单链 (子菜单 triggered 也不气泡到顶层),
                        # 自己干净收起 —— 与鼠标点击走的 dismiss 同一条路
                        QTimer.singleShot(0, self.dismiss_overlays)
            if pressed(GP_B) or pressed(GP_START):
                if isinstance(menu.parentWidget(), QMenu):
                    self._gp_sub_open = False
                menu.close()
        except Exception:
            log_exc("Dock._gp_menu_nav")

    def _gp_menu_step(self, menu, d):
        """高亮在可用动作间移动 (跳过分隔线/禁用项, 循环)。"""
        try:
            acts = [a for a in menu.actions()
                    if not a.isSeparator() and a.isEnabled()]
            if not acts:
                return
            cur = menu.activeAction()
            i = acts.index(cur) if cur in acts else (-1 if d > 0 else 0)
            menu.setActiveAction(acts[(i + d) % len(acts)])
        except Exception:
            log_exc("Dock._gp_menu_step")

    def _gp_send(self, w, key):
        """给菜单/弹窗合成键盘事件 (手柄替代方向键+回车+Esc)。"""
        QApplication.postEvent(
            w, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        QApplication.postEvent(
            w, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))

    def _gp_direction(self, wB, lx, now):
        """十字键/左摇杆横向方向: 沿触发一步, 按住 0.35s 后每 0.22s 一步。"""
        d = 0
        if (wB & GP_DPAD_LEFT) or lx < -GP_LTHUMB_DZ:
            d = -1
        elif (wB & GP_DPAD_RIGHT) or lx > GP_LTHUMB_DZ:
            d = 1
        if d == 0:
            self._gp_dir = 0
            return
        if d != self._gp_dir:
            self._gp_dir = d
            self._gp_hold_t = now
            self._gp_last_step = now
            self._gp_step(d)
        elif (now - self._gp_hold_t > 0.35
              and now - self._gp_last_step >= 0.22):
            self._gp_last_step = now
            self._gp_step(d)

    def _gp_step(self, d):
        if self._gp_move is not None:
            self._gp_move_step(d)
        else:
            self._gp_focus_step(d)

    def _gp_cards(self):
        out = []
        for k in range(self.row_lay.count()):
            w = self.row_lay.itemAt(k).widget()
            if isinstance(w, Card):
                out.append(w)
        return out

    def _gp_card_at(self, i):
        cards = self._gp_cards()
        return cards[i] if 0 <= i < len(cards) else None

    def _gp_focus_step(self, d):
        n = len(self._gp_cards())
        if n == 0:
            return
        i = self._gp_focus_i
        i = (0 if d > 0 else n - 1) if i is None else max(0, min(n - 1, i + d))
        self._gp_set_focus(i)

    def _gp_set_focus(self, i):
        try:
            cards = self._gp_cards()
            old = self._gp_focus_i
            if old is not None and old < len(cards):
                cards[old].set_gfocus(False)
            if i is None:
                self._gp_focus_i = None
                return
            i = max(0, min(len(cards) - 1, i))
            self._gp_focus_i = i
            c = cards[i]
            c.set_gfocus(True)
            c.set_gp_hints(True)
            self._gp_scroll_to(i)
        except Exception:
            log_exc("Dock._gp_set_focus")

    def _gp_scroll_to(self, i):
        try:
            bar = self.scroller.horizontalScrollBar()
            step = max(1, HOVER_W)
            x = i * step
            vw = self.scroller.viewport().width()
            v = bar.value()
            if x < v:
                nv = x
            elif x + HOVER_W > v + vw:
                nv = x + HOVER_W - vw
            else:
                return
            # 精确落位, 不做格点对齐 (对齐向下取整会把右缘的卡截掉一截)
            bar.setValue(max(0, min(bar.maximum(), nv)))
        except Exception:
            log_exc("Dock._gp_scroll_to")

    def _gp_move_begin(self):
        """长按 Y 进入移动模式 (等价鼠标长按抓起)。"""
        try:
            c = self._gp_card_at(self._gp_focus_i)
            if c is None or c._drag:
                return
            self._card_drag = True
            c._drag = True
            c.setPixmap(c.pm_hover)
            c.btn_row.hide()
            c.raise_()
            c.setCursor(Qt.ClosedHandCursor)
            self.drag_begin(c)
            orig = 0
            for k, (w, _bx) in enumerate(self._drag_base):
                if w is c:
                    orig = k
                    break
            self._gp_move = {"card": c, "orig": orig, "gap": None}
        except Exception:
            log_exc("Dock._gp_move_begin")

    def _gp_move_step(self, d):
        try:
            mv, base = self._gp_move, self._drag_base
            if not mv or not base:
                return
            card = mv["card"]
            others = [(w, bx) for w, bx in base if w is not card]
            cur = mv["orig"] if mv["gap"] is None else mv["gap"]
            idx = max(0, min(len(base) - 1, cur + d))
            if idx == cur:
                return
            mv["gap"] = idx
            for m, (w, bx) in enumerate(others):
                slot = m if m < idx else m + 1
                w.move(base[slot][1], w.y())
            card.move(base[idx][1], card.y())
            card.raise_()
        except Exception:
            log_exc("Dock._gp_move_step")

    def _gp_drop(self, cancel):
        """落下 (A/松Y) 或取消 (B)。走自定义排序落盘, 与鼠标拖拽同一终点。"""
        try:
            mv, self._gp_move = self._gp_move, None
            if not mv:
                return
            card = mv["card"]
            self._card_drag = False
            self._dragging_card = None        # 手柄落下: 清鼠标边缘滚引用 (防御)
            card._drag = False
            card.setCursor(Qt.PointingHandCursor)
            base = self._drag_base
            self._gp_refocus_gid = card.game.gid
            if cancel or not base:
                self._drag_base = None
                self.set_sort(self.sort_mode)     # 原地重建, 恢复布局
                return
            order = [w.game.gid for w, _bx in base]
            idx = mv["gap"] if mv["gap"] is not None else mv["orig"]
            gids = [gid for gid in order if gid != card.game.gid]
            gids.insert(idx, card.game.gid)
            rest = [g.gid for g in self.games if g.gid not in set(gids)]
            self.custom_order = gids + rest
            save_custom_order(self.custom_order)
            self._drag_base = None
            self.set_sort("custom")
        except Exception:
            log_exc("Dock._gp_drop")

    # ---- 卡片缩放 (调整尺寸) ----

    def schedule_scale(self, pct):
        try:
            self._scale_pending = max(50, min(150, int(pct)))
            self._scale_t.start()      # 防抖: 连续滚轮/点按只重建最后一次
        except Exception:
            log_exc("Dock.schedule_scale")

    def _scale_apply_tick(self):
        try:
            pct = self._scale_pending
            if pct is None or pct == self.card_scale:
                return
            self.apply_scale(pct)
        except Exception:
            log_exc("Dock._scale_apply_tick")

    def apply_scale(self, pct):
        """按百分比重算卡片尺寸常量, 缩放坞条高度并重建全部卡片。"""
        try:
            global CARD_W, CARD_H, HOVER_W, HOVER_H
            f = max(0.5, min(1.5, pct / 100.0))
            CARD_W = max(70, int(164 * f))
            CARD_H = max(100, int(234 * f))
            HOVER_W = CARD_W + max(8, int(16 * f))
            HOVER_H = CARD_H + max(10, int(22 * f))
            self.card_scale = int(pct)
            save_scale(self.card_scale)
            self.dock_h = max(150, int(300 * f))
            av = QGuiApplication.primaryScreen().availableGeometry()
            self.setGeometry(0, av.height() - self.dock_h + 2,
                             self.dock_w, self.dock_h)
            self.box.setGeometry(0, 0, self.dock_w, self.dock_h)
            self._rebuild_cards()
        except Exception:
            log_exc("Dock.apply_scale")

    def show_scalebox(self, global_pos):
        """菜单里的『调整尺寸』-> 在鼠标位置弹出百分比框。"""
        try:
            self._menu_ref = None
            self.shield.setGeometry(
                QGuiApplication.primaryScreen().availableGeometry())
            self.shield.show()
            self.shield.raise_()
            av = QGuiApplication.primaryScreen().availableGeometry()
            x = global_pos.x() + 10
            y = global_pos.y() - self.scalebox.height() - 6
            x = max(av.left() + 8, min(x, av.right() - self.scalebox.width() - 8))
            y = max(av.top() + 8, min(y, av.bottom() - self.scalebox.height() - 8))
            self.scalebox.move(x, y)
            self.scalebox.sync_value(self.card_scale)
            self.scalebox.show()
            self.scalebox.raise_()
            self.scalebox.spin.setFocus()
            self.scalebox.spin.selectAll()
        except Exception:
            log_exc("Dock.show_scalebox")

    def unhide_game(self, gid):
        try:
            self.hidden.discard(gid)
            save_hidden(self.hidden)
            self.rescan()
        except Exception:
            log_exc("Dock.unhide_game")

    def unhide_all(self):
        try:
            self.hidden.clear()
            save_hidden(self.hidden)
            self.rescan()
        except Exception:
            log_exc("Dock.unhide_all")

    def hide_game(self, game):
        try:
            self.hidden.add(game.gid)
            save_hidden(self.hidden)
            self.rescan()
        except Exception:
            log_exc("Dock.hide_game")


def main():
    # 单实例
    ctypes.windll.kernel32.CreateMutexW(None, False, "GameDock_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)

    # 原生崩溃 (段错误等) 时把 Python 调用栈写进 fatal.log
    try:
        import faulthandler
        os.makedirs(CFG_DIR, exist_ok=True)
        faulthandler.enable(open(os.path.join(CFG_DIR, "fatal.log"), "a", encoding="utf-8"))
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # 有托盘常驻, 只能从菜单退出
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setWindowIcon(make_tray_icon())    # G·D 字标同时作为程序图标
    dock = Dock()
    # 开机自启"隐藏至托盘"模式: 注册表项带 --tray 参数, 启动只出托盘不显示主体
    if "--tray" in sys.argv and load_startup() == "hide":
        dock._tray_hidden = True
    else:
        dock.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
