import argparse
import hashlib
import json
import threading
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .feedback import ValidationError
from .server import make_server
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Local nine-candidate fitness feedback UI")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / ".mutations"), help="Local database and media directory")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Start the WebUI (default)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="Run without opening a browser tab")
    ingest = sub.add_parser("import", help="Import nine candidates from a local JSON manifest")
    ingest.add_argument("manifest", type=Path)
    export = sub.add_parser("export", help="Write feedback JSON to standard output")
    export.add_argument("--include-demo", action="store_true")
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if args.command == "import":
            manifest = args.manifest.resolve()
            value = json.loads(manifest.read_text())
            if not isinstance(value, dict) or not isinstance(value.get("candidates"), list) or len(value["candidates"]) != 9:
                raise ValidationError("The manifest needs exactly nine candidates.")
            if any(not isinstance(c, dict) or not isinstance(c.get("file"), str)
                   for c in value["candidates"]):
                raise ValidationError("Each candidate needs a file path relative to the manifest.")
            # Validate existence before copying any media. Paths are explicit CLI inputs.
            paths = [(manifest.parent / c["file"]).resolve() for c in value["candidates"]]
            for path in paths:
                if not path.is_file():
                    raise ValidationError(f"Media not found: {path}")
            for candidate, path in zip(value["candidates"], paths):
                candidate["asset_id"] = store.add_asset(path.name, path.read_bytes())["asset_id"]
            generation = store.create_generation(value)
            print(json.dumps({"id": generation["id"], "title": generation["title"]}))
        elif args.command == "export":
            print(json.dumps(store.export(args.include_demo), indent=2))
        else:
            port = getattr(args, "port", 8765)
            if not 0 <= port <= 65535:
                raise ValidationError("The port must be between 0 and 65535.")
            open_browser = not getattr(args, "no_browser", False)
            # Reuse this workspace's existing process when the launcher is clicked twice.
            if port:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=.5) as response:
                        health = json.load(response)
                    if (isinstance(health, dict) and health.get("app") == "mutations-selection-room" and health.get("version") == 3
                            and health.get("workspace") == hashlib.sha256(str(store.root).encode()).hexdigest()):
                        print(f"Mutations is already running: http://127.0.0.1:{port}", flush=True)
                        if open_browser:
                            webbrowser.open(f"http://127.0.0.1:{port}")
                        return
                except (URLError, OSError, ValueError):
                    pass
            try:
                server = make_server(store, port)
            except OSError as exc:
                # An unrelated process can occupy the default port; select a free local port.
                import errno
                if port != 8765 or exc.errno != errno.EADDRINUSE:
                    raise
                server = make_server(store, 0)
            print(f"Mutations WebUI: http://127.0.0.1:{server.server_port}", flush=True)
            print(f"Data: {store.root}", flush=True)
            print("Generating nine new images locally. First launch downloads and caches the model automatically.", flush=True)
            browser_timer = None
            if open_browser:
                browser_timer = threading.Timer(.4, webbrowser.open, args=(f"http://127.0.0.1:{server.server_port}",))
                browser_timer.daemon = True
                browser_timer.start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                if browser_timer:
                    browser_timer.cancel()
                server.server_close()
    except (ValidationError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
