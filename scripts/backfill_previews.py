"""Create small grid previews for already generated local images."""

import io
import json

from PIL import Image

from mutations_ui.storage import Store


def main():
    store = Store(".mutations")
    with store.connect() as db:
        rows = db.execute("SELECT id, payload FROM generations").fetchall()
    count = 0
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("demo", {}).get("mode") != "local-diffusion" or payload.get("generation", {}).get("status") != "ready":
            continue
        for candidate in payload["candidates"]:
            media = candidate.get("media")
            if not media or media.get("preview_url") or media.get("mime") != "image/png":
                continue
            source = store.assets / media["url"].removeprefix("/media/")
            if not source.is_file():
                continue
            with Image.open(source) as image:
                thumbnail = image.copy()
            thumbnail.thumbnail((320, 320))
            output = io.BytesIO()
            thumbnail.convert("RGB").save(output, format="JPEG", quality=65, optimize=True)
            media["preview_url"] = store.add_asset("preview.jpg", output.getvalue())["url"]
            count += 1
        with store.connect() as db:
            db.execute("UPDATE generations SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    print(f"Created {count} previews")


if __name__ == "__main__":
    main()
