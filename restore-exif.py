import argparse
import logging
import os
import re
from datetime import datetime, timedelta
from collections import Counter
import subprocess
from dotenv import load_dotenv
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import sys
import signal
import colorlog

executor = None  # Global ThreadPoolExecutor reference

handler = colorlog.StreamHandler(stream=sys.stdout)
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(name)-12s %(levelname)-8s%(reset)s %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
)

logger = colorlog.getLogger("restore-exif")
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def normalize_extension(ext):
    """Normalize file extension to lowercase and ensure it starts with a dot"""
    if not ext.startswith("."):
        ext = "." + ext
    return ext.lower()


def create_extensions_set(*extensions):
    """Create a set of extensions"""
    return {ext if ext.startswith(".") else "." + ext for ext in extensions}


PHOTO_EXTENSIONS = create_extensions_set("jpg", "jpeg", "png", "gif", "webp")
VIDEO_EXTENSIONS = create_extensions_set(
    "mp4", "3gp", "mov", "avi", "mkv", "flv", "wmv", "webm"
)

SUPPORTED_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

img_filename_regex = re.compile(r"(IMG-\d{8}-WA\d{4}|IMG_\d{8}_\d{6}(_\d{3})?)\..+")
vid_filename_regex = re.compile(r"VID-\d{8}-WA\d{4}\..+")


def get_datetime(filename):
    """Extract datetime from various image filename formats"""
    # Liste des patterns connus avec leurs formats de date associés
    patterns = [
        # Format WhatsApp: IMG-YYYYMMDD-WAXXXX
        (r"IMG-(\d{8})-WA\d{4}", "%Y%m%d"),
        # Format Google Photos: IMG_YYYYMMDD_HHMMSS(_XXX)
        (r"IMG_(\d{8})_(\d{6})(?:_\d{3})?", "%Y%m%d%H%M%S", 2),
        # Format mms: mms_YYYYMMDD_HHMMSS
        (r"mms_(\d{8})_(\d{6})", "%Y%m%d%H%M%S", 2),
        # Format Resized avec date: Resized_YYYYMMDD_HHMMSS
        (r"Resized_(\d{8})_(\d{6})", "%Y%m%d%H%M%S", 2),
        # Format Screenshot: Screenshot_YYYYMMDD-HHMMSS
        (r"Screenshot_(\d{8})-(\d{6})", "%Y%m%d%H%M%S", 2),
        # Format ClumsyNinja: ClumsyNinja_DDMMYYYY_HHMMSS
        (r"ClumsyNinja_(\d{8})_(\d{6})", "%d%m%Y%H%M%S", 2),
        # Format Screenshot with dash: Screenshot_YYYY-MM-DD-HH-MM-SS
        (r"Screenshot_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", "%Y-%m-%d-%H-%M-%S"),
    ]

    base_filename = os.path.splitext(filename)[0]

    for pattern, date_format, *groups in patterns:
        match = re.search(pattern, base_filename)
        if match:
            if len(groups) > 0 and groups[0] == 2:
                # Format avec date ET heure
                date_str = match.group(1)
                time_str = match.group(2)
                datetime_str = f"{date_str}{time_str}"
            else:
                # Format avec date uniquement
                datetime_str = match.group(1)
                if len(datetime_str) == 8:  # YYYYMMDD
                    datetime_str += "000000"  # Ajouter 00:00:00 comme heure par défaut

            try:
                return datetime.strptime(datetime_str, date_format)
            except ValueError:
                continue

    # Si aucun pattern ne correspond, chercher une date générique dans le nom
    generic_date_pattern = r"(\d{8})[_-]?(\d{6})?"
    match = re.search(generic_date_pattern, base_filename)
    if match:
        date_str = match.group(1)
        time_str = match.group(2) if match.group(2) else "000000"
        try:
            return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
        except ValueError:
            pass

    raise ValueError(f"Unsupported filename format: {filename}")


def get_exif_datestr(filename):
    return get_datetime(filename).strftime("%Y:%m:%d %H:%M:%S")


def get_filepaths(path, recursive):
    all_filepaths = []
    if not recursive:
        abspath = os.path.abspath(path)
        all_filepaths += [
            (abspath, f)
            for f in os.listdir(abspath)
            if os.path.isfile(os.path.join(abspath, f))
        ]
    else:
        for dirpath, dirnames, filenames in os.walk(path):
            abspath = os.path.abspath(dirpath)
            all_filepaths += [(abspath, f) for f in filenames]
    return all_filepaths


