"""
processor.py

Standalone video processing module for an Anime/Web-novel Recap Dynamic Video Retiming Pipeline.
"""

import os
import sys
import shutil
import zipfile
import tempfile
import logging
import subprocess
from pathlib import Path
from typing import Optional

import pysrt

try:
    import mutagen
except ImportError:
    mutagen = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def _get_audio_duration(file_path: str) -> float:
    if mutagen is not None:
        try:
            audio_info = mutagen.File(file_path)
            if audio_info is not None and audio_info.info is not None:
                duration = getattr(audio_info.info, "length", None)
                if duration and duration > 0:
                    return float(duration)
        except Exception as exc:
            logger.debug(f"Mutagen failed: {exc}. Trying ffprobe.")

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as exc:
        raise RuntimeError(f"Could not determine audio duration for '{file_path}': {exc}") from exc


def _find_audio_file(base_dir: str, index: int) -> Optional[str]:
    extensions = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"]
    candidate_names = [
        f"{index}",
        f"{index:02d}",
        f"{index:03d}",
        f"{index:04d}",
    ]

    for root, _, files in os.walk(base_dir):
        file_map = {f.lower(): f for f in files}
        for name in candidate_names:
            for ext in extensions:
                lookup = f"{name}{ext}".lower()
                if lookup in file_map:
                    return os.path.join(root, file_map[lookup])

    return None


def process_recap_pipeline(
    video_path: str,
    srt_path: str,
    audio_zip_path: str,
    output_path: str
) -> str:
    for path, desc in [(video_path, "Video"), (srt_path, "SRT"), (audio_zip_path, "Audio ZIP")]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{desc} file not found: {path}")

    resolved_output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(resolved_output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    temp_dir = tempfile.mkdtemp(prefix="recap_pipeline_")
    logger.info(f"Initialized temporary workspace at: {temp_dir}")

    try:
        audio_extract_dir = os.path.join(temp_dir, "extracted_audio")
        os.makedirs(audio_extract_dir, exist_ok=True)
        with zipfile.ZipFile(audio_zip_path, "r") as zip_ref:
            zip_ref.extractall(audio_extract_dir)

        subs = pysrt.open(srt_path)
        if not subs:
            raise ValueError(f"SRT file '{srt_path}' contains no subtitle entries.")

        segments_dir = os.path.join(temp_dir, "segments")
        os.makedirs(segments_dir, exist_ok=True)
        segment_paths = []

        total_subs = len(subs)
        for idx, sub in enumerate(subs, start=1):
            sub_index = getattr(sub, "index", idx)
            start_time = sub.start.ordinal / 1000.0
            end_time = sub.end.ordinal / 1000.0
            d_video = end_time - start_time

            if d_video <= 0:
                continue

            matching_audio = _find_audio_file(audio_extract_dir, sub_index)
            if not matching_audio:
                raise FileNotFoundError(
                    f"No matching audio found for subtitle index {sub_index} (e.g., {sub_index}.mp3)"
                )

            d_audio = _get_audio_duration(matching_audio)
            if d_audio <= 0:
                raise ValueError(f"Invalid audio duration ({d_audio}s) for file: {matching_audio}")

            speed_factor = d_audio / d_video
            segment_path = os.path.join(segments_dir, f"segment_{idx:05d}.mp4")

            ffmpeg_segment_cmd = [
                "ffmpeg",
                "-y",
                "-ss", f"{start_time:.3f}",
                "-to", f"{end_time:.3f}",
                "-i", video_path,
                "-i", matching_audio,
                "-filter:v", f"setpts={speed_factor:.6f}*PTS",
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                segment_path,
            ]

            process = subprocess.run(
                ffmpeg_segment_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if process.returncode != 0:
                logger.error(f"FFmpeg error on segment #{sub_index}:\n{process.stderr}")
                raise subprocess.CalledProcessError(
                    process.returncode,
                    ffmpeg_segment_cmd,
                    output=process.stdout,
                    stderr=process.stderr
                )

            segment_paths.append(segment_path)

        if not segment_paths:
            raise RuntimeError("No segments were successfully rendered.")

        concat_list_file = os.path.join(temp_dir, "concat_list.txt")
        with open(concat_list_file, "w", encoding="utf-8") as f:
            for seg in segment_paths:
                escaped_seg_path = seg.replace("'", "'\\''")
                f.write(f"file '{escaped_seg_path}'\n")

        ffmpeg_concat_cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_file,
            "-c", "copy",
            resolved_output_path,
        ]

        concat_proc = subprocess.run(
            ffmpeg_concat_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if concat_proc.returncode != 0:
            logger.error(f"FFmpeg concat failure:\n{concat_proc.stderr}")
            raise subprocess.CalledProcessError(
                concat_proc.returncode,
                ffmpeg_concat_cmd,
                output=concat_proc.stdout,
                stderr=concat_proc.stderr
            )

        return resolved_output_path

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
                  
