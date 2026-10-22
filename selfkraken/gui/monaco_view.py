"""Monaco editor embedded in the Qt window — and the plain viewer behind it.

:class:`MonacoView` is the GUI's code viewer: the vendored Monaco editor in a
``QWebEngineView`` when that can run, and the old ``QPlainTextEdit`` diff viewer
when it cannot.  The API is the same either way, so the rest of the window never
asks which one it got.

Privacy and security, in the order they are applied:

1. **Nothing is downloaded.**  The editor bytes live in ``selfkraken/gui/monaco``
   (see ``scripts/vendor_monaco.py``) and the page is a local file.
2. **Nothing is allowed out.**  One :class:`QWebEngineUrlRequestInterceptor` on a
   shared off-the-record profile drops every request whose scheme is not
   ``file``/``qrc``/``data``/``blob`` and every ``file`` path outside the vendor
   directory — so even a bug in Monaco, or repository *content* that tries to
   fetch something, cannot open a connection.  The policy is the pure function
   :func:`request_is_allowed`, which the tests exercise directly.
3. **Nothing is executed as code.**  Payloads are built with ``json.dumps``
   (:func:`selfkraken.gui.monaco_payload.payload_json`), never concatenated into
   JavaScript, so file content containing ``</script>`` or backticks stays text.
4. **Nothing is kept, and nothing navigates.**  Off-the-record profile with no
   cache and no cookies, popups and clipboard off, and a page subclass that
   refuses every navigation except its own file.

The fallback is not decoration: QtWebEngine cannot paint under
``QT_QPA_PLATFORM=offscreen``, so the test suite — and any machine without the
web-engine addon — always gets the plain viewer, with one muted line saying why.
"""

from __future__ import annotations

import difflib
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from selfkraken.gui import monaco_payload as mp
from selfkraken.gui.diff_view import DiffHighlighter, monospace_font
from selfkraken.gui.theme import (
    META_PT,
    MONO_PT,
    SPACE_4,
    Palette,
    resolve as resolve_palette,
)

