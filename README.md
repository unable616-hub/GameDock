# GameDock

Windows 本地游戏启动坞 —— 屏幕底部一条悬浮卡片栏，自动聚合你电脑里的所有游戏，双击启动。

A local game launcher dock for Windows: a bottom card bar that auto-collects your PC games. Launch with a double-click or a gamepad.

## 功能

- **自动扫描聚合**：Steam / Epic / MuMu 模拟器（安卓实例）/ WeGame / 本地 exe，一次扫描全进一条卡片栏
- **官方封面**：Steam 本地缓存、Epic 索引库图源、WeGame 官方海报 CDN 自动抓取并缓存；也可右键单卡「导入封面」手动指定
- **手柄全操作**：XInput 直连（零依赖）——摇杆/十字键滚动、A 游玩、☰ 呼出菜单、长按 Y 移动卡片；进游戏后自动静默，不会误触
- **鼠标手势**：滚轮一步一卡；按住空白处拖动 = 整条跟手滑动；长按卡片 1 秒 = 换位（可拖到屏幕边缘自动滚到任意远）
- **轻量单文件**：PyInstaller 单 exe，免安装，数据全在 `%APPDATA%\GameDock\`
- **实用小件**：本地搜索、按类型/时长/自定义排序、隐藏不想看到的卡、开机自启、单实例互斥

## 下载

到 [Releases](https://github.com/unable616-hub/GameDock/releases) 下载 `GameDock-vX.Y.Z.zip`，解压即用。

> **首次运行**：SmartScreen / 杀毒提示属正常（未做代码签名），选「仍要运行」即可。卸载 = 删除 exe + 删除 `%APPDATA%\GameDock\`。

## 从源码运行

```
pip install PyQt5 Pillow
python GameDock.pyw
```

打包单文件 exe：

```
pip install pyinstaller
pyinstaller GameDock.spec --noconfirm
```

## 环境要求

- Windows 10/11
- Python 3.12+（源码运行时）
- 手柄功能：Xbox 及兼容 XInput 的手柄

## 许可

[MIT](LICENSE)
