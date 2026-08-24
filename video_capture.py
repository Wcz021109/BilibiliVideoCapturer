"""
B站视频下载器（下载/合并分离 + 本地编码识别 + 可中止合并）
流程: 解析并下载(自动) → 本地ffprobe识别编码 → 选择输出格式 → 开始合并(可中止)
下载仅执行一次，合并可反复尝试，避免重复请求B站服务器。

依赖:
  pip install requests
  系统需安装 ffmpeg + ffprobe
"""

import os
import re
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import requests

# ============================================================
# 常量
# ============================================================

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/151.0.0.0 Safari/537.36")

QUALITY_DESC = {
    127: "8K 超高清", 126: "杜比视界", 125: "HDR 真彩",
    120: "4K 超清", 116: "1080P60 高帧率", 112: "1080P+ 高码率",
    80: "1080P 高清", 74: "720P60 高帧率", 64: "720P 高清",
    48: "720P", 32: "480P 清晰", 16: "360P 流畅", 6: "240P 极速",
}

TEMP_DIR = "temp"
OUT_DIR = "out"

VIDEO_CODECS = [
    ("copy - 保持原始", "copy"),
    ("H.264 (libx264)", "libx264"),
    ("H.264 Lossless (libx264)", "libx264_lossless"),
    ("H.265 (libx265)", "libx265"),
    ("H.265 Lossless (libx265)", "libx265_lossless"),
    ("AV1 (libaom-av1)", "libaom-av1"),
    ("ProRes 422", "prores_ks"),
    ("MPEG-4", "mpeg4"),
]

AUDIO_CODECS = [
    ("copy - 保持原始", "copy"),
    ("AAC", "aac"),
    ("MP3 (libmp3lame)", "libmp3lame"),
    ("FLAC", "flac"),
    ("PCM (pcm_s16le)", "pcm_s16le"),
]

CONTAINERS = [
    ("MP4", "mp4"),
    ("MOV", "mov"),
    ("MKV", "mkv"),
    ("AVI", "avi"),
]


# ============================================================
# 工具函数
# ============================================================

def extract_bvid(text):
    m = re.search(r'BV[a-zA-Z0-9]{10}', text)
    return m.group() if m else None


def extract_codecid(url):
    m = re.search(r'-1-(\d+)\.m4s', url)
    return int(m.group(1)) if m else None


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def build_headers(ua, referer):
    return {"user-agent": ua, "referer": referer}


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


# ============================================================
# B站 API
# ============================================================

def fetch_video_meta(bvid, headers):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()
    data = res.json()
    if data["code"] != 0:
        raise RuntimeError(f"获取视频信息失败: {data['message']}")
    d = data["data"]
    return {"title": d["title"], "owner": d["owner"]["name"], "cid": d["cid"]}


def fetch_quality(bvid, cid, codecid, headers):
    url = (f"https://api.bilibili.com/x/player/playurl"
           f"?bvid={bvid}&cid={cid}&qn=0&fnval=16&fourk=1")
    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()
    data = res.json()
    if data["code"] != 0:
        return "未知"
    dash = data["data"].get("dash")
    if not dash:
        return "未知"
    for v in dash.get("video", []):
        if v.get("codecid") == codecid:
            qn = v.get("id")
            return QUALITY_DESC.get(qn, f"qn={qn}")
    return "未知"


def fetch_dash_streams(bvid, cid, headers):
    """
    自动获取 DASH 音视频流地址。
    返回 (video_url, audio_url, quality_desc, v_codecid)
    选择最高清晰度视频 + 最高音质音频。
    """
    url = (f"https://api.bilibili.com/x/player/playurl"
           f"?bvid={bvid}&cid={cid}&qn=0&fnval=16&fourk=1")
    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()
    data = res.json()
    if data["code"] != 0:
        raise RuntimeError(f"获取流地址失败: {data['message']}")
    dash = data["data"].get("dash")
    if not dash:
        raise RuntimeError("该视频不支持 DASH 流（可能为旧版 FLV/MP4 格式）")

    videos = dash.get("video", [])
    audios = dash.get("audio", [])
    if not videos:
        raise RuntimeError("未找到视频流")
    if not audios:
        raise RuntimeError("未找到音频流")

    # 选最高清晰度（按 id 降序）
    videos.sort(key=lambda x: x.get("id", 0), reverse=True)
    best_v = videos[0]
    # 选最高音质（按 id 降序，30251 Dolby > 30232 FLAC > 30280 320k > 30216 64k）
    audios.sort(key=lambda x: x.get("id", 0), reverse=True)
    best_a = audios[0]

    v_url = best_v.get("baseUrl") or best_v.get("base_url") or ""
    a_url = best_a.get("baseUrl") or best_a.get("base_url") or ""
    if not v_url or not a_url:
        raise RuntimeError("流地址字段为空")

    qn = best_v.get("id")
    quality = QUALITY_DESC.get(qn, f"qn={qn}")
    return v_url, a_url, quality, best_v.get("codecid")