def _monaco_dir() -> Path:
    """Where the vendored editor lives (``vs/`` + ``editor.html`` + licences).

    Source and wheel installs keep it beside this module. A PyInstaller bundle
    carries it as data under ``_MEIPASS`` instead, because frozen modules have no
    real directory of their own.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "selfkraken" / "gui" / "monaco"
    return Path(__file__).resolve().parent / "monaco"


MONACO_DIR = _monaco_dir()
EDITOR_PAGE = MONACO_DIR / "editor.html"

#: ``SK_GUI_NO_WEBENGINE=1`` forces the plain viewer; ``SK_GUI_FORCE_WEBENGINE=1``
#: forces Monaco even offscreen (it will not paint there, but it proves the page
#: boots and lets its console errors reach the terminal).
NO_WEBENGINE_VAR = "SK_GUI_NO_WEBENGINE"
FORCE_WEBENGINE_VAR = "SK_GUI_FORCE_WEBENGINE"
DEBUG_VAR = "SK_GUI_DEBUG"

#: Monaco's AMD loader walks the tree beside the page and its worker bootstrap
#: ``importScripts`` a file URL, which Chromium only permits for file pages with
#: this switch.  Qt reads ``QTWEBENGINE_CHROMIUM_FLAGS`` when the engine first
#: initialises, so it is set here — before QtWebEngine is imported — and nowhere
#: else; it adds no network access, the interceptor still decides every request.
CHROMIUM_FLAGS_VAR = "QTWEBENGINE_CHROMIUM_FLAGS"
REQUIRED_CHROMIUM_FLAGS: tuple[str, ...] = ("--allow-file-access-from-files",)

#: how long to wait for the page to boot before deciding it never will
BOOT_TIMEOUT_MS = 20_000
BOOT_POLL_MS = 60

#: what the fallback says, in one short muted line
FALLBACK_NOTE = "plain viewer (Monaco unavailable)"

#: the plain viewer stops painting past this many lines (it is a text widget, not
#: a viewport-culled editor)
FALLBACK_MAX_LINES = 5000

#: schemes the viewer may load; ``blob`` is Monaco's own worker bootstrap
ALLOWED_SCHEMES: tuple[str, ...] = ("file", "qrc", "data", "blob")


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #
def vendored_files_present() -> bool:
    """True when the page and enough of ``vs/`` exist to boot Monaco."""
    if not EDITOR_PAGE.is_file():
        return False
    for relative in ("vs/loader.js", "vs/editor/editor.main.js",
                     "vs/editor/editor.main.css"):
        if not (MONACO_DIR / relative).is_file():
            return False
    return True


def _is_offscreen() -> bool:
    platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    return platform.startswith("offscreen") or platform in ("minimal", "minimalextended")


def _debug_on() -> bool:
    return os.environ.get(DEBUG_VAR, "").strip() == "1"


def webengine_requested() -> bool:
    """True when this process should try Monaco at all."""
    if os.environ.get(NO_WEBENGINE_VAR, "").strip() == "1":
        return False
    if os.environ.get(FORCE_WEBENGINE_VAR, "").strip() == "1":
        return True
    if _is_offscreen():
        return False  # offscreen QtWebEngine cannot paint: tests want the fallback
    return True


def import_webengine() -> Optional[Sequence[object]]:
    """The five web-engine classes, or ``None`` when the addon is not installed.

    The Chromium flag has to be in the environment before this import, because
    the engine may initialise during it.
    """
    flags = os.environ.get(CHROMIUM_FLAGS_VAR, "")
    add = [flag for flag in REQUIRED_CHROMIUM_FLAGS if flag not in flags]
    if add:
        os.environ[CHROMIUM_FLAGS_VAR] = " ".join([flags, *add]).strip()
    try:
        from PySide6.QtWebEngineCore import (
            QWebEnginePage,
            QWebEngineProfile,
            QWebEngineSettings,
            QWebEngineUrlRequestInterceptor,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except Exception as exc:
        if _debug_on():
            print(f"selfkraken gui: QtWebEngine unavailable ({exc})", file=sys.stderr)
        return None
    return (
        QWebEngineView,
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineSettings,
        QWebEngineUrlRequestInterceptor,
    )


def monaco_supported() -> bool:
    """Both halves of the requirement: asked for, and actually installable."""
    if not webengine_requested() or not vendored_files_present():
        return False
    return import_webengine() is not None


# --------------------------------------------------------------------------- #
# request policy (pure, testable)
# --------------------------------------------------------------------------- #
def request_is_allowed(url: QUrl, root: Path = MONACO_DIR) -> bool:
    """The whole network policy of the viewer, as one decision.

    ``file`` URLs must resolve *inside* the vendor directory; ``qrc`` is Qt's own
    resource scheme; ``data`` and ``blob`` are what Monaco's worker bootstrap
    uses to start a worker without a fetch.  Everything else — http, https, ws,
    a ``filesystem`` or ``chrome`` scheme, and any ``file://`` pointing outside
    the vendor tree (your repository, your ``~/.ssh``, a world-writable temp dir)
    — is refused.
    """
    scheme = (url.scheme() or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False
    if scheme != "file":
        return True
    local = url.toLocalFile()
    if not local:
        return False
    try:
        target = Path(local).resolve()
    except OSError:  # pragma: no cover - pathological path
        return False
    root = Path(root).resolve()
    return target == root or root in target.parents


def unified_diff_text(original: str, modified: str, path: str) -> str:
    """The plain viewer's text for a before/after pair, in git's own shape."""
    label = str(path or "") or "file"
    body = "".join(
        difflib.unified_diff(
            (original or "").splitlines(keepends=True),
            (modified or "").splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )
    header = f"diff --git a/{label} b/{label}\n"
    if not body:
        return f"{header}(no changes)\n"
    return header + body


# --------------------------------------------------------------------------- #
# the shared, off-the-record profile
# --------------------------------------------------------------------------- #
def _build_page_classes(engine: Sequence[object]) -> tuple[type, type]:
    """Subclasses of the engine's Page/Interceptor, made once per process."""
    _view, page_class, _profile, _settings, interceptor_class = engine

    class Interceptor(interceptor_class):  # type: ignore[misc,valid-type]
        def interceptRequest(self, info):  # noqa: N802 - Qt name
            url = info.requestUrl()
            if not request_is_allowed(url):
                info.block(True)
                if _debug_on():
                    # the scheme only: a request URL can carry a path out of the
                    # repository, and the log is not where that belongs
                    print(
                        f"selfkraken gui: blocked web request "
                        f"scheme={url.scheme() or '(none)'}",
                        file=sys.stderr,
                    )

    class Page(page_class):  # type: ignore[misc,valid-type]
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # noqa: N802
            # this widget shows data, it is not a browser: nothing may load in
            # the page's place, not even a link inside a README
            return url.scheme() == "file" and Path(url.toLocalFile()) == EDITOR_PAGE

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if _debug_on():
                print(
                    f"monaco js [{level}]: {message} ({source}:{line})",
                    file=sys.stderr,
                )

    return Interceptor, Page


