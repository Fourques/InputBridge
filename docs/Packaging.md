# 本地打包

`packaging/build.py` 只向一个新的输出目录写入文件；它不安装应用，不设置登录项，不启动真实键鼠核心，不上传、不发布。所有二进制产物文件名以 `-local` 标明本地验证用途。

## 工具和输入

- Python 3.9+（Mac 已测试现有 Python 3.9 开发环境；公开包需在选定的受支持 Python 版本上复验）。运行依赖见根目录 `requirements.lock.txt`。
- Git、CMake、Ninja 和 C++ 编译器。输入核心使用下面从官方归档构建的私有 QtBase 6.7.3。界面使用锁定的 PySide6 wheel。Mac 使用 Xcode Command Line Tools 提供的 Clang、Swift、`otool`、`install_name_tool` 和 `codesign`。
- Linux 需 pkg-config、patchelf、OpenSSL 3 和 X11 开发库，包括 Xext、Xtst、Xi、Xrandr、Xinerama、XKB、xkbregistry、QtBase 所需的 XCB 扩展、xkbcommon-x11 及 D-Bus。准备完整的发行版开发库后构建。打包脚本只收集核心所需的 Qt 动态库和所选平台插件。
- Deskflow 本地 checkout。固定提交读取自 `native/engine/upstream.json`，和 tag 一起核对：

```sh
git clone --branch v1.27.0 https://github.com/deskflow/deskflow.git ./deskflow-src
git -C ./deskflow-src rev-parse HEAD
# 081f6478e654a112168879e67a41ce7d7e4ae3af
```

打包脚本按固定提交另建工作副本，再用 `native/engine/prepare.py` 生成不带上游 GUI 的输入核心；不会修改传入的 checkout。核心使用相对编译路径，打包时会检查是否残留构建机的本地路径。清单、源码和对应输入核心处于同一输出目录，便于对账。

## 编译输入核心专用 QtBase

按[源码清单](../third_party/SOURCE-MATERIALS.json)下载 QtBase 官方归档。每个目标平台原生运行一次：

```sh
python3 packaging/build_engine_qt.py \
  --source /path/to/qtbase-everywhere-src-6.7.3.tar.xz \
  --out ./local/engine-qt
export QT_SDK_PREFIX="$PWD/local/engine-qt/sdk"
```

脚本校验归档哈希、关闭内嵌 MIME 数据库，并生成 SDK 清单。发布包中的 Qt 依赖指向引擎旁的 `qt/`，界面和引擎各自选择平台插件。Apple Silicon 的构建脚本记录了 Apple Clang 的兼容改动。

## 生成审阅用源码

```sh
python3 packaging/build.py source --deskflow-checkout ./deskflow-src --out ./local/source-review
```

可省略 checkout，只准备 InputBridge 自身源码。产出一个通过显式名单筛选的源码目录与归档、固定版本 Deskflow 原始与改版源码归档（提供 checkout 时）、`source-files.txt`、`build-info.json` 和 `SHA256SUMS`。源码归档包含应用代码、Swift helper 源码、构建脚本和原生核心补丁生成器；不会导出机器配置、历史实机笔记、测试回退脚本、启动快捷方式或私有构建树。上传前仍须人工审阅并定版。第三方官方源码归档不嵌入每个开发包，来源和核验命令见[许可复核](Licensing.md)。

## 生成 Mac 开发包

在项目根目录用独立环境：

```sh
python3 -m venv ./local/package-venv
./local/package-venv/bin/python -m pip install -r requirements.lock.txt -r packaging/requirements-macos.lock.txt
./local/package-venv/bin/python packaging/build.py macos \
  --deskflow-checkout ./deskflow-src --qt-prefix "$QT_SDK_PREFIX" \
  --cmake-arg=-DOPENSSL_USE_STATIC_LIBS=ON \
  --cmake-arg="-DOPENSSL_ROOT_DIR=$OPENSSL_PREFIX" \
  --out ./local/macos-review
```

将 `OPENSSL_PREFIX` 设为选定的 OpenSSL 3 开发前缀并记录实际链接版本。脚本编译 Swift helper、冻结 Python，随后把独立的 Qt 运行库和原生程序放进应用包并校验签名结构。输出 `dist/InputBridge.app`、ZIP、源码及校验文件。包使用本地临时签名。

可在不接管键鼠的情况下验证：

```sh
app=./local/macos-review/dist/InputBridge.app
codesign --verify --deep --strict "$app"
QT_QPA_PLATFORM=offscreen DYLD_PRINT_LIBRARIES=1 \
  "$app/Contents/Frameworks/engine/inputbridge-core" --version
"$app/Contents/MacOS/InputBridge" --preview ./local/macos-review/preview.png
```

正式签名、公证、提交和发布放在单独授权步骤。本流程不读取证书密钥、Apple 账号或签名存储凭据。Apple 的 [公证文档](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)说明了 Developer ID 和公证流程。

## 生成 Linux X11 开发包

在目标发行版和架构上原生编译：

```sh
python3 packaging/build.py linux \
  --deskflow-checkout ./deskflow-src --qt-prefix "$QT_SDK_PREFIX" \
  --out ./local/linux-review
```

如果使用 Ninja，可追加 `--ninja /path/to/ninja`。自定义 sysroot 可用多个 `--cmake-arg=-DNAME=value`。Linux 工件包含应用源码、内置核心及核心专用 Qt 库和平台插件。界面通过独立虚拟环境安装，系统提供基础运行库。请在目标发行版的干净环境检查 `ldd`，再以 `QT_QPA_PLATFORM=offscreen LD_DEBUG=libs local/engine/inputbridge-core --version` 检查核心加载路径。检查日志里 Core、Gui 等 Qt 库和平台插件的位置均位于 `engine/qt/`。实际 X11 使用、配对和双向切屏还要在图形会话验收。

## 校验

在构建输出目录运行 `shasum -a 256 -c SHA256SUMS`（Linux 可用 `sha256sum -c SHA256SUMS`）。公共源码测试：

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
python scripts/smoke_two_nodes.py
c++ -std=c++17 -I native/engine/wheel tests/native/wheel_motion.cpp -o /tmp/inputbridge-wheel-test
/tmp/inputbridge-wheel-test
```

回环 TLS、离屏窗口和核心启动检查不会证明物理设备兼容。发布所需的双机项目见[检查清单](Release.md)。

检查打包后的 QtCore，可用 `python3 packaging/audit_qtcore_mime.py <引擎的QtCore库> <原始QtBase归档> --require-absent`。若本机未自动发现 Zstandard，使用 `--zstd-library` 指定已安装的 libzstd。将结果和 Qt 构建配置一起留作复审。
