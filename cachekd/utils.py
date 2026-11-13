"""Small helpers: paths, seeds, run logging, git state, memory/timing."""
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parent.parent


def root() -> Path:
    """$CF_ROOT holds data, caches, checkpoints and results (git-ignored)."""
    p = Path(os.environ.get("CF_ROOT", REPO / "cf_data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def path(*parts) -> Path:
    p = root().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def git_state() -> dict:
    """Commit hash and whether the tree has uncommitted changes to tracked files."""
    try:
        h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True).stdout.strip() != ""
        return {"git_hash": h or None, "git_dirty": dirty}
    except OSError:
        return {"git_hash": None, "git_dirty": None}


def append_jsonl(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl(p: Path) -> list:
    if not Path(p).exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(p: Path, rows: list) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def md5_ids(ids) -> str:
    return hashlib.md5(",".join(map(str, ids)).encode()).hexdigest()[:10]


def device() -> torch.device:
    if os.environ.get("CF_DEVICE"):
        return torch.device(os.environ["CF_DEVICE"])
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def reset_peak_mem() -> None:
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def peak_mem_gb() -> float | None:
    return round(torch.cuda.max_memory_allocated() / 2**30, 3) if torch.cuda.is_available() else None


def gpu_info() -> dict:
    if not torch.cuda.is_available():
        return {"gpu": None}
    return {"gpu": torch.cuda.get_device_name(0), "torch": torch.__version__, "cuda": torch.version.cuda}


class Timer:
    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *a):
        self.s = round(time.time() - self.t0, 2)


def log(msg: str, file: Path | None = None) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if file is not None:
        with open(file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
