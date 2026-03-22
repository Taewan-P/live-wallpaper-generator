#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PUBLIC_HOST = "localhost"
DEFAULT_PORT = 50505
DEFAULT_CATEGORY_NAME = "Custom"
LAUNCH_AGENT_LABEL = "dev.chungjungsoo.livewallpaper.local-server"
BACKGROUND_LABEL = "live-wallpaper-local-server"


class WallpaperApplyError(Exception):
    pass


@dataclass(frozen=True)
class WallpaperPaths:
    root: Path

    @property
    def manifest_dir(self) -> Path:
        return self.root / "aerials" / "manifest"

    @property
    def manifest_path(self) -> Path:
        return self.manifest_dir / "entries.json"

    @property
    def strings_bundle_path(self) -> Path:
        return self.manifest_dir / "TVIdleScreenStrings.bundle"

    @property
    def loctable_path(self) -> Path:
        return self.strings_bundle_path / "Contents" / "Resources" / "Localizable.nocache.loctable"

    @property
    def videos_dir(self) -> Path:
        return self.root / "aerials" / "videos"

    @property
    def thumbnails_dir(self) -> Path:
        return self.root / "aerials" / "thumbnails"

    @property
    def index_path(self) -> Path:
        return self.root / "Store" / "Index.plist"


def default_wallpaper_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "com.apple.wallpaper"


def server_state_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "LiveWallpaperLocalServer"


def server_pid_path() -> Path:
    return server_state_dir() / f"{BACKGROUND_LABEL}.pid"


def server_stdout_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / f"{BACKGROUND_LABEL}.log"


def server_stderr_log_path() -> Path:
    return Path.home() / "Library" / "Logs" / f"{BACKGROUND_LABEL}.err.log"


def resolve_paths(root: Path | None) -> WallpaperPaths:
    return WallpaperPaths(root=(root or default_wallpaper_root()).expanduser().resolve())


def ensure_file(path: Path, description: str) -> None:
    if path.exists():
        return
    raise WallpaperApplyError(f"{description} does not exist: {path}")


def require_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise WallpaperApplyError(f"Required executable '{name}' was not found in PATH")


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode == 0:
        return
    message = completed.stderr.strip() or completed.stdout.strip() or "Subprocess failed"
    raise WallpaperApplyError(message)


def backup_if_missing(path: Path) -> None:
    backup_path = path.with_suffix(path.suffix + ".bak")
    if path.exists() and not backup_path.exists():
        shutil.copy2(path, backup_path)


def read_manifest(paths: WallpaperPaths) -> dict[str, Any]:
    ensure_file(paths.manifest_path, "Wallpaper manifest")
    with paths.manifest_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_manifest(paths: WallpaperPaths, manifest: dict[str, Any]) -> None:
    backup_if_missing(paths.manifest_path)
    paths.manifest_dir.mkdir(parents=True, exist_ok=True)
    with paths.manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_loctable(paths: WallpaperPaths) -> dict[str, Any] | None:
    if not paths.loctable_path.exists():
        return None
    return plistlib.loads(paths.loctable_path.read_bytes())


def write_loctable(paths: WallpaperPaths, loctable: dict[str, Any]) -> None:
    paths.loctable_path.parent.mkdir(parents=True, exist_ok=True)
    paths.loctable_path.write_bytes(plistlib.dumps(loctable, fmt=plistlib.FMT_BINARY))


def update_strings(paths: WallpaperPaths, key: str, value: str) -> None:
    languages = ["en", "ko"]
    for language in languages:
        folder = paths.strings_bundle_path / "Contents" / "Resources" / f"{language}.lproj"
        strings_path = folder / "Localizable.nocache.strings"
        folder.mkdir(parents=True, exist_ok=True)

        current: dict[str, str] = {}
        if strings_path.exists():
            current = plistlib.loads(strings_path.read_bytes())
        current[key] = value
        strings_path.write_bytes(plistlib.dumps(current, fmt=plistlib.FMT_BINARY))

    loctable = read_loctable(paths)
    if loctable is not None:
        for language, payload in list(loctable.items()):
            if language == "LocProvenance":
                continue
            if isinstance(payload, dict):
                payload[key] = value
                loctable[language] = payload
        write_loctable(paths, loctable)


def make_category_id(category_name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"live-wallpaper-category:{category_name}")).upper()


def make_localized_key(prefix: str, value: str) -> str:
    return f"{prefix}_{value.replace('-', '')}"


