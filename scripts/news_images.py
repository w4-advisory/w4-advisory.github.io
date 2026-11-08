"""Validate and cache publisher previews; render a unique card for every story."""
import hashlib
import html
import io
import re
import textwrap
import warnings
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps, UnidentifiedImageError

from news_dedupe import story_id

Image.MAX_IMAGE_PIXELS = 25_000_000
LEGACY_FALLBACK = 'https://w4-advisory.github.io/og-image.png'
GENERIC = re.compile(r'news-story|twittercardlogo|logo-preview|Cybersecurity_News-SecurityWeek|CS_Logos_', re.I)


def local_path(value, root):
    if not isinstance(value, str) or not re.fullmatch(r'assets/news/[a-f0-9]{16,64}\.(webp|svg)', value):
        return None
    return root / value


def valid_asset(value, root):
    path = local_path(value, root)
    if not path or not path.is_file():
        return False
    try:
        if path.suffix == '.svg':
            svg = ET.fromstring(path.read_text())
            return svg.tag == '{http://www.w3.org/2000/svg}svg' and bool(svg.findall('.//{*}text'))
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.width >= 320 and image.height >= 160
    except (OSError, ValueError, ET.ParseError):
        return False


def headline_card(story):
    """An honest, deterministic editorial card, never a fake publisher photo."""
    title = re.sub(r'\s+', ' ', story['title']).strip()
    lines = textwrap.wrap(title, width=42, max_lines=5, placeholder='…')
    source = story.get('source', 'W4 Advisory')[:60]
    date = story.get('published', '')[:10]
    palette = ['#f16449', '#65c6b4', '#b3a1ed', '#7eb6f3', '#e1b562']
    color = palette[int(story_id(story)[:4], 16) % len(palette)]
    text = ''.join(f'<tspan x="70" y="{245 + i * 62}">{html.escape(line)}</tspan>' for i, line in enumerate(lines))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675" role="img" aria-labelledby="title">'
            f'<title id="title">{html.escape(title)}</title>'
            '<rect width="1200" height="675" fill="#142027"/>'
            f'<path d="M0 0H1200V18H0Z" fill="{color}"/>'
            f'<circle cx="1150" cy="80" r="245" fill="none" stroke="{color}" stroke-opacity=".12" stroke-width="80"/>'
            f'<text x="70" y="103" fill="{color}" font-size="26" font-family="monospace" letter-spacing="4">AI SECURITY / {html.escape(date)}</text>'
            f'<text fill="#f5f5ef" font-size="48" font-weight="600" font-family="Arial,sans-serif">{text}</text>'
            '<path d="M70 568H1130" stroke="#425057"/>'
            f'<text x="70" y="618" fill="#b7c5cd" font-size="25" font-family="Arial,sans-serif">{html.escape(source)}</text>'
            '<text x="1130" y="618" text-anchor="end" fill="#b7c5cd" font-size="23" font-family="Arial,sans-serif">W4 ADVISORY</text></svg>')


def store_bytes(data, suffix, root):
    name = hashlib.sha256(data).hexdigest()[:24] + suffix
    path = root / 'assets' / 'news' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return path.relative_to(root).as_posix()


def cache_publisher_image(url, root, fetch):
    if not url or urlsplit(url).scheme != 'https' or url == LEGACY_FALLBACK or GENERIC.search(url):
        return None
    raw, final, headers = fetch(url, 8_000_000)
    if urlsplit(final).scheme != 'https' or not headers.get('Content-Type', '').lower().startswith('image/'):
        raise ValueError('Preview response is not an HTTPS image')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.width < 320 or image.height < 160:
                raise ValueError('Preview is too small')
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((960, 640))
            output = io.BytesIO()
            image.save(output, format='WEBP', quality=85, method=4)
    return store_bytes(output.getvalue(), '.webp', root)


def ensure_preview(story, root, fetch):
    card = store_bytes(headline_card(story).encode(), '.svg', root)
    story['image_fallback'] = card
    if story.get('image_kind') == 'publisher' and not GENERIC.search(story.get('image_origin', '')) and valid_asset(story.get('image'), root):
        return
    candidates = list(dict.fromkeys(filter(None, [story.get('image_origin'), story.get('image')] + story.pop('_images', []))))
    for url in candidates:
        try:
            cached = cache_publisher_image(url, root, fetch)
            if cached:
                story.update(image=cached, image_kind='publisher', image_origin=url)
                return
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            print(f"Rejected preview for {story.get('source', 'article')}: {exc}")
    story.update(image=card, image_kind='headline')


def validate_previews(stories, root):
    for story in stories:
        for field in ('image', 'image_fallback'):
            if not valid_asset(story.get(field), root):
                raise ValueError(f"Missing/invalid {field}: {story['title']}")
        if story.get('image_kind') not in {'publisher', 'headline'}:
            raise ValueError(f"Unclassified preview: {story['title']}")


def remove_unused_assets(stories, root):
    used = {s.get(field) for s in stories for field in ('image', 'image_fallback')}
    for path in (root / 'assets' / 'news').glob('*'):
        name = path.relative_to(root).as_posix()
        if local_path(name, root) and name not in used:
            path.unlink()
