import argparse
import json
import logging
import sys
from pathlib import Path

from publisher import run as run_publish

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("publish-cli")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Publish a dated upload folder to YouTube and Instagram.")
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD (maps to uploads/YYYY/MM/DD)")
    parser.add_argument(
        "--file",
        required=False,
        default=None,
        help="Optional explicit path to the MP4 to publish (overrides folder detection).",
    )
    args = parser.parse_args(argv)

    try:
        result = run_publish(args.date, args.file)
        print(json.dumps(
            {
                "date": result.date,
                "video_file": result.video_file,
                "youtube": result.youtube,
                "gcs_public_url": result.gcs_public_url,
                "instagram": result.instagram,
            },
            indent=2,
        ))
        return 0
    except Exception as exc:
        logger.exception("Publish failed: %s", exc)
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


