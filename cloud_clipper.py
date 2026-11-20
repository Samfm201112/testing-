import os
import sys
import time
import glob
import shutil
import subprocess
import threading
from collections import deque
from chat_downloader import ChatDownloader
from faster_whisper import WhisperModel

STREAM_URL = sys.argv[1].strip() if len(sys.argv) > 1 else "https://twitch.tv"
WATERMARK_TEXT = "SL_gta6"

OUTPUT_DIR = os.path.abspath("GTA6_Clips")
BUFFER_DIR = "live_buffer"
SEGMENT_SECONDS = 15
KEEP_SEGMENTS = 8
CLIP_SEGMENTS = 3
CHAT_WINDOW_SEC = 10
SPIKE_THRESHOLD = 80
COOLDOWN_SEC = 60
MAX_CLIPS_PER_SHIFT = 40
MAX_SHIFT_SECONDS = 170 * 60  # Exit cleanly before GitHub's 185m timeout
CAM_COORDS = (20, 20, 420, 280)

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BUFFER_DIR, exist_ok=True)
clip_lock = threading.Lock()
clips_saved = 0
SHIFT_START_TIME = time.time()


def shift_time_remaining():
    return MAX_SHIFT_SECONDS - (time.time() - SHIFT_START_TIME)


def find_live_stream(url):
    while shift_time_remaining() > 120:
        try:
            cmd = ["yt-dlp", "--no-warnings"]
            if os.path.exists("/tmp/cookies.txt") and os.path.getsize("/tmp/cookies.txt") > 0:
                cmd.extend(["--cookies", "/tmp/cookies.txt"])
            cmd.extend([
                "-f", "best[height<=1080]",
                "--print", "webpage_url",
                "--print", "urls",
                url
            ])
            out = subprocess.check_output(cmd, stderr=subprocess.PIPE, text=True).strip().splitlines()
            if len(out) >= 2 and out[0].startswith("http"):
                resolved_page_url = out[0].strip()
                hls_url = out[1].strip()
                print(f"[✓] LIVE LOCKED: {resolved_page_url}", flush=True)
                return resolved_page_url, hls_url
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip().splitlines()[-1] if e.stderr else "Offline"
            print(f"[*] Waiting on {url} ({err_msg})...", flush=True)
        time.sleep(30)
    return None, None