# ============================================================
# 本地文件编码识别（ffprobe）
# ============================================================

def probe_file_codecs(filepath):
    """
    用 ffprobe 识别本地文件的视频和音频编码名称。
    返回 (video_codec, audio_codec)，未识别到返回空字符串。
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "json",
            filepath,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        v_codec = ""
        a_codec = ""
        for s in data.get("streams", []):
            if s.get("codec_type") == "video" and not v_codec:
                v_codec = s.get("codec_name", "")
            elif s.get("codec_type") == "audio" and not a_codec:
                a_codec = s.get("codec_name", "")
        return v_codec, a_codec
    except Exception:
        return "", ""


# ============================================================
# 下载
# ============================================================

def download_stream(url, filepath, headers, progress_cb=None):
    with requests.get(url, headers=headers, stream=True, timeout=60) as res:
        res.raise_for_status()
        total = int(res.headers.get("content-length", 0))
        downloaded = 0
        with open(filepath, "wb") as f:
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:
                        progress_cb(min(downloaded / total * 100, 100))
        if progress_cb and total > 0:
            progress_cb(100)


# ============================================================
# ffmpeg 参数构建
# ============================================================

def build_video_args(codec):
    if codec == "copy":
        return ["-c:v", "copy"]
    if codec == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if codec == "libx264_lossless":
        return ["-c:v", "libx264", "-preset", "veryslow", "-crf", "0", "-pix_fmt", "yuv444p"]
    if codec == "libx265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"]
    if codec == "libx265_lossless":
        return ["-c:v", "libx265", "-preset", "veryslow", "-crf", "0", "-pix_fmt", "yuv444p"]
    if codec == "libaom-av1":
        return ["-c:v", "libaom-av1", "-crf", "30", "-b:v", "0"]
    if codec == "prores_ks":
        return ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    if codec == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", "5"]
    return ["-c:v", "copy"]


def build_audio_args(codec):
    if codec == "copy":
        return ["-c:a", "copy"]
    if codec == "aac":
        return ["-c:a", "aac", "-b:a", "320k"]
    if codec == "libmp3lame":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if codec == "flac":
        return ["-c:a", "flac"]
    if codec == "pcm_s16le":
        return ["-c:a", "pcm_s16le"]
    return ["-c:a", "copy"]


def get_media_duration(filepath):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def run_ffmpeg_with_progress(cmd, total, progress_cb=None, process_holder=None):
    """
    执行 ffmpeg，解析 -progress 输出回调进度。
    process_holder: 字典，创建子进程后存入 {"proc": Popen}，用于外部强制终止。
    """
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
    )
    if process_holder is not None:
        process_holder["proc"] = process

    try:
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    current = us / 1_000_000
                    if total > 0 and progress_cb:
                        progress_cb(min(current / total * 100, 99.9))
                except (ValueError, IndexError):
                    pass
            elif line == "progress=end":
                if progress_cb:
                    progress_cb(100)
        process.wait()
    finally:
        if process_holder is not None:
            process_holder["proc"] = None

    if process.returncode != 0:
        stderr = process.stderr.read() if process.stderr else ""
        if process.returncode == -15 or process.returncode == -9:
            raise RuntimeError("合并已被用户中止")
        raise RuntimeError(f"ffmpeg 执行失败 (code={process.returncode}):\n{stderr}")


def merge_av(video_path, audio_path, output_path, v_codec, a_codec,
             progress_cb=None, process_holder=None):
    total = get_media_duration(video_path)
    if total <= 0:
        total = get_media_duration(audio_path)

    cmd = ["ffmpeg", "-y", "-i", video_path, "-i", audio_path]
    cmd += build_video_args(v_codec)
    cmd += build_audio_args(a_codec)
    cmd += ["-progress", "-", "-nostats", "-loglevel", "error", output_path]

    run_ffmpeg_with_progress(cmd, total, progress_cb, process_holder)


# ============================================================
# 清理
# ============================================================

def cleanup(files):
    for f in files:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


# ============================================================
# 教程窗口
# ============================================================

TUTORIAL_TEXT = """\
如何获取音视频流地址和伪装信息
═══════════════════════════════════════════

