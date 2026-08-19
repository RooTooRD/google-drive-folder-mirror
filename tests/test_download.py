"""Downloader tests against a local HTTP server that speaks Range requests."""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakedrive  # noqa: E402
from fakedrive import BLOBS, SLOW, make_file, start_server  # noqa: E402

import gdmirror.download as dlmod  # noqa: E402
import gdmirror.state as state_mod  # noqa: E402
from gdmirror.drive import Node  # noqa: E402


def patched_downloader(dest: Path, files: list[Node], state) -> dlmod.Downloader:
    dl = dlmod.Downloader(creds=None, dest=dest, files=files, state=state, workers=3)
    fakedrive.patched_session(dl)
    return dl


def main() -> int:
    server, base = start_server()
    dlmod.API = base
    tmp = Path(tempfile.mkdtemp(prefix="gdmirror-dl-test-"))
    state_mod.STATE_FILE = tmp / "state.json"

    try:
        dest = tmp / "downloads"
        files = [
            make_file("a", "Unit 1/lesson1.mp4", 300_000),
            make_file("b", "Unit 1/lesson2.mp4", 150_000),
            make_file("c", "Unit 2/sub/deep.mp4", 90_000),
        ]

        # 1. fresh run
        state = state_mod.State()
        prog = patched_downloader(dest, files, state).run()
        assert prog.ok == 3 and prog.failed == 0, prog
        assert prog.done_bytes == sum(f.size for f in files), prog.done_bytes
        for f in files:
            assert (dest / f.path).read_bytes() == BLOBS[f.id], f.path
        assert not list(dest.rglob("*.part")), "leftover .part files"
        print("fresh download ok")

        # 2. rerun uses the state file and skips everything
        prog = patched_downloader(dest, files, state_mod.State()).run()
        assert prog.skipped == 3 and prog.ok == 0, prog
        print("resume-by-state ok")

        # 3. partial .part on disk is continued, not restarted
        target = dest / files[0].path
        part = target.with_name(target.name + ".part")
        target.unlink()
        part.write_bytes(BLOBS["a"][:120_000])
        fresh_state = state_mod.State()
        fresh_state.forget(files[0].key)
        prog = patched_downloader(dest, [files[0]], fresh_state).run()
        assert prog.ok == 1, prog
        assert target.read_bytes() == BLOBS["a"]
        assert prog.done_bytes == files[0].size, prog.done_bytes
        print("byte-range resume ok")

        # 4. a wrong md5 fails the file instead of keeping bad data
        bad = make_file("d", "Unit 3/corrupt.mp4", 50_000)
        bad.md5 = "0" * 32
        prog = patched_downloader(dest, [bad], state_mod.State()).run()
        assert prog.failed == 1 and prog.ok == 0, prog
        assert not (dest / bad.path).exists()
        assert "md5 mismatch" in prog.failures[0][1]
        print("md5 verification ok")

        # 5. cancelling mid-flight leaves a .part behind, then a rerun finishes it
        big = make_file("e", "Unit 4/big.mp4", 5_000_000)
        SLOW.add("e")
        dl = patched_downloader(dest, [big], state_mod.State())
        threading.Timer(0.4, dl.cancel).start()
        dl.run()
        target = dest / big.path
        part = target.with_name(target.name + ".part")
        assert not target.exists(), "cancelled file must not be published"
        assert part.exists() and 0 < part.stat().st_size < big.size
        SLOW.discard("e")
        prog = patched_downloader(dest, [big], state_mod.State()).run()
        assert prog.ok == 1, prog
        assert target.read_bytes() == BLOBS["e"]
        print("cancel + resume ok")

        print("\nall download tests passed")
        return 0
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
