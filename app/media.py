from __future__ import annotations

import io
import os
import secrets
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .db import IS_VERCEL, UPLOADS

# Vercel server uploads have a 4.5 MB request-body cap. We keep individual
# processed forms comfortably below that ceiling. Upload one large source image
# at a time from the owner studio when hosted on Vercel.
MAX_IMAGE_SIZE = (3 * 1024 * 1024) if IS_VERCEL else (8 * 1024 * 1024)
Image.MAX_IMAGE_PIXELS = 25_000_000


def _encode_webp(raw: bytes, filename: str) -> tuple[bytes, str]:
    if not raw or len(raw) > MAX_IMAGE_SIZE:
        mb = MAX_IMAGE_SIZE // (1024 * 1024)
        raise ValueError(f'Each photograph must be a JPG, PNG, or WebP file smaller than {mb} MB.')
    if Path(filename).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
        raise ValueError('Use JPG, PNG, or WebP images. SVG and other file types are not accepted.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as check:
                if check.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise ValueError('This is not a supported image.')
                check.verify()
            with Image.open(io.BytesIO(raw)) as original:
                im = ImageOps.exif_transpose(original)
                if im.width < 64 or im.height < 64:
                    raise ValueError('Please upload an image at least 64 pixels wide and high.')
                im.thumbnail((2400, 2400))
                im = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
                name = secrets.token_hex(20) + '.webp'
                output = io.BytesIO()
                im.save(output, format='WEBP', quality=90, method=4)
                return output.getvalue(), name
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('The image is unreadable or exceeds the 25-megapixel safety limit.') from exc


def save_image(raw: bytes, filename: str) -> str:
    data, name = _encode_webp(raw, filename)
    token = os.environ.get('BLOB_READ_WRITE_TOKEN', '').strip()
    if token:
        try:
            from vercel.blob import BlobClient
            with BlobClient(token=token) as client:
                blob = client.put(
                    f'jaghvi/{name}', data,
                    access='public',
                    content_type='image/webp',
                    add_random_suffix=False,
                    cache_control_max_age=31536000,
                )
            return blob.url
        except Exception as exc:
            raise ValueError('The photograph could not be stored. Check the connected Vercel Blob store and try again.') from exc

    if IS_VERCEL:
        raise ValueError('Image storage is not connected. Create a Public Vercel Blob store for this project first.')

    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / name).write_bytes(data)
    return '/media/' + name


def delete_image(path: str):
    token = os.environ.get('BLOB_READ_WRITE_TOKEN', '').strip()
    if path.startswith('https://') and '.blob.vercel-storage.com/' in path and token:
        try:
            from vercel.blob import BlobClient
            with BlobClient(token=token) as client:
                client.delete(path)
        except Exception:
            # Deletion failures should not prevent the owner from saving a product.
            pass
        return
    if path.startswith('/media/'):
        name = path.removeprefix('/media/')
        if '/' not in name and '\\' not in name:
            (UPLOADS / name).unlink(missing_ok=True)
