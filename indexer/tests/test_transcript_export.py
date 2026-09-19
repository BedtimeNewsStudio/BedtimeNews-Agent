"""Safe Markdown-to-reader projection tests."""

import hashlib

from src.transcript_export import _rewrite_href, project_transcript

TRANSCRIPT = """# 【睡前消息588】城投债四大网红

**发布日期** 2023-05-12 | [YouTube](https://example.com/watch) | [B站](https://www.bilibili.com/video/x)

## 正文

## 第一节

正文含有 [站内文稿](../0601-0700/0601.md) 和 [外链](https://example.com/x)。

脚本 <script>alert('x')</script>，事件 <u onclick="evil()">下划线</u>。

![远程图](https://example.com/image.png)

脚注[^1]。

## 附录

### 事实订正

[^1]: 脚注正文在附录。
"""


def test_projection_includes_preamble_body_and_appendix():
    raw = TRANSCRIPT.encode()
    projection = project_transcript(
        "ShuiQianXiaoXi/0501-0600/0588.md", raw, "睡前消息588"
    )

    assert projection.canonical_title == "睡前消息588"
    assert projection.source_title == "【睡前消息588】城投债四大网红"
    assert projection.channel == "ShuiQianXiaoXi"
    assert projection.publication_date == "2023-05-12"
    html = projection.body_html
    assert "第一节" in html
    assert "正文含有" in html
    assert "事实订正" in html
    assert "脚注正文在附录" in html
    assert "bilibili.com" in html
    assert "example.com/watch" in html
    assert "<h1" not in html.lower()
    assert projection.source_hash == hashlib.sha256(raw).hexdigest()


def test_projection_sanitizes_html_and_suppresses_remote_images():
    html = project_transcript("x/0001/a.md", TRANSCRIPT.encode(), "标题").body_html

    assert "<script" not in html
    assert "onclick" not in html
    assert "<img" not in html
    assert "[图片：远程图]" in html
    assert "<u>下划线</u>" in html


def test_footnotes_are_bidirectional_links():
    html = project_transcript(
        "ShuiQianXiaoXi/0501-0600/0588.md", TRANSCRIPT.encode(), "标题"
    ).body_html

    assert "footnote-ref" in html
    assert 'href="#' in html
    assert "footnote-backref" in html or "fnref" in html
    assert 'id="' in html


def test_projection_localizes_internal_and_old_site_links():
    markdown = """# T

## 正文

[relative](../0201-0300/0201.md)
[old](https://bedtimenewsstudio.github.io/BedtimeNews-Transcripts/contents/CanKaoXinXi/0401-0500/0490.html)
[outside](../../../../secret.md)
[unsafe](javascript:alert(1))

## 附录
"""
    html = project_transcript(
        "ShuiQianXiaoXi/0101-0200/0150.md", markdown.encode(), "标题"
    ).body_html

    assert "/transcripts/ShuiQianXiaoXi/0201-0300/0201.md" in html
    assert "/transcripts/CanKaoXinXi/0401-0500/0490.md" in html
    assert "bedtimenewsstudio.github.io" not in html
    assert 'href="javascript:' not in html
    assert "javascript:" in html
    assert "secret.md" not in html


def test_projection_rejects_absolute_encoded_and_backslash_paths():
    base = "ShuiQianXiaoXi/0101-0200/0150.md"

    assert _rewrite_href(base, "/root.md") is None
    assert _rewrite_href(base, "%2Froot.md") is None
    assert _rewrite_href(base, "..\\secret.md") is None


def test_projection_handles_missing_title_and_date():
    projection = project_transcript(
        "GaoJian/misc/example.md",
        "## 正文\n\n只有正文。\n".encode(),
        "标准标题",
    )
    assert projection.source_title == "标准标题"
    assert projection.publication_date is None
    assert "只有正文" in projection.body_html
