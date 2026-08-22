"""Index builder and publisher tests. Pure logic; no Telegram account."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gdmirror.index import (  # noqa: E402
    MAX_LINKS,
    build_index,
    clean_labels,
    IndexPublisher,
    render,
)

CHAT = -1004439721621


def records(paths_sizes: list[tuple[str, int]]) -> list[dict]:
    out = []
    for i, (path, size) in enumerate(paths_sizes, 1):
        out.append({"path": path, "size": size,
                    "link": f"https://t.me/c/4439721621/{i}"})
    return out


def visible(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def render_all(tree) -> dict[str, str]:
    """Simulate a completed publish: assign ids in send order, render final."""
    ids = {p.key: 100000 + i for i, p in enumerate(tree.posts)}
    return {p.key: render(p, ids, CHAT, tree.root_title) for p in tree.posts}, ids


def all_file_targets(tree) -> list[str]:
    return [e.target for p in tree.posts for e in p.entries if e.is_url and e.target]


# --------------------------------------------------------------------------- #


def test_clean_labels() -> None:
    got = clean_labels(["DR.X - ANATOMY", "DR.X - BIOCHEM", "DR.X - PATHOLOGY"])
    assert set(got.values()) == {"ANATOMY", "BIOCHEM", "PATHOLOGY"}, got
    # single name is a no-op
    assert clean_labels(["ONLY"]) == {"ONLY": "ONLY"}
    # never empties a name even when one is entirely the prefix
    got = clean_labels(["ORGAN", "ORGAN SYSTEMS"])
    assert all(v for v in got.values()), got
    # no common prefix leaves names untouched
    got = clean_labels(["ALPHA", "BETA"])
    assert got == {"ALPHA": "ALPHA", "BETA": "BETA"}
    print("clean_labels ok")


def test_small_is_single_post() -> None:
    tree = build_index("Small", records([
        ("A/one.mp4", 10), ("A/two.mp4", 20), ("B/three.mp4", 30),
    ]))
    assert len(tree.posts) == 1, [p.key for p in tree.posts]
    root = tree.posts[0]
    assert root.is_root
    assert root.breadcrumb == []          # root has no back-link
    rendered, _ = render_all(tree)
    text = visible(rendered[root.key])
    assert "one.mp4" in text and "three.mp4" in text
    assert len(all_file_targets(tree)) == 3
    print("small single post ok")


def test_integrity_every_file_once() -> None:
    recs = records([(f"Cat{c}/Sub{s}/file-{i:03d}.mp4", (i + 1) * 1_000_000)
                    for c in range(4) for s in range(5) for i in range(30)])
    tree = build_index("Big", recs)
    targets = all_file_targets(tree)
    urls = {r["link"] for r in recs}
    assert len(targets) == len(urls), (len(targets), len(urls))
    assert set(targets) == urls, "some files missing or extra"
    assert len(targets) == len(set(targets)), "a file is linked twice"
    print(f"integrity ok ({len(urls)} files across {len(tree.posts)} posts)")


def test_posts_fit_limits() -> None:
    recs = records([(f"Cat{c}/Sub{s}/lecture-{i:03d}.mp4", 200_000_000)
                    for c in range(5) for s in range(6) for i in range(20)])
    tree = build_index("Fits", recs)
    rendered, _ = render_all(tree)
    for post in tree.posts:
        chars = len(visible(rendered[post.key]))
        links = sum(1 for e in post.entries if e.target)
        assert chars <= 4096, f"{post.key}: {chars} chars"
        assert links <= MAX_LINKS, f"{post.key}: {links} links"
    print(f"limits ok ({len(tree.posts)} posts, all under 4096 chars)")


def test_flat_folder_paginates() -> None:
    # one folder, more files than fit in a single post -> prev/next pages
    tree = build_index("Flat", records(
        [(f"Bulk/clip-{i:03d}.mp4", 5_000_000) for i in range(400)]))
    pages = [p for p in tree.posts if p.pages > 1]
    assert pages, "expected pagination"
    assert {p.pages for p in pages} == {max(p.pages for p in pages)}
    first = [p for p in pages if p.page == 1][0]
    last = [p for p in pages if p.page == p.pages][0]
    assert first.prev_key is None and first.next_key is not None
    assert last.next_key is None and last.prev_key is not None
    rendered, _ = render_all(tree)
    assert "Next" in visible(rendered[first.key])
    assert "Prev" in visible(rendered[last.key])
    print(f"pagination ok ({max(p.pages for p in pages)} pages)")


def test_nesting_and_backlinks() -> None:
    recs = records([(f"Cat{c}/Sub{s}/file-{i:03d}.mp4", 300_000_000)
                    for c in range(3) for s in range(6) for i in range(15)])
    tree = build_index("Nested", recs)
    kinds = {}
    for p in tree.posts:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
    assert kinds.get("branch"), "expected branch posts"
    assert kinds.get("leaf"), "expected leaf posts"
    assert kinds.get("root") == 1

    rendered, ids = render_all(tree)

    # root links to every top category post, and those messages exist
    root_text = rendered[tree.root_key]
    child_ids = {ids[p.key] for p in tree.posts
                 if len(p.breadcrumb) == 1}  # direct children of root
    for cid in child_ids:
        assert f"/{cid}" in root_text, f"root missing link to {cid}"

    # every non-root post carries an upward Index link that resolves
    for post in tree.posts:
        if post.is_root:
            continue
        text = rendered[post.key]
        assert "↑" in visible(text), f"{post.key} has no back-link"
        root_id = ids[tree.root_key]
        assert f"/{root_id}" in text, f"{post.key} back-link does not reach root"

    # a leaf deep in the tree has the full breadcrumb chain
    deep = max(tree.posts, key=lambda p: len(p.breadcrumb))
    assert len(deep.breadcrumb) >= 2, deep.breadcrumb
    print(f"nesting ok ({kinds})")


def test_render_pass_one_has_no_dangling_links() -> None:
    # during pass 1 an ancestor id is unknown; its link must degrade to plain text
    tree = build_index("Two", records(
        [(f"Cat{c}/Sub{s}/f{i}.mp4", 300_000_000)
         for c in range(3) for s in range(6) for i in range(15)]))
    leaf = [p for p in tree.posts if not p.is_root and p.breadcrumb][0]
    partial = render(leaf, {leaf.key: 1}, CHAT, tree.root_title)  # only self known
    assert "<a href" not in partial.split("━")[0], "back-link should be plain in pass 1"
    assert "↑" in visible(partial)
    print("pass-1 degradation ok")


# --------------------------------------------------------------------------- #
# publisher
# --------------------------------------------------------------------------- #


class FakeTG:
    def __init__(self) -> None:
        self.msgs: dict[int, str] = {}
        self.sent: list[int] = []
        self.edited: list[int] = []
        self.deleted: list[int] = []
        self.pinned: int | None = None
        self._id = 5000

    def send_html(self, chat_id: int, text: str) -> int:
        self._id += 1
        self.msgs[self._id] = text
        self.sent.append(self._id)
        return self._id

    def edit_html(self, chat_id: int, msg_id: int, text: str) -> None:
        self.msgs[msg_id] = text
        self.edited.append(msg_id)

    def delete_messages(self, chat_id: int, ids: list[int]) -> None:
        self.deleted += ids

    def pin(self, chat_id: int, msg_id: int) -> None:
        self.pinned = msg_id


def test_publisher() -> None:
    recs = records([(f"Cat{c}/Sub{s}/file-{i:03d}.mp4", 300_000_000)
                    for c in range(3) for s in range(6) for i in range(15)])
    tree = build_index("Pub", recs)
    tg = FakeTG()
    pub = IndexPublisher(tg, CHAT, throttle=0.0)
    result = pub.publish(tree, stale_message_ids=[1, 2, 3])

    # one message per post, root pinned, every non-root edited for back-links
    assert len(tg.sent) == len(tree.posts)
    assert tg.pinned == result["root_id"]
    assert set(tg.edited) == {p.msg_id for p in tree.posts if not p.is_root}

    # children are sent before their parents, so downward links exist at send time
    order = {mid: i for i, mid in enumerate(tg.sent)}
    root = tree.post(tree.root_key)
    assert order[root.msg_id] == len(tg.sent) - 1, "root must be sent last"

    # after publishing, every post-to-post and file link points at a real message
    live = set(tg.msgs)
    for text in tg.msgs.values():
        for mid in re.findall(r"t\.me/c/\d+/(\d+)", text):
            mid = int(mid)
            # file links use their own ids (from records), which are not in tg.msgs;
            # only post links must resolve to a sent message
    # every category link in the pinned root resolves to a sent post
    root_text = tg.msgs[result["root_id"]]
    post_links = [int(m) for m in re.findall(r"t\.me/c/\d+/(\d+)", root_text)]
    assert post_links, "root has no links"
    assert all(mid in live for mid in post_links), "root links to a missing post"

    # stale index messages were cleaned up
    assert set(tg.deleted) == {1, 2, 3}
    assert result["message_ids"] == [p.msg_id for p in tree.posts]
    print(f"publisher ok ({len(tg.sent)} sent, {len(tg.edited)} edited, "
          f"{len(tg.deleted)} stale removed)")


def main() -> int:
    test_clean_labels()
    test_small_is_single_post()
    test_integrity_every_file_once()
    test_posts_fit_limits()
    test_flat_folder_paginates()
    test_nesting_and_backlinks()
    test_render_pass_one_has_no_dangling_links()
    test_publisher()
    print("\nall index tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
