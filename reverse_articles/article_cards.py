"""Bounded, offline extraction of public-account article card metadata.

This module never fetches a URL, accesses a Weixin account, or reads cookies.
A card's title and description are not the article body.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 262_144
MAX_NODES = 4096
MAX_CARDS = 20
MAX_URL = 8192
MAX_TITLE = 1024
MAX_DESCRIPTION = 4096
_PATH = re.compile(r'^/s(?:/[A-Za-z0-9_-]+)?/?$')
_CONTROL = re.compile(r'[\x00-\x20\x7f\\]')
_ENCODED_CONTROL = re.compile(r'%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)', re.I)
_GROUP_PREFIX = re.compile(r'^[A-Za-z0-9_@.\-]{1,128}:\r?\n')


@dataclass(frozen=True)
class ArticleCard:
    title: str
    description: str
    url: str
    source_kind: str
    appmsg_type: int | None = None


def canonical_article_url(value: object) -> str | None:
    """Return an allowed direct HTTPS article link with its fragment removed.

    This is input validation, not authorization to fetch the URL. Any future
    network client must independently validate DNS, redirects and response
    limits; a URL can also be unavailable or require a logged-in Weixin view.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL:
        return None
    if not value.isascii() or _CONTROL.search(value) or _ENCODED_CONTROL.search(value):
        return None
    try:
        parts = urlsplit(value)
        if not (parts.scheme.lower() == 'https' and parts.hostname == 'mp.weixin.qq.com'
                and parts.username is None and parts.password is None
                and parts.port in (None, 443) and bool(_PATH.fullmatch(parts.path))):
            return None
        # Fragments are client-side navigation data, not part of an HTTP
        # request. Some genuine Weixin cards include one; do not retain it in
        # any returned URL, especially if it contains user-specific data.
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
    except ValueError:
        return None


def is_article_url(value: object) -> bool:
    """Test URL eligibility; use canonical_article_url before handling it."""
    return canonical_article_url(value) is not None


def _direct_text(element: ET.Element, name: str, maximum: int, *, required: bool = False) -> str | None:
    nodes = element.findall(name)
    if len(nodes) > 1 or (required and len(nodes) != 1):
        return None
    value = nodes[0].text or '' if nodes else ''
    if not isinstance(value, str) or '\x00' in value or len(value) > maximum:
        return None
    if required and not value.strip():
        return None
    return value


def _root(value: object) -> ET.Element | None:
    if isinstance(value, bytes):
        if len(value) > MAX_XML_BYTES:
            return None
        try:
            value = value.decode('utf-8-sig', errors='strict')
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value or len(value) > MAX_XML_BYTES:
        return None
    if len(value.encode('utf-8', errors='replace')) > MAX_XML_BYTES or '\x00' in value:
        return None
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', value, re.I):
        return None
    value = _GROUP_PREFIX.sub('', value, count=1)
    try:
        root = ET.fromstring(value)
        if sum(1 for _ in root.iter()) > MAX_NODES:
            return None
        return root
    except (ET.ParseError, ValueError, RecursionError):
        return None


def parse_appmsg(kind: object, value: object) -> list[ArticleCard]:
    """Extract a verified article link card from a type-49 app message.

    Appmsg subtypes 4 and 5 are link-card candidates in existing project
    sources. Other subtypes, including files, quotes and forwarded records,
    are deliberately excluded, even if nested XML happens to contain a URL.
    """
    if type(kind) is not int or kind & 0xffffffff != 49:
        return []
    root = _root(value)
    if root is None:
        return []
    if root.tag == 'appmsg':
        app = root
    elif root.tag == 'msg':
        apps = root.findall('appmsg')
        if len(apps) != 1:
            return []
        app = apps[0]
    else:
        return []
    subtype = _direct_text(app, 'type', 2, required=True)
    if subtype not in ('4', '5'):
        return []
    title = _direct_text(app, 'title', MAX_TITLE, required=True)
    description = _direct_text(app, 'des', MAX_DESCRIPTION)
    url = _direct_text(app, 'url', MAX_URL, required=True)
    canonical = canonical_article_url(url)
    if title is None or description is None or canonical is None:
        return []
    return [ArticleCard(title, description, canonical, 'appmsg', int(subtype))]


def parse_mmreader(value: object) -> list[ArticleCard]:
    """Extract direct items from a cached multi-article message when present.

    Callers must first authenticate and classify the message itself; this
    parser does not establish that a cache record was received by any account.
    It does not discover unpushed articles or fetch a public-account homepage.
    """
    root = _root(value)
    if root is None:
        return []
    if root.tag == 'mmreader':
        reader = root
    elif root.tag == 'msg':
        readers = root.findall('mmreader')
        if len(readers) != 1:
            return []
        reader = readers[0]
    else:
        return []
    categories = reader.findall('category')
    if len(categories) != 1:
        return []
    items = categories[0].findall('item')
    if not 1 <= len(items) <= MAX_CARDS:
        return []
    result: list[ArticleCard] = []
    seen = set()
    for item in items:
        title = _direct_text(item, 'title', MAX_TITLE, required=True)
        description = _direct_text(item, 'digest', MAX_DESCRIPTION)
        url = _direct_text(item, 'url', MAX_URL, required=True)
        canonical = canonical_article_url(url)
        if title is None or description is None or canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        result.append(ArticleCard(title, description, canonical, 'mmreader'))
    return result
