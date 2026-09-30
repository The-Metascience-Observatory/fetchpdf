from types import SimpleNamespace as N
import pytest
from fetchpdf.retrieval.supplement_jci import enumerate_jci

def context(doi='10.1172/JCI126346',download='/articles/view/126346/sd/pdf/render/1'):
    def get(url,**kw):
        html=(f'<meta name="citation_doi" content="{doi}"><a href="/articles/view/126346/sd/1">Supplement</a>'
              if url.endswith('/126346') else f'<a href="{download}">Download</a>')
        return N(ok=True,text=html)
    return N(http=N(get=get))

def test_jci_lists_verified_article_download():
    result=enumerate_jci(N(doi='10.1172/jci126346'),context())
    assert len(result)==1
    assert result[0].url=='https://www.jci.org/articles/view/126346/sd/pdf/render/1'
    assert result[0].extra['article_doi_verified'] is True

def test_wrong_article_and_cross_article_download_fail_closed():
    with pytest.raises(RuntimeError,match='DOI'):
        enumerate_jci(N(doi='10.1172/jci126346'),context(doi='10.1172/JCI999'))
    with pytest.raises(RuntimeError,match='download'):
        enumerate_jci(N(doi='10.1172/jci126346'),context(download='/articles/view/999/sd/pdf/render/1'))

@pytest.mark.parametrize('extension',['xlsx','mp4'])
def test_jci_article_owned_cdn_supplements(extension):
    url=f'//dm5migu4zj3pb.cloudfront.net/manuscripts/126000/126346/JCI126346.sdt1.{extension}'
    result=enumerate_jci(N(doi='10.1172/jci126346'),context(download=url))
    assert result[0].name==f'JCI126346.sdt1.{extension}'
    assert result[0].url.startswith('https://dm5migu4zj3pb.cloudfront.net/')

@pytest.mark.parametrize('url',[
 '//dm5migu4zj3pb.cloudfront.net/manuscripts/126000/999/JCI126346.sdt1.xlsx',
 '//dm5migu4zj3pb.cloudfront.net/manuscripts/126000/126346/JCI999.sdt1.xlsx',
 '//unrelated.example/manuscripts/126000/126346/JCI126346.sdt1.xlsx',
])
def test_jci_cdn_requires_article_identity(url):
    with pytest.raises(RuntimeError,match='download'):
        enumerate_jci(N(doi='10.1172/jci126346'),context(download=url))
