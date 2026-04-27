"""
MangaDex Sync
Export and import your MangaDex library to/from MyAnimeList and AniList.
"""

__version__ = "2.0.1"
__author__ = "Spider2742"

from .app import app  # noqa: F401


def _webview_fix_hint():
    """Return a distro/OS-aware fix message for missing pywebview GUI backends."""
    import platform

    system = platform.system()

    if system == "Windows":
        return (
            "  On Windows, install Qt support:\n"
            "    pip install PyQt6 qtpy\n"
            "  Or reinstall pywebview:\n"
            "    pip install --upgrade pywebview"
        )

    if system == "Darwin":
        return (
            "  On macOS, pywebview uses WebKit which should work out of the box.\n"
            "  Try upgrading pywebview:\n"
            "    pip install --upgrade pywebview\n"
            "  If that doesn't help, install Qt as a fallback:\n"
            "    brew install pyqt  (requires Homebrew: brew.sh)\n"
            "    pip install PyQt6 qtpy"
        )

    if system == "Linux":
        distro_id   = ""
        distro_like = ""
        try:
            info = platform.freedesktop_os_release()  # Python 3.10+
            distro_id   = info.get("ID", "").lower()
            distro_like = info.get("ID_LIKE", "").lower()
        except Exception:
            pass

        combined = f"{distro_id} {distro_like}"
        is_debian = any(x in combined for x in ("debian", "ubuntu", "mint", "pop", "elementary", "kali", "zorin", "raspbian"))
        is_fedora = any(x in combined for x in ("fedora", "rhel", "centos", "rocky", "alma", "nobara"))
        is_arch   = any(x in combined for x in ("arch", "manjaro", "endeavour", "garuda", "artix"))
        is_suse   = any(x in combined for x in ("suse", "opensuse"))
        is_void   = "void" in combined

        if is_debian:
            gtk_hint = "    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.0"
        elif is_fedora:
            gtk_hint = "    sudo dnf install python3-gobject python3-gobject-base webkit2gtk4.1"
        elif is_arch:
            gtk_hint = "    sudo pacman -S python-gobject webkit2gtk-4.1"
        elif is_suse:
            gtk_hint = "    sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-WebKit2-4_1"
        elif is_void:
            gtk_hint = "    sudo xbps-install python3-gobject webkit2gtk"
        else:
            gtk_hint = "    Install python3-gobject and webkit2gtk via your package manager"

        return (
            f"  Fix with GTK (recommended):\n"
            f"{gtk_hint}\n\n"
            f"  Or use Qt instead:\n"
            f"    pip install PyQt6 qtpy"
        )

    return (
        "  Try installing Qt support:\n"
        "    pip install PyQt6 qtpy\n"
        "  Or GTK via your system package manager (python3-gobject + webkit2gtk)"
    )


def main():
    """CLI entry point: runs the Flask app with optional pywebview."""
    import sys
    import time
    import webbrowser
    import threading
    import requests as req_lib

    missing = []
    for pkg in ["requests", "pandas", "openpyxl", "flask"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing packages. Run:  pip install {' '.join(missing)}")
        sys.exit(1)

    PORT = 7337

    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    for _ in range(20):
        try:
            req_lib.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            break
        except Exception:
            time.sleep(0.3)

    try:
        import webview

        print(f"\n  MangaDex Sync v{__version__}")
        print(f"  Opening native window...")
        print(f"  (Also accessible at http://localhost:{PORT})\n")
        webview.create_window(
            title=f"MangaDex Sync v{__version__}",
            url=f"http://127.0.0.1:{PORT}",
            width=1200,
            height=800,
            min_size=(900, 600),
            background_color="#0c0e14",
        )
        import os, sys
        with open(os.devnull, 'w') as devnull:
            old_stderr = sys.stderr
            sys.stderr = devnull
            try:
                webview.start()
            finally:
                sys.stderr = old_stderr

    except ImportError:
        print(f"\n  pywebview not installed - opening in browser instead.")
        print(f"  To get the native window:  pip install 'mangadex-sync[desktop]'\n")
        print(f"  MangaDex Sync v{__version__}")
        print(f"  Open in browser:  http://localhost:{PORT}\n")
        webbrowser.open(f"http://localhost:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down.")

    except Exception:
        print(f"\n  pywebview is installed but couldn't find a GUI backend.")
        print(f"\n{_webview_fix_hint()}")
        print(f"\n  Falling back to browser...")
        print(f"  Open in browser:  http://localhost:{PORT}\n")
        webbrowser.open(f"http://localhost:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down.")