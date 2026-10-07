#!/usr/bin/env python3
"""Turn the pages downloaded by fetch_wayback.py into a browsable offline site.

    python3 scripts/build_site.py                  # archive/ -> docs/
    python3 scripts/build_site.py --archive archive --out docs

Every archived page (HTML, XHTML-MP or WML) becomes docs/p/<id>.html: decoded to
UTF-8, WML converted to HTML, scripts and forms made inert, and links pointing at
other archived pages rewritten to the local copies (closest capture in time).
Links to pages that were never archived go to the Wayback Machine instead.

docs/index.html is the viewer: search, filters, site map, timeline and an
embedded page viewer. It works straight from disk (file://) — no server needed.
"""
import argparse
import html
import json
import os
import re
import shutil
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "site_template")
WAYBACK = "https://web.archive.org/web"

# Query parameters that only carry a session or cache-buster; they are ignored
# when matching links to archived pages.
SESSION_PARAMS = {"sid", "phpsessid", "sess", "session", "sessid", "rnd", "rand", "r", "nocache", "ts", "time"}
SNIPPET_LEN = 300
SEARCH_TEXT_LEN = 4000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- decoding

CHARSET_RE = re.compile(rb"""(?:charset|encoding)\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.I)
RU_COMMON = set("оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё")


def russian_score(text):
    return sum(1 for ch in text if ch in RU_COMMON)


def decode(body, content_type=""):
    declared = None
    m = CHARSET_RE.search(content_type.encode()) or CHARSET_RE.search(body[:2048])
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
    candidates = []
    if declared:
        candidates.append({"win-1251": "cp1251", "windows1251": "cp1251"}.get(declared, declared))
    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in candidates:
        try:
            return body.decode(enc), enc
        except (LookupError, UnicodeDecodeError):
            pass
    # Russian WAP sites of the era: windows-1251, sometimes koi8-r
    best = max(("cp1251", "koi8_r"), key=lambda e: russian_score(body.decode(e, "replace")))
    return body.decode(best, "replace"), best


# ---------------------------------------------------------------- URLs

def clean_path(path):
    """Path with session noise removed. forum.wapcity.ru (a JSP engine) keeps
    the session in the path: ;jsessionid=..., /uk/<token> (user key), and
    profile/photo links carry a /frm/<return path> that only sets the back link."""
    path = re.sub(r"/{2,}", "/", path or "/")
    path = re.sub(r";(jsessionid|phpsessid|sid)=[^/?]*", "", path, flags=re.I)
    path = re.sub(r"/uk/[^/]+", "", path)
    if re.match(r"/(user|oldu|foto|u)/", path):
        path = re.sub(r"/frm(/.*?)?(?=/a/s$|$)", "", path)
    path = re.sub(r"/index\.(php|html?|wml|cgi|pl|jsp)$", "/", path, flags=re.I)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def normalize(url):
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = clean_path(p.path)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in SESSION_PARAMS]
    q.sort()
    return urlunsplit(("http", host, path, urlencode(q), ""))


def wayback(ts, url, mode=""):
    return f"{WAYBACK}/{ts}{mode}/{url}"


class Resolver:
    """Maps any URL to the nearest-in-time local capture, if one exists."""

    def __init__(self, pages):
        self.by_norm = defaultdict(list)
        for p in pages:
            self.by_norm[normalize(p["url"])].append((p["timestamp"], p["id"]))
        for v in self.by_norm.values():
            v.sort()
        self.media = {}

    def find(self, url, ts):
        caps = self.by_norm.get(normalize(url))
        if not caps:
            return None
        i = bisect_left(caps, (ts, ""))
        best = min(caps[max(0, i - 1):i + 1], key=lambda c: abs(int(c[0]) - int(ts)))
        return best[1]


# ---------------------------------------------------------------- HTML/WML rewriting

DROP_WITH_CONTENT = {"script", "style", "noscript", "head", "template", "onevent", "access",
                     "object", "embed", "applet", "iframe", "frame", "frameset", "noembed", "timer",
                     "setvar", "postfield", "refresh", "prev", "noop", "meta", "link"}
VOID = {"br", "img", "hr", "input", "col", "wbr", "area"}
KEEP = {"a", "b", "i", "u", "s", "em", "strong", "small", "big", "p", "br", "img", "hr", "div", "span",
        "table", "tr", "td", "th", "thead", "tbody", "tfoot", "caption", "col", "colgroup",
        "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code",
        "blockquote", "q", "center", "font", "sup", "sub", "tt", "strike", "del", "ins", "abbr",
        "label", "fieldset", "legend", "select", "option", "optgroup", "textarea", "input", "button",
        "nobr", "address", "cite", "dfn", "kbd", "samp", "var", "marquee", "fieldset"}
SAFE_ATTRS = {"href", "src", "alt", "title", "colspan", "rowspan", "align", "valign", "width", "height",
              "border", "cellpadding", "cellspacing", "color", "bgcolor", "face", "size", "name", "value",
              "type", "class", "id", "style", "nowrap", "start", "checked", "selected", "multiple", "rows",
              "cols", "maxlength", "label", "dir", "lang", "mode", "background"}
# WML elements mapped to HTML equivalents
WML_MAP = {"wml": "div", "card": "section", "anchor": "a", "do": "a", "fieldset": "fieldset",
           "optgroup": "optgroup", "form": "div", "body": "div", "html": "div", "center": "center"}


class Rewriter(HTMLParser):
    def __init__(self, page, resolver, is_wml):
        super().__init__(convert_charrefs=False)
        self.page = page
        self.base = page["url"]
        self.ts = page["timestamp"]
        self.resolver = resolver
        self.is_wml = is_wml
        self.out = []
        self.drop_depth = 0
        self.drop_tag = None
        self.stack = []  # (output tag or None, out index of start tag, attrs-dict)
        self.title = None
        self.in_title = False
        self.title_buf = []
        self.text = []
        self.cards = 0
        self.local_links = set()

    # -- link mapping
    def link(self, href, kind="a"):
        href = (href or "").strip()
        if not href or href.lower().startswith(("javascript:", "vbscript:", "data:")):
            return None, False
        if href.startswith("#"):
            return href, False
        if href.lower().startswith(("mailto:", "tel:", "wtai:", "sms:")):
            return href, False
        if "$(" in href or "$" in href and re.search(r"\$\w", href):
            href = re.sub(r"\$\(?\w+(:\w+)?\)?", "", href)  # WML variables can't be resolved offline
        absu = urljoin(self.base, href)
        if not absu.lower().startswith(("http://", "https://")):
            return None, False
        if kind == "img":
            local = self.resolver.media.get(normalize(absu))
            return (local if local else wayback(self.ts, absu, "im_")), False
        pid = self.resolver.find(absu, self.ts)
        if pid:
            self.local_links.add(pid)
            frag = urlsplit(href).fragment
            return f"{pid}.html" + (f"#{frag}" if frag else ""), True
        return wayback(self.ts, absu), False

    def emit_attrs(self, attrs):
        parts = []
        for k, v in attrs.items():
            if v is None:
                parts.append(f" {k}")
            else:
                parts.append(f' {k}="{html.escape(v, quote=True)}"')
        return "".join(parts)

    def clean_attrs(self, tag, attrs):
        res = {}
        for k, v in attrs:
            k = k.lower()
            if k.startswith("on") or k not in SAFE_ATTRS:
                continue
            if k == "style" and v and re.search(r"expression|url\s*\(|behavior", v, re.I):
                continue
            res[k] = v
        if "background" in res:
            del res["background"]
        if tag == "a" and "href" in res:
            href, local = self.link(res["href"])
            if href is None:
                del res["href"]
            else:
                res["href"] = href
                if not local and not href.startswith("#"):
                    res["target"] = "_blank"
                    res["rel"] = "noopener"
                    res["class"] = (res.get("class", "") + " wc-ext").strip()
        if tag == "img" and "src" in res:
            src, _ = self.link(res["src"], "img")
            if src is None:
                del res["src"]
            else:
                res["src"] = src
            res["loading"] = "lazy"
        if tag in ("input", "select", "textarea", "button"):
            res["disabled"] = None
            if tag == "input" and (res.get("type") or "").lower() == "password":
                res["value"] = ""
        return res

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
            self.title_buf = []
            return
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            if tag not in ("meta", "link", "postfield", "setvar", "noop", "refresh", "prev"):
                self.drop_tag, self.drop_depth = tag, 1
            return
        a = dict(attrs)
        if tag == "go":
            # <anchor>text<go href/></anchor> or <do><go/></do>: give the href to the parent link
            for i in range(len(self.stack) - 1, -1, -1):
                otag, idx, pattrs = self.stack[i]
                if otag == "a" and idx is not None and "href" not in pattrs:
                    pattrs["href"] = a.get("href", "")
                    method = (a.get("method") or "get").lower()
                    cleaned = self.clean_attrs("a", list(pattrs.items()))
                    if method == "post":
                        cleaned["title"] = "форма (POST) — не сохраняется в архиве"
                    self.out[idx] = "<a" + self.emit_attrs(cleaned) + ">"
                    break
            return
        if tag == "base":
            if a.get("href"):
                self.base = urljoin(self.base, a["href"])
            return
        orig = tag
        if self.is_wml or tag in ("form", "body", "html"):
            tag = WML_MAP.get(tag, tag)
        if orig == "card":
            self.cards += 1
            title = a.get("title")
            cid = a.get("id")
            attrs_out = {"class": "wml-card"}
            if cid:
                attrs_out["id"] = cid
            self.out.append("<section" + self.emit_attrs(attrs_out) + ">")
            self.stack.append(("section", None, {}))
            if title:
                if not self.title:
                    self.title = title.strip()
                self.out.append(f'<div class="wml-card-title">{html.escape(title)}</div>')
            return
        if orig == "do":
            label = a.get("label") or a.get("name") or a.get("type") or "→"
            self.out.append("<a>")
            idx = len(self.out) - 1
            self.stack.append(("a", idx, {"class": "wml-do"}))
            self.out[idx] = '<a class="wml-do">'
            self.out.append(html.escape(label))
            return
        if orig == "anchor":
            self.out.append("<a>")
            self.stack.append(("a", len(self.out) - 1, {}))
            return
        if orig == "form":
            self.out.append('<div class="wc-form">')
            self.stack.append(("div", None, {}))
            return
        if tag not in KEEP and tag not in ("section", "div"):
            # unknown element: drop the tag but keep its content
            if tag not in VOID:
                self.stack.append((None, None, {}))
            return
        cleaned = self.clean_attrs(tag, attrs)
        self.out.append(f"<{tag}" + self.emit_attrs(cleaned) + (">"))
        if tag not in VOID:
            self.stack.append((tag, len(self.out) - 1, cleaned))

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID or tag in ("go", "postfield", "setvar", "meta", "link", "base", "noop", "prev", "refresh", "timer"):
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            if self.in_title:
                self.in_title = False
                t = "".join(self.title_buf).strip()
                if t and not self.title:
                    self.title = html.unescape(re.sub(r"\s+", " ", t))
            return
        if self.drop_depth:
            if tag == self.drop_tag:
                self.drop_depth -= 1
            return
        if tag in VOID or tag == "go":
            return
        if not self.stack:
            return
        # close the most recent element; tolerate mis-nesting
        otag, _, _ = self.stack.pop()
        if otag:
            self.out.append(f"</{otag}>")

    def handle_data(self, data):
        if self.in_title:
            self.title_buf.append(data)
            return
        if self.drop_depth:
            return
        self.out.append(html.escape(html.unescape(data), quote=False))
        self.text.append(html.unescape(data))

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        if data.upper().startswith("CDATA["):
            self.handle_data(data[6:])

    def result(self):
        while self.stack:
            otag, _, _ = self.stack.pop()
            if otag:
                self.out.append(f"</{otag}>")
        return "".join(self.out)


# ---------------------------------------------------------------- classification

# URL layout of the forum.wapcity.ru engine (checked against the Wayback index)
WAPCITY_RULES = [
    ("home", re.compile(r"^/(index\.(jsp|html?))?$")),
    ("forums", re.compile(r"^/(old)?f$")),
    ("forum", re.compile(r"^/(old)?t/f/\d+")),
    ("topic", re.compile(r"^/(old)?m/f/\d+/t/\d+")),
    ("user", re.compile(r"^/(user|oldu)/id/\d+")),
    ("photo", re.compile(r"^/foto/")),
    ("news", re.compile(r"^/news")),
    ("files", re.compile(r"^/(ringpix|djpix|mmsbox|mtm|mbox)")),
    ("service", re.compile(r"^/(auth|singin|singup|info|rlz|vc|bd|online|search)")),
]
GENERIC_RULES = [
    ("topic", re.compile(r"(topic|thread|tema|theme|showtopic|viewtopic|read|post|msg|message)", re.I),
     {"t", "tid", "topic", "thread", "showtopic", "tema", "post", "p", "msg", "mid"}),
    ("forum", re.compile(r"(forum|razdel|board|cat|section|showforum|viewforum)", re.I),
     {"f", "fid", "forum", "showforum", "cat", "c", "razdel", "board", "r"}),
    ("user", re.compile(r"(user|profile|member|anketa|nick|people)", re.I),
     {"u", "uid", "user", "member", "profile", "nick", "login", "who"}),
    ("chat", re.compile(r"(chat|room|guest|gb)", re.I), {"room"}),
    ("files", re.compile(r"(down|load|file|zip|mp3|mid|pic|img|photo|logo|melod|ring|game|java)", re.I),
     set()),
]
TYPE_LABELS = {"home": "Главная", "forums": "Список форумов", "forum": "Форум (темы)", "topic": "Тема",
               "user": "Пользователь", "photo": "Фото", "news": "Новости", "chat": "Чат/гостевая",
               "files": "Файлы и сервисы", "service": "Служебные", "other": "Прочее"}


def classify(url):
    p = urlsplit(url)
    path = clean_path(p.path)
    keys = {k.lower() for k, _ in parse_qsl(p.query, keep_blank_values=True)} - SESSION_PARAMS
    for name, path_re in WAPCITY_RULES:
        if path_re.search(path):
            return name
    for name, path_re, qkeys in GENERIC_RULES:
        if keys & qkeys and (path_re.search(path) or name in ("topic", "forum", "user")):
            return name
    for name, path_re, _ in GENERIC_RULES:
        if path_re.search(path):
            return name
    return "other"


def describe(url):
    """Human label from path ids: /m/f/9/t/118/page/16 → форум 9 · тема 118 · стр. 16."""
    path = clean_path(urlsplit(url).path)
    names = {"f": "форум", "t": "тема", "page": "стр.", "id": "id", "p": "часть", "tp": "стр.", "u": "польз."}
    parts = [f"{names[k]} {v}" for k, v in re.findall(r"/(f|t|page|id|p|tp|u)/(\d+)", path)]
    return " · ".join(parts)


def section_of(url):
    """Host plus a short path; numeric ids stay with their key (f 9, t 118) so
    the site map reads host → m → f 9 → t 118. Query keys form a last level."""
    p = urlsplit(url)
    host = re.sub(r"^www\.", "", (p.hostname or "").lower())
    toks = [t for t in clean_path(p.path).split("/") if t]
    segs, i = [], 0
    while i < len(toks):
        if i + 1 < len(toks) and toks[i + 1].isdigit() and not toks[i].isdigit():
            if toks[i] not in ("page", "p", "p1", "tid", "tp"):
                segs.append(f"{toks[i]} {toks[i + 1]}")
            i += 2
            continue
        if not toks[i].isdigit():
            segs.append(toks[i])
        i += 1
    segs = segs[:4]
    keys = sorted({k.lower() for k, _ in parse_qsl(p.query, keep_blank_values=True)} - SESSION_PARAMS)
    if keys:
        segs.append("?" + "&".join(keys[:3]))
    return host, segs


# ---------------------------------------------------------------- build

PAGE_TMPL = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="../assets/page.css">
</head><body class="{kind}" data-id="{id}">
<div class="wc-standalone" hidden>Архивная копия · {date} · <a href="../index.html#/view/{id}">открыть в навигаторе</a> · <a href="{wb}" target="_blank" rel="noopener">Wayback</a></div>
<main class="wc-page">{body}</main>
<script src="../assets/page.js"></script>
</body></html>
"""


def fmt_date(ts):
    return f"{ts[6:8]}.{ts[4:6]}.{ts[0:4]}" + (f" {ts[8:10]}:{ts[10:12]}" if len(ts) >= 12 else "")


def load_manifest(archive):
    path = os.path.join(archive, "manifest.jsonl")
    pages, seen = [], set()
    if not os.path.exists(path):
        return pages
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            pages.append(rec)
    return pages


def load_cdx(archive):
    rows = []
    d = os.path.join(archive, "cdx")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    rows.extend(json.load(f))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--out", default="docs", help="output folder (docs/ can be served by GitHub Pages)")
    ap.add_argument("--title", default="forum.wapcity.ru")
    args = ap.parse_args()

    manifest = load_manifest(args.archive)
    cdx = load_cdx(args.archive)
    sources = {}
    sp = os.path.join(args.archive, "sources.json")
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            sources = json.load(f)
    log(f"{len(manifest)} downloaded captures, {len(cdx)} CDX rows")

    pages_dir = os.path.join(args.out, "p")
    media_dir = os.path.join(args.out, "media")
    if os.path.isdir(pages_dir):
        shutil.rmtree(pages_dir)
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(os.path.join(args.out, "assets"), exist_ok=True)

    docs, media = [], []
    for rec in manifest:
        mt = rec.get("mimetype", "")
        if rec["file"].endswith((".gif", ".png", ".jpg", ".wbmp", ".bin", ".css")) or mt.startswith("image/"):
            media.append(rec)
        else:
            docs.append(rec)

    resolver = Resolver(docs)
    for rec in media:
        src = os.path.join(args.archive, rec["file"])
        if not os.path.exists(src):
            continue
        os.makedirs(media_dir, exist_ok=True)
        name = os.path.basename(rec["file"])
        shutil.copyfile(src, os.path.join(media_dir, name))
        resolver.media[normalize(rec["url"])] = f"../media/{name}"

    index, search = [], {}
    inbound = Counter()
    for n, rec in enumerate(docs, 1):
        src = os.path.join(args.archive, rec["file"])
        if not os.path.exists(src):
            continue
        with open(src, "rb") as f:
            body = f.read()
        text, enc = decode(body, rec.get("contentType", ""))
        is_wml = "wml" in rec.get("mimetype", "") or bool(re.search(r"<wml[\s>]", text[:4000], re.I))
        rw = Rewriter(rec, resolver, is_wml)
        try:
            rw.feed(text)
            rw.close()
            out_html = rw.result()
        except Exception as e:  # malformed beyond repair: show as plain text
            log(f"  parse error in {rec['url']}: {e}")
            out_html = f"<pre>{html.escape(text)}</pre>"
        plain = re.sub(r"\s+", " ", " ".join(rw.text)).strip()
        title = rw.title or (plain[:80] if plain else rec["url"])
        for pid in rw.local_links:
            inbound[pid] += 1
        kind = classify(rec["url"])
        with open(os.path.join(pages_dir, f"{rec['id']}.html"), "w", encoding="utf-8") as f:
            f.write(PAGE_TMPL.format(
                title=html.escape(title), kind="wml" if is_wml else "html", id=rec["id"],
                date=fmt_date(rec["timestamp"]), wb=html.escape(wayback(rec["timestamp"], rec["url"])),
                body=out_html))
        host, segs = section_of(rec["url"])
        index.append({
            "id": rec["id"], "ts": rec["timestamp"], "url": rec["url"], "n": normalize(rec["url"]),
            "t": title[:200], "k": kind, "h": host, "s": segs, "d": describe(rec["url"]), "fmt": "wml" if is_wml else "html",
            "enc": enc, "sz": rec.get("size", len(body)), "sn": plain[:SNIPPET_LEN],
        })
        search[rec["id"]] = plain[:SEARCH_TEXT_LEN].lower()
        if n % 500 == 0:
            log(f"  {n}/{len(docs)} pages")

    for item in index:
        item["in"] = inbound.get(item["id"], 0)

    # every URL the Wayback index knows about, archived locally or not
    known = {}
    for r in cdx:
        k = normalize(r["original"])
        e = known.get(k)
        if not e:
            e = known[k] = {"u": r["original"], "c": 0, "f": r["timestamp"], "l": r["timestamp"], "st": set()}
        e["c"] += 1
        e["f"] = min(e["f"], r["timestamp"])
        e["l"] = max(e["l"], r["timestamp"])
        e["st"].add(r["statuscode"])
    latest_local = {}
    for i in index:
        if i["n"] not in latest_local or latest_local[i["n"]]["ts"] < i["ts"]:
            latest_local[i["n"]] = i
    known_list = []
    for k, e in sorted(known.items()):
        loc = latest_local.get(k)
        h, sg = section_of(e["u"])
        known_list.append({"u": e["u"], "c": e["c"], "f": e["f"], "l": e["l"], "h": h, "s": sg,
                           "st": ",".join(sorted(e["st"])), "id": loc["id"] if loc else None})

    per_month = Counter(r["timestamp"][:6] for r in cdx) if cdx else Counter(i["ts"][:6] for i in index)

    data = {
        "title": args.title,
        "pages": index,
        "known": known_list,
        "perMonth": sorted(per_month.items()),
        "types": TYPE_LABELS,
        "sources": sources,
        "cdxTotal": len(cdx),
    }
    with open(os.path.join(args.out, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.WC_DATA=")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    with open(os.path.join(args.out, "search.js"), "w", encoding="utf-8") as f:
        f.write("window.WC_SEARCH=")
        json.dump(search, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    for name in os.listdir(TEMPLATE_DIR):
        dst = os.path.join(args.out, name) if name == "index.html" else os.path.join(args.out, "assets", name)
        shutil.copyfile(os.path.join(TEMPLATE_DIR, name), dst)

    log(f"site written to {args.out}/index.html — {len(index)} pages, {len(known_list)} known URLs")


if __name__ == "__main__":
    main()