快速方式（推荐）
───────────────────────────────────────
• URL/refer 一般就是视频网页地址（如 https://www.bilibili.com/video/BVxxxxxx）
• 音视频流地址可留空，留空时自动获取该视频默认清晰度的音视频流
• 只需填写 URL/refer + User-Agent，点击「解析并下载」即可

手动方式（需要指定特定清晰度时）
───────────────────────────────────────
第 1 步：打开 B 站视频页面并播放
第 2 步：按 F12 → Network（网络）面板
第 3 步：筛选 m4s
第 4 步：识别音视频流
  • 视频流：URL 末尾 -1-100028.m4s 等
  • 音频流：URL 末尾 -1-30280.m4s（30 开头）
第 5 步：右键 → Copy → Copy URL address
第 6 步：Headers 中复制 URL/refer 和 User-Agent
  （URL/refer 一般就是当前视频网页地址）
第 7 步：粘贴到下载器 →「解析并下载」
        → 等待下载完成并自动识别编码
        → 选择输出格式 →「开始合并」

注意事项
───────────────────────────────────────
• 视频流和音频流必须同时填写，或同时留空
• 只填一个会报错
• 下载仅执行一次，合并可反复尝试（无需重新下载）
• 合并中可点击「中止合并」终止 ffmpeg 进程
• 编码与容器不兼容时 ffmpeg 会报错（如 ProRes 不能封装进 AVI）
• 临时文件在 temp/，成品在 out/
"""


class TutorialWindow:
    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title("使用教程")
        self.window.geometry("620x540")
        self.window.transient(parent)
        self.window.grab_set()
        text = tk.Text(self.window, wrap=tk.WORD, font=("Consolas", 10))
        text.insert("1.0", TUTORIAL_TEXT)
        text.config(state=tk.DISABLED)
        sb = ttk.Scrollbar(self.window, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        sb.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        ttk.Button(self.window, text="关闭", command=self.window.destroy).pack(side=tk.BOTTOM, pady=6)


# ============================================================
# 主应用
# ============================================================

class BilibiliDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("B站视频下载器")
        self.root.geometry("800x920")
        self.root.resizable(True, True)

        # 状态
        self.is_busy = False          # 解析/下载中
        self.is_merging = False       # 合并中
        self.download_ready = False   # 下载完成，可合并
        self.title = None
        self.temp_video = None
        self.temp_audio = None
        self._process_holder = {}     # {"proc": Popen} 用于中止合并

        if not check_ffmpeg():
            self.root.after(100, lambda: messagebox.showwarning(
                "提示", "未检测到 ffmpeg / ffprobe。\n下载可用，但合并和编码识别不可用。"
            ))

        self._build_ui()

    # ----------------------------------------------------------
    # UI 构建
    # ----------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 顶部栏
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, **pad)
        ttk.Label(top, text="B站视频下载器", font=("Microsoft YaHei", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="教程", command=self._open_tutorial).pack(side=tk.RIGHT)

        # 输入区
        inp = ttk.LabelFrame(self.root, text="第一步：输入信息并解析下载")
        inp.pack(fill=tk.X, **pad)

        ttk.Label(inp, text="视频流地址:").grid(row=0, column=0, sticky="w", **pad)
        self.video_url_entry = ttk.Entry(inp, width=80)
        self.video_url_entry.grid(row=0, column=1, sticky="ew", **pad)

        ttk.Label(inp, text="音频流地址:").grid(row=1, column=0, sticky="w", **pad)
        self.audio_url_entry = ttk.Entry(inp, width=80)
        self.audio_url_entry.grid(row=1, column=1, sticky="ew", **pad)

        ttk.Label(inp, text="URL/refer:").grid(row=2, column=0, sticky="w", **pad)
        self.referer_entry = ttk.Entry(inp, width=80)
        self.referer_entry.grid(row=2, column=1, sticky="ew", **pad)

        ttk.Label(inp, text="User-Agent:").grid(row=3, column=0, sticky="w", **pad)
        self.ua_entry = ttk.Entry(inp, width=80)
        self.ua_entry.insert(0, DEFAULT_UA)
        self.ua_entry.grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(inp, text="填充默认 UA", command=self._fill_default_ua).grid(row=3, column=2, **pad)

        self.parse_btn = ttk.Button(inp, text="解析并下载", command=self._parse_and_download)
        self.parse_btn.grid(row=4, column=0, columnspan=3, pady=6)
        inp.columnconfigure(1, weight=1)

        # 视频信息 + 输出格式区
        info = ttk.LabelFrame(self.root, text="第二步：确认信息并合并")
        info.pack(fill=tk.X, **pad)

        ttk.Label(info, text="标题:").grid(row=0, column=0, sticky="w", **pad)
        self.title_label = ttk.Label(info, text="（未解析）", foreground="gray")
        self.title_label.grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(info, text="Up主:").grid(row=1, column=0, sticky="w", **pad)
        self.owner_label = ttk.Label(info, text="（未解析）", foreground="gray")
        self.owner_label.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(info, text="清晰度:").grid(row=2, column=0, sticky="w", **pad)
        self.quality_label = ttk.Label(info, text="（未解析）", foreground="gray")
        self.quality_label.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(info, text="原始编码:").grid(row=3, column=0, sticky="w", **pad)
        self.codec_label = ttk.Label(info, text="（下载后自动识别）", foreground="gray")
        self.codec_label.grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(info, text="输出文件名:").grid(row=4, column=0, sticky="w", **pad)
        self.output_entry = ttk.Entry(info, width=60)
        self.output_entry.insert(0, "output")
        self.output_entry.grid(row=4, column=1, sticky="ew", **pad)

        # 输出格式选择
        fmt = ttk.Frame(info)
        fmt.grid(row=5, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(fmt, text="视频编码:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.v_codec_var = tk.StringVar(value=VIDEO_CODECS[0][0])
        self.v_codec_combo = ttk.Combobox(fmt, textvariable=self.v_codec_var, state="readonly", width=22)
        self.v_codec_combo["values"] = [x[0] for x in VIDEO_CODECS]
        self.v_codec_combo.grid(row=0, column=1, padx=4)

        ttk.Label(fmt, text="音频编码:").grid(row=0, column=2, sticky="w", padx=(12, 4))
        self.a_codec_var = tk.StringVar(value=AUDIO_CODECS[0][0])
        self.a_codec_combo = ttk.Combobox(fmt, textvariable=self.a_codec_var, state="readonly", width=22)
        self.a_codec_combo["values"] = [x[0] for x in AUDIO_CODECS]
        self.a_codec_combo.grid(row=0, column=3, padx=4)

        ttk.Label(fmt, text="容器:").grid(row=0, column=4, sticky="w", padx=(12, 4))
        self.container_var = tk.StringVar(value=CONTAINERS[0][0])
        self.container_combo = ttk.Combobox(fmt, textvariable=self.container_var, state="readonly", width=8)
        self.container_combo["values"] = [x[0] for x in CONTAINERS]
        self.container_combo.grid(row=0, column=5, padx=4)

        # 操作按钮行
        btn_frame = ttk.Frame(info)
        btn_frame.grid(row=6, column=0, columnspan=2, pady=6)

        self.merge_btn = ttk.Button(btn_frame, text="开始合并", command=self._start_merge, state=tk.DISABLED)
        self.merge_btn.pack(side=tk.LEFT, padx=4)

        self.stop_btn = ttk.Button(btn_frame, text="中止合并", command=self._force_stop_merge, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        # 解析前禁用格式选择
        for cb in (self.v_codec_combo, self.a_codec_combo, self.container_combo):
            cb.config(state=tk.DISABLED)

        info.columnconfigure(1, weight=1)

        # 进度条区
        prog = ttk.LabelFrame(self.root, text="进度")
        prog.pack(fill=tk.X, **pad)
        self._build_progress_row(prog, 0, "视频下载", "video")
        self._build_progress_row(prog, 1, "音频下载", "audio")
        self._build_progress_row(prog, 2, "合并", "merge")

        # 日志区
        logf = ttk.LabelFrame(self.root, text="运行日志")
        logf.pack(fill=tk.BOTH, expand=True, **pad)
        self.log_text = tk.Text(logf, wrap=tk.WORD, state=tk.DISABLED)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_progress_row(self, parent, row, label_text, key):
        ttk.Label(parent, text=f"{label_text}:").grid(row=row, column=0, sticky="w", padx=8, pady=3)
        var = tk.DoubleVar(value=0)
        ttk.Progressbar(parent, variable=var, maximum=100, length=400).grid(
            row=row, column=1, sticky="ew", padx=6, pady=3)
        lbl = ttk.Label(parent, text="0.0%", width=8, anchor="e")
        lbl.grid(row=row, column=2, padx=8, pady=3)
        parent.columnconfigure(1, weight=1)
        setattr(self, f"{key}_progress_var", var)
        setattr(self, f"{key}_progress_label", lbl)

    # ----------------------------------------------------------
    # 进度条控制
    # ----------------------------------------------------------
    def _set_progress(self, key, pct):
        getattr(self, f"{key}_progress_var").set(pct)
        getattr(self, f"{key}_progress_label").config(text=f"{pct:.1f}%")

    def _reset_all_progress(self):
        for k in ("video", "audio", "merge"):
            self._set_progress(k, 0)

    def _video_progress_cb(self, pct):
        self.root.after(0, lambda: self._set_progress("video", pct))

    def _audio_progress_cb(self, pct):
        self.root.after(0, lambda: self._set_progress("audio", pct))

    def _merge_progress_cb(self, pct):
        self.root.after(0, lambda: self._set_progress("merge", pct))

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------
    def _log(self, msg):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _fill_default_ua(self):
        self.ua_entry.delete(0, tk.END)
        self.ua_entry.insert(0, DEFAULT_UA)

    def _fill_stream_urls(self, video_url, audio_url):
        """自动获取流地址后回填到输入框"""
        self.video_url_entry.delete(0, tk.END)
        self.video_url_entry.insert(0, video_url)
        self.audio_url_entry.delete(0, tk.END)
        self.audio_url_entry.insert(0, audio_url)

    def _open_tutorial(self):
        TutorialWindow(self.root)

    def _get_selected_codec(self, combo_var, options):
        for display, value in options:
            if display == combo_var.get():
                return value
        return "copy"

    def _get_temp_filenames(self):
        base = sanitize_filename(self.title) if self.title else "output"
        return (
            os.path.join(TEMP_DIR, f"vd_{base}.m4s"),
            os.path.join(TEMP_DIR, f"ad_{base}.m4s"),
        )

    def _set_controls_busy(self, busy):
        """解析/下载中的控件状态"""
        state = tk.DISABLED if busy else tk.NORMAL
        for w in (self.video_url_entry, self.audio_url_entry, self.referer_entry,
                  self.ua_entry, self.parse_btn):
            w.config(state=state)

    def _set_merge_ready(self, ready):
        """下载完成后启用合并相关控件"""
        fmt_state = "readonly" if ready else tk.DISABLED
        for cb in (self.v_codec_combo, self.a_codec_combo, self.container_combo):
            cb.config(state=fmt_state)
        self.output_entry.config(state=tk.NORMAL if ready else tk.DISABLED)
        self.merge_btn.config(state=tk.NORMAL if (ready and not self.is_merging) else tk.DISABLED)

    # ----------------------------------------------------------
    # 第一步：解析并下载
    # ----------------------------------------------------------
    def _parse_and_download(self):
        if self.is_busy or self.is_merging:
            return

        video_url = self.video_url_entry.get().strip()
        audio_url = self.audio_url_entry.get().strip()
        referer = self.referer_entry.get().strip()
        ua = self.ua_entry.get().strip()

        has_video = bool(video_url)
        has_audio = bool(audio_url)

        # 只提供一个时报错
        if has_video != has_audio:
            messagebox.showerror(
                "错误",
                "视频流地址和音频流地址必须同时提供，或同时留空。\n"
                "留空时将自动从 URL/refer 获取默认清晰度的音视频流。"
            )
            return

        auto_fetch = not has_video  # 两个都为空时自动获取

        if not referer:
            messagebox.showerror("错误", "请填写 URL/refer（一般为视频网页地址）")
            return
        if not ua:
            messagebox.showerror("错误", "请填写 User-Agent")
            return

        # 手动提供时验证格式
        if has_video:
            if not video_url.startswith("http") or not audio_url.startswith("http"):
                messagebox.showerror("错误", "流地址应以 http(s):// 开头")
                return

        bvid = extract_bvid(referer)
        if not bvid:
            messagebox.showerror("错误", "无法从 URL/refer 中提取 BV 号")
            return

        headers = build_headers(ua, referer)
        v_codecid = extract_codecid(video_url) if has_video else None

        self.is_busy = True
        self.download_ready = False
        self._set_controls_busy(True)
        self._set_merge_ready(False)
        self._reset_all_progress()
        self._log("=" * 50)
        self._log(f"[解析] 正在获取视频信息: {bvid}")
        if auto_fetch:
            self._log("[模式] 未提供流地址，将自动获取默认清晰度音视频流")

        os.makedirs(TEMP_DIR, exist_ok=True)
        os.makedirs(OUT_DIR, exist_ok=True)

        threading.Thread(
            target=self._parse_download_worker,
            args=(bvid, headers, v_codecid, video_url, audio_url, auto_fetch),
            daemon=True,
        ).start()

    def _parse_download_worker(self, bvid, headers, v_codecid, video_url, audio_url, auto_fetch):
        try:
            # 1. 解析视频信息
            meta = fetch_video_meta(bvid, headers)
            self.title = meta["title"]
            owner = meta["owner"]
            cid = meta["cid"]

            # 1.5 自动获取流地址（两个都留空时）
            if auto_fetch:
                self._log("[自动] 正在从 API 获取默认清晰度音视频流地址...")
                video_url, audio_url, quality, v_codecid = fetch_dash_streams(bvid, cid, headers)
                self._log(f"[自动] 默认清晰度: {quality}")
                self._log(f"[自动] 视频流: {video_url[:90]}...")
                self._log(f"[自动] 音频流: {audio_url[:90]}...")
                # 回填到输入框
                self.root.after(0, lambda: self._fill_stream_urls(video_url, audio_url))
            else:
                quality = fetch_quality(bvid, cid, v_codecid, headers) if v_codecid else "未知"

            self._log(f"[解析] 标题: {self.title}")
            self._log(f"[解析] Up主: {owner}")
            self._log(f"[解析] 清晰度: {quality}")

            # 2. 下载视频流
            self.temp_video, self.temp_audio = self._get_temp_filenames()
            self._log(f"[下载] 视频临时文件: {self.temp_video}")
            download_stream(video_url, self.temp_video, headers, self._video_progress_cb)
            self._log("[下载] 视频流下载完成")

            # 3. 下载音频流
            self._log(f"[下载] 音频临时文件: {self.temp_audio}")
            download_stream(audio_url, self.temp_audio, headers, self._audio_progress_cb)
            self._log("[下载] 音频流下载完成")

            # 4. 本地 ffprobe 识别编码
            self._log("[识别] 正在用 ffprobe 识别本地文件编码...")
            v_codec, a_codec = probe_file_codecs(self.temp_video)
            # 视频文件可能不含音频，音频文件可能不含视频，分别识别
            _, a_codec2 = probe_file_codecs(self.temp_audio)
            if a_codec2:
                a_codec = a_codec2

            codec_display = f"视频 {v_codec or '未知'} | 音频 {a_codec or '未知'}"
            self._log(f"[识别] 原始编码: {codec_display}")

            # 5. 更新UI，启用合并
            def _update():
                self.title_label.config(text=self.title, foreground="black")
                self.owner_label.config(text=owner, foreground="black")
                self.quality_label.config(text=quality, foreground="black")
                self.codec_label.config(text=codec_display, foreground="black")
                # 输出文件名设为标题
                self.output_entry.delete(0, tk.END)
                self.output_entry.insert(0, sanitize_filename(self.title))
                # 更新 copy 选项显示原始编码
                self.v_codec_combo["values"] = [
                    f"copy - {v_codec}" if v_codec else "copy - 保持原始",
                ] + [x[0] for x in VIDEO_CODECS[1:]]
                self.a_codec_combo["values"] = [
                    f"copy - {a_codec}" if a_codec else "copy - 保持原始",
                ] + [x[0] for x in AUDIO_CODECS[1:]]
                self.v_codec_combo.current(0)
                self.a_codec_combo.current(0)
                self._set_merge_ready(True)
                self._log("[就绪] 下载完成，可选择输出格式后开始合并")
            self.root.after(0, _update)
            self.download_ready = True

        except requests.exceptions.RequestException as e:
            err_msg = f"网络请求失败:\n{e}"
            self._log(f"[错误] 网络请求失败: {e}")
            self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", m))
        except RuntimeError as e:
            err_msg = str(e)
            self._log(f"[错误] {e}")
            self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", m))
        except Exception as e:
            err_msg = f"未知错误:\n{e}"
            self._log(f"[错误] 未知错误: {e}")
            self.root.after(0, lambda m=err_msg: messagebox.showerror("错误", m))
        finally:
            self.is_busy = False
            self.root.after(0, lambda: self._set_controls_busy(False))

    # ----------------------------------------------------------
    # 第二步：合并
    # ----------------------------------------------------------
    def _start_merge(self):
        if self.is_busy or self.is_merging or not self.download_ready:
            return
        if not self.temp_video or not os.path.exists(self.temp_video):
            messagebox.showerror("错误", "临时视频文件不存在，请重新下载")
            self.download_ready = False
            self._set_merge_ready(False)
            return

        output_base = self.output_entry.get().strip()
        if not output_base:
            output_base = sanitize_filename(self.title) if self.title else "output"
        output_base = sanitize_filename(output_base)
        container = self._get_selected_codec(self.container_var, CONTAINERS)
        output_name = os.path.join(OUT_DIR, f"{output_base}.{container}")

        v_codec = self._get_selected_codec(self.v_codec_var, VIDEO_CODECS)
        a_codec = self._get_selected_codec(self.a_codec_var, AUDIO_CODECS)

        self.is_merging = True
        self._set_controls_busy(True)
        self._set_merge_ready(False)
        self.stop_btn.config(state=tk.NORMAL)
        self._set_progress("merge", 0)
        self._log("=" * 50)
        self._log(f"[合并] 视频={v_codec} 音频={a_codec} 容器={container}")
        self._log(f"[合并] 输出: {output_name}")

        threading.Thread(
            target=self._merge_worker,
            args=(self.temp_video, self.temp_audio, output_name, v_codec, a_codec),
            daemon=True,
        ).start()

    def _merge_worker(self, temp_video, temp_audio, output_name, v_codec, a_codec):
        try:
            merge_av(temp_video, temp_audio, output_name,
                     v_codec, a_codec, self._merge_progress_cb, self._process_holder)
            self.root.after(0, lambda: self._set_progress("merge", 100))
            self._log(f"[完成] 视频已保存为: {output_name}")
            # 合并成功后清理临时文件
            cleanup([temp_video, temp_audio])
            self._log("[清理] 临时文件已删除")
            self.download_ready = False
            self.root.after(0, lambda: messagebox.showinfo("完成", f"视频已保存为:\n{output_name}"))

        except RuntimeError as e:
            err_msg = f"运行时错误：\n{e}"
            self._log(f"[错误] 运行时错误： {e}")
            self.root.after(0, lambda: m=err_msg: messagrbox.showerror("错误", m))
        except Exception as e:
            err_msg = f"未知错误：\n{e}"
            self._log(f"[错误] 未知错误: {e}")
            self.root.after(0, lambda: m=err_msg: messagrbox.showerror("错误", m))
        finally:
            self.is_merging = False
            self._process_holder.clear()
            self.root.after(0, self.stop_btn.config(state=tk.DISABLED))
            self.root.after(0, lambda: self._set_controls_busy(False))
            # 合并失败时保留临时文件，允许用户改设置重试
            if self.download_ready and os.path.exists(temp_video):
                self.root.after(0, lambda: self._set_merge_ready(True))
            else:
                self.root.after(0, lambda: self._set_merge_ready(False))

    # ----------------------------------------------------------
    # 中止合并
    # ----------------------------------------------------------
    def _force_stop_merge(self):
        proc = self._process_holder.get("proc")
        if proc and proc.poll() is None:
            self._log("[中止] 正在终止 ffmpeg 进程...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self._log("[中止] ffmpeg 进程已结束")
        else:
            self._log("[中止] 没有正在运行的合并进程")


# ============================================================
# 入口
# ============================================================

def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    BilibiliDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
