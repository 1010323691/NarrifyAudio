"""EPUB extraction: reading order, markup, encodings and invalid archives."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from backend.engines import epub


def make_epub(*, spine='second first nav extra', first=None, extra_members=None, version='3.0'):
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        refs = ''.join(f'<itemref idref="{item}" linear="{"no" if item == "extra" else "yes"}"/>' for item in spine.split())
        archive.writestr('OPS/book.opf', f'''<package xmlns="http://www.idpf.org/2007/opf" version="{version}"><manifest>
<item id="first" href="Text/first%20chapter.xhtml#start" media-type="application/xhtml+xml"/>
<item id="second" href="Text/second.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="extra" href="extra.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine>{refs}</spine></package>''')
        archive.writestr('OPS/Text/first chapter.xhtml', first if first is not None else '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>不要朗读书名</title><style>隐藏样式</style></head><body><h1>第 2 章 归来</h1><p>秦尘推开<em>房门</em>。<br/>清晨&amp;鸟鸣。</p><p>汉<ruby>字<rt>zi</rt></ruby>。</p><script>不要朗读脚本</script><nav>目录链接</nav><p hidden="hidden">隐藏内容</p></body></html>''')
        archive.writestr('OPS/Text/second.xhtml', '<html><body><h1>第 1 章 出发</h1><p>他整理行囊，准备启程。</p></body></html>')
        archive.writestr('OPS/nav.xhtml', '<html><body>目录</body></html>')
        archive.writestr('OPS/extra.xhtml', '<html><body>附加非线性内容</body></html>')
        for name, body in (extra_members or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


@pytest.mark.parametrize('version', ['2.0', '3.0'])
def test_reading_order_and_body_markup(tmp_path, version):
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub(version=version))
    progress = []
    text = epub.read_epub(path, on_progress=progress.append)
    assert text == '第 1 章 出发\n他整理行囊，准备启程。\n\n第 2 章 归来\n秦尘推开房门。\n清晨&鸟鸣。\n汉字。'
    assert progress == sorted(progress)
    assert progress[-1] == 1


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'gb18030'])
def test_document_encoding(tmp_path, encoding):
    body = f'<?xml version="1.0" encoding="{encoding}"?><html><body><p>中文正文</p></body></html>'
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub(spine='first', first=body.encode(encoding)))
    assert epub.read_epub(path) == '中文正文'


@pytest.mark.parametrize(('body', 'message'), [
    (b'not a zip file', '损坏'),
    (make_epub(spine='missing'), '不存在'),
    (make_epub(spine=''), '阅读顺序'),
    (make_epub(spine='nav extra'), '没有可读取'),
    (make_epub(spine='first', first='<html><body><img src="scan.jpg"/></body></html>'), '没有可读取'),
    (make_epub(extra_members={'META-INF/encryption.xml': '<encryption><EncryptedData><CipherData><CipherReference URI="OPS/Text/second.xhtml"/></CipherData></EncryptedData></encryption>'}), '加密'),
], ids=['not-zip', 'missing-spine-entry', 'empty-spine', 'no-linear-body',
        'image-only-body', 'encrypted-body'])
def test_invalid_sources(tmp_path, body, message):
    path = tmp_path / 'book.epub'
    path.write_bytes(body)
    with pytest.raises(epub.EpubError, match=message):
        epub.read_epub(path)


@pytest.mark.parametrize('href', ['../../escape.xhtml', '/etc/passwd', 'https://example.com/book.xhtml', '..%2F..%2Fescape.xhtml', r'Text\first.xhtml'])
def test_invalid_member_paths(href):
    with pytest.raises(epub.EpubError, match='非法'):
        epub._member_path('OPS', href)


def test_size_limit(tmp_path, monkeypatch):
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub())
    monkeypatch.setattr(epub, 'MAX_MEMBER_BYTES', 10)
    with pytest.raises(epub.EpubError, match='大小限制'):
        epub.read_epub(path)


def test_metadata_entities_are_rejected():
    xml = '<!DOCTYPE x [<!ENTITY x "expanded">]><container>&x;</container>'
    for encoding in ['utf-8', 'utf-16']:
        with pytest.raises(epub.EpubError, match='实体声明'):
            epub._xml(xml.encode(encoding))


def test_cancellation_propagates(tmp_path):
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub())
    class Cancelled(RuntimeError):
        pass
    def cancel(_fraction):
        raise Cancelled()
    with pytest.raises(Cancelled):
        epub.read_epub(path, on_progress=cancel)


@pytest.mark.parametrize('limit', ['MAX_TEXT_BYTES', 'MAX_MEMBERS'])
def test_archive_limits(tmp_path, monkeypatch, limit):
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub())
    monkeypatch.setattr(epub, limit, 1)
    with pytest.raises(epub.EpubError, match='限制|过多'):
        epub.read_epub(path)


def test_encrypted_fonts_do_not_block_unencrypted_body(tmp_path):
    path = tmp_path / 'book.epub'
    path.write_bytes(make_epub(extra_members={
        'META-INF/encryption.xml': '<encryption><EncryptedData><CipherData><CipherReference URI="OPS/font.otf"/></CipherData></EncryptedData></encryption>',
    }))
    assert '出发' in epub.read_epub(path)
