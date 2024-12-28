import argparse
import logging
import os
import re
from datetime import datetime
from collections import Counter
import subprocess
from dotenv import load_dotenv
import requests
import piexif
import time

img_filename_regex = re.compile(r'IMG-\d{8}-WA\d{4}\..+')
vid_filename_regex = re.compile(r'VID-\d{8}-WA\d{4}\..+')

def get_datetime(filename):
    date_str = filename.split('-')[1]
    return datetime.strptime(date_str, '%Y%m%d')


def get_exif_datestr(filename):
    return get_datetime(filename).strftime("%Y:%m:%d %H:%M:%S")


def get_filepaths(path, recursive):
    all_filepaths = []
    if not recursive:
        abspath = os.path.abspath(path)
        all_filepaths += [(abspath, f) for f in os.listdir(abspath)
                          if os.path.isfile(os.path.join(abspath, f))]
    else:
        for dirpath, dirnames, filenames in os.walk(path):
            abspath = os.path.abspath(dirpath)
            all_filepaths += [(abspath, f) for f in filenames]
    return all_filepaths


def filter_filepaths(filepaths, allowed_ext):
    return [(fp, fn) for fp, fn in filepaths if os.path.splitext(fn)[-1] in allowed_ext]


def make_new_exif(filename):
    exif_dict = {
        'Exif': {piexif.ExifIFD.DateTimeOriginal: get_exif_datestr(filename)}}
    return piexif.dump(exif_dict)


def is_whatsapp_img(filename):
    return bool(img_filename_regex.match(filename))


def is_whatsapp_vid(filename):
    return bool(vid_filename_regex.match(filename))

def refresh_asset_metadata(api, asset_id):
    # Récupérer les variables d'environnement
    IMMICH_SERVER_URL = os.getenv('IMMICH_SERVER_URL')
    IMMICH_API_KEY = os.getenv(api)
    if not IMMICH_SERVER_URL or not IMMICH_API_KEY:
        logger.error(f'Missing IMMICH_SERVER_URL or {api}')
        return False
    
    headers = {
        "x-api-key": IMMICH_API_KEY,
        "content-type": "application/json"
    }
    
    # Endpoint pour mettre à jour un asset
    url = f"{IMMICH_SERVER_URL}/api/assets/jobs"
    body = {
        "assetIds": [asset_id],
        "name": "refresh-metadata"
    }
    
    # Effectuer la requête PUT pour rafraîchir les métadonnées
    response = requests.post(url, headers=headers, json=body)
    if response.status_code != 204:
        logger.warning(f'Error refreshing asset metadata: {response.text}')
    return response.status_code == 204

def get_asset_by_path(api, file_path):
    IMMICH_SERVER_URL = os.getenv('IMMICH_SERVER_URL')
    IMMICH_API_KEY = os.getenv(api)
    if not IMMICH_SERVER_URL or not IMMICH_API_KEY:
        logger.error(f'Missing IMMICH_SERVER_URL or {api}')
        return
    headers = {
        "x-api-key": IMMICH_API_KEY,
        "accept": "application/json"
    }
    
    # Endpoint pour rechercher un asset par son chemin
    url = f"{IMMICH_SERVER_URL}/api/search/metadata"
    body = {
        "originalFileName": file_path
    }
    
    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 200:
        json = response.json()
        assets = json.get('assets')
        if assets:
            items = assets.get('items')
            if items:
                return items[0].get('id')
    logger.warning(f'Error fetching asset for file: {response.text}')
    return None

def trigger_asset_refresh(path, relative_path):  
    filename = os.path.basename(path)
    username =  path.split(relative_path)[-1].split("/")[1]
    username = username.upper()
    key = f'IMMICH_API_KEY_{username}'
    
    assetid = get_asset_by_path(key, filename)
    
    if assetid is None:
        logger.warning(f'Asset not found for file: {filename}')
        return
    
    if not refresh_asset_metadata(key, assetid):
        logger.warning(f'Error refreshing asset metadata for file: {filename}')
        return

