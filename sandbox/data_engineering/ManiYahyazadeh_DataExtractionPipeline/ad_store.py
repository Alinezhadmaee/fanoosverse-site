"""File storage for scraped ads: raw copy, final output, dropped ads and duplicates."""
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("fanoos.store")


@dataclass
class Ad:
    document_id: str
    collector: str
    collected_at: str
    content_hash: str
    text: str


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file. A broken last line (crash while writing) is removed."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if any(rest.strip() for rest in lines[number:]):
                raise ValueError(f"Corrupt line {number} in {path}")
            log.warning("Removed an incomplete last line from %s", path.name)
            good = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            path.write_text(good, encoding="utf-8")
            break
    return rows


def append_line(path: Path, line: str) -> None:
    """Append one line and flush, so a crash loses nothing."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


class AdStore:
    """
    Keeps every ad on disk. The raw file is the queue, so a run can always resume.
    An ad is final when it is in processed.jsonl or in dropped.jsonl.
    """

    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.folder / "raw_ads.jsonl"
        self.processed_path = self.folder / "processed.jsonl"
        self.dropped_path = self.folder / "dropped.jsonl"
        self.duplicates_path = self.folder / "duplicates.txt"

        self.ads = [Ad(**row) for row in read_jsonl(self.raw_path)]
        self.hashes = {ad.content_hash: ad.document_id for ad in self.ads}
        self.done = {r["document_id"] for r in read_jsonl(self.processed_path)}
        self.done |= {r["document_id"] for r in read_jsonl(self.dropped_path)}

    def next_number(self, collector: str) -> int:
        """Next free number for this collector, for ids like job_bot_0001."""
        pattern = re.compile(rf"^job_{re.escape(collector)}_(\d+)$")
        used = [int(m.group(1)) for ad in self.ads if (m := pattern.match(ad.document_id))]
        return max(used, default=0) + 1

    def add_ads(self, texts: list, collector: str, collected_at: str | None = None) -> dict:
        """Number the ads, save the raw text at once and skip duplicates."""
        collected_at = collected_at or date.today().isoformat()
        date.fromisoformat(collected_at)  # fail early on a bad date
        counts = {"added": 0, "duplicates": 0, "empty": 0}
        number = self.next_number(collector)
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                counts["empty"] += 1
                continue
            digest = text_hash(text)
            if digest in self.hashes:
                counts["duplicates"] += 1
                entry = f"duplicate of {self.hashes[digest]}\n{text}\nend of duplicate\n"
                append_line(self.duplicates_path, entry)
                continue
            ad = Ad(f"job_{collector}_{number:04d}", collector, collected_at, digest, text)
            append_line(self.raw_path, json.dumps(asdict(ad), ensure_ascii=False))
            self.ads.append(ad)
            self.hashes[digest] = ad.document_id
            number += 1
            counts["added"] += 1
        return counts

    def pending(self) -> list[Ad]:
        """Ads without a final state, in their original order."""
        return [ad for ad in self.ads if ad.document_id not in self.done]

    def save_processed(self, document_id: str, record: dict) -> None:
        append_line(self.processed_path, json.dumps(record, ensure_ascii=False))
        self.done.add(document_id)

    def save_dropped(self, ad: Ad, reason: str, attempts: int, problems: list[str]) -> None:
        """A dropped ad keeps its raw text and the reason."""
        row = {**asdict(ad), "reason": reason, "attempts": attempts, "last_problems": problems}
        append_line(self.dropped_path, json.dumps(row, ensure_ascii=False))
        self.done.add(ad.document_id)
