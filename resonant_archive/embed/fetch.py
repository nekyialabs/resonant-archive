"""Download and verify the nomic-embed-text-v1.5 model files.

The model (about half a gigabyte) is not bundled. ``fetch_model`` downloads four files
from the ``nomic-ai/nomic-embed-text-v1.5`` Hugging Face repo at a pinned revision into
the XDG data dir, and checks every file against a SHA-256 sum and size pinned in this
source file. It fails closed: a file that does not match is deleted and an error is
raised; nothing unverified is ever left under its final name.

Offline path: place the files in a directory yourself and point
``RESONANT_EMBEDDER_MODEL_DIR`` (or ``--model-dir``) at it; ``verify_model_dir`` runs the
same pinned check. This download is the only network use of the embedder.

Model licence: Apache-2.0 (nomic-ai/nomic-embed-text-v1.5 model card).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from .. import fsutil
from .paths import model_dir as _default_model_dir

REPO = "nomic-ai/nomic-embed-text-v1.5"
# Upstream commit the sums below were taken from (HEAD on 2026-10-16; the files are
# identical to the ones first fetched on 2026-10-04).
PINNED_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
DEFAULT_BASE_URL = "https://huggingface.co"


@dataclass(frozen=True)
class FileSpec:
    remote: str  # path inside the HF repo
    sha256: str
    size: int


# local file name -> spec. Sums for model.onnx match the upstream LFS object id; the
# three small files were re-downloaded at PINNED_REVISION and hashed.
PINNED: Mapping[str, FileSpec] = {
    "model.onnx": FileSpec(
        "onnx/model.onnx",
        "147d5aa88c2101237358e17796cf3a227cead1ec304ec34b465bb08e9d952965",
        547310275,
    ),
    "tokenizer.json": FileSpec(
        "tokenizer.json",
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        711396,
    ),
    "config.json": FileSpec(
        "config.json",
        "9ab00bd92cee80a569f708140b7b6c1661a65891ff3765b1519e181ba2f2c92b",
        2538,
    ),
    "tokenizer_config.json": FileSpec(
        "tokenizer_config.json",
        "d7e0000bcc80134debd2222220427e6bf5fa20a669f40a0d0d1409cc18e0a9bc",
        1191,
    ),
}

_CHUNK = 1 << 20


class ModelIntegrityError(RuntimeError):
    """A model file is missing, the wrong size, or does not match its pinned SHA-256."""


def sha256_file(path: os.PathLike | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: Path, spec: FileSpec) -> None:
    if not path.is_file():
        raise ModelIntegrityError(f"{path.name}: missing")
    size = path.stat().st_size
    if size != spec.size:
        raise ModelIntegrityError(f"{path.name}: size {size} != pinned {spec.size}")
    got = sha256_file(path)
    if got != spec.sha256:
        raise ModelIntegrityError(f"{path.name}: sha256 {got} != pinned {spec.sha256}")


def verify_model_dir(
    model_dir: os.PathLike | str | None = None,
    manifest: Mapping[str, FileSpec] = PINNED,
    *,
    names: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Check files against the pinned sums. Returns ``{name: sha256}`` or raises."""
    d = Path(model_dir) if model_dir is not None else _default_model_dir()
    out: dict[str, str] = {}
    for name in names or tuple(manifest):
        verify_file(d / name, manifest[name])
        out[name] = manifest[name].sha256
    return out


VERIFIED_MARKER = ".verified.json"