def ensure_category(manifest: dict[str, Any], paths: WallpaperPaths, category_name: str) -> str:
    categories = manifest.setdefault("categories", [])
    desired_id = make_category_id(category_name)

    for category in categories:
        if category.get("id") == desired_id:
            return desired_id

    category_key = make_localized_key("AerialCategory", desired_id)
    new_category = {
        "id": desired_id,
        "localizedNameKey": category_key,
        "localizedDescriptionKey": None,
        "representativeAssetID": None,
        "previewImage": None,
        "preferredOrder": len(categories) + 1,
        "subcategories": None,
    }
    categories.append(new_category)
    update_strings(paths, category_key, category_name)
    return desired_id


def generate_thumbnail(video_path: Path, output_path: Path, ffmpeg_bin: str) -> None:
    require_tool(ffmpeg_bin)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0.5",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=900:-2",
        str(output_path),
    ]
    run_command(command)


def register_asset(
    video_path: Path,
    thumbnail_path: Path | None,
    name: str,
    category_name: str,
    paths: WallpaperPaths,
    public_base_url: str,
    ffmpeg_bin: str,
) -> str:
    ensure_file(video_path, "Patched video")
    manifest = read_manifest(paths)
    category_id = ensure_category(manifest, paths, category_name)

    asset_id = str(uuid.uuid4()).upper()
    clean_asset_id = asset_id.replace("-", "")
    asset_name_key = f"AerialAsset_{clean_asset_id}_NAME"

    paths.videos_dir.mkdir(parents=True, exist_ok=True)
    paths.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    target_video = paths.videos_dir / f"{asset_id}.mov"
    target_thumb = paths.thumbnails_dir / f"{asset_id}.png"

    shutil.copy2(video_path, target_video)
    if thumbnail_path is not None:
        ensure_file(thumbnail_path, "Thumbnail")
        shutil.copy2(thumbnail_path, target_thumb)
    else:
        generate_thumbnail(target_video, target_thumb, ffmpeg_bin)

    video_url = f"{public_base_url.rstrip('/')}/video/{asset_id}.mov"
    thumb_url = f"{public_base_url.rstrip('/')}/thumbnail/{asset_id}.png"

    assets = manifest.setdefault("assets", [])
    preferred_order = sum(1 for asset in assets if category_id in asset.get("categories", [])) + 1

    new_asset = {
        "id": asset_id,
        "localizedNameKey": asset_name_key,
        "accessibilityLabel": name,
        "previewImage": thumb_url,
        "previewImage-900x580": thumb_url,
        "url-4K-SDR-240FPS": video_url,
        "preferredOrder": preferred_order,
        "categories": [category_id],
        "subcategories": None,
        "shotID": asset_id,
        "includeInShuffle": True,
        "showInTopLevel": True,
        "pointsOfInterest": {},
    }
    assets.append(new_asset)

    for category in manifest.get("categories", []):
        if category.get("id") != category_id:
            continue
        if not category.get("representativeAssetID"):
            category["representativeAssetID"] = asset_id
            category["previewImage"] = thumb_url
        break

    update_strings(paths, asset_name_key, name)
    write_manifest(paths, manifest)
    return asset_id


def load_index(paths: WallpaperPaths) -> dict[str, Any]:
    ensure_file(paths.index_path, "Wallpaper index plist")
    return plistlib.loads(paths.index_path.read_bytes())


def write_index(paths: WallpaperPaths, payload: dict[str, Any]) -> None:
    backup_if_missing(paths.index_path)
    paths.index_path.parent.mkdir(parents=True, exist_ok=True)
    paths.index_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY))


def configuration_blob(asset_id: str) -> bytes:
    return plistlib.dumps({"assetID": asset_id}, fmt=plistlib.FMT_BINARY)


def inject_content(node: dict[str, Any], asset_id: str) -> None:
    node["Type"] = "individual"
    content = node.get("Content")
    if not isinstance(content, dict):
        content = {}

    choices = content.get("Choices")
    if not isinstance(choices, list) or not choices:
        choices = [{}]
    if not isinstance(choices[0], dict):
        choices[0] = {}

    choices[0]["Provider"] = "com.apple.wallpaper.choice.aerials"
    choices[0]["Configuration"] = configuration_blob(asset_id)
    choices[0]["Files"] = []

    content["Choices"] = choices
    content["Shuffle"] = "$null"
    content.pop("EncodedOptionValues", None)
    node["Content"] = content


