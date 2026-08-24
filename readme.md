# B站视频下载器（Bilibili Video Downloader）

一个基于 Python + Tkinter 的图形化 B 站视频下载工具，支持自动/手动获取 DASH 音视频流、本地编码识别、灵活转码封装，并支持合并过程**可中止重试**，无需重复下载。

---

## 📌 主要特性

- 🎬 **自动获取最高清晰度音视频流** – 只需粘贴视频页面 URL，一键解析并下载 DASH 格式的视音频（适用于**公开视频**）。
- 🔄 **下载与合并分离** – 下载仅执行一次，合并可反复尝试（改变编码/容器），避免重复请求 B 站服务器，节省流量。
- 🧠 **本地编码智能识别** – 下载后自动用 `ffprobe` 识别原始音视频编码，并在界面显示，方便选择 `copy` 保持原始格式。
- ✋ **可中止合并** – 合并过程中可随时点击"中止合并"强制终止 `ffmpeg`，临时文件保留，便于重新设置参数后继续合并。
- 🎛️ **丰富的编码/容器选项** – 支持 H.264 / H.265 / AV1 / ProRes / MPEG‑4 视频编码，AAC / MP3 / FLAC / PCM 音频编码，以及 MP4 / MOV / MKV / AVI 容器。
- 📊 **实时进度反馈** – 下载和合并阶段均显示独立进度条及百分比。
- 📖 **内置使用教程** – 提供图文教程，指导手动抓取流地址和伪装信息，满足进阶需求。

---

## 🔧 依赖

### Python 库

- `requests` – 网络请求

安装命令：

```bash
pip install requests
```

### 系统软件（必须）

