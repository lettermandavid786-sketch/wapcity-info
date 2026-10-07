#!/usr/bin/env python3
"""Download every archived page of forum.wapcity.ru from the Wayback Machine.

Uses only the standard library. Re-running resumes: files already on disk are
skipped, so an interrupted crawl can simply be restarted.

    python3 scripts/fetch_wayback.py                      # forum.wapcity.ru, latest version of each URL
    python3 scripts/fetch_wayback.py --versions 0         # every distinct version of each URL
    python3 scripts/fetch_wayback.py --domain forum.wapcity.ru --domain wap.wapcity.ru

Output (default ./archive):
    archive/cdx/<domain>.json   raw CDX index rows
    archive/raw/<ts>/<id>.<ext> page bodies exactly as archived (id_ mode, no Wayback toolbar)
    archive/manifest.jsonl      one JSON line per downloaded capture
    archive/sources.json        what was queried, when, and how many captures were found
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

CDX_FIELDS = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
TEXT_TYPES = ("text/", "application/xhtml", "application/vnd.wap", "application/xml", "unk", "warc/revisit")
EXT_BY_TYPE = {
    "text/vnd.wap.wml": "wml",
    "application/vnd.wap.xhtml+xml": "html",
    "application/xhtml+xml": "html",
    "text/html": "html",
    "text/plain": "txt",
    "text/css": "css",
    "image/gif": "gif",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/vnd.wap.wbmp": "wbmp",
}

UA = "wapcity-archive/1.0 (personal archival research; contact via repository)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, retries=5, timeout=90):
    delay = 2
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 403, 410):
                return e.code, "", b""
            if attempt == retries - 1:
                raise
            # 429 / 5xx: Wayback asks clients to slow down; ignoring 429 gets the IP blocked
            wait = 60 * (attempt + 1) if e.code == 429 else delay
            log(f"  HTTP {e.code}, retry in {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries - 1:
                raise
            log(f"  {e}, retry in {delay}s")
            time.sleep(delay)
        delay *= 2


def cdx_query(base, domain, match_type):
    """Return all CDX rows for a domain, following pagination."""
    params = {
        "url": domain,
        "matchType": match_type,
        "output": "json",
        "fl": ",".join(CDX_FIELDS),
    }
    q = urllib.parse.urlencode(params)
    status, _, body = http_get(f"{base}/cdx/search/cdx?{q}&showNumPages=true")
    pages = 1
    if status == 200:
        try:
            pages = max(1, int(body.decode().strip() or 1))
        except ValueError:
            pages = 1
    rows = []
    for p in range(pages):
        status, _, body = http_get(f"{base}/cdx/search/cdx?{q}&page={p}")
        if status != 200 or not body.strip():
            continue
        data = json.loads(body)
        if data and data[0] == CDX_FIELDS:
            data = data[1:]
        rows.extend(dict(zip(CDX_FIELDS, r)) for r in data)
        log(f"  CDX page {p + 1}/{pages}: {len(rows)} rows")
        time.sleep(1)
    return rows


def pick_captures(rows, versions, include_assets, include_redirects):
    """Group by canonical URL and choose which captures to download."""
    by_key = defaultdict(list)
    for r in rows:
        mt = (r["mimetype"] or "").lower()
        if not include_assets and not mt.startswith(TEXT_TYPES):
            continue
        sc = r["statuscode"]
        ok = sc == "200" or sc == "-" or (include_redirects and sc.startswith("3"))
        if not ok:
            continue
        by_key[r["urlkey"]].append(r)
    chosen = []
    for key, caps in by_key.items():
        caps.sort(key=lambda r: r["timestamp"])
        seen, uniq = set(), []
        for c in caps:
            if c["digest"] in seen:
                continue
            seen.add(c["digest"])
            uniq.append(c)
        if versions > 0 and len(uniq) > versions:
            if versions == 1:
                uniq = [uniq[-1]]
            else:
                # spread picks across the timeline, always keeping first and last
                step = (len(uniq) - 1) / (versions - 1)
                uniq = [uniq[round(i * step)] for i in range(versions)]
        chosen.extend(uniq)
    chosen.sort(key=lambda r: (r["timestamp"], r["original"]))
    return chosen


def capture_id(c):
    return hashlib.sha1(f'{c["timestamp"]} {c["original"]}'.encode()).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", action="append", help="host to crawl (repeatable); default forum.wapcity.ru")
    ap.add_argument("--match", default="domain", choices=["exact", "prefix", "host", "domain"],
                    help="CDX matchType (domain = host and all subdomains)")
    ap.add_argument("--out", default="archive")
    ap.add_argument("--versions", type=int, default=1,
                    help="distinct versions per URL to keep: 1 = latest only, N = spread over time, 0 = all")
    ap.add_argument("--assets", action="store_true", help="also download images/css")
    ap.add_argument("--redirects", action="store_true", help="also keep 3xx captures")
    ap.add_argument("--limit", type=int, default=0, help="stop after N downloads (testing)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between downloads")
    ap.add_argument("--wayback-base", default="https://web.archive.org")
    ap.add_argument("--cdx-only", action="store_true", help="only fetch the index, download nothing")
    ap.add_argument("--refresh-cdx", action="store_true", help="re-query the index even if archive/cdx/ has it")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop downloading after this long (rerun to continue); sources.json records completeness")
    args = ap.parse_args()
    domains = args.domain or ["forum.wapcity.ru"]

    os.makedirs(os.path.join(args.out, "cdx"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "raw"), exist_ok=True)

    started = time.monotonic()
    all_rows, sources = [], []
    for d in domains:
        cdx_path = os.path.join(args.out, "cdx", f"{d}.json")
        if os.path.exists(cdx_path) and not args.refresh_cdx:
            log(f"CDX: reusing {cdx_path}")
            with open(cdx_path, encoding="utf-8") as f:
                rows = json.load(f)
        else:
            log(f"CDX query: {d} (matchType={args.match})")
            rows = cdx_query(args.wayback_base, d, args.match)
            with open(cdx_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        ts = sorted(r["timestamp"] for r in rows)
        sources.append({
            "source": "Wayback Machine (web.archive.org)",
            "query": d,
            "matchType": args.match,
            "captures": len(rows),
            "uniqueUrls": len({r["urlkey"] for r in rows}),
            "first": ts[0] if ts else None,
            "last": ts[-1] if ts else None,
        })
        all_rows.extend(rows)

    chosen = pick_captures(all_rows, args.versions, args.assets, args.redirects)
    log(f"{len(all_rows)} captures in index, {len(chosen)} selected for download")

    sources_doc = {"fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "versionsPerUrl": args.versions, "selected": len(chosen), "complete": False,
                   "sources": sources}

    def write_sources():
        with open(os.path.join(args.out, "sources.json"), "w", encoding="utf-8") as f:
            json.dump(sources_doc, f, ensure_ascii=False, indent=2)

    write_sources()
    if args.cdx_only:
        return

    manifest_path = os.path.join(args.out, "manifest.jsonl")
    done = set()
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    done.add(json.loads(line)["id"])

    n, stopped_early, failed = 0, False, 0
    with open(manifest_path, "a", encoding="utf-8") as mf:
        for i, c in enumerate(chosen, 1):
            cid = capture_id(c)
            if cid in done:
                continue
            if (args.limit and n >= args.limit) or \
                    (args.max_minutes and time.monotonic() - started > args.max_minutes * 60):
                stopped_early = True
                break
            mt = (c["mimetype"] or "").split(";")[0].strip().lower()
            ext = EXT_BY_TYPE.get(mt, "bin" if not mt.startswith(TEXT_TYPES) else "html")
            rel = os.path.join("raw", c["timestamp"][:4], f"{c['timestamp']}_{cid}.{ext}")
            path = os.path.join(args.out, rel)
            url = f"{args.wayback_base}/web/{c['timestamp']}id_/{c['original']}"
            log(f"[{i}/{len(chosen)}] {c['timestamp']} {c['original']}")
            try:
                status, ctype, body = http_get(url)
            except Exception as e:  # keep going; a rerun retries the failures
                log(f"  FAILED: {e}")
                failed += 1
                continue
            if status != 200:
                log(f"  skipped: HTTP {status}")
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
            rec = {"id": cid, "timestamp": c["timestamp"], "url": c["original"], "urlkey": c["urlkey"],
                   "mimetype": mt, "contentType": ctype, "status": c["statuscode"], "digest": c["digest"],
                   "file": rel.replace(os.sep, "/"), "size": len(body)}
            mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mf.flush()
            n += 1
            time.sleep(args.delay)
    sources_doc["downloaded"] = len(done) + n
    sources_doc["complete"] = not stopped_early
    sources_doc["failedLastRun"] = failed
    write_sources()
    log(f"done: {n} new files" + (" (time budget reached, rerun to continue)" if stopped_early else ""))


if __name__ == "__main__":
    main()
