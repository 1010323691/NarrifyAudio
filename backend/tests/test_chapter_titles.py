"""Chapter metadata must exclude delimiters while preserving title punctuation."""
import pytest

from backend.core.chapter_titles import clean_chapter_title
from backend.core.config import TextConfig
from backend.engines import book
from backend.engines.text import format_text


@pytest.mark.parametrize('delimiter', [
    ';', '；', '﹔', ':', '：', ',', '，', '﹐', '、', '·', '•', '・', '‧',
    '|', '｜', '/', '／', '_', '＿', '-', '—', '–', '； ：—｜、', '\t；\u00a0:',
    '\u200b；\ufeff：',
])
def test_delimiters_removed_from_metadata_without_changing_source(delimiter):
    title = '崩坏的吞噬世界，重生者夜雨！'
    source = f'第1章 {delimiter}{title}\n正文里的；分号、逗号，都要保留。\n第2章 尾声\n结束。'
    analysis = book.analyze_text(source)
    assert [chapter['title'] for chapter in analysis['chapters']] == [title, '尾声']
    assert ''.join(book.chapter_content(analysis, chapter) for chapter in analysis['chapters']) == source
    assert title in book.make_smart_filenames(analysis['chapters'])[0]
    assert title in book.analyze_text(format_text(source, TextConfig())['text'])['chapters'][0]['title']


@pytest.mark.parametrize('title', [
    '“你是谁？”', '《崩坏》世界', '【特别篇】重生', '（上）夜雨归来',
    '……原来如此！', '...And then?', '.NET 世界', '！？这是怎么回事？',
    '夜雨；崩坏：重生者，来了！', 'A/B 的选择', '夜雨—重生',
])
def test_meaningful_title_openers_and_interior_punctuation_are_preserved(title):
    source = f'第1章 ；{title}\n正文。'
    assert book.analyze_text(source)['chapters'][0]['title'] == title
    assert clean_chapter_title(f'\u200b；： {title} \u200b \ufeff \u2060') == title
    assert clean_chapter_title(clean_chapter_title(f'； {title}')) == title


@pytest.mark.parametrize('headers', [
    ['第1章；开场', '第2章;转折', '第3章；结局'],
    ['【第１章】；开场', '[第２章]；转折', '第３章；结局'],
    ['书名 第1章；开场', '书名 第2章；转折', '书名 第3章；结局'],
    ['书名系列第1章；开场', '书名系列第2章；转折', '书名系列第3章；结局'],
    ['第1节；开场', '第2节;转折', '第3节；结局'],
    ['1章；开场', '2章；转折', '3章；结局'],
    ['Chapter 1;开场', 'Chapter 2；转折', 'Chapter 3;结局'],
    ['Chapter One;开场', 'Chapter Two；转折', 'Chapter Three;结局'],
    ['Part 1；开场', 'Part 2;转折', 'Part 3；结局'],
    ['卷一；开场', '卷二；转折', '卷三；结局'],
    ['1；开场', '2;转折', '3；结局'],
])
def test_all_admitted_header_shapes_use_same_title_cleanup(headers):
    text = '\n\n'.join(f'{header}\n正文。' for header in headers)
    assert [chapter['title'] for chapter in book.analyze_text(text)['chapters']] == ['开场', '转折', '结局']


def test_long_title_with_many_delimiters_is_still_detected():
    title = '标题' * 25
    source = f'第1章 ；；； ： {title}\n正文。'
    assert book.analyze_text(source)['chapters'][0]['title'] == title


def test_separator_only_titles_and_weak_numbered_prose():
    assert book.analyze_text('第1章；：｜\n正文。')['chapters'][0]['title'] == ''
    assert book.analyze_text('1；只是正文中的一个列表项。')['chapters'] == []
    assert book.analyze_text('他读到第1章；崩坏的世界，随后合上了书。')['chapters'] == []


def test_internal_rescan_uses_cleaned_titles():
    text = '第1章；开场\n正文。\n第2章；：归来\n正文。'
    evidence = book._internal_title_scan(text, {'start': 0, 'end': len(text), 'num': 1}, 0)
    assert evidence[0]['title'] == '归来'


def test_smart_filename_normalizes_external_chapter_metadata():
    names = book.make_smart_filenames([{'seq': 1, 'final_num': 1, 'title': '；：标题；内部！'}])
    assert names == ['第 001 章 标题；内部！.txt']
