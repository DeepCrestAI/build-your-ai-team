#!/usr/bin/env python3
"""Build Your AI Team, Part 1: save your published posts as Markdown files.

Reads one of:
  --fetch     your Substack's address: reads its public archive, no sign-in needed.
              Paid posts come back only up to the paywall.
  --export    the .zip from Substack's Settings > Export, for the full text of paid
              posts (reads the posts only, never the subscriber list)
  --linkedin  Shares.csv from LinkedIn's "Get a copy of your data"

and writes, inside --folder (default: the current folder):
  substack/posts/YYYY-MM-DD_title_words.md   one file per post
  substack/index.md                          every post, with likes, comments
                                            and restacks
  (LinkedIn: linkedin/posts/ and linkedin/index.md)

Run it again to add new posts and refresh the counts. A post file is never
rewritten, except when an earlier run could only read part of it.
"""

import argparse
import csv
import io
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

COLUMNS = ["title", "date", "link", "author", "guest", "audience", "read",
           "source", "likes", "comments", "restacks", "file"]
USER_AGENT = "Mozilla/5.0 (DeepCrest Substack reader)"
AUDIENCE = {"everyone": "free", "only_free": "free", "only_paid": "paid",
            "founding": "founding"}

SKIP_TAGS = {"script", "style", "svg", "button", "form", "input", "noscript",
             "iframe", "select", "textarea"}
SKIP_CLASSES = ("subscription-widget", "button-wrapper", "captioned-button",
                "image-link-expand", "digest-post-embed", "restack-image",
                "view-image", "paywall", "share-dialog", "post-ufi")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}


