"""Extract EPUB body text in spine order without unpacking files to disk."""
from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 10000


class EpubError(ValueError):
    """An unsupported, damaged or oversized source document."""


def _member_path(base: str, href: str) -> str:
    url = urlsplit(href)
    path = unquote(url.path)
    if url.scheme or url.netloc or path.startswith('/') or '\\' in path:
        raise EpubError("EPUB 包含非法的正文路径。")
    name = posixpath.normpath(posixpath.join(base, path))
    if name in {'', '.', '..'} or name.startswith('../'):
        raise EpubError("EPUB 包含非法的正文路径。")
    return name


def _xml(data: bytes) -> ET.Element:
    # EPUB metadata has no need for DTDs or custom entities. Reject them before
    # parsing, including UTF-16 metadata, to avoid entity expansion.
    normalized = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in normalized or b'<!ENTITY' in normalized:
        raise EpubError("EPUB 元数据包含不支持的 DTD 或实体声明。")
    return ET.fromstring(data)


class _BodyText(HTMLParser):
    BLOCKS = {'p', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
              'li', 'blockquote', 'pre', 'tr', 'hr'}
    SKIP = {'head', 'script', 'style', 'nav', 'rt', 'rp', 'svg'}
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
            'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_body = False
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.split(':')[-1].lower()
        if tag == 'body':
            self.in_body = True
        attributes = dict(attrs)
        hidden = (tag in self.SKIP or 'hidden' in attributes
                  or attributes.get('aria-hidden') == 'true')
        skipped = hidden or bool(self.stack and self.stack[-1][1])
        if self.in_body and not skipped:
            if tag in self.BLOCKS or tag == 'br':
                self.parts.append('\n')
            elif tag in {'td', 'th'}:
                self.parts.append(' ')
        if tag not in self.VOID:
            self.stack.append((tag, skipped))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.split(':')[-1].lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.split(':')[-1].lower()
        skipped = bool(self.stack and self.stack[-1][1])
        if self.in_body and not skipped and tag in self.BLOCKS:
            self.parts.append('\n')
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == 'body':
            self.in_body = False

    def handle_data(self, data):
        if self.in_body and not (self.stack and self.stack[-1][1]):
            # XML indentation is layout whitespace, not paragraph boundaries.
            self.parts.append(re.sub(r'\s+', ' ', data))

    def text(self) -> str:
        return '\n'.join(line.strip() for line in ''.join(self.parts).splitlines() if line.strip())


def _decode_html(data: bytes) -> str:
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    declaration = re.search(br'<\?xml[^>]*encoding=[\'"]([^\'"]+)', data[:256], re.I)
    encoding = declaration.group(1).decode('ascii') if declaration else 'utf-8-sig'
    return data.decode(encoding)


def read_epub(path: Path, *, on_progress: Callable[[float], None] | None = None) -> str:
    """Read EPUB 2/3 XHTML spine entries; leave images/fonts and navigation out."""
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise EpubError("EPUB 文件条目过多。")
            if len({item.filename for item in members}) != len(members):
                raise EpubError("EPUB 包含重复的文件条目。")
            total = 0

            def read(name: str) -> bytes:
                nonlocal total
                item = archive.getinfo(name)
                if item.flag_bits & 1:
                    raise EpubError("不支持加密的 EPUB 正文，请使用未加密的文件。")
                total += item.file_size
                if item.file_size > MAX_MEMBER_BYTES or total > MAX_TEXT_BYTES:
                    raise EpubError("EPUB 解压后的正文超过大小限制。")
                try:
                    return archive.read(item)
                except RuntimeError as exc:
                    raise EpubError("EPUB 正文无法读取，可能已加密。") from exc

            container = _xml(read('META-INF/container.xml'))
            rootfile = next((entry for entry in container.findall('.//{*}rootfile')
                             if entry.get('media-type') == 'application/oebps-package+xml'), None)
            if rootfile is None:
                raise EpubError("EPUB 缺少有效的内容包。")
            package_path = _member_path('', rootfile.get('full-path', ''))
            package = _xml(read(package_path))
            manifest = {item.get('id'): item for item in package.findall('./{*}manifest/{*}item')}
            spine = package.findall('./{*}spine/{*}itemref')
            if not spine:
                raise EpubError("EPUB 缺少阅读顺序（spine）。")
            encrypted: set[str] = set()
            if 'META-INF/encryption.xml' in archive.namelist():
                encryption = _xml(read('META-INF/encryption.xml'))
                encrypted = {_member_path('', entry.get('URI', ''))
                             for entry in encryption.findall('.//{*}CipherReference')}
            chapters = []
            for index, reference in enumerate(spine):
                if on_progress:
                    on_progress(index / len(spine))
                if reference.get('linear', 'yes') == 'no':
                    continue
                item = manifest.get(reference.get('idref'))
                if item is None:
                    raise EpubError("EPUB 阅读顺序引用了不存在的正文。")
                if 'nav' in item.get('properties', '').split():
                    continue
                if item.get('media-type') not in {'application/xhtml+xml', 'text/html'}:
                    raise EpubError("EPUB 包含不支持的正文格式，仅支持 XHTML/HTML 文本。")
                name = _member_path(posixpath.dirname(package_path), item.get('href', ''))
                if name in encrypted:
                    raise EpubError("不支持加密的 EPUB 正文，请使用未加密的文件。")
                parser = _BodyText()
                parser.feed(_decode_html(read(name)))
                parser.close()
                if text := parser.text():
                    chapters.append(text)
            if not chapters:
                raise EpubError("EPUB 中没有可读取的正文文本（可能是图片扫描版）。")
            if on_progress:
                on_progress(1)
            return '\n\n'.join(chapters)
    except EpubError:
        raise
    except (BadZipFile, KeyError, ET.ParseError, UnicodeError, LookupError,
            NotImplementedError, EOFError, ValueError) as exc:
        raise EpubError("EPUB 文件损坏或格式无效，无法读取正文。") from exc
