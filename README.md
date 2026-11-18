# InputBridge

**一套键鼠，在相邻电脑之间工作；文件也能跟着过去。**

InputBridge 在菜单栏或系统托盘运行。把鼠标移出屏幕边缘即可操作另一台电脑；文件可以在一台电脑复制后去另一台粘贴，也可以从 Mac 直接拖到 Linux 的目标位置。App 直接管理内置的键鼠核心，无需另装 Deskflow。Mac 主控还可单独调本机鼠标的滚动手感，以及跨屏滚轮方向。

当前为 **v0.2.0a1 源码预览**。GitHub Releases 提供项目和改版键鼠核心的源码归档。试用需要按下方说明自行构建。

| 能力 | 目前覆盖 |
| --- | --- |
| 键鼠共享 | Apple Silicon Mac 主控与 GNOME / X11 Linux 实机连通，双向切屏已验证 |
| 文件剪贴板 | Mac 与 GNOME / X11 Linux 已做文件、文件夹传输及系统文件管理器操作验证 |
| 原生拖放 | **Mac → GNOME / X11 Linux** 已验证，按实际松手位置落下；反向拖放尚未实机验收 |
| 鼠标滚轮 | Mac 本机平滑、反向和手感调节；Mac 主控时可独立纠正跨屏方向，保留触控板原路径 |
| 其他环境 | Windows、Wayland、Intel Mac 和 Linux 反向主控没有成品承诺 |

## 开始使用

1. 按[安装说明](docs/Installation.md)在两端准备开发版。macOS 键鼠功能需要系统辅助功能授权。
2. 两端接入能彼此访问的局域网。在一台点「连接」生成邀请，在另一台粘贴邀请。地址、端口和配对排查见安装说明。
3. 把键鼠来源设为 Mac，选择 Linux 位于哪一侧，保存布局，再在两端启用键鼠。
4. 复制文件后，等接收端显示可以粘贴，再到目标目录粘贴；测试拖放时从 Finder 按住文件夹，跨过边缘后再松手。

如果 Linux 的鼠标滚轮方向反了，从 Mac 打开「键鼠设置 → 鼠标滚轮 → 跨屏反向」，只开启需要反转的轴。选项默认关闭，不改系统自然滚动设置。[滚轮说明](docs/Scroll.md)

## 构建与贡献

- [安装和配对](docs/Installation.md)
- [源码、Mac 和 Linux 打包流程](docs/Packaging.md)
- [首发前检查清单](docs/Release.md)

本项目原创应用代码采用 [GPL-3.0-or-later](LICENSE)。Deskflow 修改版是单独的输入核心，macOS 拖放 helper 包含固定版本的 TwinDesk 源文件。各组件的许可证、对应源码和发布前条件见[第三方声明](THIRD_PARTY_NOTICES.md)与[许可复核](docs/Licensing.md)。
