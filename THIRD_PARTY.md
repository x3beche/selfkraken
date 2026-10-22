# Third-party components

selfkraken itself is MIT (see [LICENSE](LICENSE)). These parts are shipped
with it and keep their own licences:

| Component | Where | Licence |
| --- | --- | --- |
| Monaco Editor (VS Code's editor) | `selfkraken/gui/monaco/` | MIT, Copyright (c) Microsoft Corporation — full text in `selfkraken/gui/monaco/LICENSE` and `ThirdPartyNotices.txt` |
| PySide6 / Qt 6 (optional `gui` extra and the GUI builds) | installed dependency | LGPL-3.0 / commercial (Qt) |
| Typer, Rich, python-dotenv | installed dependency | MIT / BSD |

The standalone binaries produced by `release.yml` bundle Python, PySide6/Qt and
the vendored Monaco build; their licences travel with those files.
