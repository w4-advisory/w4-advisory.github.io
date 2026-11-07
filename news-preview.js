window.W4NewsPreview = {
  media(story) {
    const wrap = document.createElement('span');
    wrap.className = 'news-preview-wrap';
    const fallback = () => {
      const tile = document.createElement('span');
      tile.className = 'news-preview news-preview-fallback';
      tile.textContent = story.source || 'AI security';
      wrap.replaceChildren(tile);
    };
    try {
      const url = new URL(story.image);
      if (url.protocol !== 'https:') throw new Error('Invalid image URL');
      const img = document.createElement('img');
      img.className = 'news-preview';
      img.src = url.href;
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.onerror = fallback;
      wrap.append(img);
    } catch (_) { fallback(); }
    return wrap;
  },
  summary(story) {
    if (!story.summary) return null;
    const p = document.createElement('p');
    p.className = 'news-summary';
    p.textContent = story.summary;
    return p;
  }
};
