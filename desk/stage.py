#!/usr/bin/env python3
"""Stage new feed jobs for the Apply Desk.

Picks jobs that first appeared in the feed recently (default: last 2 days; the first run takes the
best 400), packs them into documents of up to 25 jobs, and writes ArtifactData batch `writes`
arrays (<= 50 writes and <= ~900 KB each) that create NEW documents in collection "feedstage".
The desk merges staged jobs on open: it skips duplicates, fills in descriptions for LinkedIn-alert
jobs it already has, adds the rest to the country queues and deletes the staging documents.
No desk reads are needed here.

Usage: stage.py --feed <path-or-url> --out /tmp/stage [--days 2] [--first]
"""
import argparse, datetime as dt, json, os, urllib.request

PER_DOC, MAX_BATCH_BYTES, FIRST_RUN_LIMIT, JD_CHARS = 25, 900_000, 400, 4500


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--first", action="store_true", help="first import: stage the best jobs regardless of age")
    ap.add_argument("--per-country", type=int, default=0, help="cap jobs staged per country (0 = no cap)")
    a = ap.parse_args()
    if a.feed.startswith("http"):
        with urllib.request.urlopen(a.feed, timeout=60) as r:
            feed = json.load(r)
    else:
        feed = json.load(open(a.feed))
    now = dt.datetime.now(dt.timezone.utc)
    ist_day = (now + dt.timedelta(hours=5, minutes=30)).date()
    cutoff = (ist_day - dt.timedelta(days=a.days)).isoformat()
    jobs = feed.get("jobs", [])
    pick = jobs if a.first else [j for j in jobs if j.get("firstSeen", "") >= cutoff]
    pick = sorted(pick, key=lambda j: -j["fit"])
    if a.per_country:
        cnt, capped = {}, []
        for j in pick:
            if cnt.get(j["country"], 0) < a.per_country:
                cnt[j["country"]] = cnt.get(j["country"], 0) + 1
                capped.append(j)
        pick = capped
    pick = pick[:FIRST_RUN_LIMIT if a.first else 600]
    keys = ("id", "country", "company", "title", "location", "url", "jd", "jobRef", "postedAt", "fit", "tier", "recipe", "sponsorship", "fitWhy", "source")
    slim = [{k: (j.get(k) or "")[:JD_CHARS] if k == "jd" else j.get(k) for k in keys} for j in pick]
    os.makedirs(a.out, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M")
    writes, sizes = [], []
    for i in range(0, len(slim), PER_DOC):
        doc_id = f"{stamp}-{i // PER_DOC:03d}"
        doc = {"createdAt": now.isoformat(timespec="seconds"), "feedGeneratedAt": feed.get("generatedAt"), "jobs": slim[i:i + PER_DOC]}
        p = os.path.join(a.out, doc_id + ".json")
        with open(p, "w") as f:
            json.dump(doc, f, ensure_ascii=False)
        writes.append({"op": "set", "collection": "feedstage", "doc_id": doc_id, "file_path": p})
        sizes.append(os.path.getsize(p))
    batches, cur, cur_size = [], [], 0
    for w, sz in zip(writes, sizes):
        if cur and (len(cur) >= 50 or cur_size + sz > MAX_BATCH_BYTES):
            batches.append(cur); cur, cur_size = [], 0
        cur.append(w); cur_size += sz
    if cur:
        batches.append(cur)
    with open(os.path.join(a.out, "batches.json"), "w") as f:
        json.dump(batches, f)
    by = {}
    for j in slim:
        by[j["country"]] = by.get(j["country"], 0) + 1
    summary = {"feedGeneratedAt": feed.get("generatedAt"), "feedJobs": feed.get("count"), "staged": len(slim), "byCountry": by, "docs": len(writes), "batches": len(batches)}
    print(json.dumps(summary))
    for b in batches:
        print(json.dumps(b))


if __name__ == "__main__":
    main()