def _stamp(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def verify_cached(
    model_dir: os.PathLike | str | None = None,
    manifest: Mapping[str, FileSpec] = PINNED,
) -> dict[str, str]:
    """``verify_model_dir`` for all files, but a directory that verified before is not hashed again while every
    file keeps the same modification time and size (recorded in ``.verified.json`` beside the model).

    The marker saves re-reading 550 MB on every start; it is a speed-up for catching a half-written or swapped file,
    not a defence against someone who can already write to the model folder (they could rewrite the marker too).
    A folder that cannot be written to is verified in full each time. Raises ``ModelIntegrityError``.
    """
    d = Path(model_dir) if model_dir is not None else _default_model_dir()
    marker = d / VERIFIED_MARKER
    try:
        have = json.loads(marker.read_text(encoding="utf-8"))
        if have.get("revision") == PINNED_REVISION and all(
            have.get("files", {}).get(n, {}).get("sha256") == spec.sha256
            and {k: have["files"][n].get(k) for k in ("mtime_ns", "size")} == _stamp(d / n)
            for n, spec in manifest.items()
        ):
            return {n: spec.sha256 for n, spec in manifest.items()}
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    sums = verify_model_dir(d, manifest)
    try:
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(json.dumps({"revision": PINNED_REVISION, "files": {
            n: {"sha256": spec.sha256, **_stamp(d / n)} for n, spec in manifest.items()}}), encoding="utf-8")
        fsutil.replace(tmp, marker)
    except OSError:
        pass
    return sums


def _check_base_url(base_url: str) -> None:
    u = urlparse(base_url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname:
        try:
            if ipaddress.ip_address(u.hostname).is_loopback or u.hostname == "localhost":
                return  # local mirror / tests only
        except ValueError:
            if u.hostname == "localhost":
                return
    raise ValueError(f"refusing base_url {base_url!r}: https required (http only for loopback)")


def _download(url: str, dest: Path, spec: FileSpec, progress: Callable[[str, int, int], None] | None) -> None:
    """Stream ``url`` to ``dest``, hashing as it goes; raise (leaving nothing) on any mismatch."""
    req = urllib.request.Request(url, headers={"User-Agent": "resonant-archive-model-fetch"})
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > spec.size:  # never accept more than the pinned size
                    raise ModelIntegrityError(f"{dest.name.removesuffix('.part')}: larger than pinned size {spec.size}")
                h.update(chunk)
                out.write(chunk)
                if progress:
                    progress(dest.name.removesuffix(".part"), n, spec.size)
        name = dest.name.removesuffix(".part")
        if n != spec.size:
            raise ModelIntegrityError(f"{name}: downloaded {n} bytes, pinned {spec.size}")
        if h.hexdigest() != spec.sha256:
            raise ModelIntegrityError(f"{name}: sha256 {h.hexdigest()} != pinned {spec.sha256}")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def fetch_model(
    model_dir: os.PathLike | str | None = None,
    *,
    manifest: Mapping[str, FileSpec] = PINNED,
    revision: str = PINNED_REVISION,
    base_url: str = DEFAULT_BASE_URL,
    repo: str = REPO,
    force: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """Download any missing or corrupt model file, verify all of them, return the directory.

    Idempotent: files that already verify are not downloaded again (unless ``force``).
    Raises ``ModelIntegrityError`` on any mismatch; the offending file is removed.
    """
    _check_base_url(base_url)
    d = Path(model_dir) if model_dir is not None else _default_model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, spec in manifest.items():
        final = d / name
        if not force:
            try:
                verify_file(final, spec)
                continue
            except ModelIntegrityError:
                pass
        part = d / f"{name}.part"
        part.unlink(missing_ok=True)
        url = f"{base_url.rstrip('/')}/{repo}/resolve/{revision}/{spec.remote}"
        _download(url, part, spec, progress)
        fsutil.replace(part, final)  # only a verified file takes its final name
    verify_model_dir(d, manifest)  # belt and braces: re-read what is on disk
    write_sha256sums(d, manifest)
    return d


def write_sha256sums(model_dir: Path, manifest: Mapping[str, FileSpec] = PINNED) -> None:
    """Write a ``sha256sum -c`` compatible SHA256SUMS beside the model."""
    lines = [f"{s.sha256}  {name}\n" for name, s in manifest.items()]
    (model_dir / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