_ENGINE: Optional[Sequence[object]] = None
_PAGE_CLASSES: Optional[tuple[type, type]] = None
_PROFILE: Optional[object] = None
_INTERCEPTOR: Optional[object] = None
_PROFILE_TRY_FAILED = False


def _shared_engine() -> Optional[Sequence[object]]:
    """Engine classes, page subclasses and one off-the-record profile, once.

    A single profile is deliberate: it is cheap, and being unnamed it is off the
    record — no cookies, no cache.  The request interceptor lives on it, so every
    viewer shares one network policy.
    """
    global _ENGINE, _PAGE_CLASSES, _PROFILE, _INTERCEPTOR, _PROFILE_TRY_FAILED
    if _ENGINE is None:
        _ENGINE = import_webengine()
    if _ENGINE is None:
        return None
    if _PAGE_CLASSES is None:
        _PAGE_CLASSES = _build_page_classes(_ENGINE)
    if _PROFILE is None and not _PROFILE_TRY_FAILED:
        profile_class = _ENGINE[2]
        interceptor_class = _PAGE_CLASSES[0]
        try:
            profile = profile_class()  # unnamed == off the record
            profile.setHttpCacheType(profile_class.HttpCacheType.NoCache)
            profile.setPersistentCookiesPolicy(
                profile_class.PersistentCookiesPolicy.NoPersistentCookies
            )
            # Qt does not own the interceptor, so the process keeps it alive
            _INTERCEPTOR = interceptor_class()
            installer = getattr(
                profile, "setUrlRequestInterceptor", None
            ) or profile.setRequestInterceptor  # older Qt spelled it differently
            installer(_INTERCEPTOR)
            _PROFILE = profile
        except Exception as exc:  # pragma: no cover - engine refused the profile
            _PROFILE_TRY_FAILED = True
            if _debug_on():
                print(f"selfkraken gui: no web profile ({exc})", file=sys.stderr)
    return _ENGINE if _PROFILE is not None else None


