"""Tree browser: choose which parts of the folder to mirror."""

from __future__ import annotations

import shutil

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from ..config import SPACE_MARGIN
from ..drive import Node
from ..util import existing_parent, human
from .common import ConfirmScreen

MARK_ALL = "✔"
MARK_SOME = "◐"
MARK_NONE = "○"


class BrowseScreen(Screen):
    BINDINGS = [
        Binding("enter", "noop", "toggle"),
        Binding("space", "noop", "expand"),
        Binding("a", "select_all", "all"),
        Binding("n", "select_none", "none"),
        Binding("m", "select_missing", "missing only"),
        Binding("d", "download", "download"),
        Binding("t", "to_telegram", "→ telegram"),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, root: Node) -> None:
        super().__init__()
        self.root_node = root
        self.selected: set[str] = set()
        self.files_by_path = {f.path: f for f in root.walk_files()}
        self._tnodes: dict[str, TreeNode] = {}
        self._populated: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("drive", id="tree")
            yield Static(id="info", classes="panel-accent")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.data = self.root_node
        self._tnodes[""] = tree.root
        self._populate(tree.root)
        tree.root.expand()
        tree.focus()
        self.action_select_missing()

    # -- labels -----------------------------------------------------------

    def _label(self, node: Node) -> Text:
        if node.is_folder:
            files = list(node.walk_files())
            chosen = sum(1 for f in files if f.path in self.selected)
            if not files:
                mark, style = MARK_NONE, "#3f7a6b"
            elif chosen == len(files):
                mark, style = MARK_ALL, "bold #9bff3c"
            elif chosen:
                mark, style = MARK_SOME, "bold #ffd166"
            else:
                mark, style = MARK_NONE, "#3f7a6b"
            text = Text(f"{mark} ", style=style)
            text.append(node.name + "/", style="bold #b8ffe4")
            text.append(
                f"  {len(files)} files · {human(sum(f.size for f in files))}",
                style="#3f7a6b",
            )
            return text

        chosen = node.path in self.selected
        mark, style = (MARK_ALL, "#9bff3c") if chosen else (MARK_NONE, "#3f7a6b")
        text = Text(f"{mark} ", style=style)
        state = self.app.dlstate
        done = state.is_done(node.key, node.size, node.md5)
        sent = state.is_uploaded(node.key)
        text.append(node.name, style="#3f7a6b" if (done or sent) else "#b8ffe4")
        suffix = " · export" if node.is_native else ""
        if sent:
            suffix += " · in telegram"
        elif done:
            suffix += " · on disk"
        text.append(f"  {human(node.size)}{suffix}", style="#3f7a6b")
        return text

    def _refresh_labels(self) -> None:
        for tnode in self._tnodes.values():
            if tnode.data is not None:
                tnode.set_label(self._label(tnode.data))

    def _populate(self, tnode: TreeNode) -> None:
        node: Node | None = tnode.data
        if node is None or not node.is_folder or node.path in self._populated:
            return
        self._populated.add(node.path)
        for child in node.children:
            if child.is_folder:
                sub = tnode.add(self._label(child), data=child, expand=False)
            else:
                sub = tnode.add_leaf(self._label(child), data=child)
            self._tnodes[child.path] = sub

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._populate(event.node)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self._toggle(event.node.data)

    # -- selection --------------------------------------------------------

    def _toggle(self, node: Node | None) -> None:
        if node is None:
            return
        if node.is_folder:
            paths = {f.path for f in node.walk_files()}
            if paths and paths <= self.selected:
                self.selected -= paths
            else:
                self.selected |= paths
        else:
            self.selected.symmetric_difference_update({node.path})
        self._sync()

    def _sync(self) -> None:
        self._refresh_labels()
        self._refresh_info()

    def action_noop(self) -> None:
        """Enter and space are handled by the Tree itself; this labels the footer."""

    def action_select_all(self) -> None:
        self.selected = set(self.files_by_path)
        self._sync()

    def action_select_none(self) -> None:
        self.selected.clear()
        self._sync()

    def action_select_missing(self) -> None:
        state = self.app.dlstate
        self.selected = {
            p for p, n in self.files_by_path.items()
            if not state.is_done(n.key, n.size, n.md5)
        }
        self._sync()

    # -- info -------------------------------------------------------------

    def _selected_nodes(self) -> list[Node]:
        return [self.files_by_path[p] for p in self.selected]

    def _refresh_info(self) -> None:
        app = self.app
        chosen = self._selected_nodes()
        chosen_bytes = sum(n.size for n in chosen)
        native = sum(1 for n in chosen if n.is_native)
        free = shutil.disk_usage(existing_parent(app.cfg.dest_path)).free
        after = free - chosen_bytes

        text = Text()
        text.append("SELECTION\n", style="bold #00f5d4")
        text.append(f"  {len(chosen)} of {len(self.files_by_path)} files\n")
        text.append(f"  {human(chosen_bytes)}\n", style="#9bff3c")
        if native:
            text.append(f"  {native} google docs (est.)\n", style="#3f7a6b")

        text.append("\nDISK\n", style="bold #00f5d4")
        text.append(f"  free now    {human(free)}\n")
        text.append(
            f"  free after  {human(after)}\n",
            style="#ff5f7e" if after < SPACE_MARGIN else "#9bff3c",
        )
        if after < SPACE_MARGIN:
            text.append(
                f"\n  NOT ENOUGH SPACE\n  short by "
                f"{human(chosen_bytes + SPACE_MARGIN - free)}\n"
                "  deselect some folders\n",
                style="bold #ff5f7e",
            )

        text.append("\nKEYS\n", style="bold #00f5d4")
        for key, what in (
            ("enter", "toggle row"),
            ("space", "expand / collapse"),
            ("a / n", "select all / none"),
            ("m", "only what is missing"),
            ("d", "download to disk"),
            ("t", "pipe to telegram"),
            ("esc", "back to menu"),
        ):
            text.append(f"  {key:<7}", style="#00f5d4")
            text.append(f"{what}\n", style="#3f7a6b")

        self.query_one("#info", Static).update(text)

    # -- go ---------------------------------------------------------------

    def action_download(self) -> None:
        app = self.app
        chosen = self._selected_nodes()
        if not chosen:
            self.notify("nothing selected", severity="warning")
            return

        chosen_bytes = sum(n.size for n in chosen)
        free = shutil.disk_usage(existing_parent(app.cfg.dest_path)).free
        if chosen_bytes > free - SPACE_MARGIN:
            body = (
                f"Selected: {human(chosen_bytes)}\n"
                f"Free:     {human(free)}\n"
                f"Short by: {human(chosen_bytes + SPACE_MARGIN - free)}\n\n"
                "The run aborts automatically once free space drops below "
                "512 MB, leaving a partial mirror you can resume later."
            )
            app.push_screen(
                ConfirmScreen("Not enough disk space", body, "Download anyway"),
                lambda go: self._start(chosen) if go else None,
            )
            return
        self._start(chosen)

    def _start(self, chosen: list[Node]) -> None:
        from .download import DownloadScreen

        self.app.push_screen(DownloadScreen(chosen))

    def action_to_telegram(self) -> None:
        """Pipe just the selection through Telegram. Disk budget applies, so a
        selection bigger than free space is fine here."""
        app = self.app
        if not app.telegram_ready():
            self.notify("finish telegram setup first", severity="warning")
            return
        pending = [
            n for n in self._selected_nodes() if not app.dlstate.is_uploaded(n.key)
        ]
        if not pending:
            self.notify("selection is already in telegram")
            return
        from .pipelinescr import PipelineScreen

        app.push_screen(PipelineScreen(pending))

    def on_screen_resume(self) -> None:
        self._sync()

    def action_back(self) -> None:
        self.app.pop_screen()
