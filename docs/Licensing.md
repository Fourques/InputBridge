# 许可证与对应源码复核

审阅对象：0.2.0a1 的 Mac arm64 和 Linux x86_64/X11 本地工件。**建议首发先准备源码。三进程结构有可行的开源路径，但当前二进制包暂不对外分发。** 以下是源码和工件核查，不能代替发行人对最终工件的授权确认。

| 边界 | 已核实 | 发布判断 |
| --- | --- | --- |
| InputBridge Python 应用 | 原创 GPL-3.0-or-later；通过子进程、配置和本地消息控制内置核心，不将 Deskflow 代码链接进 Python 进程 | 保留自身 GPLv3 或后续版本选择；和使用 GPLv2 的原生核心分别声明 |
| Deskflow 内置核心 | 固定 v1.27.0 提交；核心示例文件声明 GPL-2.0-only WITH LicenseRef-OpenSSL-Exception；上游带不同许可的文件按原 SPDX 保留。用户可从源码、生成器和打包脚本重建改版 | 不用项目根 GPLv3 覆盖 Deskflow，也不用 OpenSSL 例外推导 Qt 许可。本项目原创核心补丁提供 MIT 路径，发布前确认贡献授权 |
| Qt 6.7.3 | 早期 Mac 包和 Linux 参考 wheel 的 QtCore 实测内嵌 Apache-2.0 Tika MIME 数据。新打包流程给 Mac 和 Linux 核心分别附上一套以关闭内嵌数据库配置编译的 QtBase。界面仍由 PySide6 提供 Qt | 执行私有 Qt 构建并验证最终装载路径。发布二进制还需核对实际启用的其他 Qt 组件、平台插件及版权和对应源码 |
| TwinDesk | `Drag.swift` 衍生自固定提交。本地修改了拖放会话、松手清理、文件承诺与预览，原文件和改版各有独立校验值。构建后的 helper 链接 macOS 系统框架和 Swift 运行库 | 保留原版权及 GPLv3，交付完整 Swift helper 源码和编译配方。独立 Swift 入口和边缘捕获也随包交付 |

GPLv2-only 与仅有 GPLv3/LGPLv3 的库存在许可障碍；动态链接本身不会化解它。但实际 Qt 6.7.3 的 [QtCore](https://raw.githubusercontent.com/qt/qtbase/v6.7.3/src/corelib/kernel/qobject.cpp)、[QtWidgets](https://raw.githubusercontent.com/qt/qtbase/v6.7.3/src/widgets/kernel/qapplication.cpp) 和 [QtGui](https://raw.githubusercontent.com/qt/qtbase/v6.7.3/src/gui/kernel/qguiapplication.cpp) 源码给出 GPLv2 选择。这与仅看到 PyPI wheel 的 LGPL 标签是两件事。参见 [GNU 兼容性表](https://www.gnu.org/licenses/gpl-faq.html#AllCompatibility)和 [Qt 许可说明](https://www.qt.io/development/open-source-lgpl-obligations)。进程隔离是当前判断的重要事实，不是对所有 IPC 架构的自动豁免。

## 旧试用包的问题与发布候选的处理

Qt 的 [Tika 归属声明](https://raw.githubusercontent.com/qt/qtbase/v6.7.3/src/corelib/mimetypes/3rdparty/qt_attribution.json)将内置 MIME 数据标记为 Apache-2.0。对实际库进行只读检查：从 Mac 冻结包的 QtCore 中解出 gzip 数据，从 Linux 参考环境的 QtCore 中解出 Zstandard 数据；两者均与固定 QtBase 源码归档内的 Tika 文件逐字节匹配。记录见 [QtCore 二进制证据](../third_party/Qt-runtime-audit.json)，可用 `packaging/audit_qtcore_mime.py` 重做；没有命中不代表不存在派生或重新编码版本。

按 [Apache 基金会的许可兼容性说明](https://www.apache.org/licenses/GPL-compatibility.html)，Apache-2.0 与 GPLv3 兼容，但不能作为 GPLv2-only 组合的兼容依据。故在完成额外许可或兼容构建前，暂缓当前原生核心与这份 QtCore 的二进制分发。Linux 不打包 Qt wheel，仍需为实际支持的核心运行组合选择合适的库，不能把外部安装当作放行证据。

三个进程继续分别运行。发布候选通过 [独立 QtBase 构建流程](Packaging.md#编译输入核心专用-qtbase)禁用内嵌 MIME 数据库，将输入核心的 Qt 依赖固定到包内私有目录。候选独立构建、验证，当前设备仍使用各自已经安装的试用版本。

## 已备齐的材料

- [第三方声明](../THIRD_PARTY_NOTICES.md)、[官方许可证文本](../third_party/licenses)、[Qt 源码 SPDX 样本](../third_party/Qt-source-SPDX.json)、[Qt 源码归属清单](../third_party/Qt-source-attributions.json)以及 [TwinDesk 固定来源](../native/drag/macos/vendor.json)。
- [源码来源和校验值](../third_party/SOURCE-MATERIALS.json)包含 Qt Base、Qt SVG、Qt Image Formats、Qt for Python（含 PySide6 和 Shiboken）、Mac 本地候选链接的 OpenSSL 3.6.3、TwinDesk 原文件与许可证，以及供归属核对的 Tika NOTICE。已下载官方源码归档并校验。
- 构建入口输出 Deskflow 官方快照、带变更声明的改版引擎源码和主项目源码。主项目归档带完整 Swift helper 源码、原生补丁生成器和构建脚本。Deskflow 上游 OpenSSL 例外随对应原文件保留。
- Mac 打包规则将 Qt、PySide/Shiboken、TwinDesk 许可证与材料清单放入包内 `licenses/`。Linux 归档可从根目录查看 `third_party/` 和 `licenses/DeskflowCore/`。

按清单下载源码至同一目录（TwinDesk 专用子目录名称按 `file` 字段），然后在项目根目录校验：

```sh
python3 packaging/verify_source_materials.py /path/to/source-archives
python3 packaging/build.py source --deskflow-checkout ./deskflow-src --out ./local/source-review
```

## 二进制发布前仍需完成

1. **对账最终工件。** 保留核心专用 Qt 的构建配置、插件来源、Tika 数据检查及装载路径证据，记录发行 wheel 的 SHA-256。复核两个版本启用的 Qt 组件、Linux 系统依赖、Mac 静态链接的 OpenSSL 及冻结包中的 CPython、其他工具链运行时和 PyInstaller 引导程序。Mac 本地候选的 OpenSSL 使用构建机已有的 3.6.3，公开发行前选用并验证仍获维护的版本。
2. **让每个二进制都能拿到对应源码。** 发布前为实际工件生成文件/组件清单和校验值，把匹配的源树、修改、构建/安装配方及适用许可放到发行方控制的位置，并把源下载入口连到对应二进制。在采用 LGPL 的部分落实适用的替换/重链接条件；正式签名后的重建和用户安装路径要另行验证。
3. **确认授权与发布范围。** 得到有权授权的贡献者对原创补丁许可的确认，复核 Deskflow 与 Qt GPLv2 方案适用于实际组合。如果具体组件只有 v3 条款又必须链接进 v2-only 核心，先更换/去掉该组件或拿到明确额外许可，再发二进制；不能靠加声明来解决。完成签名、干净环境安装和实机回归后再发布。

源码可按组件边界先行整理，不附带当前二进制、私有配置或本机授权。本次没有上传源码、建发布或改动现有设备上的运行版本。
