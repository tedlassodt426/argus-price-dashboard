# -*- coding: utf-8 -*-
import urllib.parse
import urllib.request


def test(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, r.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return "ERR", str(e)[:200]


q = "European thermal coal prices corrected in a slow start to the month"
u1 = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en"
      "&tl=zh-CN&dt=t&q=" + urllib.parse.quote(q))
s1, b1 = test(u1)
print("Google gtx:", s1)
print(b1[:250])

u2 = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(q)
      + "&langpair=en|zh-CN")
s2, b2 = test(u2)
print("MyMemory:", s2)
print(b2[:250])
