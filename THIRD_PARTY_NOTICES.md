# Third-party notices

InputBridge application code is GPL-3.0-or-later; see [LICENSE](LICENSE).
The following components keep their own notices and license terms. The current
packages are local development artifacts; this notice does not clear public
binary redistribution.

## Deskflow input engine

`inputbridge-core` is a separate executable built from [Deskflow v1.27.0, commit
081f6478e654a112168879e67a41ce7d7e4ae3af](https://github.com/deskflow/deskflow/tree/081f6478e654a112168879e67a41ce7d7e4ae3af).
It retains upstream per-file copyright and license notices, including
GPL-2.0-only and the OpenSSL exception on files carrying that exception.
That exception concerns OpenSSL; it is not a Qt or GPLv3 exception.
Original InputBridge contributions to the generated engine patches, including
its wheel implementation, use the MIT terms in
[native/engine/wheel/LICENSE.txt](native/engine/wheel/LICENSE.txt).
They add private configuration/IPC, input focus, drag coordination and wheel
handling and exclude the upstream GUI. `native/engine/prepare.py` adds change
notices and the build produces both the original checkout snapshot and the
modified engine source archive. Deskflow-derived code remains under the
applicable upstream terms; it is not relicensed by the MIT or GPLv3 notices.

## Qt and Qt for Python

Qt 6.7.3, Copyright The Qt Company Ltd. and the contributors identified in the
upstream files. The macOS build bundles Qt Base (Core, GUI, Widgets, Network and
DBus), Qt SVG, image-format plugins, and related Qt Base plugins. It also uses
PySide6 Essentials and Shiboken6 6.7.3 from Qt for Python. The input engine uses
a private Qt Base build and a small set of platform plugins on macOS and Linux.
The Linux archive contains the engine runtime. PySide6 is installed separately.
These modules incorporate third-party code described by their upstream
attributions; [Qt-source-attributions.json](third_party/Qt-source-attributions.json)
is a source inventory and not a statement that every listed option was compiled.

The inspected 6.7.3 runtime sources offer GPL-2.0-only, GPL-3.0-only and
LGPL-3.0-only alternatives, alongside Qt commercial terms. Check the terms of
each actual shipped component rather than applying one license to the whole
bundle. The GPLv2 route for the Deskflow core relies on Qt files actually
available under compatible terms. The reference QtCore binaries include Apache
Tika MIME data under Apache-2.0 (Copyright 2024 The Apache Software Foundation).
The private build disables the embedded MIME database. The GPLv3 route remains
available for eligible Qt components on the Python application side. The
remaining components require review against each final release artifact.
See [reference runtime evidence](third_party/Qt-runtime-audit.json).
See [official license texts](third_party/licenses) and the
[versioned source manifest](third_party/SOURCE-MATERIALS.json).
The manifest also identifies the official release sources and PyPI wheels of the
matching version. Downstream availability of corresponding source, wheel build
provenance, and distribution of a specific signed package are separate checks.

## OpenSSL

The local macOS candidate statically links OpenSSL 3.6.3. Linux uses the
target distribution's shared OpenSSL 3 libraries. The Deskflow license
text includes its upstream OpenSSL exception. See the
[OpenSSL license](third_party/licenses/OpenSSL/LICENSE.txt) and the pinned Mac
source archive in the [source manifest](third_party/SOURCE-MATERIALS.json).

## TwinDesk native drag source

[native/drag/macos/Drag.swift](native/drag/macos/Drag.swift) is a verbatim copy of
[TwinDesk’s Drag.swift](https://raw.githubusercontent.com/InukaAsith/TwinDesk/375fae38e9408eaa2ab7acd652e11cd32b63bc37/mac-helper/Sources/macshare-helper/Drag.swift)
at commit `375fae38e9408eaa2ab7acd652e11cd32b63bc37`.
Copyright (C) 2026 inukaasith; the file declares GPL-3.0 and the project retains
the [upstream GPLv3 text](third_party/licenses/TwinDesk/LICENSE.txt).
It is built with the original InputBridge Swift entry point and edge capture
into the separate GPLv3 native drag helper, not linked into the Deskflow core.
No TwinDesk app, UI, or runtime installation is shipped.

Other runtime and build dependencies (including CPython, PyInstaller, OpenSSL,
cryptography, cffi and pycparser) keep their respective terms. Review the exact
runtime, bootloader, optional plugins and nested third-party code before any
public binary release. This audit does not assume that an externally installed
or system library is a redistribution exception for every target.
See [license review](docs/Licensing.md) and the [release checklist](docs/Release.md).
