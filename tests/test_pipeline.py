"""Pipeline tests: budget, ordering, purge-only-after-confirm, resume.

No Telegram account and no network: the Drive side is a local Range server and
the Telegram side is a fake that records what it was handed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakedrive  # noqa: E402

import gdmirror.download as dlmod  # noqa: E402
import gdmirror.pipeline as pipemod  # noqa: E402
import gdmirror.state as state_mod  # noqa: E402
from gdmirror.util import human  # noqa: E402


class FakeTG:
    """Stands in for gdmirror.tg.TG. Records uploads, can be told to misbehave."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.sent: list[tuple[str, int]] = []
        self.captions: list[str] = []
        self.fail_paths: set[str] = set()
        self.lie_about_size: set[str] = set()
        self.pinned: list[int] = []
        self.messages: dict[int, str] = {}
        self._next_id = 100
        self._lock = threading.Lock()

    def upload(self, path, chat_id, caption, progress=None, cancel=None,
               on_flood=None, retries=4) -> dict:
        from gdmirror.tg import TelegramError

        path = Path(path)
        size = path.stat().st_size
        if path.name in self.fail_paths:
            raise TelegramError("simulated telegram rejection")
        if self.delay:
            steps = 4
            for i in range(steps):
                if cancel is not None and cancel.is_set():
                    raise TelegramError("cancelled")
                time.sleep(self.delay / steps)
                if progress:
                    progress(int(size * (i + 1) / steps), size)
        elif progress:
            progress(size, size)

        reported = size + 1 if path.name in self.lie_about_size else size
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self.sent.append((path.name, size))
            self.captions.append(caption)
        return {"msg_id": msg_id, "size": reported,
                "link": f"https://t.me/c/1234/{msg_id}"}

    def pin(self, chat_id, msg_id) -> None:
        self.pinned.append(msg_id)

    # text-message API used by the index publisher
    def send_html(self, chat_id, text) -> int:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            self.messages[mid] = text
            return mid

    def edit_html(self, chat_id, msg_id, text) -> None:
        self.messages[msg_id] = text

    def delete_messages(self, chat_id, ids) -> None:
        for mid in ids:
            self.messages.pop(mid, None)

    def close(self) -> None:
        pass


def build_pipeline(tmp: Path, files, tg, budget, workers=3, purge=True):
    state = state_mod.State()
    pipe = pipemod.Pipeline(
        creds=None,
        tg=tg,
        chat_id=-1001234,
        files=files,
        state=state,
        dest=tmp / "buffer",
        budget_bytes=budget,
        download_workers=workers,
        purge=purge,
        throttle=0.0,
        verify_md5=True,
        on_log=lambda m: None,
    )
    fakedrive.patched_session(pipe.downloader)
    return pipe, state


