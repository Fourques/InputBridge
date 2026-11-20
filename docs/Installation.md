# 安装与配对

以下说明适用于从 v0.2.0a2 源码构建的开发包。先看[打包流程](Packaging.md)，在单独目录准备开发包。

## macOS

1. 在 Apple Silicon Mac 上构建后，从输出的 `dist/InputBridge.app` 或解压本地 ZIP 获得应用。开发包使用临时签名，仅供本地验证；公开分发前还要走 Developer ID 签名和 Apple 公证。遇到系统阻止请先核对包及签名，勿关闭系统保护。
2. 打开一次应用，在「系统设置 → 隐私与安全性 → 辅助功能」为这份应用开启权限。部分系统把此项称为「设备控制和数据访问」。使用文件配对若遇到本地网络请求，按系统提示确认。重新编译的临时签名包可能需要重新授权。
3. 默认配置保存在 `~/Library/Application Support/InputBridge/`。只测试 UI 时用构建说明里的 `--preview`；它用临时配置，不启用键鼠、不读系统剪贴板。

## Linux（GNOME / X11）

下列步骤适用于构建得到的开发 `-linux-…-local.tar.gz`，不是通用安装包。解压到个人目录（密钥和缓存不应在多人可读目录），在已登录的 **X11** 图形桌面内执行：

```sh
cd InputBridge-0.2.0a2-linux-x86_64
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
./packaging/start-linux.sh
```

上述包带内置输入核心和核心专用 Qt。界面使用虚拟环境中的 PySide6。系统需提供 X11、XTest、XKB、xcb 扩展、xkbcommon-x11、xkbregistry、D-Bus 和 OpenSSL 3 所需的运行库。如果缺库，请用 `ldd local/engine/inputbridge-core` 和 `ldd local/engine/qt/plugins/platforms/libqxcbInputBridge.so` 检查。当前 Linux 开发包面向 X11 图形会话。

Linux 配置默认在 `${XDG_DATA_HOME:-$HOME/.local/share}/inputbridge`。在终端或无托盘会话里先以前台方式打开。两平台都不会自动添加登录自启；确认日常可用后，可用桌面自己的登录项功能启动，Linux 启动命令可加 `--background`。

## 首次连接

1. 两台在相互可达的可信局域网中运行 InputBridge。放行双方文件通道的 **TCP 24820**；键鼠主控还需允许对侧连接其 **TCP 24830**。端口只对需要的局域网设备开放，不配置公网转发。
2. A 点「连接」复制邀请，B 点「连接」粘贴。邀请只用一次，过期后重新生成。请通过可信途径交给自己的另一台电脑。
3. 在「键鼠设置」选择另一台电脑、键鼠来源和屏幕相对位置，保存布局后在两端启用键鼠。启用前先退出正在接管同一套设备的 Deskflow 或其他共享工具。
4. 先测试鼠标切过去、切回来，再试复制粘贴和原生拖放。只想测试文件时可以不启用键鼠。

多个网卡时可在启动参数里加 `--address <本机可被对端访问的局域网 IPv4>`。它用于发布可达地址，也是输入核心生成本地绑定地址的依据；文件服务仍按开发版默认方式监听。若用 `--port` 改文件服务端口，就按指定端口放行并基于新地址重新配对。切屏失败先核对 X11 会话、辅助功能、主控选择、屏幕方向和端口，再检查是否有另一个键鼠引擎。

文件接收使用本地暂存并校验内容。复制后等提示准备好再粘贴；不会自动打开接收内容。需要清缓存时先退出应用，并确认要保留的文件已粘贴到目标目录。证书私钥、配对设置和缓存不要上传。