def filter_filepaths(filepaths, allowed_ext):
    """Filter filepaths based on their extensions (case insensitive)"""
    return [
        (fp, fn)
        for fp, fn in filepaths
        if normalize_extension(os.path.splitext(fn)[-1]) in allowed_ext
    ]


def is_whatsapp_img(filename):
    # Look for the basic WhatsApp pattern, ignore anything after
    return bool(re.search(r"IMG-\d{8}-WA\d{4}", filename))


def is_whatsapp_vid(filename):
    return bool(vid_filename_regex.match(filename))


def refresh_asset_metadata(api, asset_id):
    # Récupérer les variables d'environnement
    IMMICH_SERVER_URL = os.getenv("IMMICH_SERVER_URL")
    IMMICH_API_KEY = os.getenv(api)
    if not IMMICH_SERVER_URL or not IMMICH_API_KEY:
        logger.error(f"Missing IMMICH_SERVER_URL or {api}")
        return False

    headers = {"x-api-key": IMMICH_API_KEY, "content-type": "application/json"}

    # Endpoint pour mettre à jour un asset
    url = f"{IMMICH_SERVER_URL}/api/assets/jobs"
    body = {"assetIds": [asset_id], "name": "refresh-metadata"}

    # Effectuer la requête PUT pour rafraîchir les métadonnées
    response = requests.post(url, headers=headers, json=body)
    if response.status_code != 204:
        logger.warning(f"Error refreshing asset metadata: {response.text}")
    return response.status_code == 204


def get_asset_by_path(api, file_path):
    IMMICH_SERVER_URL = os.getenv("IMMICH_SERVER_URL")
    IMMICH_API_KEY = os.getenv(api)
    if not IMMICH_SERVER_URL or not IMMICH_API_KEY:
        logger.error(f"Missing IMMICH_SERVER_URL or {api}")
        return
    headers = {"x-api-key": IMMICH_API_KEY, "accept": "application/json"}

    # Endpoint pour rechercher un asset par son chemin
    url = f"{IMMICH_SERVER_URL}/api/search/metadata"
    body = {"originalFileName": file_path}

    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 200:
        json = response.json()
        assets = json.get("assets")
        if assets:
            items = assets.get("items")
            if items:
                return items[0].get("id")
    logger.warning(f"Error fetching asset for file: {response.text}")
    return None


def trigger_asset_refresh(path, relative_path):
    filename = os.path.basename(path)
    username = path.split(relative_path)[-1].split("/")[1]
    username = username.upper()
    key = f"IMMICH_API_KEY_{username}"

    assetid = get_asset_by_path(key, filename)

    if assetid is None:
        logger.warning(f"Asset not found for file: {filename}")
        return

    if not refresh_asset_metadata(key, assetid):
        logger.warning(f"Error refreshing asset metadata for file: {filename}")
        return


