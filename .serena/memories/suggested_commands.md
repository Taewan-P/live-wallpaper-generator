Useful commands for this project:
- `python3 script.py -i input.mp4 -o output.mov` : convert and patch a video.
- `python3 apply_wallpaper.py register -i output.mov -n "My Wallpaper" -c "Custom" --apply --restart-agent` : register and apply a wallpaper.
- `python3 apply_wallpaper.py serve` / `python3 apply_wallpaper.py stop` : manually manage the localhost asset server.
- `python3 - <<'PY' ... compile(open('script.py').read(), 'script.py', 'exec') ... PY` : syntax-check without creating `__pycache__` when sandboxing blocks writes.
- `ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,codec_tag_string,pix_fmt,color_space,color_transfer,color_primaries -of json <file>` : inspect output compatibility.