def disk_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    server, base = fakedrive.start_server()
    dlmod.API = base
    tmp = Path(tempfile.mkdtemp(prefix="gdmirror-pipe-test-"))
    state_mod.STATE_FILE = tmp / "state.json"
    pipemod.MANIFEST_FILE = tmp / "manifest.json"

    try:
        # tree order matters: the channel should read like the folder
        files = [
            fakedrive.make_file("a", "Unit 1/lecture-01.mp4", 400_000),
            fakedrive.make_file("b", "Unit 1/lecture-02.mp4", 380_000),
            fakedrive.make_file("c", "Unit 2/lecture-01.mp4", 360_000),
            fakedrive.make_file("d", "Unit 2/lecture-02.mp4", 340_000),
            fakedrive.make_file("e", "Unit 3/lecture-01.mp4", 320_000),
        ]
        buffer_dir = tmp / "buffer"

        # 1. happy path: everything uploaded, in order, and purged locally
        tg = FakeTG(delay=0.08)
        pipe, state = build_pipeline(tmp, files, tg, budget=900_000, workers=3)

        peak = [0]
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                peak[0] = max(peak[0], disk_bytes(buffer_dir))
                time.sleep(0.01)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        prog = pipe.run()
        stop.set()
        watcher.join()

        assert prog.uploaded_files == 5, prog
        assert prog.failed == 0, prog.failures
        assert [name for name, _ in tg.sent] == [
            "lecture-01.mp4", "lecture-02.mp4",
            "lecture-01.mp4", "lecture-02.mp4", "lecture-01.mp4",
        ]
        # captions carry the folder so a flat channel is still navigable
        assert "Unit 1" in tg.captions[0] and "Unit 3" in tg.captions[4]
        print("upload order ok")

        assert disk_bytes(buffer_dir) == 0, "buffer must be empty when done"
        assert prog.purged_bytes == sum(f.size for f in files)
        print(f"purge ok (freed {human(prog.purged_bytes)})")

        # 2. the disk budget was respected the whole way through
        assert peak[0] <= 900_000 + 400_000, f"peak {peak[0]} blew the budget"
        assert peak[0] > 0, "watcher never saw a file on disk"
        print(f"budget ok (peak on disk {human(peak[0])}, budget {human(900_000)})")

        # 3. every upload is recorded and resumable
        assert state.uploaded_count == 5
        rec = state.upload_record(files[0].key)
        assert rec and rec["msg_id"] and rec["link"].startswith("https://t.me/c/")
        assert not state.is_done(files[0].key, files[0].size, files[0].md5)
        assert pipemod.MANIFEST_FILE.exists()
        print("state + manifest ok")

        # 4. a rerun does nothing at all
        tg2 = FakeTG()
        pipe2, _ = build_pipeline(tmp, files, tg2, budget=900_000)
        prog2 = pipe2.run()
        assert prog2.total_files == 0 and tg2.sent == [], prog2
        assert pipe2.skipped_existing == 5
        print("resume ok")

        # 5. a rejected upload must not delete the local file
        state_mod.STATE_FILE = tmp / "state2.json"
        extra = [fakedrive.make_file("f", "Unit 4/lecture-01.mp4", 200_000)]
        tg3 = FakeTG()
        tg3.fail_paths.add("lecture-01.mp4")
        pipe3, state3 = build_pipeline(tmp, extra, tg3, budget=900_000)
        prog3 = pipe3.run()
        assert prog3.failed == 1 and prog3.uploaded_files == 0, prog3
        assert not state3.is_uploaded(extra[0].key)
        kept = buffer_dir / extra[0].path
        assert kept.exists() and kept.stat().st_size == extra[0].size
        print("failed upload keeps the local file ok")

        # 6. a size mismatch reported by Telegram is a failure, not a delete
        state_mod.STATE_FILE = tmp / "state3.json"
        other = [fakedrive.make_file("g", "Unit 5/odd.mp4", 150_000)]
        tg4 = FakeTG()
        tg4.lie_about_size.add("odd.mp4")
        pipe4, state4 = build_pipeline(tmp, other, tg4, budget=900_000)
        prog4 = pipe4.run()
        assert prog4.failed == 1, prog4
        assert not state4.is_uploaded(other[0].key)
        assert (buffer_dir / other[0].path).exists()
        assert "size mismatch" in prog4.failures[0][1]
        print("size-mismatch guard ok")

        # 7. the navigable index covers every uploaded file and is pinned
        state_mod.STATE_FILE = tmp / "state.json"
        tg5 = FakeTG()
        pipe5, state5 = build_pipeline(tmp, files, tg5, budget=900_000)
        tree = pipe5.build_index_tree()
        file_links = [e.target for p in tree.posts for e in p.entries
                      if e.is_url and e.target]
        assert len(file_links) == len(files) == len(set(file_links)), file_links
        stages: list[tuple[str, int, int]] = []
        result = pipe5.publish_index(on_progress=lambda *a: stages.append(a))
        assert result["root_link"].startswith("https://t.me/c/") and tg5.pinned
        assert result["posts"] == len(tree.posts)
        # progress fired through the stages and ended on "done"
        # (this dataset fits one post, so there is no "link" stage)
        assert {s[0] for s in stages} >= {"send", "pin"}
        assert stages[0][0] == "send" and stages[-1][0] == "done"
        # the pinned post is the root, and its id was recorded for later cleanup
        assert tg5.pinned[-1] in state5.index_messages()
        assert not (buffer_dir / "INDEX.md").exists(), "no index file should be written"
        # re-publishing replaces the previous posts rather than duplicating them
        before = set(state5.index_messages())
        pipe5.publish_index()
        after = set(state5.index_messages())
        assert before.isdisjoint(after), "re-publish reused old message ids"
        assert all(m not in tg5.messages for m in before), "old index not deleted"
        print("index ok")

        # 8. regression: a big file at the head of the queue must not be starved
        #    of disk by smaller files behind it. Reservations are granted in
        #    queue order, so the index the uploader waits on always holds disk.
        #    Without that, files 1..n take the whole budget, file 0 can never
        #    reserve, and the uploader waits on file 0 forever.
        state_mod.STATE_FILE = tmp / "state4.json"
        starve = [fakedrive.make_file("h0", "Big/first.mp4", 900_000)] + [
            fakedrive.make_file(f"h{i}", f"Big/small-{i:02d}.mp4", 500_000)
            for i in range(1, 6)
        ]
        tg6 = FakeTG(delay=0.05)
        pipe6, state6 = build_pipeline(
            tmp, starve, tg6, budget=1_000_000, workers=3
        )

        # Hold the head worker back so the losing interleaving is guaranteed
        # rather than left to the scheduler; unordered reservation deadlocks
        # here every time.
        real_reserve = pipe6._reserve

        def slow_head(index: int, size: int) -> bool:
            if index == 0:
                time.sleep(0.4)
            return real_reserve(index, size)

        pipe6._reserve = slow_head
        done = threading.Event()

        def run_it() -> None:
            pipe6.run()
            done.set()

        runner = threading.Thread(target=run_it, daemon=True)
        runner.start()
        if not done.wait(60):
            pipe6.cancel()
            prog6, _, _ = pipe6.snapshot()
            raise AssertionError(
                f"pipeline deadlocked: {prog6.uploaded_files}/{prog6.total_files} "
                f"uploaded, {human(prog6.reserved_bytes)} reserved"
            )
        prog6, _, _ = pipe6.snapshot()
        assert prog6.uploaded_files == 6, prog6
        assert prog6.failed == 0, prog6.failures
        assert [n for n, _ in tg6.sent] == [
            "first.mp4", "small-01.mp4", "small-02.mp4",
            "small-03.mp4", "small-04.mp4", "small-05.mp4",
        ], tg6.sent
        assert state6.uploaded_count == 6
        print("head-of-queue starvation ok")

        # 9. a failed upload leaves its file on disk on purpose, so those bytes
        #    must keep holding budget. Releasing them would let the buffer creep
        #    past the budget one failure at a time until the disk filled up.
        state_mod.STATE_FILE = tmp / "state5.json"
        leak = [
            fakedrive.make_file("k0", "Leak/keeps.mp4", 600_000),
            fakedrive.make_file("k1", "Leak/fine-01.mp4", 300_000),
            fakedrive.make_file("k2", "Leak/fine-02.mp4", 300_000),
        ]
        tg7 = FakeTG()
        tg7.fail_paths.add("keeps.mp4")
        pipe7, state7 = build_pipeline(tmp, leak, tg7, budget=1_500_000, workers=2)
        prog7 = pipe7.run()

        assert prog7.failed == 1 and prog7.uploaded_files == 2, prog7
        stuck = buffer_dir / leak[0].path
        assert stuck.exists(), "failed upload must keep its local file"
        # the stranded file's bytes are still accounted for, not handed back
        assert prog7.stranded_bytes == leak[0].size, prog7.stranded_bytes
        assert prog7.reserved_bytes == leak[0].size, prog7.reserved_bytes
        # and the two healthy files still went through in order
        assert [n for n, _ in tg7.sent] == ["fine-01.mp4", "fine-02.mp4"]
        assert state7.uploaded_count == 2
        print(f"stranded-byte accounting ok ({human(prog7.stranded_bytes)} held)")

        # 10. the configured buffer is clamped to what the disk can really give
        state_mod.STATE_FILE = tmp / "state6.json"
        tiny = [fakedrive.make_file("m0", "Clamp/one.mp4", 100_000)]
        logs: list[str] = []
        pipe8 = pipemod.Pipeline(
            creds=None, tg=FakeTG(), chat_id=-1, files=tiny,
            state=state_mod.State(), dest=tmp / "clamped",
            budget_bytes=500 * 1024**3,  # 500 GB, far more than any test disk
            download_workers=1, purge=True, throttle=0.0,
            on_log=logs.append,
        )
        fakedrive.patched_session(pipe8.downloader)
        pipe8.run()
        assert pipe8.budget < 500 * 1024**3, "budget was not clamped"
        assert any("buffer trimmed" in line for line in logs), logs
        print(f"budget clamp ok (trimmed to {human(pipe8.budget)})")

        # 11. crash recovery: a buffer full of finished downloads whose records
        #     were never saved must be adopted, not fetched again.
        state_mod.STATE_FILE = tmp / "state7.json"
        orphans = [
            fakedrive.make_file("o1", "Orphan/one.mp4", 200_000),
            fakedrive.make_file("o2", "Orphan/two.mp4", 180_000),
        ]
        for node in orphans:  # simulate the files a killed run left behind
            path = buffer_dir / node.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fakedrive.BLOBS[node.id])
        fakedrive.HITS.clear()

        tg9 = FakeTG()
        pipe9, state9 = build_pipeline(tmp, orphans, tg9, budget=1_000_000)
        assert not state9.is_done(orphans[0].key, orphans[0].size, orphans[0].md5)
        prog9 = pipe9.run()

        assert prog9.uploaded_files == 2, prog9
        assert fakedrive.HITS == {}, f"re-downloaded despite local copies: {fakedrive.HITS}"
        assert [n for n, _ in tg9.sent] == ["one.mp4", "two.mp4"]
        print("adopt-orphaned-downloads ok (no refetch)")

        # 12. a corrupt orphan must NOT be adopted - it gets refetched
        state_mod.STATE_FILE = tmp / "state8.json"
        bad_orphan = fakedrive.make_file("o3", "Orphan/bad.mp4", 150_000)
        path = buffer_dir / bad_orphan.path
        path.write_bytes(b"\x00" * bad_orphan.size)  # right size, wrong bytes
        fakedrive.HITS.clear()
        tg10 = FakeTG()
        pipe10, _ = build_pipeline(tmp, [bad_orphan], tg10, budget=1_000_000)
        prog10 = pipe10.run()
        assert prog10.uploaded_files == 1, prog10
        assert fakedrive.HITS.get("o3") == 1, "corrupt orphan should be refetched"
        print("corrupt orphan refetched ok")

        # 13. the upload record is on disk before the next file starts, so a kill
        #     cannot leave a file in Telegram that nothing points at
        state_mod.STATE_FILE = tmp / "state9.json"
        pair = [
            fakedrive.make_file("s1", "Save/one.mp4", 120_000),
            fakedrive.make_file("s2", "Save/two.mp4", 120_000),
        ]
        seen_after_first: list[int] = []

        class WatchingTG(FakeTG):
            def upload(self, path, *a, **kw):
                result = super().upload(path, *a, **kw)
                # count what is already persisted when the *next* upload begins
                if len(self.sent) == 2:
                    saved = json.loads((tmp / "state9.json").read_text())
                    seen_after_first.append(len(saved.get("uploaded", {})))
                return result

        pipe11, _ = build_pipeline(tmp, pair, WatchingTG(), budget=1_000_000)
        pipe11.run()
        assert seen_after_first == [1], (
            f"first upload was not persisted before the second: {seen_after_first}"
        )
        print("per-upload persistence ok")

        print("\nall pipeline tests passed")
        return 0
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
