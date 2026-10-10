"""推し活速報: YouTube と公式サイトの新着を Discord に1件ずつ投稿する。

GitHub Actions から15分ごとに実行される。既に投稿したものは seen.json に記録し、
同じものは二度と投稿しない。初めて見る情報源は、過去分を記録するだけで投稿しない。
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# ---- 設定（増やしたい時はここに足すだけ） ----------------------------------

# YouTube: (表示名, チャンネルURL)。@ハンドル でも /channel/UC... でもよい
YOUTUBE = [
    ("M!LK公式", "https://www.youtube.com/@MILK_2014"),
    ("M!LK関連", "https://www.youtube.com/channel/UCpfwRncr8S0S3YosR52PX2w"),
    ("吉田仁人", "https://www.youtube.com/channel/UCk8oGFgksVxjh2YiU_D6hFQ"),
    ("佐野勇斗", "https://www.youtube.com/@sanohayatodazo"),
    ("山中柔太朗", "https://www.youtube.com/@jyutaro_the_movie"),
    ("曽野舜太", "https://www.youtube.com/@Im_SONO_SHUNTA"),
]

# 公式サイト: (表示名, 確認するページ, 絞り込み語)。絞り込み語が空なら全件投稿
SITES = [
    ("M!LK公式サイト", ["https://sd-milk.com/", "https://sd-milk.com/news/"], []),
    ("EBiDAN公式サイト", ["https://ebidan.jp/", "https://ebidan.jp/news/"],
     ["M!LK", "MILK", "ミルク", "吉田", "仁人", "佐野", "勇斗", "山中", "柔太朗",
      "塩﨑", "塩崎", "太智", "曽野", "舜太", "EBiDAN", "エビダン",
      "スーパースター", "シュスエビ", "合同", "全員"]),
]

# お知らせとみなすリンクのパスに含まれる語
NEWS_PATH_WORDS = ["news", "info", "topics", "schedule", "media", "release",
                   "event", "live", "ticket", "goods", "disco", "blog"]

# ⭐ を付ける語（最推し）
STAR_WORDS = ["吉田仁人", "仁人", "じんと", "JINTO", "Jinto"]

MAX_POSTS_PER_RUN = 15
STATE_FILE = "seen.json"
UA = "Mozilla/5.0 (compatible; oshi-watch/1.0)"

# ---------------------------------------------------------------------------


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"channel_ids": {}, "seen": {}}


def save_state(state):
    for key in state["seen"]:
        state["seen"][key] = state["seen"][key][-500:]  # 古い記録は捨てる
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def channel_id(url, state):
    m = re.search(r"/channel/(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    if url in state["channel_ids"]:
        return state["channel_ids"][url]
    page = fetch(url)
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'"channelId":"(UC[\w-]{22})"', page))
    if not m:
        raise RuntimeError(f"チャンネルIDが見つかりません: {url}")
    state["channel_ids"][url] = m.group(1)
    return m.group(1)


def youtube_items(name, url, state):
    cid = channel_id(url, state)
    xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    items = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", namespaces=ns)
        title = e.findtext("a:title", namespaces=ns) or ""
        link = e.find("a:link", ns).get("href")
        items.append((vid, f"🎬 **{name}** の新着動画\n{title}\n{link}", title + name))
    return items


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self._href, self._text = a.get("href"), [a.get("title") or ""]

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None


def site_items(name, pages, words):
    found = {}
    for page in pages:
        try:
            body = fetch(page)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        p = LinkParser()
        p.feed(body)
        host = urlparse(page).netloc
        for href, text in p.links:
            url = urljoin(page, href).split("#")[0]
            u = urlparse(url)
            path = u.path.lower()
            if u.netloc != host or len(text) < 6:
                continue
            if not any(w in path for w in NEWS_PATH_WORDS):
                continue
            if path.rstrip("/").count("/") < 2 and not u.query:
                continue  # 一覧ページそのもの（/news/ など）は除く
            if words and not any(w.lower() in text.lower() for w in words):
                continue
            found.setdefault(url, html.unescape(text)[:200])
    return [(url, f"📢 **{name}** のお知らせ\n{text}\n{url}", text) for url, text in found.items()]


def post(webhook, content):
    name = "YouTube新着" if content.startswith(("🎬", "⭐ 🎬")) else "推し活速報"
    data = json.dumps({"content": content[:1990], "username": name}).encode()
    for _ in range(4):
        req = urllib.request.Request(webhook, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=20)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)) + 0.5)
            elif e.code >= 500:
                time.sleep(3)
            else:
                raise
    raise RuntimeError("Discordへの投稿に失敗しました")


def main():
    # YouTube は #youtube へ、公式サイトは #推し-速報 へ。登録されていない方は確認しない
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    youtube_webhook = os.environ.get("YOUTUBE_WEBHOOK_URL")
    if not webhook and not youtube_webhook:
        sys.exit("YOUTUBE_WEBHOOK_URL も DISCORD_WEBHOOK_URL も設定されていません")
    state = load_state()

    sources = []
    if youtube_webhook:
        sources += [(f"yt:{url}", lambda n=n, u=url: youtube_items(n, u, state)) for n, url in YOUTUBE]
    if webhook:
        sources += [(f"site:{n}", lambda n=n, p=p, w=w: site_items(n, p, w)) for n, p, w in SITES]

    queue, errors = [], []
    for key, get in sources:
        try:
            items = get()
        except Exception as e:  # 1つの情報源が失敗しても他は続ける
            errors.append(f"{key}: {e}")
            continue
        seen = state["seen"].get(key)
        if seen is None:  # 初回は記録だけ。YouTubeは動作確認として最新1本だけ投稿する
            state["seen"][key] = [i[0] for i in items]
            print(f"{key}: 初回のため {len(items)} 件を記録")
            if key.startswith("yt:") and items:
                state["seen"][key].remove(items[0][0])
                queue.append((key, items[0]))
            continue
        new = [i for i in items if i[0] not in seen]
        print(f"{key}: 新着 {len(new)} 件")
        queue += [(key, i) for i in reversed(new)]  # 古い順に投稿

    for key, (item_id, text, check) in queue[:MAX_POSTS_PER_RUN]:
        if any(w in check for w in STAR_WORDS):
            text = "⭐ " + text
        post(youtube_webhook if key.startswith("yt:") else webhook, text)
        state["seen"][key].append(item_id)
        time.sleep(1)

    save_state(state)
    for e in errors:
        print("取得エラー:", e)


if __name__ == "__main__":
    main()
