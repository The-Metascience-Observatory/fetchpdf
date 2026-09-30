"""JCI supplements linked from an article whose DOI is verified on the page."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from .supplement_index import ROLE_SUPPLEMENT, SupplementFile

class _Links(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links=[]; self.dois=[]; self.feed(text)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='a' and a.get('href'): self.links.append(a['href'])
        if tag=='meta' and a.get('name','').lower()=='citation_doi':
            self.dois.append(a.get('content','').strip().lower())

def enumerate_jci(ids, ctx):
    match=re.fullmatch(r'10\.1172/jci(\d+)',str(ids.doi or ''),re.I)
    if not match:return []
    number=match.group(1);base=f'https://www.jci.org/articles/view/{number}'
    page=ctx.http.get(base,timeout=30)
    if not page.ok:raise RuntimeError('JCI article page unavailable')
    parsed=_Links(page.text)
    if ids.doi.lower() not in parsed.dois:raise RuntimeError('JCI article DOI mismatch or absent')
    paths=sorted({urlsplit(urljoin(base,h)).path for h in parsed.links
                  if urlsplit(urljoin(base,h)).hostname=='www.jci.org'
                  and re.fullmatch(rf'/articles/view/{number}/sd/\d+',urlsplit(urljoin(base,h)).path)})
    files=[];seen=set()
    for path in paths:
        response=ctx.http.get('https://www.jci.org'+path,timeout=30)
        if not response.ok:raise RuntimeError('JCI supplement listing unavailable')
        links=_Links(response.text).links
        found=False
        for href in links:
            url=urljoin(base,href);part=urlsplit(url)
            m=re.fullmatch(rf'/articles/view/{number}/sd/([a-z0-9]+)/render/(\d+)',part.path) if part.hostname=='www.jci.org' else None
            # JCI serves tables/videos on its manuscript CDN. Require both the
            # article directory and exact article-prefixed supplementary filename.
            cdn=re.fullmatch(rf'/manuscripts/\d+/{number}/(JCI{number}\.sd[^/]+\.[a-z0-9]+)',part.path,re.I) if part.hostname=='dm5migu4zj3pb.cloudfront.net' else None
            if not m and not cdn:continue
            name=cdn.group(1) if cdn else f'JCI{number}_supplement_{m.group(2)}.{m.group(1)}'
            found=True
            if url in seen:continue
            seen.add(url)
            files.append(SupplementFile(name=name,url=url,
                provider='jci',role=ROLE_SUPPLEMENT,origin_doi=ids.doi,listing_index=len(files),
                extra={'article_url':base,'listing_url':'https://www.jci.org'+path,'article_doi_verified':True}))
        if not found:raise RuntimeError('JCI supplement page had no supported download link')
    return files