def record_rolling_buffer(hls_url, stop_event):
    sub_buf = os.path.join(BUFFER_DIR, "active")
    shutil.rmtree(sub_buf, ignore_errors=True)
    os.makedirs(sub_buf, exist_ok=True)

    segment_pattern = os.path.join(sub_buf, "seg_%05d.ts")
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", hls_url,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-reset_timestamps", "1",
        segment_pattern
    ]
    proc = subprocess.Popen(cmd)
    print("[✓] Rolling buffer recording...", flush=True)

    try:
        while proc.poll() is None and not stop_event.is_set() and shift_time_remaining() > 60:
            files = sorted(glob.glob(os.path.join(sub_buf, "seg_*.ts")))
            if len(files) > KEEP_SEGMENTS:
                for old_file in files[:-KEEP_SEGMENTS]:
                    try:
                        os.remove(old_file)
                    except OSError:
                        pass
            time.sleep(5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("[!] Video buffer stopped cleanly.", flush=True)
        stop_event.set()


def format_srt_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{hrs:02}:{mins:02}:{secs:02},{ms:03}"


def generate_viral_captions(whisper_model, video_path, srt_path):
    segments, _ = whisper_model.transcribe(video_path, word_timestamps=True)
    words = []
    for seg in segments:
        if seg.words:
            words.extend(seg.words)

    with open(srt_path, "w", encoding="utf-8") as f:
        idx = 1
        for i in range(0, len(words), 3):
            chunk = words[i:i+3]
            if not chunk:
                continue
            start = format_srt_time(chunk[0].start)
            end = format_srt_time(chunk[-1].end)
            text = " ".join(w.word.strip().upper() for w in chunk)
            f.write(f"{idx}\n{start} --> {end}\n{text}\n\n")
            idx += 1


def create_viral_clip(whisper_model, trigger_reason):
    global clips_saved
    if clips_saved >= MAX_CLIPS_PER_SHIFT:
        return
    if not clip_lock.acquire(blocking=False):
        return

    try:
        sub_buf = os.path.join(BUFFER_DIR, "active")
        files = sorted(glob.glob(os.path.join(sub_buf, "seg_*.ts")))
        completed_files = files[:-1]
        if len(completed_files) < CLIP_SEGMENTS:
            return

        target_segments = completed_files[-CLIP_SEGMENTS:]
        concat_list = os.path.join(sub_buf, "concat.txt")
        raw_merged = os.path.join(sub_buf, "raw_merged.mp4")
        srt_file = os.path.join(sub_buf, "captions.srt")

        with open(concat_list, "w") as f:
            for seg in target_segments:
                f.write(f"file '{os.path.abspath(seg)}'\n")

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        final_output = os.path.join(OUTPUT_DIR, f"GTA6_{timestamp}_{trigger_reason}.mp4")

        print(f"[+] HYPE SPIKE! Rendering clip {clips_saved + 1}...", flush=True)
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", raw_merged
        ], check=True)

        generate_viral_captions(whisper_model, raw_merged, srt_file)

        safe_srt_path = srt_file.replace("\\", "/").replace(":", "\\:")
        caption_style = (
            "Alignment=10,Fontname=DejaVu Sans,Bold=1,Fontsize=20,"
            "PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=1"
        )
        watermark_filter = (
            f"drawtext=text='{WATERMARK_TEXT}':fontcolor=white@0.50:fontsize=32:"
            f"borderw=2:bordercolor=black@0.50:x=(w-text_w)/2:y=645"
        )

        cam_x, cam_y, cam_w, cam_h = CAM_COORDS
        filter_complex = (
            f"[0:v]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},scale=1080:672:force_original_aspect_ratio=increase,crop=1080:672[cam];"
            f"[0:v]crop=ih*(1080/1248):ih,scale=1080:1248[game];"
            f"[cam][game]vstack=inputs=2[stacked];"
            f"[stacked]{watermark_filter}[watermarked];"
            f"[watermarked]subtitles='{safe_srt_path}':force_style='{caption_style}'[out]"
        )

        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-i", raw_merged,
            "-filter_complex", filter_complex,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k",
            final_output
        ], check=True)

        clips_saved += 1
        print(f"[✓] SAVED ({clips_saved}/{MAX_CLIPS_PER_SHIFT}) -> {final_output}", flush=True)
    except Exception as e:
        print(f"[!] Clip render failed: {e}", flush=True)
    finally:
        clip_lock.release()


def monitor_chat(url, whisper_model, stop_event):
    timestamps = deque()
    last_clip_time = 0
    try:
        print(f"[*] Connecting to live chat for {url}...", flush=True)
        chat = ChatDownloader().get_chat(url)
        print("[✓] Live chat connected!", flush=True)
        for _ in chat:
            if stop_event.is_set() or shift_time_remaining() <= 60:
                break
            now = time.time()
            timestamps.append(now)

            while timestamps and now - timestamps[0] > CHAT_WINDOW_SEC:
                timestamps.popleft()

            if len(timestamps) >= SPIKE_THRESHOLD and (now - last_clip_time) > COOLDOWN_SEC:
                last_clip_time = now
                threading.Thread(
                    target=create_viral_clip,
                    args=(whisper_model, "CHAT_HYPE")
                ).start()
    except Exception as e:
        print(f"[!] Chat error: {e}", flush=True)
    finally:
        stop_event.set()


if __name__ == "__main__":
    print(f"[*] Initializing Whisper AI for {STREAM_URL}...", flush=True)
    whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")

    while shift_time_remaining() > 120 and clips_saved < MAX_CLIPS_PER_SHIFT:
        active_url, hls_url = find_live_stream(STREAM_URL)
        if not active_url:
            break
        stop_event = threading.Event()
        buf_thread = threading.Thread(
            target=record_rolling_buffer,
            args=(hls_url, stop_event),
            daemon=True
        )
        buf_thread.start()
        monitor_chat(active_url, whisper_model, stop_event)
        buf_thread.join(timeout=10)
        time.sleep(10)

    # Wait for any in-progress render to finish before exiting
    with clip_lock:
        print(f"[✓] Shift complete. Total clips saved: {clips_saved}", flush=True)
