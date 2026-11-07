window.W4NewsPreview = {
  media(story) {
    const wrap = document.createElement('span');
    wrap.className = 'news-preview-wrap';
    const finalFallback = () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 160 90');
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', 'W4 Advisory preview image');
      svg.classList.add('news-preview');
      const background = document.createElementNS(svg.namespaceURI, 'rect');
      background.setAttribute('width', '160');
      background.setAttribute('height', '90');
      background.setAttribute('fill', '#142436');
      const label = document.createElementNS(svg.namespaceURI, 'text');
      label.setAttribute('x', '12');
      label.setAttribute('y', '52');
      label.setAttribute('fill', '#f3f0e9');
      label.setAttribute('font-size', '18');
      label.setAttribute('font-family', 'sans-serif');
      label.textContent = 'W4 Advisory';
      svg.append(background, label);
      wrap.replaceChildren(svg);
    };
    const fallback = () => {
      const img = document.createElement('img');
      img.className = 'news-preview';
      img.src = '/og-image.png';
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.onerror = finalFallback;
      wrap.replaceChildren(img);
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