- **ffmpeg** 和 **ffprobe**（用于合并、转码及编码识别）
  下载地址：[ffmpeg.org](https://ffmpeg.org/download.html)
  *安装后请确保 `ffmpeg` 和 `ffprobe` 命令可在终端/命令行中直接调用（已加入系统 PATH）。*
  ffmpeg支持通过包管理软件安装。

```bash
  # Debian / Ubuntu / Kali Linux
  sudo apt update
  sudo apt install ffmpeg
```
```bash
  # ArchLinux / Manjaro
  sudo pacman -Syu ffmpeg
```
```bash
  # RHEL/CentOS7（需要先配置RPM Fusion/Nux Dextop第三方源）
  sudo yum install epel-release
  # 配置RPM Fusion源之后，再执行
  sudo yum install ffmpeg
```
```bash
  # RHEL8+/CentOS8+/Rocky/AlmaLinux 使用 dnf
  sudo dnf install epel-release
  # 配置RPM Fusion源之后，再执行
  sudo dnf install ffmpeg
```

  在Windows端可使用Winget或Chocolaety包管理器安装

```Powershell
  # Winget（需要先安装Winget）
  winget install Gyan.FFmpeg
```
  注意：
  - 包 ID：`Gyan.FFmpeg` 为 full_build 完整版本；另有精简版 `Gyan.FFmpeg.Essentials`。
  - 安装后自动写入用户 PATH，**必须新开终端才生效**。
  - Win10 旧版本如果提示`winget`不是命令，需要在微软商店安装更新「App Installer」。
  - 不支持 Windows7。

```Powershell
  # Chocolatey（需要先安装Chocolatey）
  choco install ffmpeg
```
  注意：
  -Chocolatey 包解压目录：`C:\ProgramData\chocolatey\lib\ffmpeg\tools`
  -**真正 ffmpeg.exe、ffprobe.exe 在下级 bin 目录：`C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin`**
  -choco 会自动将 bin 目录加入系统 PATH，新开终端即可直接调用 ffmpeg 命令；
  -Chocolatey 的根目录可通过环境变量`%ChocolateyInstall%`修改，路径不一定固定在 C 盘。

---

## 📥 安装与运行

1. **克隆或下载脚本**
   将 `video_capture.py` 保存到本地目录。

2. **安装 Python 依赖**

   ```bash
   pip install requests
   ```

3. **确保 ffmpeg 可用**
   在终端执行 `ffmpeg -version` 和 `ffprobe -version` 验证安装。

4. **运行程序**

   ```bash
   python video_capture.py
   ```

---

## 🚀 使用方法

### 自动模式（推荐，适用于公开视频）

1. 打开 B 站视频页面，复制浏览器地址栏中的 URL（例如 `https://www.bilibili.com/video/BV1...`）。
2. 将 URL 粘贴到 **"URL/refer"** 输入框。
3. **"视频流地址"** 和 **"音频流地址"** 留空（程序将自动获取默认最高清晰度）。
4. 填写或保留默认的 **User-Agent**（建议使用浏览器中的真实 UA）。
5. 点击 **"解析并下载"**，等待下载完成。
6. 下载完成后，界面会显示视频标题、UP主、清晰度及原始编码。
7. 在 **"输出格式"** 区域选择需要的视频编码、音频编码和容器格式（默认 `copy` 保持原始）。
8. 点击 **"开始合并"**，等待合成完成。
9. 成品文件保存在 `out/` 目录中。

> ⚠️ **注意**：自动模式**不支持需要登录或大会员权限的视频**，也无法解析加密的付费内容。
> 若视频需要登录/大会员，请参考下面的**手动模式**。

---

### 手动模式（适用于需登录、大会员或指定特定清晰度）

当自动模式无法获取流地址（如视频需登录、大会员、或您想选择非默认的清晰度）时，请手动抓取：

1. 在浏览器中**登录您的 B 站账号**，打开目标视频，按 `F12` 打开开发者工具 → **Network（网络）** 面板。
2. 筛选 `m4s` 类型，找到视频流（URL 含 `-1-xxx.m4s`）和音频流（URL 含 `-1-30xxx.m4s`）。
   - 视频流通常为 `-1-100xxx.m4s`（数值代表清晰度），音频流为 `-1-30xxx.m4s`。
3. 右键 → Copy → **Copy URL address**，分别粘贴到 **"视频流地址"** 和 **"音频流地址"**。
4. 填写 **URL/refer**（视频页面地址）和 **User-Agent**（建议从浏览器复制）。
5. 点击 **"解析并下载"**，其余步骤同自动模式。

> 💡 点击界面右上角的 **"教程"** 按钮可查看详细抓取说明。

---

### 中止与重试

- 合并过程中可点击 **"中止合并"** 终止 `ffmpeg` 进程，临时文件（`temp/` 目录）不会删除。
- 您可以更改编码/容器设置后再次点击 **"开始合并"**，无需重新下载。

---

## 📂 目录结构

```
项目目录/
├── video_capture.py      # 主程序
├── temp/                 # 临时音视频文件（下载后存放）
└── out/                  # 合并完成的视频文件输出目录
```

---

## ⚠️ 注意事项

1. **流地址必须同时提供或同时留空**，不能只填一个。

2. **关于登录/大会员/付费视频**：
   - ✅ **可下载**：未加密的普通视频，以及**需要登录或大会员但未加密**的视频（需使用**手动模式**抓取流地址，因为自动模式不带 Cookie）。
   - ❌ **不可下载**：采用 DRM 加密的大会员视频、单独付费（PVV）视频、或任何需要额外许可证的内容。此类视频的流地址通常带有访问限制或加密，工具无法处理。
   - 对于需登录的视频，手动抓取的流地址**本身可能包含临时访问令牌**，请在复制后尽快使用（通常有效时间较短）。

3. 若下载失败，请检查网络连接、User-Agent 是否与浏览器一致，以及是否在手动模式下使用了正确的流地址（注意流地址可能包含 `access_key` 等参数）。

4. 某些编码组合与容器不兼容（例如 ProRes 不支持 AVI），`ffmpeg` 会报错，请选择兼容的组合。

5. 合并时若出现 `ffmpeg` 错误，请查看日志区输出，调整编码/容器后重试。

6. 本工具仅供学习研究使用，请勿用于商业或侵权用途。请尊重视频版权，仅下载您有权观看的内容。

---

## 🐛 常见问题

**Q：提示"未检测到 ffmpeg / ffprobe"**
A：请确保已安装 ffmpeg 并将其所在目录添加到系统环境变量 PATH 中。

**Q：下载后原始编码显示"未知"**
A：通常是因为 `ffprobe` 未正确识别文件格式，但不影响合并（若选择 `copy` 可能会失败，建议选择其他编码重试）。

**Q：合并进度卡住不动**
A：可能是高码率视频编码较慢，请耐心等待。若长时间无响应，可点击"中止合并"并尝试降低编码参数（如选择 `copy` 或更快的编码器）。

**Q：自动模式解析时提示"该视频不支持 DASH 流"**
A：部分旧视频或特殊格式可能不提供 DASH 流，请尝试手动模式抓取 FLV 或 MP4 直链（需自行抓取）。

**Q：需要登录的大会员视频如何下载？**
A：请使用**手动模式**：在浏览器中登录后，从 Network 面板复制音视频流地址（这些地址通常包含登录态令牌），粘贴到工具中下载。工具本身不处理 Cookie，因此必须手动获取有效 URL。

**Q：为什么有些大会员视频下载后无法播放或花屏？**
A：如果视频采用 DRM 加密，流地址可能指向加密数据，本工具无法解密。请确认视频是否允许离线缓存（如 B 站客户端缓存），若不允许则无法下载。

---

## 📄 许可证

本项目仅供个人学习使用，未经授权不得用于商业目的。

---

**Enjoy!** 🎉
如有问题或建议，欢迎提 Issue 或自行修改代码。