def apply_to_target_node(node: dict[str, Any], asset_id: str) -> None:
    containers = ["Desktop", "Idle", "Linked"]
    updated = False

    for key in containers:
        container = node.get(key)
        if isinstance(container, dict):
            inject_content(container, asset_id)
            container["LastSet"] = datetime.now()
            node[key] = container
            updated = True

    if not updated:
        new_container = {}
        inject_content(new_container, asset_id)
        new_container["LastSet"] = datetime.now()
        node["Desktop"] = dict(new_container)
        node["Idle"] = dict(new_container)

    node["Type"] = "individual"


def iter_target_nodes(index_data: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    for key in ("AllSpacesAndDisplays", "SystemDefault"):
        value = index_data.get(key)
        if not isinstance(value, dict):
            value = {}
            index_data[key] = value
        nodes.append(value)

    displays = index_data.get("Displays")
    if isinstance(displays, dict):
        for display in displays.values():
            if isinstance(display, dict):
                nodes.append(display)

    spaces = index_data.get("Spaces")
    if isinstance(spaces, dict):
        for space in spaces.values():
            if not isinstance(space, dict):
                continue
            default_node = space.get("Default")
            if isinstance(default_node, dict):
                nodes.append(default_node)
            space_displays = space.get("Displays")
            if isinstance(space_displays, dict):
                for display in space_displays.values():
                    if isinstance(display, dict):
                        nodes.append(display)

    return nodes


def apply_asset(paths: WallpaperPaths, asset_id: str) -> None:
    index_data = load_index(paths)
    for node in iter_target_nodes(index_data):
        apply_to_target_node(node, asset_id)
    write_index(paths, index_data)


def restart_wallpaper_agent() -> None:
    run_command(["/bin/launchctl", "stop", "com.apple.wallpaper.agent"])
    completed = subprocess.run(
        ["/usr/bin/pkill", "-f", "WallpaperAgent|WallpaperAerialsExtension|NeptuneOneWallpaper"],
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 1):
        message = completed.stderr.strip() or completed.stdout.strip() or "pkill failed"
        raise WallpaperApplyError(message)
    run_command(["/bin/launchctl", "start", "com.apple.wallpaper.agent"])


class LocalAssetRequestHandler(BaseHTTPRequestHandler):
    server_version = "LiveWallpaperLocalServer/1.0"

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            target = unquote(parsed.path)
            if target.startswith("/video/"):
                self.serve_named_file("video", target.removeprefix("/video/"), send_body=True)
                return
            if target.startswith("/thumbnail/"):
                self.serve_named_file("thumbnail", target.removeprefix("/thumbnail/"), send_body=True)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")
        except Exception as exc:  # pragma: no cover - defensive server path
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:
        try:
            parsed = urlparse(self.path)
            target = unquote(parsed.path)
            if target.startswith("/video/"):
                self.serve_named_file("video", target.removeprefix("/video/"), send_body=False)
                return
            if target.startswith("/thumbnail/"):
                self.serve_named_file("thumbnail", target.removeprefix("/thumbnail/"), send_body=False)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")
        except Exception as exc:  # pragma: no cover - defensive server path
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        message = format % args
        sys.stdout.write(f"[server] {self.address_string()} {message}\n")

    def serve_named_file(self, kind: str, filename: str, send_body: bool) -> None:
        if "/" in filename or "\\" in filename or ".." in filename:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid filename")
            return

        if kind == "video":
            root_dir = self.server.wallpaper_paths.videos_dir  # type: ignore[attr-defined]
            content_type = "video/quicktime"
        else:
            root_dir = self.server.wallpaper_paths.thumbnails_dir  # type: ignore[attr-defined]
            content_type = "image/png"

        file_path = root_dir / filename
        if not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)


def serve_forever(paths: WallpaperPaths, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), LocalAssetRequestHandler)
    server.wallpaper_paths = paths  # type: ignore[attr-defined]
    print(f"Serving wallpaper assets from {paths.root} on http://{host}:{port}")
    server.serve_forever()


def read_server_pid() -> int | None:
    pid_path = server_pid_path()
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_path.unlink(missing_ok=True)
        return None


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def background_server_status() -> tuple[int | None, bool]:
    pid = read_server_pid()
    if pid is None:
        return None, False
    alive = is_pid_running(pid)
    if not alive:
        server_pid_path().unlink(missing_ok=True)
    return pid, alive


def stop_background_server_if_running() -> int | None:
    pid, alive = background_server_status()
    if pid is None or not alive:
        return None
    return stop_background_server()


