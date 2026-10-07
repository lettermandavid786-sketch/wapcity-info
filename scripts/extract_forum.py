#!/usr/bin/env python3
"""Extract the forum itself from the archived pages of forum.wapcity.ru.

    python3 scripts/extract_forum.py              # archive/ -> docs/forum.js

The pages are WML produced by "wapForum" (Мобитех / Мобидо.ру). Two engines
appear in the archive:

  2006–2007  /index.jsp, /auth, /part?root=N        sections with message counters
  2007–2017  /f, /t/f/<forum>, /m/f/<forum>/t/<topic>, /user/id/<id>, /news/id/<n>
             and the same under /oldf, /oldt, /oldm, /oldu (the forum's own archive)

Everything is merged across captures: a topic seen in several snapshots keeps
every distinct message. Personal data from profiles (phone, e-mail, ICQ, real
name, birthday) is never extracted, and phone-like numbers in message text are
masked.
"""
import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_site import clean_path, decode, load_cdx, load_manifest  # noqa: E402

TIME_RE = re.compile(r"\((\d{1,2}):(\d{2}):(\d{2})\s+(\d{2})\.(\d{2})\.(\d{4})\)\s*$")
DATE_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})\s+(\d{2})\.(\d{2})\.(\d{4})")
COUNT_RE = re.compile(r"\s*\[(\d+)\]\s*$")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?[78]|\+?38)?[\s\-(]*9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")

OPERATORS = {
    "wap.mts.ru": "МТС",
    "wap.djuice.com.ua": "djuice (Киевстар, Украина)",
    "wap.ncc.nnov.ru": "НСС (Нижний Новгород)",
    "wap.on16.ru": "on16.ru",
    "wapcity.ru": "WapCity.ru",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def iso(m):
    h, mi, s, d, mo, y = m.groups()
    return f"{y}-{mo}-{d} {int(h):02d}:{mi}:{s}"


def mask(text):
    return PHONE_RE.sub("•••", text)


def clean(text):
    return re.sub(r"[ \t\r\f\v ]+", " ", text).strip()


class Blocks(HTMLParser):
    """Splits a WML/XHTML page into paragraphs of ('t', text) / ('a', href, text) items."""

    BREAK = {"p", "div", "card", "tr", "li", "table", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.cur, self.link, self.skip = [], [], None, 0
        self.title = None

    def flush(self):
        items = [i for i in self.cur if (i[0] == "a") or clean(i[1])]
        if items:
            self.paras.append(items)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "head", "onevent", "template", "select"):
            self.skip += 1
        if tag == "card" and a.get("title") and not self.title:
            self.title = a["title"]
        if tag in self.BREAK:
            self.flush()
        elif tag == "br":
            self.cur.append(("t", "\n"))
        elif tag == "a":
            self.link = [a.get("href", ""), []]
        elif tag == "anchor":
            self.link = ["", []]
        elif tag == "go" and self.link is not None and not self.link[0]:
            self.link[0] = a.get("href", "")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in ("a", "anchor"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "onevent", "template", "select"):
            self.skip = max(0, self.skip - 1)
        if tag in ("a", "anchor") and self.link is not None:
            href, parts = self.link
            self.cur.append(("a", href, clean("".join(parts))))
            self.link = None
        elif tag in self.BREAK:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.link is not None:
            self.link[1].append(data)
        else:
            self.cur.append(("t", data))

    def close(self):
        super().close()
        self.flush()
        return self.paras


NAV = {"назад", "на главную", "на головну", "главная", "wapcity.ru", "еще...", "отправить", "на форум",
       "вход на форум", "сообщение администратору", "добавить в закладки"}
FORM_LABELS = ("Тема сообщения", "Текст сообщения", "Телефон для", "Тема повідомлення", "Текст повідомлення",
               "Телефон для зворотного", "Имя:", "Пароль:", "Ім’я:")


def para_text(items):
    return clean("".join(i[1] if i[0] == "t" else i[2] for i in items))


def content_lines(paras):
    """Readable lines of a text page: navigation links and form labels removed."""
    out = []
    for para in paras:
        buf = ""
        for i in para:
            if i[0] == "a":
                if i[2].strip(" :\\").lower() in NAV or i[1].startswith(("/index", "http")) and len(i[2]) < 25:
                    buf += "\n"
                    continue
                buf += i[2]
            else:
                buf += i[1]
        for line in buf.split("\n"):
            line = clean(line)
            if line and not line.startswith(FORM_LABELS):
                out.append(line)
    return out


def lead_text(paras):
    """Text before the first link of the page (greeting lines)."""
    out = []
    for para in paras:
        for i in para:
            if i[0] == "a":
                return clean(" ".join(out))
            out.append(i[1])
    return clean(" ".join(out))


def page_paras(text):
    b = Blocks()
    b.feed(text)
    paras = b.close()
    # drop the logo-only paragraph every page starts with
    return [p for p in paras if para_text(p) or any(i[0] == "a" and i[2] for i in p)], b.title


GALLERY_KINDS = [
    ("portal", re.compile(r"^/(index\.(jsp|html))?$"), "Главная страница"),
    ("sections", re.compile(r"^/auth$"), "Рубрики (старый движок)"),
    ("forums", re.compile(r"^/f$"), "Список форумов"),
    ("topics", re.compile(r"^/t/f/\d+$"), "Темы раздела"),
    ("thread", re.compile(r"^/m/f/\d+/t/\d+$"), "Сообщения темы"),
    ("archive", re.compile(r"^/oldf$|^/oldt/f/\d+"), "Архив форума"),
    ("news", re.compile(r"^/news/id/\d+"), "Новость"),
    ("profile", re.compile(r"^/user/id/\d+$"), "Анкета пользователя"),
    ("about", re.compile(r"^/info$"), "Служебная страница"),
    ("rules", re.compile(r"^/rlz$"), "Правила"),
    ("fame", re.compile(r"^/vc$"), "Зал славы"),
]


def gallery_kind(path):
    for kind, rx, _ in GALLERY_KINDS:
        if rx.search(path):
            return kind
    return None


class Forum:
    def __init__(self):
        self.hosts = defaultdict(lambda: {"captures": 0, "first": None, "last": None, "links": set(),
                                          "engines": set(), "greetings": {}})
        self.forums = {}      # (host, old, fid) -> {...}
        self.topics = {}      # (host, old, fid, tid) -> {...}
        self.posts = {}       # dedupe key -> post
        self.users = {}       # (host, uid) -> {...}
        self.news = {}        # (host, nid) -> {...}
        self.info = {}        # (host, page) -> {...}
        self.sections = []    # old engine counters: {host, ts, name, count}
        self.growth = []      # newest registered user id over time
        self.newbies = []
        self.moderators = defaultdict(dict)
        self.extras = {}      # hall of fame, rules, birthdays …
        self.gallery = {}     # (kind, host, year) -> first capture, for the "how it looked" strip

    # -- helpers
    def touch_host(self, host, ts, engine):
        h = self.hosts[host]
        if engine:
            ef = h.setdefault("engineFirst", {})
            ef[engine] = min(ef.get(engine) or ts, ts)
            el = h.setdefault("engineLast", {})
            el[engine] = max(el.get(engine) or ts, ts)
        h["captures"] += 1
        h["first"] = min(h["first"] or ts, ts)
        h["last"] = max(h["last"] or ts, ts)
        if engine:
            h["engines"].add(engine)

    def user(self, host, uid, nick, ts):
        u = self.users.setdefault((host, uid), {"nick": nick, "nicks": set(), "posts": 0, "topics": set(),
                                                 "first": ts, "last": ts})
        if nick:
            u["nicks"].add(nick)
            u["nick"] = nick
        u["first"] = min(u["first"], ts)
        u["last"] = max(u["last"], ts)
        return u

    def forum(self, host, old, fid, name=None, ts=None):
        f = self.forums.setdefault((host, old, fid), {"name": None, "topics": set(), "first": ts, "last": ts,
                                                      "seen": 0})
        if name and not f["name"]:
            f["name"] = name
        if ts:
            f["first"] = min(f["first"] or ts, ts)
            f["last"] = max(f["last"] or ts, ts)
        return f

    def topic(self, host, old, fid, tid, ts):
        key = (host, old, fid, tid)
        t = self.topics.setdefault(key, {"title": None, "short": None, "count": 0, "lastBy": None, "lastAt": None,
                                         "first": ts, "srcs": set()})
        t["first"] = min(t["first"], ts)
        self.forum(host, old, fid, ts=ts)["topics"].add(tid)
        return t

    # -- page handlers
    def handle(self, rec, text):
        p = urlsplit(rec["url"])
        host = re.sub(r"^www\.", "", (p.hostname or "").lower())
        path = clean_path(p.path)
        query = dict(parse_qsl(p.query))
        ts = rec["timestamp"]
        paras, card_title = page_paras(text)
        flat = [para_text(x) for x in paras]
        self._lines = content_lines(paras)
        old = path.startswith("/old")
        links = [i for para in paras for i in para if i[0] == "a"]

        # "old": the 2005 JSP engine (/index.jsp, /auth, /part?root=N); "new": the /f /t/f /m/f engine
        if re.match(r"/(old)?(f|t/f|m/f|user|oldu|news|foto|vc|bd|rlz|mbox|cab)(/|$)|/index\.html", path):
            engine = "new"
        elif path in ("/index.jsp", "/auth", "/part", "/view", "/signup", "/info") or ";jsessionid" in rec["url"]:
            engine = "old"
        else:
            engine = None
        self.touch_host(host, ts, engine)
        for i in links:
            m = re.match(r"https?://([^/;?:]+)", i[1])
            if m:
                self.hosts[host]["links"].add(m.group(1).lower())

        kind = gallery_kind(path)
        if kind:
            self.gallery.setdefault((kind, host, ts[:4]), {"id": rec["id"], "ts": ts, "host": host, "kind": kind})

        if re.fullmatch(r"/(old)?f", path):
            return self.forum_list(host, old, paras, ts)
        m = re.fullmatch(r"/(?:old)?t/f/(\d+)(?:/page/(\d+))?", path)
        if m:
            return self.topic_list(host, old, int(m.group(1)), paras, flat, ts, rec)
        m = re.fullmatch(r"/(?:old)?m/f/(\d+)/t/(\d+)(?:/.*)?", path)
        if m and "/a/s" not in path:
            return self.messages(host, old, int(m.group(1)), int(m.group(2)), paras, flat, ts, rec)
        m = re.fullmatch(r"/(?:user|oldu)/id/(\d+)(?:/.*)?", path)
        if m:
            return self.profile(host, int(m.group(1)), flat, ts)
        m = re.fullmatch(r"/news/id/(\d+)(?:/page/\d+)?", path)
        if m:
            return self.news_item(host, int(m.group(1)), paras, flat, ts, rec)
        if path == "/auth":
            return self.sections_page(host, paras, flat, ts)
        if path in ("/index.jsp", "/", "/index.html") or path.startswith("/index.html/l/"):
            return self.portal(host, paras, flat, links, ts, rec)
        if path == "/info":
            return self.info_page(host, query.get("page", "info"), flat, ts, rec)
        if path in ("/rlz", "/vc", "/news"):  # /bd (birthdays) is left out on purpose
            return self.extra(host, path[1:], flat, ts, rec)

    def forum_list(self, host, old, paras, ts):
        for para in paras:
            for i in para:
                if i[0] != "a":
                    continue
                m = re.match(r"/(?:old)?t/f/(\d+)", clean_path(urlsplit(i[1]).path))
                if m and i[2]:
                    f = self.forum(host, old, int(m.group(1)), COUNT_RE.sub("", i[2]), ts)
                    f["seen"] += 1

    def topic_list(self, host, old, fid, paras, flat, ts, rec):
        f = self.forum(host, old, fid, ts=ts)
        header = next((t for t in flat if t and not t.startswith("*")), None)
        if header and len(header) < 60 and not f["name"]:
            f["name"] = header
        for para in paras:
            text = para_text(para)
            if text.startswith("Модератор"):
                for i in para:
                    if i[0] == "a":
                        m = re.search(r"/(?:user|oldu)/id/(\d+)", i[1])
                        if m:
                            self.moderators[(host, fid)][int(m.group(1))] = i[2].rstrip(",")
                continue
            for idx, i in enumerate(para):
                if i[0] != "a":
                    continue
                m = re.search(r"/(?:old)?m/f/(\d+)/t/(\d+)", i[1])
                if not m:
                    continue
                t = self.topic(host, old, int(m.group(1)), int(m.group(2)), ts)
                t["srcs"].add(rec["id"])
                title = i[2]
                cm = COUNT_RE.search(title)
                if cm:
                    t["count"] = max(t["count"], int(cm.group(1)))
                    title = COUNT_RE.sub("", title)
                if title and (not t["title"] or ts >= t.get("titleTs", "")):
                    t["title"], t["titleTs"] = clean(title), ts
                rest = clean("".join(j[1] if j[0] == "t" else j[2] for j in para[idx + 1:]))
                dm = DATE_RE.search(rest)
                if dm:
                    at = iso(dm)
                    by = clean(rest[:dm.start()].strip(" ("))
                    if not t["lastAt"] or at > t["lastAt"]:
                        t["lastAt"], t["lastBy"] = at, by or None

    def messages(self, host, old, fid, tid, paras, flat, ts, rec):
        t = self.topic(host, old, fid, tid, ts)
        t["srcs"].add(rec["id"])
        for line in flat[:3]:
            if " : " in line:
                fname, short = line.split(" : ", 1)
                self.forum(host, old, fid, fname.strip(), ts)
                if not t["short"]:
                    t["short"] = short.strip()
                break
        for para in paras:
            ui = None
            for idx, i in enumerate(para):
                if i[0] == "a" and re.search(r"/(?:user|oldu)/id/\d+", i[1]) and i[2]:
                    ui = idx
                    break
            if ui is None:
                continue
            uid = int(re.search(r"/(?:user|oldu)/id/(\d+)", para[ui][1]).group(1))
            nick = para[ui][2]
            body = "".join(j[1] if j[0] == "t" else j[2] for j in para[ui + 1:])
            body = re.sub(r"[ \t ]+", " ", body).strip()
            tm = TIME_RE.search(body)
            at = iso(tm) if tm else None
            if tm:
                body = body[:tm.start()].strip()
            body = re.sub(r"\n\s*\n+", "\n", body).strip()
            if not body and not at:
                continue
            key = (host, old, fid, tid, uid, at, body[:200])
            if key in self.posts:
                continue
            self.posts[key] = {"host": host, "old": old, "f": fid, "t": tid, "u": uid, "n": nick, "at": at,
                               "x": mask(body), "src": rec["id"]}
            u = self.user(host, uid, nick, ts)
            u["posts"] += 1
            u["topics"].add((old, fid, tid))

    def profile(self, host, uid, flat, ts):
        u = self.user(host, uid, None, ts)
        for line in self._lines:
            m = re.match(r"(.+?)\s*\(([^()]+)\)$", line)
            if m and not u.get("status") and "сообщ" not in line and len(line) < 60:
                u["nick"] = u["nick"] or m.group(1)
                u["nicks"].add(m.group(1))
                u["status"] = m.group(2)
            for label, field in (("количество сообщений", "msgs"), ("количество штрафов", "fines")):
                if line.startswith(label):
                    n = re.search(r"(\d+)", line)
                    if n:
                        u[field] = max(u.get(field, 0), int(n.group(1)))
            for label, field in (("дата регистрации", "reg"), ("дата последнего входа", "seen")):
                if line.startswith(label):
                    d = DATE_RE.search(line)
                    if d:
                        val = iso(d)
                        if field == "reg":
                            u[field] = min(u.get(field) or val, val)
                        else:
                            u[field] = max(u.get(field) or val, val)
            if line.startswith("пол:"):
                g = line.split(":", 1)[1].strip()
                if g in ("мужской", "женский"):
                    u["gender"] = g

    def news_item(self, host, nid, paras, flat, ts, rec):
        body = [l for l in content_lines(paras) if not re.fullmatch(r"[\d\s]+", l)]
        n = self.news.setdefault((host, nid), {"title": None, "date": None, "text": "", "src": rec["id"], "ts": ts})
        if body and not n["title"]:
            n["title"] = body[0][:120]
        text = "\n".join(body[1:])
        if len(text) > len(n["text"]):
            n["text"], n["src"] = mask(text), rec["id"]

    def sections_page(self, host, paras, flat, ts):
        g = lead_text(paras)
        if g:
            self.hosts[host]["greetings"][ts] = g
        for para in paras:
            for i in para:
                if i[0] == "a" and "/part" in i[1]:
                    m = COUNT_RE.search(i[2])
                    if m:
                        self.sections.append({"host": host, "ts": ts, "name": COUNT_RE.sub("", i[2]),
                                              "count": int(m.group(1))})

    def portal(self, host, paras, flat, links, ts, rec):
        ids = []
        for i in links:
            m = re.search(r"[?&]aid=(\d+)", i[1]) or re.search(r"/user/id/(\d+)/frm/mn", i[1])
            if m and i[2]:
                ids.append(int(m.group(1)))
                self.newbies.append({"host": host, "ts": ts, "id": int(m.group(1)), "nick": i[2],
                                     "kind": "aid" if "aid=" in i[1] else "uid"})
        if ids:
            self.growth.append({"host": host, "ts": ts, "max": max(ids), "kind": "aid" if any(
                "aid=" in i[1] for i in links) else "uid"})
        g = lead_text(paras)
        if g and not g.startswith(("Приветствуем", "wapForum")) and len(g) < 300:
            self.hosts[host]["greetings"][ts] = g
        # news list on the 2007+ portal: date line followed by a /news/id link
        last_date = None
        for para in paras:
            for i in para:
                if i[0] == "t":
                    d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", i[1])
                    if d:
                        last_date = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
                elif "/news/id/" in i[1]:
                    m = re.search(r"/news/id/(\d+)", i[1])
                    n = self.news.setdefault((host, int(m.group(1))), {"title": None, "date": None, "text": "",
                                                                       "src": None, "ts": ts})
                    n["title"] = n["title"] or i[2]
                    n["date"] = n["date"] or last_date

    def info_page(self, host, page, flat, ts, rec):
        body = self._lines
        cur = self.info.get((host, page))
        if not cur or ts > cur["ts"]:
            self.info[(host, page)] = {"title": body[0] if body else page, "text": "\n".join(body[1:]),
                                       "ts": ts, "src": rec["id"]}

    def extra(self, host, kind, flat, ts, rec):
        body = self._lines
        cur = self.extras.get((host, kind))
        if not cur or len("\n".join(body)) > len(cur["text"]):
            self.extras[(host, kind)] = {"title": body[0] if body else kind, "text": mask("\n".join(body[1:])),
                                         "ts": ts, "src": rec["id"]}

    # -- output
    def gallery_list(self):
        """A few captures per kind of page, spread over hosts and years."""
        labels = {k: label for k, _, label in GALLERY_KINDS}
        out = []
        for kind, _, _ in GALLERY_KINDS:
            items = sorted((v for (k, _, _), v in self.gallery.items() if k == kind), key=lambda v: v["ts"])
            seen_hosts, picked = set(), []
            for v in items:  # one per host first, then other years
                if v["host"] not in seen_hosts:
                    seen_hosts.add(v["host"])
                    picked.append(v)
            for v in items:
                if v not in picked and v["ts"][:4] not in {p["ts"][:4] for p in picked}:
                    picked.append(v)
            for v in sorted(picked, key=lambda v: v["ts"])[:4]:
                out.append({**v, "label": labels[kind]})
        return out

    def export(self, cdx_rows):
        topics = []
        tkey = {}
        for (host, old, fid, tid), t in sorted(self.topics.items()):
            tkey[(host, old, fid, tid)] = len(topics)
            topics.append({"h": host, "o": old, "f": fid, "t": tid,
                           "title": t["title"] or (t["short"] or "").rstrip(".") or f"Тема {tid}",
                           "full": bool(t["title"]), "count": t["count"], "lastBy": t["lastBy"],
                           "lastAt": t["lastAt"], "src": sorted(t["srcs"])[:5]})
        posts = sorted(self.posts.values(), key=lambda p: (p["host"], p["old"], p["f"], p["t"], p["at"] or ""))
        for p in posts:
            p["k"] = tkey[(p["host"], p["old"], p["f"], p["t"])]
            for k in ("host", "old", "f", "t"):
                del p[k]
        forums = []
        for (host, old, fid), f in sorted(self.forums.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
            forums.append({"h": host, "o": old, "f": fid, "name": f["name"] or f"Форум {fid}",
                           "topics": len(f["topics"]), "first": f["first"], "last": f["last"],
                           "mods": sorted(self.moderators.get((host, fid), {}).values())})
        users = []
        for (host, uid), u in sorted(self.users.items()):
            users.append({"h": host, "id": uid, "nick": u["nick"] or f"#{uid}",
                          "aka": sorted(n for n in u["nicks"] if n != u["nick"])[:5],
                          "status": u.get("status"), "msgs": u.get("msgs"), "reg": u.get("reg"),
                          "seen": u.get("seen"), "gender": u.get("gender"), "posts": u["posts"],
                          "topics": len(u["topics"])})
        hosts = {}
        for host, h in self.hosts.items():
            ops = sorted({OPERATORS[l] for l in h["links"] if l in OPERATORS and l != "wapcity.ru"})
            hosts[host] = {"captures": h["captures"], "first": h["first"], "last": h["last"],
                           "engines": sorted(h["engines"]), "engineFirst": h.get("engineFirst", {}),
                           "engineLast": h.get("engineLast", {}),
                           "operators": ops,
                           "greetings": [{"ts": k, "text": v} for k, v in sorted(h["greetings"].items())]}
        per_year = defaultdict(int)
        cdx_hosts = defaultdict(lambda: {"first": None, "last": None, "n": 0})
        for r in cdx_rows:
            per_year[r["timestamp"][:4]] += 1
            hn = re.sub(r"^www\.", "", (urlsplit(r["original"]).hostname or "").lower())
            c = cdx_hosts[hn]
            c["n"] += 1
            c["first"] = min(c["first"] or r["timestamp"], r["timestamp"])
            c["last"] = max(c["last"] or r["timestamp"], r["timestamp"])
        return {
            "hosts": hosts,
            "forums": forums,
            "topics": topics,
            "posts": posts,
            "users": users,
            "news": [{"h": h, "id": i, **n} for (h, i), n in sorted(self.news.items())],
            "info": [{"h": h, "page": pg, **v} for (h, pg), v in sorted(self.info.items())],
            "extras": [{"h": h, "kind": k, **v} for (h, k), v in sorted(self.extras.items())],
            "sections": self.sections,
            "growth": sorted(self.growth, key=lambda g: g["ts"]),
            "newbies": self.newbies[-60:],
            "capturesPerYear": sorted(per_year.items()),
            "cdxHosts": dict(cdx_hosts),
            "cdxTotal": len(cdx_rows),
            "gallery": self.gallery_list(),
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default="archive")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()
    F = Forum()
    manifest = load_manifest(args.archive)
    for rec in manifest:
        if not rec["file"].endswith((".wml", ".html", ".txt")):
            continue
        path = os.path.join(args.archive, rec["file"])
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            text, _ = decode(f.read(), rec.get("contentType", ""))
        try:
            F.handle(rec, text)
        except Exception as e:  # one odd page must not stop the rest
            log(f"  skipped {rec['url']}: {e!r}")
    data = F.export(load_cdx(args.archive))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "forum.js"), "w", encoding="utf-8") as f:
        f.write("window.WC_FORUM=")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    log(f"forum: {len(data['forums'])} forums, {len(data['topics'])} topics, {len(data['posts'])} posts, "
        f"{len(data['users'])} users, {len(data['news'])} news")


if __name__ == "__main__":
    main()
