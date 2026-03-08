#!/usr/bin/env python3
import argparse
import hashlib
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
import tomllib


def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"invalid toml in {path}: {exc}")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def verify_sha256(path: Path, expected: str) -> None:
    if not expected:
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise SystemExit(f"sha256 mismatch: expected {expected}, got {actual}")


def extract(tar_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:*") as tar:
        tar.extractall(dest_dir)
    entries = [p for p in dest_dir.iterdir() if p.is_dir()]
    for entry in entries:
        if (entry / "configure").exists():
            return entry
    raise SystemExit("configure script not found after extraction")


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def read_rev(root: Path, ffmpeg_cfg: dict) -> str:
    rev = ffmpeg_cfg.get("rev", "")
    rev_file = ffmpeg_cfg.get("rev_file", "")
    if rev_file:
        rev_path = root / rev_file
        if rev_path.exists():
            rev = rev_path.read_text().strip()
    return rev


def build_url(root: Path, ffmpeg_cfg: dict) -> str:
    url = ffmpeg_cfg.get("url", "")
    if url:
        return url
    rev = read_rev(root, ffmpeg_cfg)
    template = ffmpeg_cfg.get("archive_url_template", "")
    if template and rev:
        return template.format(rev=rev)
    raise SystemExit("ffmpeg.toml must include url or archive_url_template with rev")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build FFmpeg static libs for a target"
    )
    parser.add_argument("target", help="target name defined in config/targets.toml")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    ffmpeg_cfg = load_toml(root / "config" / "ffmpeg.toml")
    targets_cfg = load_toml(root / "config" / "targets.toml")

    target_name = args.target
    target_cfg = None
    for item in targets_cfg.get("targets", []):
        if item.get("name") == target_name:
            target_cfg = item
            break
    if not target_cfg:
        available = [t.get("name") for t in targets_cfg.get("targets", [])]
        raise SystemExit(f"target not found: {target_name}. available: {available}")

    url = build_url(root, ffmpeg_cfg)
    rev = read_rev(root, ffmpeg_cfg)
    if rev:
        print(f"Using FFmpeg rev {rev}")

    tar_name = Path(url).name
    downloads_dir = root / "downloads"
    tar_path = downloads_dir / tar_name
    download(url, tar_path)
    verify_sha256(tar_path, ffmpeg_cfg.get("sha256", ""))

    build_root = root / "build" / target_name
    src_dir = build_root / "src"
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)
    source_dir = extract(tar_path, src_dir)

    install_dir = build_root / "install"
    install_dir.mkdir(parents=True, exist_ok=True)

    common_flags = ffmpeg_cfg.get("configure_common", [])
    target_flags = target_cfg.get("configure", [])
    extra_flags = target_cfg.get("extra_configure", [])
    configure_flags = (
        common_flags + target_flags + extra_flags + [f"--prefix={install_dir}"]
    )

    env = os.environ.copy()
    for key, value in (target_cfg.get("env") or {}).items():
        env[key] = value
    extra_cflags = target_cfg.get("extra_cflags", "")
    extra_ldflags = target_cfg.get("extra_ldflags", "")
    if extra_cflags:
        env["CFLAGS"] = f"{env.get('CFLAGS', '')} {extra_cflags}".strip()
    if extra_ldflags:
        env["LDFLAGS"] = f"{env.get('LDFLAGS', '')} {extra_ldflags}".strip()

    run(["./configure", *configure_flags], cwd=source_dir, env=env)

    jobs = int(ffmpeg_cfg.get("make_jobs", 0) or 0)
    if jobs <= 0:
        jobs = os.cpu_count() or 4
    run(["make", f"-j{jobs}"], cwd=source_dir, env=env)
    run(["make", "install"], cwd=source_dir, env=env)

    package_libs = target_cfg.get("package_libs") or ffmpeg_cfg.get("package_libs", [])
    if not package_libs:
        raise SystemExit("package_libs is empty")

    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    tar_output = dist_dir / f"{target_name}.tar.gz"
    if tar_output.exists():
        tar_output.unlink()

    layout = ffmpeg_cfg.get("package_layout", "flat")
    with tarfile.open(tar_output, "w:gz") as tar:
        for lib_name in package_libs:
            lib_path = install_dir / "lib" / lib_name
            if not lib_path.exists():
                raise SystemExit(f"missing library: {lib_path}")
            if layout == "flat":
                arcname = lib_name
            elif layout == "target-dir":
                arcname = f"{target_name}/{lib_name}"
            else:
                raise SystemExit(f"unknown package_layout: {layout}")
            tar.add(lib_path, arcname=arcname)

    print(f"Packaged {tar_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