def parse_localhost_base_url(public_base_url: str) -> tuple[str, int] | None:
    parsed = urlparse(public_base_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return None
    if parsed.port is not None:
        return parsed.hostname, parsed.port
    if parsed.scheme == "https":
        return parsed.hostname, 443
    return parsed.hostname, 80


def ensure_localhost_server_running(paths: WallpaperPaths, public_base_url: str, python_bin: str) -> int | None:
    localhost_target = parse_localhost_base_url(public_base_url)
    if localhost_target is None:
        return None

    pid, alive = background_server_status()
    if alive:
        return pid

    _, port = localhost_target
    return start_background_server(paths, DEFAULT_HOST, port, python_bin)


def start_background_server(paths: WallpaperPaths, host: str, port: int, python_bin: str) -> int:
    pid, alive = background_server_status()
    if alive and pid is not None:
        raise WallpaperApplyError(f"Background server is already running with PID {pid}")

    server_state_dir().mkdir(parents=True, exist_ok=True)
    server_stdout_log_path().parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()

    with server_stdout_log_path().open("ab") as stdout_handle, server_stderr_log_path().open("ab") as stderr_handle:
        process = subprocess.Popen(
            [
                python_bin,
                str(script_path),
                "serve",
                "--foreground",
                "--host",
                host,
                "--port",
                str(port),
                "--wallpaper-root",
                str(paths.root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            cwd=str(script_path.parent),
            start_new_session=True,
        )

    time.sleep(0.6)
    if process.poll() is not None:
        error_tail = ""
        if server_stderr_log_path().exists():
            try:
                error_tail = server_stderr_log_path().read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                error_tail = ""
        raise WallpaperApplyError(error_tail.strip() or f"Server exited immediately with code {process.returncode}")

    server_pid_path().write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop_background_server() -> int:
    pid, alive = background_server_status()
    if pid is None or not alive:
        raise WallpaperApplyError("Background server is not running")

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not is_pid_running(pid):
            server_pid_path().unlink(missing_ok=True)
            return pid
        time.sleep(0.1)

    raise WallpaperApplyError(f"Server PID {pid} did not stop within 5 seconds")


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def install_launch_agent(script_path: Path, python_bin: str, host: str, port: int, paths: WallpaperPaths) -> Path:
    plist_path = launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            python_bin,
            str(script_path),
            "serve",
            "--foreground",
            "--host",
            host,
            "--port",
            str(port),
            "--wallpaper-root",
            str(paths.root),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / "live-wallpaper-local-server.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "live-wallpaper-local-server.err.log"),
        "WorkingDirectory": str(script_path.parent),
    }
    plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))
    return plist_path


