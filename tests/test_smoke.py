"""Headless tests: model, banner, settings, and every TUI screen."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gdmirror import banner  # noqa: E402
from gdmirror.config import FOLDER_MIME  # noqa: E402
from gdmirror.drive import Node  # noqa: E402
from gdmirror.util import dedupe, human, sanitize  # noqa: E402


def plain(widget) -> str:
    """Text currently shown by a Static, across Textual versions."""
    content = getattr(widget, "content", None)
    if content is None:
        content = getattr(widget, "renderable", "")
    return getattr(content, "plain", str(content))

# Screenshots land beside the repo, or wherever $GDM_SHOTS points.
SHOTS = Path(
    os.environ.get("GDM_SHOTS", Path(__file__).resolve().parent.parent / ".screenshots")
)


def make_tree() -> Node:
    root = Node(id="r", name="Lecture Archive", mime=FOLDER_MIME, size=0, md5=None,
                modified="", path="")
    for i in (1, 2, 3):
        folder = Node(id=f"f{i}", name=f"Unit {i} - Foundations", mime=FOLDER_MIME,
                      size=0, md5=None, modified="", path=f"Unit {i} - Foundations")
        for j in (1, 2, 3, 4):
            folder.children.append(
                Node(id=f"f{i}v{j}", name=f"lecture-{j:02d}.mp4", mime="video/mp4",
                     size=(400 + 60 * j) * 1024 * 1024, md5=f"md5{i}{j}",
                     modified="", path=f"Unit {i} - Foundations/lecture-{j:02d}.mp4")
            )
        root.children.append(folder)
    root.children.append(
        Node(id="doc", name="syllabus", mime="application/vnd.google-apps.document",
             size=2 * 1024 * 1024, md5=None, modified="", path="syllabus")
    )
    return root


def test_util() -> None:
    assert sanitize("a/b:c") == "a_b:c"
    assert sanitize("  ..  ") == "unnamed"
    taken: set[str] = set()
    assert dedupe("a.mp4", taken) == "a.mp4"
    assert dedupe("a.mp4", taken) == "a (2).mp4"
    assert human(1536) == "1.5 KB"
    print("util ok")


def test_model() -> None:
    root = make_tree()
    files = list(root.walk_files())
    assert len(files) == 13, len(files)
    assert files[-1].is_native
    assert files[0].key.endswith(":Unit 1 - Foundations/lecture-01.mp4")
    clone = Node.from_dict(root.to_dict())
    assert clone.count_files() == 13
    assert clone.total_size() == root.total_size()
    print("model ok")


def test_banner() -> None:
    # every glyph the wordmark needs must exist, and be the same height
    for ch in banner.WORDMARK:
        assert ch in banner.GLYPHS, ch
    for name, glyph in banner.GLYPHS.items():
        assert len(glyph) == banner.GLYPH_H, name
        assert len({len(line) for line in glyph}) == 1, f"{name} rows differ in width"

    # the wordmark has to clear an 80-column terminal
    _, wide = banner.glyph_cells(banner.WORDMARK)
    assert wide == 64, wide
    assert wide <= 80, "wordmark must fit an 80-column terminal"

    art = banner.render(reveal=1.0, sweep=0.5)
    lines = art.plain.rstrip("\n").split("\n")
    assert len(lines) == banner.GLYPH_H, len(lines)
    assert all(len(line) == wide for line in lines)

    # depth comes from the letterform, so the plain text alone must read as 3D:
    # solid front faces plus a bevel drawn in box characters
    assert "█" in art.plain
    assert banner.BEVEL & set(art.plain), "no bevel characters in the wordmark"

    # stacked words are centred and separated by a blank row
    stacked = banner.plain(["DRIVE", "MIRROR"]).split("\n")
    assert len(stacked) == banner.GLYPH_H * 2 + banner.LINE_GAP, len(stacked)
    gap = stacked[banner.GLYPH_H:banner.GLYPH_H + banner.LINE_GAP]
    assert all(not row.strip() for row in gap), "stacked lines run together"

    # a partial wipe must draw strictly fewer blocks than the finished banner
    assert banner.render(reveal=0.35).plain.count("█") < art.plain.count("█")
    assert banner.render(reveal=0.0).plain.count("█") == 0

    # the documentation form is the same art without colour
    assert banner.plain().split("\n") == [
        line.rstrip() for line in art.plain.rstrip("\n").split("\n")
    ]
    print("banner ok")


def test_settings(tmp: Path) -> None:
    import gdmirror.config as cfg_mod
    import gdmirror.settings as settings_mod

    cfg_mod.SETTINGS_FILE = settings_mod.SETTINGS_FILE = tmp / "settings.json"
    cfg = settings_mod.Settings()
    cfg.folder_id = "abc123"
    cfg.workers = 9
    cfg.dest = str(tmp / "out")
    cfg.save()
    loaded = settings_mod.Settings.load()
    assert loaded.folder_id == "abc123" and loaded.workers == 9
    assert loaded.dest_path == tmp / "out"
    print("settings ok")


async def test_screens(tmp: Path) -> None:
    import gdmirror.config as cfg_mod
    import gdmirror.settings as settings_mod
    import gdmirror.state as state_mod
    import gdmirror.tgconfig as tgcfg_mod

    # Every path the app reads must point inside tmp. Miss one and the app picks
    # up the developer's own credentials, which then end up in the screenshots
    # this test publishes.
    cfg_mod.STATE_FILE = state_mod.STATE_FILE = tmp / "state.json"
    cfg_mod.SETTINGS_FILE = settings_mod.SETTINGS_FILE = tmp / "settings.json"
    tgcfg_mod.TG_CONFIG_FILE = tmp / "telegram.json"

    from gdmirror.ui.app import MirrorApp
    from gdmirror.ui.browse import BrowseScreen
    from gdmirror.ui.menu import MenuScreen
    from gdmirror.ui.settingsscr import SettingsScreen
    from gdmirror.ui.splash import SplashScreen

    cfg = settings_mod.Settings(folder_id="fake-folder", dest=str(tmp / "downloads"))
    root = make_tree()
    app = MirrorApp(cfg=cfg, splash=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        # ignore any real token / cached tree in the working directory
        app.creds = None
        app.drive_tree = None
        app.tgcfg = tgcfg_mod.TgConfig()
        app.tg_authorized = False

        # 1. splash renders and self-advances
        splash = app.screen
        assert isinstance(splash, SplashScreen), splash
        splash._t0 = time.monotonic() - 2.6  # jump past wipe + sweep
        splash._tick()
        await pilot.pause()
        app.save_screenshot(str(SHOTS / "gdmirror-splash.svg"))
        assert "█" in plain(splash.query_one("#banner"))

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen), app.screen

        # 2. menu without a tree gates the download actions
        menu = app.screen
        assert menu._ready("scan") is False  # not authenticated yet
        menu._dispatch("browse")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen), "browse must be blocked"

        # 3. with creds + tree the menu unlocks and the status panel fills in
        app.creds = object()
        app.drive_tree = root
        menu.on_screen_resume()
        menu._pulse()
        await pilot.pause()
        assert menu._ready("browse") is True
        status = plain(menu.query_one("#menu-status"))
        assert "13" in status and "Lecture Archive" in status
        app.save_screenshot(str(SHOTS / "gdmirror-menu.svg"))

        # 4. browse screen: selection maths and disk projection
        menu._dispatch("browse")
        await pilot.pause()
        browse = app.screen
        assert isinstance(browse, BrowseScreen)
        assert len(browse.selected) == 13  # nothing downloaded yet

        await pilot.press("n")
        await pilot.pause()
        assert browse.selected == set()

        unit1 = root.children[0]
        browse._toggle(unit1)
        await pilot.pause()
        assert len(browse.selected) == 4
        assert browse._label(unit1).plain.startswith("✔")
        assert browse._label(root).plain.startswith("◐")

        tree = browse.query_one("#tree")
        tree.root.children[0].expand()
        await pilot.pause()
        assert len(tree.root.children[0].children) == 4
        app.save_screenshot(str(SHOTS / "gdmirror-browse.svg"))

        await pilot.press("a")
        await pilot.pause()
        assert len(browse.selected) == 13
        info = plain(browse.query_one("#info"))
        assert "NOT ENOUGH SPACE" in info or "free after" in info

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

        # 5. settings round-trip through the form
        menu._dispatch("settings")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SettingsScreen)
        form.query_one("#workers").value = "7"
        form.action_save()
        await pilot.pause()
        assert app.cfg.workers == 7
        assert settings_mod.Settings.load().workers == 7
        assert isinstance(app.screen, MenuScreen)

    print("screens ok")
    print(f"screenshots -> {SHOTS}")


async def test_telegram_screens(tmp: Path) -> None:
    """Telegram setup form and the pipeline screen, both with the network faked."""
    import requests

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fakedrive
    from test_pipeline import FakeTG

    import gdmirror.config as cfg_mod
    import gdmirror.download as dlmod
    import gdmirror.pipeline as pipemod
    import gdmirror.settings as settings_mod
    import gdmirror.state as state_mod
    import gdmirror.tgconfig as tgcfg_mod

    cfg_mod.STATE_FILE = state_mod.STATE_FILE = tmp / "state.json"
    cfg_mod.SETTINGS_FILE = settings_mod.SETTINGS_FILE = tmp / "settings.json"
    tgcfg_mod.TG_CONFIG_FILE = tmp / "telegram.json"
    pipemod.MANIFEST_FILE = tmp / "manifest.json"

    # every Downloader in this test talks to the local fake Drive
    dlmod.AuthorizedSession = lambda creds: requests.Session()
    server, base = fakedrive.start_server()
    dlmod.API = base

    from gdmirror.ui.app import MirrorApp
    from gdmirror.ui.menu import MenuScreen
    from gdmirror.ui.pipelinescr import PipelineScreen
    from gdmirror.ui.tglogin import TelegramSetupScreen

    files = [
        fakedrive.make_file("p1", "Unit 1/lecture-01.mp4", 200_000),
        fakedrive.make_file("p2", "Unit 1/lecture-02.mp4", 150_000),
        fakedrive.make_file("p3", "Unit 2/lecture-01.mp4", 120_000),
    ]

    cfg = settings_mod.Settings(dest=str(tmp / "buffer"), workers=2)
    app = MirrorApp(cfg=cfg, splash=False)

    try:
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.creds = None
            app.drive_tree = None
            assert isinstance(app.screen, MenuScreen)

            # 1. no credentials yet -> the setup screen opens on the API form
            app.screen._dispatch("telegram")
            await pilot.pause()
            setup = app.screen
            assert isinstance(setup, TelegramSetupScreen)
            await pilot.pause()
            assert setup.query_one("#api_id") is not None
            assert setup.query_one("#api_hash").password is True, "api_hash must be masked"
            app.save_screenshot(str(SHOTS / "gdmirror-telegram-setup.svg"))
            await pilot.press("escape")
            await pilot.pause()

            # 2. the pipeline entry stays locked until telegram is ready
            menu = app.screen
            assert menu._ready("pipe") is False
            app.creds = object()
            app.drive_tree = make_tree()
            menu.on_screen_resume()
            assert menu._ready("pipe") is False, "still no telegram target"

            # 3. with a fake client + target, the pipeline screen runs end to end
            tg = FakeTG(delay=0.02)
            app.tg = tg
            app.tg_authorized = True
            app.tgcfg.api_id, app.tgcfg.api_hash = 1, "hash"
            app.tgcfg.chat_id, app.tgcfg.chat_title = -1001234, "TEST CHANNEL"
            app.tgcfg.buffer_gb = 0.001  # ~1 MB, forces the budget to actually bind
            app.tgcfg.throttle_seconds = 0.0
            menu.on_screen_resume()
            assert menu._ready("pipe") is True

            app.push_screen(PipelineScreen(files))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PipelineScreen)

            for _ in range(200):
                if not screen.running:
                    break
                await pilot.pause(0.05)
            assert not screen.running, "pipeline did not finish"

            prog, _, _ = screen.pipe.snapshot()
            assert prog.uploaded_files == 3, prog
            assert prog.failed == 0, prog.failures
            assert [n for n, _ in tg.sent] == [
                "lecture-01.mp4", "lecture-02.mp4", "lecture-01.mp4",
            ]
            buffer_dir = tmp / "buffer"
            assert not [f for f in buffer_dir.rglob("*") if f.is_file()], \
                "local copies should be gone"
            assert app.dlstate.uploaded_count == 3
            screen._tick()
            await pilot.pause()
            app.save_screenshot(str(SHOTS / "gdmirror-pipeline.svg"))
            assert "sent" in plain(screen.query_one("#status-line"))
    finally:
        server.shutdown()

    print("telegram screens ok")


def test_screenshots_carry_no_private_data() -> None:
    """Screenshots get committed and published, so nothing personal may reach them.

    The screens render whatever configuration the app loaded. If a test forgets to
    redirect one of the config paths into its temp directory, the real value is
    drawn on screen and captured. Assert directly against the real files rather
    than trusting each test to isolate itself.
    """
    import gdmirror.settings as settings_mod
    import gdmirror.tgconfig as tgcfg_mod

    secrets: list[tuple[str, str]] = []
    for path, keys in (
        (Path("telegram.json"), ("chat_title", "api_hash")),
        (Path("settings.json"), ("folder_id",)),
    ):
        if not path.exists():
            continue
        import json

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for key in keys:
            value = str(data.get(key, "")).strip()
            if len(value) > 3:
                secrets.append((f"{path}:{key}", value))

    # the home directory reveals a username, and screens print absolute paths
    home = str(Path.home())
    if len(home) > 3:
        secrets.append(("$HOME", home))

    # the cached scan names the real folder too
    tree = Path("tree.json")
    if tree.exists():
        import json

        try:
            name = json.loads(tree.read_text())["root"]["name"]
            if len(name) > 3:
                secrets.append(("tree.json:root", name))
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    if not secrets:
        print("screenshot privacy ok (no local config to leak)")
        return

    shots = sorted(SHOTS.glob("*.svg")) + sorted(
        Path("docs/screenshots").glob("*")
    )
    leaked: list[str] = []
    for shot in shots:
        try:
            blob = shot.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        # SVG text is split across elements, so compare on a squeezed form
        squeezed = "".join(blob.split())
        for label, value in secrets:
            if "".join(value.split())[:24] in squeezed:
                leaked.append(f"{shot} contains {label}")

    assert not leaked, "private data in screenshots:\n  " + "\n  ".join(leaked)
    print(f"screenshot privacy ok ({len(secrets)} local values checked)")


def main() -> int:
    test_util()
    test_model()
    test_banner()
    with tempfile.TemporaryDirectory() as d:
        test_settings(Path(d))
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(test_screens(Path(d)))
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(test_telegram_screens(Path(d)))
    test_screenshots_carry_no_private_data()
    print("\nall smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
