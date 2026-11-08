window.W4NewsPreview = {
  media(story) {
    const wrap = document.createElement('span');
    wrap.className = 'news-preview-wrap';
    const inlineCard = () => {
      const ns = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(ns, 'svg');
      svg.setAttribute('viewBox', '0 0 480 270');
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', story.title || 'AI security news');
      svg.classList.add('news-preview');
      const background = document.createElementNS(ns, 'rect');
      background.setAttribute('width', '480');
      background.setAttribute('height', '270');
      background.setAttribute('fill', '#142027');
      const source = document.createElementNS(ns, 'text');
      source.setAttribute('x', '24');
      source.setAttribute('y', '38');
      source.setAttribute('fill', '#f16449');
      source.setAttribute('font-size', '15');
      source.setAttribute('font-family', 'sans-serif');
      source.textContent = (story.source || 'AI SECURITY').slice(0, 48);
      const title = document.createElementNS(ns, 'text');
      title.setAttribute('fill', '#f5f5ef');
      title.setAttribute('font-size', '24');
      title.setAttribute('font-family', 'sans-serif');
      const words = (story.title || 'AI security news').split(/\s+/);
      const lines = [''];
      words.forEach(word => {
        if ((lines[lines.length - 1] + word).length > 31) lines.push('');
        lines[lines.length - 1] += word + ' ';
      });
      lines.slice(0, 5).forEach((line, i) => {
        const span = document.createElementNS(ns, 'tspan');
        span.setAttribute('x', '24');
        span.setAttribute('y', String(86 + i * 33));
        span.textContent = line.trim() + (i === 4 && lines.length > 5 ? '…' : '');
        title.append(span);
      });
      svg.append(background, source, title);
      wrap.replaceChildren(svg);
    };
    const safeUrl = value => {
      if (!value) throw new Error('Missing image');
      const url = new URL(value, document.baseURI);
      if (url.origin === location.origin && /^\/assets\/news\/[a-f0-9]+\.(webp|svg)$/.test(url.pathname)) return url.href;
      if (url.protocol === 'https:') return url.href; // Older cached news.json during deployment.
      throw new Error('Invalid image');
    };
    const show = (value, failure) => {
      try {
        const img = document.createElement('img');
        img.className = 'news-preview';
        img.alt = '';
        img.loading = 'lazy';
        img.decoding = 'async';
        img.width = 480;
        img.height = 270;
        img.onerror = failure;
        img.src = safeUrl(value);
        wrap.replaceChildren(img);
      } catch (_) { failure(); }
    };
    show(story.image, () => show(story.image_fallback, inlineCard));
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