# --------------------------------------------------------------------------- #
# the widget
# --------------------------------------------------------------------------- #
class MonacoView(QWidget):
    """A read-only code viewer: Monaco when it can be, plain text when not."""

    def __init__(
        self,
        palette: Palette,
        *,
        parent: Optional[QWidget] = None,
        name: str = "code viewer",
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._tokens = mp.theme_tokens(palette)
        self._mode = ""
        self._text = ""
        self._ready = False
        self._queued: dict = {}
        self._diff_pair: Optional[tuple[str, str, str]] = None
        self._side_by_side = False
        self._view = None
        self._page = None
        self._poll_left = 0
        self._boot_timer: Optional[QTimer] = None
        self._fallback_editor: Optional[QPlainTextEdit] = None
        self._fallback_highlighter: Optional[DiffHighlighter] = None
        self._fallback_note: Optional[QLabel] = None

        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)

        self.uses_monaco = False
        engine = _shared_engine() if monaco_supported() else None
        if engine is not None:
            self.uses_monaco = self._build_webengine(engine)
        if not self.uses_monaco:
            self._build_fallback(name)
        self.setAccessibleName(
            f"{name} — {'Monaco' if self.uses_monaco else 'plain'} viewer"
        )

    # ------------------------------------------------------------------ setup
    def _build_webengine(self, engine: Sequence[object]) -> bool:
        """Create the view on the shared profile; False when it cannot start."""
        view_class = engine[0]
        settings_class = engine[3]
        _interceptor_class, page_class = _PAGE_CLASSES or (None, None)
        # name -> wanted; a name this Qt does not know is simply skipped
        wanted = {
            "JavascriptEnabled": True,
            "JavascriptCanOpenWindows": False,
            "JavascriptCanAccessClipboard": False,
            "JavascriptCanPaste": False,
            "LocalContentCanAccessRemoteUrls": False,
            "LocalContentCanAccessFileUrls": True,
            "AllowRunningInsecureContent": False,
            "LinksClickedExternallyEnabled": False,
            "ScrollAnimatorEnabled": False,
            "SpatialNavigationEnabled": False,
            "FullScreenSupportEnabled": False,
            "ErrorMessagesEnabled": True,
            # NOTE: images and localStorage are left at their defaults — Monaco
            # reads both, and with an off-the-record profile plus the request
            # interceptor neither can reach the network or the disk.
        }
        try:
            self._page = page_class(_PROFILE, self)
            # paint the view in the app's own colour before the page has drawn
            # anything: a dark window must not flash white while Monaco boots
            self._page.setBackgroundColor(QColor(self._palette.base))
            settings = self._page.settings()
            attributes = settings_class.WebAttribute
            for name, enabled in wanted.items():
                attribute = getattr(attributes, name, None)
                if attribute is None:
                    continue  # this Qt version has no such knob
                settings.setAttribute(attribute, enabled)
            self._view = view_class(self._page, self)
            self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self._view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception as exc:  # pragma: no cover - engine refuses to start
            if _debug_on():
                print(f"selfkraken gui: QtWebEngine init failed ({exc})", file=sys.stderr)
            self._view = None
            self._page = None
            return False

        self._box.addWidget(self._view, 1)
        self._view.loadFinished.connect(self._on_load_finished)
        self._boot_timer = QTimer(self)
        self._boot_timer.setInterval(BOOT_POLL_MS)
        self._boot_timer.timeout.connect(self._poll_boot)
        self._view.load(QUrl.fromLocalFile(str(EDITOR_PAGE)))
        return True

    def _build_fallback(self, name: str) -> None:
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.setFont(monospace_font(MONO_PT))
        editor.document().setDocumentMargin(0)
        editor.setAccessibleName(f"{name} (plain viewer)")
        self._fallback_editor = editor
        self._fallback_highlighter = DiffHighlighter(editor.document(), self._palette)
        note = QLabel(FALLBACK_NOTE, self)
        note.setObjectName("fieldLabel")
        note.setStyleSheet(f"color: {self._palette.muted}; font-size: {META_PT:g}pt;")
        note.setContentsMargins(SPACE_4, 0, SPACE_4, 0)
        note.setAccessibleName("Viewer mode note")
        self._fallback_note = note
        self._box.addWidget(editor, 1)
        self._box.addWidget(note)

    # ------------------------------------------------------------------ boots
    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._give_up("the editor page could not load")
            return
        self._poll_left = max(BOOT_TIMEOUT_MS // BOOT_POLL_MS, 1)
        self._boot_timer.start()
        self._poll_boot()

    def _poll_boot(self) -> None:
        """Ask the page whether Monaco came up; hold one payload until it has."""
        if self._ready:
            self._boot_timer.stop()
            return
        self._poll_left -= 1
        if self._poll_left <= 0:
            self._boot_timer.stop()
            self._give_up("Monaco did not finish loading")
            return

        def answered(value: object) -> None:
            if self._ready:
                return
            status = str(value or "booting")
            if status == "ready":
                self._ready = True
                self._boot_timer.stop()
                payload, self._queued = self._queued, {}
                if payload:
                    self._push(payload)
            elif status.startswith("error:"):
                self._boot_timer.stop()
                reason = status[len("error:") :].strip() or "Monaco reported an error"
                self._give_up(reason)

        try:
            self._page.runJavaScript("window.skStatus && window.skStatus()", answered)
        except Exception as exc:  # pragma: no cover - the page died under us
            self._boot_timer.stop()
            self._give_up(str(exc))

    def _give_up(self, reason: str) -> None:
        """Swap in the plain viewer for good, with the reason in the note."""
        if _debug_on():
            print(f"selfkraken gui: Monaco unusable ({reason})", file=sys.stderr)
        if self._boot_timer is not None:
            self._boot_timer.stop()
        if self._view is not None:
            self._view.setParent(None)
            self._view.deleteLater()
        self._view = None
        self._page = None
        self._ready = False
        self.uses_monaco = False
        self._queued = {}
        self._build_fallback("code viewer")
        if self._fallback_note is not None:
            self._fallback_note.setText(f"{FALLBACK_NOTE}: {reason}")
        self._show_plain(self._mode, self._text)

    # ----------------------------------------------------------------- pushing
    def _push(self, payload: dict) -> None:
        try:
            self._view.page().runJavaScript(mp.js_apply(payload))
        except Exception as exc:  # pragma: no cover - page died under us
            self._give_up(str(exc))

    def _dispatch(self, payload: dict, mode: str, plain_text: str) -> None:
        """Monaco when it is up, the plain viewer otherwise; the newest wins."""
        self._mode = mode
        self._text = plain_text
        if self.uses_monaco and self._view is not None:
            if self._ready:
                self._push(payload)
            else:
                self._queued = payload
            # the plain widget still holds the same text for toPlainText()
            return
        self._show_plain(mode, plain_text)

    # -------------------------------------------------------------------- API
    def show_diff(
        self, original: str, modified: str, path: str, *, side_by_side: bool = False
    ) -> None:
        """Before/after of one file, side by side or inline."""
        self._diff_pair = (original, modified, path)
        self._side_by_side = bool(side_by_side)
        self._dispatch(
            mp.build_diff_payload(
                original, modified, path, side_by_side=side_by_side,
                tokens=self._tokens,
            ),
            "diff",
            unified_diff_text(original, modified, path),
        )

    def show_file(
        self, text: str, path: str, *, highlight_lines: Iterable[int] = ()
    ) -> None:
        """One file, read-only; *highlight_lines* mark what a proposal touches."""
        lines = {int(n) for n in highlight_lines if int(n) > 0}
        self._dispatch(
            mp.build_file_payload(text, path, highlight_lines=lines, tokens=self._tokens),
            "file",
            text,
        )

    def show_text(self, text: str) -> None:
        """Test output and other console text: plaintext, never a language guess."""
        self._dispatch(mp.build_text_payload(text, tokens=self._tokens), "text", text)

    def show_message(self, message: str) -> None:
        """A short explanation instead of code (binary file, gone from disk…)."""
        self._dispatch(mp.build_text_payload(message, tokens=self._tokens), "text", message)

    def set_theme(self, theme: str) -> None:
        """Recolour to ``'dark'`` or ``'light'`` — the app's palette, in Monaco."""
        palette = resolve_palette(theme, prefers_dark=None)
        self._palette = palette
        self._tokens = mp.theme_tokens(palette)
        if self._fallback_highlighter is not None:
            self._fallback_highlighter.set_palette(palette)
        if self._fallback_note is not None:
            self._fallback_note.setStyleSheet(
                f"color: {palette.muted}; font-size: {META_PT:g}pt;"
            )
        if self.uses_monaco:
            payload = mp.build_theme_payload(self._tokens)
            if self._ready:
                self._push(payload)
            else:
                self._queued = payload

    def set_side_by_side(self, side_by_side: bool) -> None:
        """Re-render the current diff in the other layout, from the same strings."""
        if self._diff_pair is None:
            return
        self._side_by_side = bool(side_by_side)
        original, modified, path = self._diff_pair
        self.show_diff(original, modified, path, side_by_side=side_by_side)

    def is_side_by_side(self) -> bool:
        return bool(self._side_by_side)

    def clear(self) -> None:
        self._diff_pair = None
        self._dispatch(mp.build_clear_payload(), "", "")

    def toPlainText(self) -> str:  # noqa: N802 - Qt name
        """What the viewer is showing, as text (tests and "copy" use this)."""
        return self._text

    # ------------------------------------------------------------- plain path
    def _show_plain(self, mode: str, text: str) -> None:
        editor = self._fallback_editor
        if editor is None:
            return
        if mode == "" or not text:
            editor.setPlainText("")
            return
        lines = text.splitlines()
        if len(lines) > FALLBACK_MAX_LINES:
            text = "\n".join(lines[:FALLBACK_MAX_LINES]) + "\n…(truncated)"
        editor.setPlainText(text)