def repair_video(filepath):
    """
    Tente de réparer une vidéo corrompue en utilisant ffmpeg
    Retourne True si la réparation a réussi, False sinon
    """
    ext = normalize_extension(os.path.splitext(filepath)[1])
    if ext not in VIDEO_EXTENSIONS:
        return False

    logger.info(f"\tTrying to repair corrupted video: {os.path.basename(filepath)}")
    temp_file = f"{filepath}.temp.mp4"
    repair_cmd = f'ffmpeg -i "{filepath}" -c copy "{temp_file}"'

    try:
        subprocess.run(
            repair_cmd,
            shell=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.replace(temp_file, filepath)
        logger.info(f"\tVideo repaired successfully")
        return True
    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        logger.warning(f"\tFailed to repair video: {str(e)}")
        return False


def has_already_creation_date(filepath):
    """
    Check if the file already has a creation date in its metadata
    Returns (bool, str):
        - True if any of DateTimeOriginal, MediaCreateDate, or CreateDate exists
        - Error message if video file is corrupted, None otherwise
    """

    # Check for any of the three date fields
    cmd = f'exiftool -DateTimeOriginal -MediaCreateDate -CreateDate "{filepath}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    # Check if video file is corrupted
    if result.returncode != 0 and (
        "Truncated" in result.stderr or "Invalid atom size" in result.stderr
    ):
        return False, "corrupted"

    if result.returncode != 0:
        logger.warning(f"Error fetching asset for file: {filepath}: {result}")
        return False, None

    return bool(result.stdout.strip()), None


def is_google_photos_img(filename):
    """Check if filename matches Google Photos format"""
    return bool(re.search(r"IMG_\d{8}_\d{6}(?:_\d{3})?\..+", filename))


def get_french_timezone_offset(date):
    """
    Determine if the given date is in summer time (+02:00) or winter time (+01:00) in France
    """
    # En France, l'heure d'été commence le dernier dimanche de mars à 2h
    # et se termine le dernier dimanche d'octobre à 3h
    year = date.year

    # Trouver le dernier dimanche de mars
    march_end = datetime(year, 3, 31)
    while march_end.weekday() != 6:  # 6 = dimanche
        march_end = march_end - timedelta(days=1)

    # Trouver le dernier dimanche d'octobre
    october_end = datetime(year, 10, 31)
    while october_end.weekday() != 6:
        october_end = october_end - timedelta(days=1)

    # Si la date est entre ces deux dates, c'est l'heure d'été
    if march_end <= date < october_end:
        return "+02:00"
    return "+01:00"


def has_timezone(filepath):
    """Check if the file already has timezone information in its metadata"""
    cmd = f'exiv2 -pt "{filepath}" | grep "Exif.Photo.OffsetTime"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip())


want_stop = False


def process_file(filepath, filename, force, dry_run, relative_path, progress_info=None):
    """Process a single file (image or video) with all metadata operations"""
    try:
        ext = normalize_extension(os.path.splitext(filename)[1])

        if ext in VIDEO_EXTENSIONS:
            has_date, error = has_already_creation_date(filepath)
            if error == "corrupted":
                if dry_run:
                    logger.info(f"Would repair video: {filepath}")
                    with open("dry-run.txt", "a") as f:
                        f.write(f"{filepath}\n")
                    return "videos_modified", filepath
                if not repair_video(filepath):
                    return "videos_error", None
                has_date, _ = has_already_creation_date(filepath)

            if has_date:
                return "videos_skipped", None

        elif ext in PHOTO_EXTENSIONS:
            has_date, _ = has_already_creation_date(filepath)
            if has_date:
                return "images_skipped", None

        else:
            logger.warning(f"Unsupported file extension: {ext}")
            return "unsupported_extension", None

        date = get_datetime(filename)
        date_str = date.strftime("%Y:%m:%d %H:%M:%S")
        timezone_offset = get_french_timezone_offset(date)

        if dry_run:
            logger.info(
                f"Processing file: {filepath} - DRY RUN - Would update date to: {date_str} {timezone_offset if is_whatsapp_img(filename) else ''}"
            )
            with open("dry-run.txt", "a") as f:
                f.write(f"{filepath}\n")
            return (
                "videos_modified" if ext in VIDEO_EXTENSIONS else "images_modified",
                None,
            )

        if ext in VIDEO_EXTENSIONS:
            # Keep using exiftool for videos
            base_cmd = (
                f'"-DateTimeOriginal={date_str}" '
                f'"-CreateDate={date_str}" '
                f'"-ModifyDate={date_str}" '
            )
            cmd = f'exiftool -q -m -overwrite_original {base_cmd} "{filepath}"'
        else:
            # Use exiv2 for all images (including WebP)
            base_cmd = (
                f'-M"set Exif.Image.DateTime {date_str}" '
                f'-M"set Exif.Photo.DateTimeOriginal {date_str}" '
                f'-M"set Exif.Photo.DateTimeDigitized {date_str}" '
            )

            has_tz = has_timezone(filepath)
            if not has_tz:
                base_cmd += f'-M"set Exif.Photo.OffsetTime {timezone_offset}" '

            cmd = f'exiv2 {base_cmd} "{filepath}"'

        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL)
        logger.info(
            f"Processing file: {filepath} - Updated date to: {date_str} {timezone_offset if is_whatsapp_img(filename) else ''}"
        )

        return (
            "videos_modified" if ext in VIDEO_EXTENSIONS else "images_modified",
            filepath,
        )

    except Exception as e:
        logger.warning(f"Error processing file {filename}: {str(e)}")
        return "videos_error" if ext in VIDEO_EXTENSIONS else "images_error", None


def signal_handler(signum, frame):
    global want_stop
    logger.info("\nInterrupt received, stopping gracefully...")
    want_stop = True
    if executor:
        logger.info("Shutting down thread pool...")
        executor.shutdown(wait=False, cancel_futures=True)  # Python 3.9+ only
    # sys.exit(0)


