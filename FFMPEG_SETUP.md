# FFmpeg Setup for IndiePulse

The vertical video renderer requires FFmpeg to be installed on your system.

## Quick Setup (Recommended)

1. **Download FFmpeg for Windows:**
   - Go to https://github.com/BtbN/FFmpeg-Builds/releases
   - Download `ffmpeg-master-latest-win64-gpl.zip`

2. **Extract and Install:**
   - Extract the ZIP file
   - Copy the `ffmpeg.exe` file from the `bin` folder
   - Place it in one of these locations:
     - `D:\IndiePulse\vendor\ffmpeg\ffmpeg.exe` (recommended)
     - `C:\ffmpeg\bin\ffmpeg.exe`
     - Or add the `bin` folder to your system PATH

3. **Alternative: Add to PATH**
   - Extract to `C:\ffmpeg\`
   - Add `C:\ffmpeg\bin` to your system PATH environment variable
   - Restart your terminal/command prompt

## Verify Installation

After installation, restart the IndiePulse server. The vertical renderer should now work.

## Troubleshooting

If you still get "FFmpeg not found" errors:

1. Check that `ffmpeg.exe` exists in the expected location
2. Try running `ffmpeg -version` in Command Prompt
3. Make sure you've restarted the IndiePulse server after installation

## Manual Installation

If the automatic detection fails, you can manually specify the FFmpeg path by editing `app/services/video_editor.py` and updating the `_ffmpeg_path()` function.
