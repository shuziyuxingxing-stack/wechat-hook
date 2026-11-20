import unittest

from article_cards import (MAX_CARDS, MAX_XML_BYTES, canonical_article_url,
                           is_article_url, parse_appmsg, parse_mmreader)


URL = 'https://mp.weixin.qq.com/s/AbCd_123-xy'


def app(url=URL, kind='5', title='一篇文章', description='测试摘要'):
    return (f'<msg><appmsg><title>{title}</title><des>{description}</des>'
            f'<type>{kind}</type><url>{url}</url></appmsg></msg>')


class UrlTests(unittest.TestCase):
    def test_valid_official_article_paths_and_query(self):
        self.assertTrue(is_article_url(URL))
        self.assertTrue(is_article_url('https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=abc'))
        self.assertTrue(is_article_url('https://MP.WEIXIN.QQ.COM:443/s/a'))

    def test_reject_non_article_or_non_official_hosts(self):
        values = [
            'http://mp.weixin.qq.com/s/abc',
            'https://mp.weixin.qq.com.evil.test/s/abc',
            'https://evil.test@mp.weixin.qq.com/s/abc',
            'https://mp.weixin.qq.com@evil.test/s/abc',
            'https://mp.weixin.qq.com:8443/s/abc',
            'https://mp.weixin.qq.com/mp/profile_ext',
            'https://mp.weixin.qq.com/s/../x',
            'https://mp.weixin.qq.com/s/%2e%2e/x',
            'https://mp.weixin.qq.com/s/a%0dfoo',
            'https://mp.weixin.qq.com/s/a?x=1%0aHeader',
            'https://mp.weixin.qq.com/s/a\\evil',
            'https://mp.weixin.qq.com/s/a?redirect=中文',
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(is_article_url(value))

    def test_reject_non_strings_and_oversized(self):
        for value in (None, 1, b'https://mp.weixin.qq.com/s/a', URL + '?x=' + 'a' * 9000):
            self.assertFalse(is_article_url(value))

    def test_fragment_removed_before_returning_any_url(self):
        candidate = 'https://mp.weixin.qq.com/s?__biz=a&mid=2#private-fragment'
        self.assertTrue(is_article_url(candidate))
        self.assertEqual(canonical_article_url(candidate), 'https://mp.weixin.qq.com/s?__biz=a&mid=2')
        self.assertIsNone(canonical_article_url('https://evil.test/s/a#fragment'))
        self.assertIsNone(canonical_article_url('https://mp.weixin.qq.com/s/a#bad%0aheader'))


class AppMessageTests(unittest.TestCase):
    def test_parses_low_49_and_high_subtype(self):
        cards = parse_appmsg((5 << 32) | 49, app())
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0].title, cards[0].description, cards[0].url), ('一篇文章', '测试摘要', URL))
        self.assertEqual((cards[0].source_kind, cards[0].appmsg_type), ('appmsg', 5))
        self.assertFalse(hasattr(cards[0], 'body'))

    def test_strips_only_valid_group_sender_prefix(self):
        self.assertEqual(len(parse_appmsg(49, 'wxid_test:\n' + app())), 1)
        self.assertEqual(parse_appmsg(49, 'unexpected junk <msg>' + app()), [])

    def test_decodes_escaped_url_query(self):
        cards = parse_appmsg(49, app(url='https://mp.weixin.qq.com/s?__biz=a&amp;mid=2&amp;sn=x'))
        self.assertEqual(cards[0].url, 'https://mp.weixin.qq.com/s?__biz=a&mid=2&sn=x')

    def test_redacted_real_card_shape_strips_fragment(self):
        xml = ('<msg><appmsg><title>脱敏标题</title><des>脱敏摘要</des><type>5</type>'
               '<url>https://mp.weixin.qq.com/s?__biz=example&amp;mid=123&amp;idx=1&amp;sn=abc#rd</url>'
               '<sourceusername>gh_redacted</sourceusername></appmsg></msg>')
        cards = parse_appmsg((5 << 32) | 49, xml)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].url, 'https://mp.weixin.qq.com/s?__biz=example&mid=123&idx=1&sn=abc')
        self.assertNotIn('#', cards[0].url)

    def test_ignores_other_message_types_and_nested_records(self):
        for kind in (1, 3, 34, 49.0, True, None):
            self.assertEqual(parse_appmsg(kind, app()), [])
        for subtype in ('6', '19', '57', '74'):
            self.assertEqual(parse_appmsg(49, app(kind=subtype)), [])
        self.assertEqual(parse_appmsg(49, '<msg><appmsg><type>19</type><recorditem>' + app() + '</recorditem></appmsg></msg>'), [])

    def test_rejects_ambiguous_duplicate_and_bad_urls(self):
        self.assertEqual(parse_appmsg(49, app().replace('<type>5</type>', '<type>5</type><type>5</type>')), [])
        self.assertEqual(parse_appmsg(49, app().replace('</appmsg>', '<url>' + URL + '</url></appmsg>')), [])
        self.assertEqual(parse_appmsg(49, app(url='https://attacker.example/s/x')), [])
        self.assertEqual(parse_appmsg(49, app().replace('</msg>', '<appmsg></appmsg></msg>')), [])

    def test_rejects_dtd_bad_encoding_and_resource_excess(self):
        payload = '<!DOCTYPE msg [<!ENTITY x "bad">]>' + app(title='&x;')
        self.assertEqual(parse_appmsg(49, payload), [])
        self.assertEqual(parse_appmsg(49, b'\xff'), [])
        self.assertEqual(parse_appmsg(49, b'x' * (MAX_XML_BYTES + 1)), [])
        self.assertEqual(parse_appmsg(49, app(title='a' * 1025)), [])
        self.assertEqual(parse_appmsg(49, '<msg>' + '<x/>' * 4097 + '</msg>'), [])


class MultiArticleTests(unittest.TestCase):
    def test_multiple_items_with_dedup_and_invalid_skipped(self):
        item = '<item><title>A</title><digest>D</digest><url>' + URL + '</url></item>'
        other = '<item><title>B</title><url>https://mp.weixin.qq.com/s/B</url></item>'
        bad = '<item><title>C</title><url>https://evil.example/s/C</url></item>'
        cards = parse_mmreader('<msg><mmreader><category>' + item + item + other + bad + '</category></mmreader></msg>')
        self.assertEqual([c.title for c in cards], ['A', 'B'])
        self.assertEqual([c.source_kind for c in cards], ['mmreader', 'mmreader'])

    def test_deduplicates_fragments_of_same_article(self):
        item = lambda fragment: '<item><title>A</title><url>' + URL + '#' + fragment + '</url></item>'
        cards = parse_mmreader('<mmreader><category>' + item('a') + item('b') + '</category></mmreader>')
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].url, URL)

    def test_rejects_ambiguous_or_too_many_items(self):
        item = '<item><title>A</title><url>' + URL + '</url></item>'
        self.assertEqual(parse_mmreader('<mmreader><category>' + item * (MAX_CARDS + 1) + '</category></mmreader>'), [])
        self.assertEqual(parse_mmreader('<mmreader><category/>' + '<category>' + item + '</category></mmreader>'), [])
        self.assertEqual(parse_mmreader('<msg><mmreader/><mmreader/></msg>'), [])


if __name__ == '__main__':
    unittest.main()