class ToMarkdown(HTMLParser):
    """Turns a Substack post body into plain Markdown."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buffers = [[]]
        self.skip_tag = None
        self.skip_depth = 0
        self.lists = []
        self.links = []
        self.pre = 0
        self.opened = None

    def emit(self, text):
        self.buffers[-1].append(text)

    def open_mark(self, mark):
        self.emit(mark)
        self.opened = mark

    def close_mark(self, mark):
        """Closes **, * or `, keeping spaces outside it and dropping empty pairs."""
        buffer = self.buffers[-1]
        trailing = ""
        while buffer and buffer[-1].strip() == "" and buffer[-1] != mark:
            trailing = buffer.pop() + trailing
        if buffer and buffer[-1].rstrip() != buffer[-1]:
            text = buffer.pop()
            trailing = text[len(text.rstrip()):] + trailing
            buffer.append(text.rstrip())
        if buffer and buffer[-1] == mark:
            buffer.pop()
        else:
            buffer.append(mark)
        if trailing:
            buffer.append(" ")

    def block(self):
        self.emit("\n\n")

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        attrs = dict(attrs)
        classes = attrs.get("class") or ""
        if tag in SKIP_TAGS or any(c in classes for c in SKIP_CLASSES):
            if tag not in VOID:
                self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in ("p", "div"):
            self.block()
        elif re.fullmatch(r"h[1-6]", tag):
            self.block()
            self.emit("#" * int(tag[1]) + " ")
        elif tag in ("strong", "b") and not self.pre:
            self.open_mark("**")
        elif tag in ("em", "i") and not self.pre:
            self.open_mark("*")
        elif tag == "code" and not self.pre:
            self.open_mark("`")
        elif tag == "pre":
            self.block()
            self.buffers.append([])
            self.pre += 1
        elif tag == "a":
            self.buffers.append([])
            self.links.append(attrs.get("href") or "")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self.emit("\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            marker = "- " if kind[0] == "ul" else "%d. " % kind[1]
            self.emit("\n" + "   " * max(len(self.lists) - 1, 0) + marker)
        elif tag == "blockquote":
            self.block()
            self.buffers.append([])
        elif tag == "br":
            self.emit("\n")
        elif tag == "hr":
            self.block()
            self.emit("---")
            self.block()
        elif tag == "img":
            src = attrs.get("src") or ""
            if src:
                self.emit("![%s](%s)" % ((attrs.get("alt") or "").strip(), src))
        elif tag == "figcaption":
            self.emit("\n\n*")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in VOID:
            return
        if tag in ("p", "div", "ul", "ol") or re.fullmatch(r"h[1-6]", tag):
            if tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            self.block()
        elif tag in ("strong", "b") and not self.pre:
            self.close_mark("**")
        elif tag in ("em", "i") and not self.pre:
            self.close_mark("*")
        elif tag == "code" and not self.pre:
            self.close_mark("`")
        elif tag == "pre" and self.pre:
            self.pre -= 1
            text = "".join(self.buffers.pop()).strip("\n")
            self.emit("```\n%s\n```" % text)
            self.block()
        elif tag == "a" and self.links:
            text = "".join(self.buffers.pop())
            href = self.links.pop()
            if (text.strip() and href and "![" not in text
                    and not href.startswith("javascript")):
                self.emit("[%s](%s)" % (text.strip(), href))
            else:
                self.emit(text)
        elif tag == "blockquote" and len(self.buffers) > 1:
            text = "".join(self.buffers.pop()).strip()
            text = re.sub(r"\n{3,}", "\n\n", text)
            self.emit("\n".join("> " + line if line.strip() else ">"
                                for line in text.split("\n")))
            self.block()
        elif tag == "figcaption":
            self.emit("*\n\n")

    def handle_data(self, data):
        if self.skip_tag:
            return
        if self.pre:
            self.emit(data)
            return
        data = re.sub(r"\s+", " ", data)
        buffer = self.buffers[-1]
        if self.opened and buffer and buffer[-1] == self.opened and data.startswith(" "):
            buffer.insert(len(buffer) - 1, " ")
            data = data.lstrip()
        self.opened = None
        self.emit(data)

    def markdown(self):
        while len(self.buffers) > 1:
            text = "".join(self.buffers.pop())
            self.emit(text)
        lines, fenced = [], False
        for line in "".join(self.buffers[0]).split("\n"):
            if line.startswith("```"):
                fenced = not fenced
            if fenced:
                lines.append(line.rstrip())
            elif re.match(r"\s*(- |\d+\. )", line):
                lines.append(line.rstrip())
            else:
                lines.append(line.strip())
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def to_markdown(html):
    parser = ToMarkdown()
    parser.feed(html or "")
    parser.close()
    return parser.markdown()


def words(text):
    return len(re.findall(r"\w+", text))


def file_stem(day, title):
    parts = re.findall(r"[a-z0-9]+", (title or "").lower())[:6]
    return "%s_%s" % (day, "_".join(parts) or "untitled")


def label(value):
    value = "" if value is None else str(value)
    if value == "" or re.search(r":\s|\s#|^[-?:,\[\]{}#&*!|>'\"%@`]|\s$", value):
        return json.dumps(value, ensure_ascii=False)
    return value


def cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def read_index(path):
    """Returns the index's rows, keyed by link."""
    rows = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip().replace("\\|", "|")
                 for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if cells and cells[0] == COLUMNS[0]:
            continue
        if len(cells) == len(COLUMNS):
            row = dict(zip(COLUMNS, cells))
            rows[row["link"]] = row
    return rows


def write_index(path, heading, rows, columns, note=""):
    lines = ["# %s" % heading, ""] + ([note, ""] if note else []) + [
             "| " + " | ".join(columns) + " |",
             "|" + "---|" * len(columns)]
    for row in sorted(rows.values(), key=lambda r: r["date"], reverse=True):
        lines.append("| " + " | ".join(cell(row.get(c, "")) for c in columns) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_post(posts_dir, existing, post, text):
    """Writes the post file unless a full copy is already saved. Returns its name."""
    old = existing.get(post["link"])
    if old and old.get("file"):
        name = re.sub(r"^\[([^\]]+)\].*$", r"\1", old["file"])
        if (posts_dir / name).exists() and not (old.get("read") == "part"
                                                 and post["read"] == "full"):
            return name, False
    else:
        name = file_stem(post["date"], post["title"]) + ".md"
        n = 2
        while (posts_dir / name).exists():
            name = "%s_%d.md" % (file_stem(post["date"], post["title"]), n)
            n += 1
    front = ["---"]
    for key in ("title", "subtitle", "date", "link", "author", "audience", "read",
                "source"):
        if post.get(key) not in (None, ""):
            front.append("%s: %s" % (key, label(post[key])))
    if post.get("guest"):
        front.append("guest: true")
    front.append("---")
    (posts_dir / name).write_text("\n".join(front) + "\n\n" + text, encoding="utf-8")
    return name, True


def get_json(url):
    """Fetches a public JSON address, waiting and retrying when Substack says slow down."""
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ssl.SSLError):
                raise
            # Some Mac Pythons lack web certificates; the built-in curl has them.
            result = subprocess.run(["curl", "-fsSL", "-A", USER_AGENT, url],
                                    capture_output=True, check=True)
            return json.loads(result.stdout.decode("utf-8"))


def substack_address(link):
    link = link.strip().rstrip("/")
    if not re.match(r"https?://", link):
        link = "https://" + link
    if re.match(r"https?://(www\.)?substack\.com", link):
        sys.exit("Use your publication's own address, such as https://yourname.substack.com, "
                 "not a substack.com profile link.")
    return re.sub(r"/(p/.*|archive.*|about.*)$", "", link)


def from_api(posts, source):
    """Turns Substack's post records into the shape this tool saves."""
    for p in posts:
        likes = p.get("reaction_count")
        if likes is None and isinstance(p.get("reactions"), dict):
            likes = sum(p["reactions"].values())
        bylines = [b.get("name") for b in p.get("publishedBylines") or []]
        yield {
            "title": p.get("title") or "", "subtitle": p.get("subtitle") or "",
            "date": (p.get("post_date") or "")[:10],
            "link": p.get("canonical_url") or "",
            "authors": [b for b in bylines if b],
            "audience": AUDIENCE.get(p.get("audience"), p.get("audience") or ""),
            "html": p.get("body_html") or "", "expected": p.get("wordcount"),
            "source": source, "likes": likes,
            "comments": p.get("comment_count"), "restacks": p.get("restacks"),
        }


def fetch_public(base, limit=None):
    """Reads the publication's public archive, then each post in it."""
    listed = {}
    offset = 0
    while True:
        page = get_json("%s/api/v1/archive?sort=new&offset=%d&limit=12" % (base, offset))
        if not page:
            break
        for item in page:
            listed.setdefault(item["id"], item)
        offset += len(page)
        if limit and len(listed) >= limit:
            break
        time.sleep(0.3)
    items = list(listed.values())[:limit] if limit else list(listed.values())
    print("Found %d posts; reading each one..." % len(items), flush=True)
    for item in items:
        try:
            full = get_json("%s/api/v1/posts/%s" % (base, item["slug"]))
        except Exception:
            full = {}
        merged = dict(item)
        merged.update({k: v for k, v in full.items() if v is not None})
        merged["body_html"] = full.get("body_html") or ""
        yield merged
        time.sleep(0.3)


def substack_posts(args):
    """Yields each post as a dict with its HTML text."""
    if args.fetch:
        for post in from_api(fetch_public(substack_address(args.fetch), args.limit),
                             "archive"):
            yield post
    else:
        base = args.base.rstrip("/")
        with zipfile.ZipFile(Path(args.export).expanduser()) as z:
            names = z.namelist()
            table = next((n for n in names if n.endswith("posts.csv")), None)
            if not table:
                sys.exit("No posts.csv in this export. Is it Substack's export file?")
            root = table[: -len("posts.csv")]
            rows = csv.DictReader(io.TextIOWrapper(z.open(table), encoding="utf-8"))
            for row in rows:
                if str(row.get("is_published", "true")).lower() != "true":
                    continue
                post_id = row.get("post_id") or ""
                html_name = "%sposts/%s.html" % (root, post_id)
                html = (z.read(html_name).decode("utf-8")
                        if html_name in names else "")
                slug = post_id.split(".", 1)[1] if "." in post_id else post_id
                yield {
                    "title": row.get("title") or "", "subtitle": row.get("subtitle") or "",
                    "date": (row.get("post_date") or "")[:10],
                    "link": "%s/p/%s" % (base, slug),
                    "authors": [args.me] if args.me else [],
                    "audience": AUDIENCE.get(row.get("audience"), row.get("audience") or ""),
                    "html": html, "expected": None, "source": "export",
                    "likes": None, "comments": None, "restacks": None,
                }


def import_substack(args):
    folder = Path(args.folder).expanduser()
    substack_dir = folder / "substack"
    posts_dir = substack_dir / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    index = substack_dir / "index.md"
    existing = read_index(index)
    rows = dict(existing)
    me = (args.me or "").strip().lower()
    added = refreshed = 0
    part, unread, guests = [], [], 0

    for post in substack_posts(args):
        if not post["link"]:
            continue
        text = to_markdown(post["html"]) if post["html"] else ""
        if not text.strip():
            post["read"] = "none"
        elif (post["audience"] != "free" and post["expected"]
              and words(text) < 0.8 * post["expected"]):
            post["read"] = "part"
        else:
            post["read"] = "full"
        post["author"] = ", ".join(post["authors"])
        post["guest"] = bool(me and post["authors"]
                             and me not in [a.lower() for a in post["authors"]])
        guests += post["guest"]
        old = existing.get(post["link"], {})
        name, written = "", False
        if post["read"] != "none":
            name, written = save_post(posts_dir, existing, post, text)
        final_read = post["read"] if written or not old else old.get("read", "")
        if final_read == "part":
            part.append("%s (%s)" % (post["title"], post["date"]))
        if final_read in ("none", ""):
            unread.append(post["title"])
        if written and not old:
            added += 1
        elif written:
            refreshed += 1
        keep_old_counts = post["source"] == "export" and old
        rows[post["link"]] = {
            "title": post["title"], "date": post["date"], "link": post["link"],
            "author": post["author"], "guest": "true" if post["guest"] else "",
            "audience": post["audience"],
            "read": final_read,
            "source": post["source"] if written or not old else old.get("source", ""),
            "likes": old.get("likes", "") if keep_old_counts else post["likes"],
            "comments": old.get("comments", "") if keep_old_counts else post["comments"],
            "restacks": old.get("restacks", "") if keep_old_counts else post["restacks"],
            "file": "[%s](posts/%s)" % (name, name) if name else old.get("file", ""),
        }

    heading = "%s: every post" % args.publication if args.publication else "Every post"
    note = ("Counts checked: %s. They change; say \"update my posts\" to refresh them."
            % date.today().isoformat())
    write_index(index, heading, rows, COLUMNS, note)
    print("Saved %d new posts; rewrote %d that were only partly saved before; "
          "%d posts listed in substack/index.md." % (added, refreshed, len(rows)))
    if guests:
        print("%d guest posts are labelled guest: true." % guests)
    if part:
        print()
        print("PAID POSTS NOT FULLY READ: %d. Substack shows them only up to the paywall,"
              % len(part))
        print("so only that part is saved. For their full text, download Substack's export")
        print("(Settings > Export > Create new export) and run this tool with --export.")
        for t in part:
            print("  - " + t)
    if unread:
        print()
        print("NO TEXT CAME BACK FOR: " + "; ".join(unread))


def import_linkedin(args):
    folder = Path(args.folder).expanduser() / "linkedin"
    posts_dir = folder / "posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    columns = ["title", "date", "link", "file"]
    index = folder / "index.md"
    rows = {}
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) == 4 and cells[0] != "title" and not line.startswith("|---"):
                rows[cells[2]] = dict(zip(columns, cells))
    added = 0
    with open(Path(args.linkedin).expanduser(), encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            text = (row.get("ShareCommentary") or "").strip()
            link = row.get("ShareLink") or ""
            if not text or not link or link in rows:
                continue
            day = (row.get("Date") or "")[:10]
            title = text.split("\n", 1)[0][:80]
            name = file_stem(day, title) + ".md"
            n = 2
            while (posts_dir / name).exists():
                name = "%s_%d.md" % (file_stem(day, title), n)
                n += 1
            front = ["---", "title: " + label(title), "date: " + day, "link: " + link]
            if args.me:
                front.append("author: " + label(args.me))
            front += ["read: full", "source: linkedin-export", "---"]
            (posts_dir / name).write_text("\n".join(front) + "\n\n" + text + "\n",
                                          encoding="utf-8")
            rows[link] = {"title": title, "date": day, "link": link,
                          "file": "[%s](posts/%s)" % (name, name)}
            added += 1
    write_index(index, "LinkedIn: every post", rows, columns)
    print("Saved %d new LinkedIn posts; %d listed in linkedin/index.md."
          % (added, len(rows)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fetch", help="your Substack's address, read without signing in")
    source.add_argument("--export", help="Substack's export .zip")
    source.add_argument("--linkedin", help="Shares.csv from LinkedIn's data download")
    parser.add_argument("--base", help="your Substack's address, needed with --export")
    parser.add_argument("--me", help="your name as it appears on your posts")
    parser.add_argument("--publication", help="your newsletter's name")
    parser.add_argument("--folder", default=".", help="your office folder")
    parser.add_argument("--limit", type=int, help="read only the newest N posts")
    args = parser.parse_args()
    if args.export and not args.base:
        parser.error("--export needs --base, your Substack's address")
    if args.linkedin:
        import_linkedin(args)
    else:
        import_substack(args)


if __name__ == "__main__":
    main()