def load_launch_agent(plist_path: Path) -> None:
    completed = subprocess.run(
        ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return
    stderr = completed.stderr.strip()
    if "already bootstrapped" in stderr.lower():
        run_command(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"])
        return
    raise WallpaperApplyError(stderr or completed.stdout.strip() or "launchctl bootstrap failed")


def unload_launch_agent() -> None:
    plist_path = launch_agent_path()
    if not plist_path.exists():
        return
    completed = subprocess.run(
        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 3):
        stderr = completed.stderr.strip()
        if "no such process" not in stderr.lower():
            raise WallpaperApplyError(stderr or completed.stdout.strip() or "launchctl bootout failed")


def uninstall_launch_agent() -> None:
    plist_path = launch_agent_path()
    unload_launch_agent()
    if plist_path.exists():
        plist_path.unlink()


def list_custom_assets(paths: WallpaperPaths) -> list[dict[str, Any]]:
    manifest = read_manifest(paths)
    assets = manifest.get("assets", [])
    categories = {category["id"]: category for category in manifest.get("categories", [])}
    results = []
    for asset in assets:
        url = asset.get("url-4K-SDR-240FPS", "")
        if "localhost" not in url and "127.0.0.1" not in url:
            continue
        names = [categories.get(category_id, {}).get("localizedNameKey", category_id) for category_id in asset.get("categories", [])]
        results.append({"id": asset.get("id"), "label": asset.get("accessibilityLabel"), "categories": names})
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register and apply a patched live-wallpaper video using localhost-backed asset URLs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Start the local asset server in the background")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--wallpaper-root", type=Path, default=None)
    serve_parser.add_argument("--python", default=sys.executable)
    serve_parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser("stop", help="Stop the background local asset server")

    install_parser = subparsers.add_parser("install-launch-agent", help="Install a launch agent that keeps the local asset server running")
    install_parser.add_argument("--python", default=sys.executable)
    install_parser.add_argument("--host", default=DEFAULT_HOST)
    install_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    install_parser.add_argument("--wallpaper-root", type=Path, default=None)

    subparsers.add_parser("uninstall-launch-agent", help="Remove the launch agent for the local asset server")

    register_parser = subparsers.add_parser("register", help="Register a patched wallpaper video into the macOS wallpaper catalog")
    register_parser.add_argument("-i", "--input", required=True, type=Path, help="Patched video path")
    register_parser.add_argument("-t", "--thumbnail", type=Path, default=None, help="Optional PNG thumbnail path")
    register_parser.add_argument("-n", "--name", required=True, help="Display name for the wallpaper")
    register_parser.add_argument("-c", "--category", default=DEFAULT_CATEGORY_NAME, help="Custom category name")
    register_parser.add_argument("--wallpaper-root", type=Path, default=None)
    register_parser.add_argument("--ffmpeg", default="ffmpeg")
    register_parser.add_argument("--public-base-url", default=f"http://{DEFAULT_PUBLIC_HOST}:{DEFAULT_PORT}")
    register_parser.add_argument("--apply", action="store_true", help="Apply the new wallpaper after registering it")
    register_parser.add_argument("--restart-agent", action="store_true", help="Restart the wallpaper agent after applying")
    register_parser.add_argument("--install-launch-agent", action="store_true", help="Install and load the local asset server launch agent")
    register_parser.add_argument("--python", default=sys.executable, help="Python executable to embed in the launch agent")

    apply_parser = subparsers.add_parser("apply", help="Apply a registered asset ID as the current wallpaper")
    apply_parser.add_argument("--asset-id", required=True, help="Asset ID to apply")
    apply_parser.add_argument("--wallpaper-root", type=Path, default=None)
    apply_parser.add_argument("--restart-agent", action="store_true")

    list_parser = subparsers.add_parser("list", help="List localhost-backed custom wallpaper assets")
    list_parser.add_argument("--wallpaper-root", type=Path, default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "serve":
            if args.foreground:
                serve_forever(resolve_paths(args.wallpaper_root), args.host, args.port)
            else:
                pid = start_background_server(resolve_paths(args.wallpaper_root), args.host, args.port, args.python)
                print(f"Started background server with PID {pid}")
                print(f"Logs: {server_stdout_log_path()} and {server_stderr_log_path()}")
            return 0

        if args.command == "stop":
            pid = stop_background_server()
            print(f"Stopped background server PID {pid}")
            return 0

        if args.command == "install-launch-agent":
            paths = resolve_paths(args.wallpaper_root)
            stopped_pid = stop_background_server_if_running()
            plist_path = install_launch_agent(Path(__file__).resolve(), args.python, args.host, args.port, paths)
            load_launch_agent(plist_path)
            if stopped_pid is not None:
                print(f"Stopped background server PID {stopped_pid} before loading launch agent")
            print(f"Installed and loaded launch agent: {plist_path}")
            return 0

        if args.command == "uninstall-launch-agent":
            uninstall_launch_agent()
            print("Removed local asset server launch agent")
            return 0

        if args.command == "register":
            paths = resolve_paths(args.wallpaper_root)
            asset_id = register_asset(
                video_path=args.input.expanduser().resolve(),
                thumbnail_path=args.thumbnail.expanduser().resolve() if args.thumbnail else None,
                name=args.name,
                category_name=args.category,
                paths=paths,
                public_base_url=args.public_base_url,
                ffmpeg_bin=args.ffmpeg,
            )
            if args.install_launch_agent:
                stopped_pid = stop_background_server_if_running()
                plist_path = install_launch_agent(Path(__file__).resolve(), args.python, DEFAULT_HOST, DEFAULT_PORT, paths)
                load_launch_agent(plist_path)
                if stopped_pid is not None:
                    print(f"Stopped background server PID {stopped_pid} before loading launch agent")
                print(f"Installed local asset server launch agent: {plist_path}")
            else:
                started_pid = ensure_localhost_server_running(paths, args.public_base_url, args.python)
                if started_pid is not None:
                    print(f"Background server ready on localhost with PID {started_pid}")
            if args.apply:
                apply_asset(paths, asset_id)
                if args.restart_agent and paths.root == default_wallpaper_root().resolve():
                    restart_wallpaper_agent()
            print(asset_id)
            return 0

        if args.command == "apply":
            paths = resolve_paths(args.wallpaper_root)
            apply_asset(paths, args.asset_id)
            if args.restart_agent and paths.root == default_wallpaper_root().resolve():
                restart_wallpaper_agent()
            print(f"Applied wallpaper asset {args.asset_id}")
            return 0

        if args.command == "list":
            for item in list_custom_assets(resolve_paths(args.wallpaper_root)):
                print(json.dumps(item, ensure_ascii=False))
            return 0

        raise WallpaperApplyError(f"Unknown command: {args.command}")
    except WallpaperApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
