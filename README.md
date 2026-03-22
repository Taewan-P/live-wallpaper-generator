### Standalone Python Workflow

If you want to use the wallpaper pipeline without opening the app, the repository also includes two standalone Python scripts:

*   `script.py`: converts a source video into a macOS-live-wallpaper-compatible MOV by transcoding to HEVC Main10 and patching the QuickTime atoms.
*   `apply_wallpaper.py`: registers the patched MOV into the macOS wallpaper catalog, serves it over localhost, and can apply it as the current wallpaper.

#### Requirements

*   macOS with access to `~/Library/Application Support/com.apple.wallpaper`
*   `python3`
*   `ffmpeg` and `ffprobe`

#### 1. Convert a Video

Create a patched wallpaper-ready MOV:

```bash
python3 script.py -i input.mp4 -o output.mov
```

Optional trim arguments:

```bash
python3 script.py -i input.mp4 -o output.mov --start 3.5 --end 18.0
```

What `script.py` does:

*   Transcodes to `HEVC` with `hvc1` tagging
*   Outputs `yuv420p10le`
*   Normalizes fractional frame rates like `29.97` to `30` when needed
*   Tone-maps HDR / wide-color inputs to BT.709 SDR when needed
*   Injects wallpaper-specific atoms such as `tapt`, `sgpd`, `csgm`, and `cslg`

#### 2. Register and Apply the Wallpaper

Register the patched MOV into macOS wallpaper storage and apply it:

```bash
python3 apply_wallpaper.py register -i output.mov -n "My Wallpaper" -c "Custom" --apply --restart-agent
```

What `apply_wallpaper.py register` does:

*   Copies the video into `~/Library/Application Support/com.apple.wallpaper/aerials/videos`
*   Creates or copies a thumbnail into `~/Library/Application Support/com.apple.wallpaper/aerials/thumbnails`
*   Updates `entries.json`
*   Updates the wallpaper localization tables
*   Applies the new wallpaper if `--apply` is used
*   Restarts the wallpaper agent if `--restart-agent` is used

When the registered asset URL uses `http://localhost:50505/...`, the script automatically starts the background local asset server if it is not already running.

#### 3. Background Local Server

Start the detached local server manually:

```bash
python3 apply_wallpaper.py serve
```

Stop it:

```bash
python3 apply_wallpaper.py stop
```

The detached server survives closing the terminal. It stores:

*   PID file: `~/Library/Application Support/LiveWallpaperLocalServer/live-wallpaper-local-server.pid`
*   stdout log: `~/Library/Logs/live-wallpaper-local-server.log`
*   stderr log: `~/Library/Logs/live-wallpaper-local-server.err.log`

#### 4. Launch Agent Option

If you prefer a macOS-managed background process instead of the detached server mode:

```bash
python3 apply_wallpaper.py install-launch-agent
```

To remove it:

```bash
python3 apply_wallpaper.py uninstall-launch-agent
```

You can also install the launch agent during registration:

```bash
python3 apply_wallpaper.py register -i output.mov -n "My Wallpaper" -c "Custom" --apply --restart-agent --install-launch-agent
```

#### 5. Useful Commands

List localhost-backed custom assets:

```bash
python3 apply_wallpaper.py list
```

Apply an existing asset again by ID:

```bash
python3 apply_wallpaper.py apply --asset-id YOUR-ASSET-ID --restart-agent
```