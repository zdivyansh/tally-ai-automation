"""Watch the invoice folder and review each new PDF as it arrives."""

import logging
import time
from collections.abc import Callable
from pathlib import Path

from tally_ai.purchase.review import Ask, Result, ReviewContext, Say, review_file

logger = logging.getLogger(__name__)


def pdfs_in(folder: Path) -> list[Path]:
    """PDF files directly in the folder (not subfolders), oldest first."""
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name.lower()))


class FolderWatcher:
    """Polls the folder; a file is read only once its size stops changing (download finished)."""

    def __init__(
        self,
        folder: Path,
        make_context: Callable[[], ReviewContext],
        ask: Ask,
        say: Say,
        *,
        interval: float = 5.0,
    ) -> None:
        self.folder = folder
        self.make_context = make_context
        self.ask = ask
        self.say = say
        self.interval = interval
        self._sizes: dict[Path, int] = {}
        self._done_this_session: set[tuple[Path, float]] = set()

    def ready_files(self, ctx: ReviewContext) -> list[Path]:
        ready = []
        for path in pdfs_in(self.folder):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            key = (path, stat.st_mtime)
            if key in self._done_this_session or ctx.state.is_final(path):
                continue
            previous = self._sizes.get(path)
            self._sizes[path] = stat.st_size
            if stat.st_size > 0 and previous == stat.st_size:
                ready.append(path)
        return ready

    def poll_once(self) -> list[Result]:
        ctx = self.make_context()
        results = []
        for path in self.ready_files(ctx):
            result = review_file(path, ctx, self.ask, self.say)
            self._done_this_session.add((path, path.stat().st_mtime))
            self.say(result.message)
            results.append(result)
        return results

    def run(self) -> None:
        self.say(f"Watching {self.folder} for PepsiCo invoice PDFs (Ctrl+C to stop).")
        try:
            while True:
                self.poll_once()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            self.say("Stopped watching.")