def main(path, recursive, mod, force, dry_run, threads):
    global executor  # Use the global executor
    # Set up signal handler for graceful interruption
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Validating arguments")
    if not os.path.exists(path):
        raise FileNotFoundError("Path specified does not exist")

    if not os.path.isdir(path):
        raise TypeError("Path specified is not a directory")

    # Reset dry-run.txt file if in dry-run mode
    if dry_run:
        with open("dry-run.txt", "w") as f:
            f.write("")  # Clear file contents
        logger.info("DRY RUN MODE - No files will be modified")

    relative_path = path
    logger.info("Listing files in target directory")
    filepaths = get_filepaths(path, recursive)
    filepaths = filter_filepaths(filepaths, allowed_ext=SUPPORTED_EXTENSIONS)
    num_files = len(filepaths)

    num_threads = threads if threads else min(os.cpu_count() * 2, 8)
    logger.info(f"Processing {num_files} files using {num_threads} threads")

    counter = Counter()
    files_to_refresh = []
    processed_count = 0

    process_func = partial(
        process_file, force=force, dry_run=dry_run, relative_path=relative_path
    )

    with ThreadPoolExecutor(max_workers=num_threads) as exec:
        executor = exec  # Store reference to executor globally
        try:
            future_to_file = {
                executor.submit(
                    process_func,
                    os.path.join(path, filename),
                    filename,
                    progress_info=(i + 1, num_files),
                ): (path, filename)
                for i, (path, filename) in enumerate(filepaths)
            }

            for future in as_completed(future_to_file):
                if want_stop:
                    break
                path, filename = future_to_file[future]
                try:
                    result_type, filepath = future.result()
                    counter[result_type] += 1
                    processed_count += 1

                    if filepath:
                        files_to_refresh.append(filepath)

                    # Ajouter un log tous les 100 fichiers
                    if processed_count % 100 == 0:
                        logger.info(
                            f"Processed {processed_count}/{num_files} files. Current counts: {dict(counter)}"
                        )

                except Exception as e:
                    logger.error(f"Error processing {filename}: {str(e)}")
                    counter["error"] += 1
                    processed_count += 1

            if want_stop:
                exit()

            if len(files_to_refresh) > 0 and not dry_run:
                logger.info("Waiting for 5 seconds before refreshing asset metadata")
                time.sleep(5)
                num_files = len(files_to_refresh)
                progress_digits = len(str(num_files))
                processed_count = 0

                # Utiliser ThreadPoolExecutor pour le rafraîchissement des métadonnées
                with ThreadPoolExecutor(max_workers=num_threads) as executor:
                    future_to_file = {
                        executor.submit(
                            trigger_asset_refresh, filepath, relative_path
                        ): filepath
                        for filepath in files_to_refresh
                    }

                    for future in as_completed(future_to_file):
                        filepath = future_to_file[future]
                        processed_count += 1
                        try:
                            future.result()
                            logger.info(
                                f"{processed_count:>{progress_digits}}/{num_files} Refreshed asset metadata for file: {os.path.basename(filepath)}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Error refreshing metadata for {os.path.basename(filepath)}: {str(e)}"
                            )

            print("")
            logger.info("Processing summary:")
            for category, count in counter.items():
                logger.info(f"{category}: {count}")

            logger.info("Finished processing files")

        finally:
            executor = None  # Clear the global reference


if __name__ == "__main__":
    # Set up signal handler at program start
    signal.signal(signal.SIGINT, signal_handler)

    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Restore discarded Exif date information in WhatsApp media based on the filename. "
            "For videos, only the created and modified dates are set."
        )
    )
    parser.add_argument("path", type=str, help="Path to WhatsApp media folder")
    parser.add_argument(
        "-r",
        "--recursive",
        default=False,
        action="store_true",
        help="Recursively process media",
    )
    parser.add_argument(
        "-m",
        "--mod",
        default=False,
        action="store_true",
        help="Set file created/modified date on top of exif for images",
    )
    parser.add_argument(
        "-f",
        "--force",
        default=False,
        action="store_true",
        help="Overwrite existing exif date",
    )
    parser.add_argument(
        "--dry-run",
        default=False,
        action="store_true",
        help="Show what would be done without actually modifying files",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=None,
        help="Number of threads to use for processing (default: min(CPU_COUNT * 2, 8))",
    )
    args = parser.parse_args()

    # print(has_already_creation_date("/media/dorian/2020/12/IMG_20201209_111448_1.jpg"))
    # process_file(
    #     "/media/dorian/2020/12/IMG_20201209_111448_1.jpg",
    #     "IMG_20201209_111448_1.jpg",
    #     False,
    #     True,
    #     "/media",
    # )
    # exit()

    main(
        args.path,
        recursive=args.recursive,
        mod=args.mod,
        force=args.force,
        dry_run=args.dry_run,
        threads=args.threads,
    )
