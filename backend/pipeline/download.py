#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# Cached impersonate-target probe (see _supports_impersonate). _UNSET is a
# sentinel so we can tell "not probed yet" apart from "probed, none found".
_UNSET = object()
_IMPERSONATE_TARGET = _UNSET


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [c for c in candidates if ".en" in c.name]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def _supports_impersonate() -> str | None:
    """Return a working impersonate target, or None if unavailable.

    Datacenter IPs (any VPS) get HTTP 403 from YouTube for both the webpage
    and the media URL unless yt-dlp impersonates a real browser. This requires
    curl_cffi AND a yt-dlp new enough to use it. Probe once per process and
    cache, so we degrade gracefully instead of hard-failing on a host that
    lacks the dependency.
    """
    global _IMPERSONATE_TARGET
    if _IMPERSONATE_TARGET is _UNSET:
        _IMPERSONATE_TARGET = None
        try:
            probe = subprocess.run(
                ["yt-dlp", "--list-impersonate-targets"],
                capture_output=True, text=True, timeout=30,
            )
            # Lines look like: "Chrome-146      Macos-26     curl_cffi"
            for line in probe.stdout.splitlines():
                if "curl_cffi" in line and "unavailable" not in line:
                    _IMPERSONATE_TARGET = line.split()[0].lower()
                    break
        except Exception as exc:
            print(f"[watch] impersonate probe failed: {exc}", file=sys.stderr)
    return _IMPERSONATE_TARGET


def download_url(url: str, out_dir: Path) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    cmd = [
        "yt-dlp",
        "-N", "2",
        "--ignore-config", "--socket-timeout", "15", "--retries", "1",
        "--fragment-retries", "1", "--extractor-retries", "1", "--max-filesize", "200M",
        "--match-filters", "duration <=? 600 & !is_live",
        "--hls-prefer-native",
        "--downloader-args", "ffmpeg_i:-protocol_whitelist http,https,tcp,tls,crypto -http_proxy " + __import__("os").environ["ADSCAN_DOWNLOAD_PROXY"],
        "--postprocessor-args", "ffmpeg_i:-protocol_whitelist file,crypto,data -threads 1",
        "--proxy", __import__("os").environ["ADSCAN_DOWNLOAD_PROXY"],
        # Prefer a merged mp4. The old "18/..." first choice fails outright on
        # YouTube clients that no longer expose format 18.
        "-f", "bv*[height<=480]+ba/b[height<=480]/b",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en,en-US,en-GB,en-orig",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",

    ]

    # Impersonation is what keeps YouTube from 403-ing a datacenter IP.
    target = _supports_impersonate()
    if target:
        cmd += ["--impersonate", target]
    else:
        print(
            "[watch] no impersonate target available — YouTube may return 403 "
            "(need curl_cffi + a recent yt-dlp)",
            file=sys.stderr,
        )

    cmd += ["-o", output_template, "--", url]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=100)
    video = _pick_video(out_dir)

    # Retry once on a different player client — YouTube rotates which clients
    # are blocked, so a fallback pass often succeeds where the default failed.
    if video is None:
        print("[watch] first pass produced no video; retrying via web_safari "
              "player client", file=sys.stderr)
        retry = cmd[:]
        idx = retry.index("--") if "--" in retry else len(retry)
        retry[idx:idx] = ["--extractor-args", "youtube:player_client=web_safari"]
        subprocess.run(retry, stdout=sys.stderr, stderr=sys.stderr, timeout=60)
        video = _pick_video(out_dir)

    if video is None:
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )

    subtitle = _pick_subtitle(out_dir)
    info_path = out_dir / "video.info.json"
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(source: str, out_dir: Path) -> dict:
    if is_url(source):
        return download_url(source, out_dir)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