def repair_video(filepath):
    """
    Tente de réparer une vidéo corrompue en utilisant ffmpeg
    Retourne True si la réparation a réussi, False sinon
    """
    logger.info(f'\tTrying to repair corrupted video: {os.path.basename(filepath)}')
    temp_file = f"{filepath}.temp.mp4"
    repair_cmd = f'ffmpeg -i "{filepath}" -c copy "{temp_file}"'
    
    try:
        subprocess.run(repair_cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(temp_file, filepath)
        logger.info(f'\tVideo repaired successfully')
        return True
    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        logger.warning(f'\tFailed to repair video: {str(e)}')
        return False

def main(path, recursive, mod, force):
    logger.info('Validating arguments')
    if not os.path.exists(path):
        raise FileNotFoundError('Path specified does not exist')

    if not os.path.isdir(path):
        raise TypeError('Path specified is not a directory')

    relative_path = path
    logger.info('Listing files in target directory')
    filepaths = get_filepaths(path, recursive)
    logger.info(f'Total files: {len(filepaths)}')

    allowed_extensions = set(['.mp4', '.jpg', '.3gp', '.jpeg'])
    logger.info(f'Filtering for valid file extensions: {allowed_extensions}')
    filepaths = filter_filepaths(filepaths, allowed_ext=allowed_extensions)
    num_files = len(filepaths)
    logger.info(f'Valid files: {num_files}')

    logger.info('Begin processing files')
    abspath = os.path.abspath(path)
    progress_digits = len(str(num_files))
    abspath_len = len(abspath) + 1
    
    counter = Counter()
    
    files_to_refresh = []
    for i, (path, filename) in enumerate(filepaths):
        
        if i % 100 == 0:
            logger.info(f'{i + 1:>{progress_digits}}/{num_files}')
            
        filepath = os.path.join(path, filename)            
        if filename.endswith('.mp4') or filename.endswith('.3gp'):
            if not is_whatsapp_vid(filename):
                counter['videos_skipped'] += 1
                continue

            # Vérifier si DateTimeOriginal existe déjà
            cmd = f'exiftool -DateTimeOriginal "{filepath}"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0 and ("Truncated" in result.stderr or "Invalid atom size" in result.stderr):
                if not repair_video(filepath):
                    counter['videos_error'] += 1
                    continue
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode == 0 and result.stdout.strip() and not force:
                counter['videos_skipped'] += 1
                continue

            try:
                logger.info(f'{i + 1:>{progress_digits}}/{num_files} Processing video file: {path}/{filename}')
                date = get_datetime(filename)
                modTime = date.timestamp()
                
                date_str = date.strftime("%Y:%m:%d %H:%M:%S")
                cmd = f'exiftool -q -m -overwrite_original "-AllDates={date_str}" "{filepath}"'
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                # Si exiftool échoue, tenter de réparer la vidéo
                if result.returncode != 0 and ("Truncated" in result.stderr or "Invalid atom size" in result.stderr):
                    if repair_video(filepath):
                        # Réessayer d'appliquer les métadonnées
                        cmd = f'exiftool -q -m -overwrite_original "-AllDates={date_str}" "{filepath}"'
                        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL)
                    else:
                        raise Exception("Failed to repair video")

                logger.info(f'\tUpdated')
                files_to_refresh.append(filepath)
                counter['videos_modified'] += 1
                
            except Exception as e:
                logger.warning(f'Error processing video file: {filename}')
                counter['videos_error'] += 1

        elif filename.endswith('.jpg') or filename.endswith('.jpeg'):
            if not is_whatsapp_img(filename):
                # logger.warning('File is not a valid WhatsApp image, skipping')
                counter['images_skipped'] += 1
                continue
            
            try:
                exif_dict = piexif.load(filepath)
                if exif_dict['Exif'].get(piexif.ExifIFD.DateTimeOriginal) and not force:
                    # logger.info('Exif date already exists, skipping')
                    counter['images_skipped'] += 1
                    continue

                logger.info(f'{i + 1:>{progress_digits}}/{num_files} Processing image file: {path}/{filename}')
                exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = get_exif_datestr(filename)
                exif_bytes = piexif.dump(exif_dict)
                counter['images_modified'] += 1
            except piexif.InvalidImageDataError:
                logger.warning(f'Invalid image data, skipping')
                counter['images_error'] += 1
                continue
            except ValueError:
                logger.warning(f'Invalid exif, overwriting with new exif')
                exif_bytes = make_new_exif(filename)
                counter['images_modified'] += 1
                
            piexif.insert(exif_bytes, filepath)

            files_to_refresh.append(filepath)
            logger.info(f'\tUpdated')
    
    
    if len(files_to_refresh) > 0:
        logger.info('Waiting for 5 seconds before refreshing asset metadata')
        time.sleep(5)
        num_files = len(files_to_refresh)
        progress_digits = len(str(num_files))
        
        for i, filepath in enumerate(files_to_refresh):
            logger.info(f'{i + 1:>{progress_digits}}/{num_files} Refreshing asset metadata for file: {os.path.basename(filepath)}')
            trigger_asset_refresh(filepath, relative_path)
    
    print('')
    logger.info('Processing summary:')
    for category, count in counter.items():
        logger.info(f'{category}: {count}')

    logger.info('Finished processing files')


if __name__ == "__main__":
    load_dotenv()
    
    parser = argparse.ArgumentParser(
        description=('Restore discarded Exif date information in WhatsApp media based on the filename. '
                     'For videos, only the created and modified dates are set.'))
    parser.add_argument('path', type=str, help='Path to WhatsApp media folder')
    parser.add_argument('-r', '--recursive', default=False,
                        action='store_true', help='Recursively process media')
    parser.add_argument('-m', '--mod', default=False,
                        action='store_true', help='Set file created/modified date on top of exif for images')
    parser.add_argument('-f', '--force', default=False,
                        action='store_true', help='Overwrite existing exif date')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    logger = logging.getLogger('restore-exif')

    main(args.path, recursive=args.recursive, mod=args.mod, force=args.force)
