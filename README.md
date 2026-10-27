# Build Your AI Team

Free tools for *Build Your AI Team*, a series in [DeepCrest: AI for solopreneurs](https://read.deepcrest.ai). Each part adds one thing to a folder that your AI (Claude Code or Codex) works in.

## Part 1: the Substack reader

`tools/substack_import.py` saves every post you've published on Substack as a Markdown file, and lists them all with their likes, comments and restacks.

You don't need to run it yourself: Part 1's first prompt has Claude Code or Codex download and run it. To run it by hand, from your folder:

```
python3 tools/substack_import.py --fetch https://yourname.substack.com --me "Your Name"
```

- **No sign-in.** It reads your publication's public archive.
- **Paid posts** come back only up to the paywall. The tool saves that part and lists those posts. For their full text, download Substack's export (Settings, then Export, then "Create new export") and run:

  ```
  python3 tools/substack_import.py --export path/to/export.zip --base https://yourname.substack.com --me "Your Name"
  ```

  It reads only the posts in the .zip, never your subscriber list. Don't unzip the export into your folder.
- **What it writes:** `substack/library/posts/`, one file per post, and `substack/library/index.md`, every post with its likes, comments and restacks.
- **Run it again** to add new posts and refresh the counts. A post saved in full is never overwritten.
- **Not on Substack?** `--linkedin Shares.csv` reads the posts file from LinkedIn's "Get a copy of your data".
- It uses only Python's standard library and contacts nothing but your own Substack.
