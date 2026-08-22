"""Build a navigable index of an uploaded channel as a tree of Telegram posts.

The channel is flat: every file is its own message. This turns the folder
structure into a set of cross-linked text posts so the channel can be browsed
in Telegram itself, rather than by downloading a Markdown file.

The shape adapts to size:

* If the whole listing fits in one message, there is a single pinned post.
* Otherwise the root becomes a menu of top-level categories; any category still
  too large becomes a menu of its subfolders, and so on. Leaves list the files.
* A single folder with more files than fit in one message is split into pages
  linked prev/next.

Telegram limits drive the design: a message is at most 4096 characters, and a
user account (unlike a bot) cannot attach tap buttons, so navigation is plain
in-channel links (``https://t.me/c/<id>/<msg>``). Links are built bottom-up:
children are sent first so their message ids exist when a parent links to them,
then a second pass edits every post to add the upward "back" links.

This module is pure: it turns upload records into `Post` objects and renders
them to HTML. Sending is done by `IndexPublisher`, which only needs a thin
Telegram client, so the whole builder is testable without an account.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Iterable

from .util import human

# Conservative ceilings under Telegram's hard limits (4096 chars, ~100 entities).
MAX_TEXT = 3500
MAX_LINKS = 80
DIVIDER = "━━━━━━━━━━━━━━━━"

# Emoji chosen by keyword in the folder name, purely cosmetic. First match wins.
_EMOJI_RULES: list[tuple[tuple[str, ...], str]] = [
    (("pharmacolog",), "💊"),
    (("behavior", "psych"), "🧠"),
    (("biochem",), "🧬"),
    (("genetic",), "🧬"),
    (("anatomy",), "🦴"),
    (("neuro", "nervous"), "🧠"),
    (("cardio", "heart"), "❤️"),
    (("respir", "pulmon", "lung"), "🫁"),
    (("renal", "kidney", "urinary"), "🧪"),
    (("git", "gastro", "hepato", "pancrea", "digest"), "🍽️"),
    (("endocrin",), "🧫"),
    (("reprod", "genital"), "🚼"),
    (("musculoskeletal", "msk", "bone"), "💪"),
    (("derma", "skin"), "🧴"),
    (("hemato", "blood"), "🩸"),
    (("immuno",), "🛡️"),
    (("micro", "bacteri", "viro", "antimicrob"), "🦠"),
    (("patho",), "🔬"),
    (("physio",), "⚡"),
    (("principle",), "📘"),
    (("organ system",), "🫀"),
    (("recording",), "🎥"),
    (("orientation", "package"), "🧭"),
    (("q bank", "qbank", "q book", "question"), "❓"),
    (("note",), "📝"),
    (("lecture", "explanation"), "🎬"),
]


def emoji_for(name: str) -> str:
    low = name.lower()
    for needles, glyph in _EMOJI_RULES:
        if any(n in low for n in needles):
            return glyph
    return "📁"


_SEP = " -–—_/:·"


def clean_labels(names: list[str]) -> dict[str, str]:
    """Strip a prefix common to every sibling, so a shared boilerplate token
    (e.g. an author name repeated on every folder) is not shown on each line.

    Fully data-driven: it only removes a prefix that all names share, trimmed
    back to a separator, and never empties a name. `{name: display}`.
    """
    if len(names) < 2:
        return {n: n.strip() for n in names}

    prefix = names[0]
    for name in names[1:]:
        i = 0
        while i < len(prefix) and i < len(name) and prefix[i].lower() == name[i].lower():
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break

    # trim to the last separator so we cut whole tokens, not mid-word
    cut = max((prefix.rfind(s) for s in _SEP), default=-1)
    prefix = prefix[: cut + 1] if cut >= 0 else ""

    out = {}
    for name in names:
        stripped = name[len(prefix):].lstrip(_SEP + " ") if prefix else name
        out[name] = (stripped or name).strip()
    return out


# --------------------------------------------------------------------------- #
# folder tree
# --------------------------------------------------------------------------- #


@dataclass
class _Node:
    name: str
    folders: dict[str, "_Node"] = field(default_factory=dict)
    files: list[tuple[str, int, str]] = field(default_factory=list)  # name, size, url

    def child(self, name: str) -> "_Node":
        return self.folders.setdefault(name, _Node(name))

    def all_files(self) -> Iterable[tuple[str, int, str]]:
        yield from self.files
        for folder in self.folders.values():
            yield from folder.all_files()

    def count(self) -> int:
        return sum(1 for _ in self.all_files())

    def nbytes(self) -> int:
        return sum(size for _, size, _ in self.all_files())


def _tree_from_records(records: Iterable[dict]) -> _Node:
    root = _Node("")
    for rec in records:
        parts = [p for p in rec["path"].split("/") if p]
        if not parts:
            continue
        *dirs, fname = parts
        node = root
        for d in dirs:
            node = node.child(d)
        node.files.append((fname, int(rec.get("size", 0)), rec["link"]))
    _sort(root)
    return root


def _sort(node: _Node) -> None:
    node.files.sort(key=lambda f: f[0].lower())
    node.folders = dict(sorted(node.folders.items(), key=lambda kv: kv[0].lower()))
    for folder in node.folders.values():
        _sort(folder)


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #


@dataclass
class Entry:
    """One line of a post. `target` is a message-post key (resolved to a link at
    render time) or a literal URL; `section` marks a subheading instead."""
    text: str
    target: str | None = None      # post key or url
    is_url: bool = False
    trailing: str = ""
    section: str | None = None

    def visible_len(self) -> int:
        if self.section is not None:
            return len(self.section) + 1
        return len(self.text) + len(self.trailing) + 2


@dataclass
class Post:
    key: str
    title: str
    emoji: str
    kind: str                       # "root" | "branch" | "leaf"
    breadcrumb: list[tuple[str, str]]  # (title, ancestor post key), root first
    entries: list[Entry]
    nfiles: int
    nbytes: int
    page: int = 1
    pages: int = 1
    prev_key: str | None = None
    next_key: str | None = None
    msg_id: int | None = None

    @property
    def is_root(self) -> bool:
        return self.kind == "root"


class IndexTree:
    def __init__(self, root_title: str, posts: list[Post], root_key: str) -> None:
        self.root_title = root_title
        self.posts = posts            # send order: children before parents, root last
        self.root_key = root_key

    def post(self, key: str) -> Post:
        return next(p for p in self.posts if p.key == key)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #


def build_index(
    root_title: str,
    records: list[dict],
    *,
    max_text: int = MAX_TEXT,
    max_links: int = MAX_LINKS,
) -> IndexTree:
    tree = _tree_from_records(records)
    posts: list[Post] = []
    counter = {"n": 0}

    def new_key(hint: str) -> str:
        counter["n"] += 1
        return f"p{counter['n']}:{hint[:24]}"

    def fits(entries: list[Entry]) -> bool:
        text = sum(e.visible_len() for e in entries)
        links = sum(1 for e in entries if e.target is not None)
        return text <= max_text and links <= max_links

    def leaf_entries(node: _Node, base: str) -> list[Entry]:
        """Every file under `node`, grouped by path relative to `base`."""
        groups: dict[str, list[tuple[str, int, str]]] = {}

        def walk(n: _Node, rel: str) -> None:
            if n.files:
                groups.setdefault(rel, []).extend(n.files)
            for name, folder in n.folders.items():
                walk(folder, f"{rel}/{name}" if rel else name)

        walk(node, "")
        entries: list[Entry] = []
        for rel in sorted(groups, key=str.lower):
            if rel:
                entries.append(Entry(text="", section=rel))
            for i, (name, size, url) in enumerate(groups[rel], 1):
                entries.append(
                    Entry(text=f"{i}. {name}", target=url, is_url=True,
                          trailing=f" · {human(size)}")
                )
        return entries

    def paginate(base_key: str, title: str, emoji: str, kind: str,
                 breadcrumb: list[tuple[str, str]], entries: list[Entry],
                 nfiles: int, nbytes: int) -> str:
        """Split `entries` into page posts, link them prev/next, return page-1 key."""
        pages_entries: list[list[Entry]] = [[]]
        text = links = 0
        for entry in entries:
            el, elk = entry.visible_len(), (1 if entry.target else 0)
            if pages_entries[-1] and (text + el > max_text or links + elk > max_links):
                pages_entries.append([])
                text = links = 0
            # a section header should not be the last line on a page
            pages_entries[-1].append(entry)
            text += el
            links += elk

        total = len(pages_entries)
        page_keys = [base_key if i == 0 else new_key(f"{title}-{i+1}")
                     for i in range(total)]
        for i, (pkey, pents) in enumerate(zip(page_keys, pages_entries)):
            posts.append(Post(
                key=pkey, title=title, emoji=emoji, kind=kind,
                breadcrumb=breadcrumb, entries=pents, nfiles=nfiles, nbytes=nbytes,
                page=i + 1, pages=total,
                prev_key=page_keys[i - 1] if i else None,
                next_key=page_keys[i + 1] if i + 1 < total else None,
            ))
        return page_keys[0]

    def build_node(node: _Node, title: str, kind: str,
                   breadcrumb: list[tuple[str, str]], key_hint: str) -> str:
        """Return the post key for `node`. Recurses, appending child posts first."""
        key = new_key(key_hint)
        emoji = emoji_for(title) if title else "📚"
        nfiles, nbytes = node.count(), node.nbytes()

        flat = leaf_entries(node, "")
        if fits(flat):
            return paginate(key, title, emoji, "root" if kind == "root" else "leaf",
                            breadcrumb, flat, nfiles, nbytes)

        # too big for one post → a menu of this node's subfolders
        child_bc = breadcrumb + [(title or root_title, key)]
        entries: list[Entry] = []

        if node.files:  # loose files living directly in this folder
            entries.append(Entry(text="", section="Files"))
            for i, (name, size, url) in enumerate(node.files, 1):
                entries.append(Entry(text=f"{i}. {name}", target=url, is_url=True,
                                     trailing=f" · {human(size)}"))

        labels = clean_labels(list(node.folders))
        for name, folder in node.folders.items():
            label = labels[name]
            child_key = build_node(folder, label, "branch", child_bc, label)
            entries.append(Entry(
                text=f"{emoji_for(name)} {label}", target=child_key,
                trailing=f" · {folder.count()} files · {human(folder.nbytes())}"))

        return paginate(key, title or root_title, emoji,
                        "root" if kind == "root" else "branch",
                        breadcrumb, entries, nfiles, nbytes)

    root_key = build_node(tree, "", "root", [], root_title)
    return IndexTree(root_title, posts, root_key)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def render(post: Post, ids: dict[str, int], chat_id: int, root_title: str) -> str:
    """Full HTML for a post, given whatever post→message-id mappings exist.

    Unknown targets (e.g. an ancestor not sent yet during the first pass) render
    as plain text, so a first pass can send bodies and a second pass can edit in
    the links once every id is known.
    """
    def url_for_key(key: str) -> str | None:
        mid = ids.get(key)
        return _channel_link(chat_id, mid) if mid else None

    lines: list[str] = []

    title = post.title or root_title
    head = f"{post.emoji} <b>{_esc(title)}</b>"
    if post.pages > 1:
        head += f"  ·  <i>page {post.page}/{post.pages}</i>"
    lines.append(head)
    lines.append(f"{post.nfiles} files · {human(post.nbytes)}")

    if post.breadcrumb:
        crumbs = []
        for i, (btitle, bkey) in enumerate(post.breadcrumb):
            label = "Index" if i == 0 else btitle
            url = url_for_key(bkey)
            crumbs.append(f'<a href="{url}">↑ {_esc(label)}</a>' if url
                          else f"↑ {_esc(label)}")
        lines.append("  ·  ".join(crumbs))

    lines.append(DIVIDER)

    for entry in post.entries:
        if entry.section is not None:
            lines.append(f"\n<b>▸ {_esc(entry.section)}</b>")
            continue
        target = entry.target
        url = target if (target and entry.is_url) else url_for_key(target) if target else None
        text = _esc(entry.text)
        body = f'<a href="{url}">{text}</a>' if url else f"<b>{text}</b>" if target else text
        lines.append(f"{body}{_esc(entry.trailing)}")

    if post.pages > 1:
        nav = []
        if post.prev_key:
            u = url_for_key(post.prev_key)
            nav.append(f'<a href="{u}">◀ Prev</a>' if u else "◀ Prev")
        nav.append(f"{post.page}/{post.pages}")
        if post.next_key:
            u = url_for_key(post.next_key)
            nav.append(f'<a href="{u}">Next ▶</a>' if u else "Next ▶")
        lines.append(DIVIDER)
        lines.append("  ·  ".join(nav))

    return "\n".join(lines)


def _channel_link(chat_id: int, msg_id: int) -> str:
    internal = str(chat_id)
    if internal.startswith("-100"):
        internal = internal[4:]
    return f"https://t.me/c/{internal}/{msg_id}"


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #


class IndexPublisher:
    """Send an `IndexTree` to a channel and pin its root.

    Needs only three operations from the Telegram client, so it is trivially
    testable with a fake: ``send_html``, ``edit_html``, ``delete_messages``,
    ``pin``. Publishing is two passes because posts link both down (a menu to its
    children) and up (a child back to its parent):

      1. Send every post in child-before-parent order. Downward links resolve
         immediately; upward links render as plain text for now.
      2. Now that every post has an id, edit each one whose rendered text gained
         a link, so the back-links become live.
    """

    def __init__(self, tg, chat_id: int, on_log=None, throttle: float = 0.4) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self.on_log = on_log
        self.throttle = throttle

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def publish(
        self,
        tree: IndexTree,
        stale_message_ids: list[int] | None = None,
        on_progress=None,
    ) -> dict:
        """Send, link, pin and clean up.

        `on_progress(stage, done, total)` fires per step so a UI can show live
        progress. Stages: "send", "link", "pin", "clean", "done".
        """
        import time

        def progress(stage: str, done: int, total: int) -> None:
            if on_progress:
                on_progress(stage, done, total)

        ids: dict[str, int] = {}
        total = len(tree.posts)
        editable = sum(1 for p in tree.posts if not p.is_root)

        # pass 1: send bodies, children first
        for i, post in enumerate(tree.posts, 1):
            text = render(post, ids, self.chat_id, tree.root_title)
            post.msg_id = self.tg.send_html(self.chat_id, text)
            ids[post.key] = post.msg_id
            progress("send", i, total)
            if self.throttle:
                time.sleep(self.throttle)
        self._log(f"posted {total} index messages")

        # pass 2: re-render now that every id is known, so upward links go live.
        # Only the root has no back-link, so only it can be skipped.
        done = 0
        for post in tree.posts:
            if post.is_root:
                continue
            self.tg.edit_html(
                self.chat_id, post.msg_id,
                render(post, ids, self.chat_id, tree.root_title),
            )
            done += 1
            progress("link", done, editable)
            if self.throttle:
                time.sleep(self.throttle)
        self._log(f"linked {editable} index messages")

        # pin the root (the parent of the whole index). Clear any previous pin
        # first, so exactly one parent stays pinned across re-publishes.
        progress("pin", 0, 1)
        root_id = ids[tree.root_key]
        if hasattr(self.tg, "unpin_all"):
            self.tg.unpin_all(self.chat_id)
        try:
            self.tg.pin(self.chat_id, root_id)
            self._log("pinned the index root post")
        except Exception as exc:  # posts already exist; surface, don't discard them
            self._log(f"could not pin the index root: {exc}")
        progress("pin", 1, 1)

        # remove the previous index now that the new one is live
        if stale_message_ids:
            still_here = [m for m in stale_message_ids if m not in ids.values()]
            if still_here:
                progress("clean", 0, len(still_here))
                self.tg.delete_messages(self.chat_id, still_here)
                self._log(f"removed {len(still_here)} old index messages")

        progress("done", total, total)
        return {
            "root_id": root_id,
            "root_link": _channel_link(self.chat_id, root_id),
            "message_ids": [p.msg_id for p in tree.posts],
            "posts": total,
        }
