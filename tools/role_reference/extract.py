"""Build the role reference sheet: every title's description copied word for word.

AvaHR: the "What is a ...?" section and the "Duties and responsibilities" list.
Workable: the "Job brief" section and the "Responsibilities" list.
O*NET 30.0 (CC BY 4.0): the occupation description and its core task statements,
  used only where no exact-title page exists, and marked approximate.
Nothing is reworded; only HTML tags and extra whitespace are removed.

    python tools/role_reference/extract.py WORKDIR
WORKDIR holds pages/<line>.html (each sources.tsv URL, fetched with plain
curl) and onet/db_30_0_text/ (the O*NET 30.0 text database). Writes
config/committee/role_reference.json, merging the matching patterns from
patterns.yaml.
"""
import csv, html, json, re, sys
from datetime import date
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
WORK = Path(sys.argv[1])
OUT = HERE.parents[1] / "config" / "committee" / "role_reference.json"
PAT = yaml.safe_load((HERE / "patterns.yaml").read_text())

def text(fragment):
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", t)        # space left where a tag closed before punctuation

def section(s, wanted):
    for m in re.finditer(r"<(h[1-4])[^>]*>(.*?)</\1>", s, re.S):
        if wanted(text(m.group(2))):
            lvl = int(m.group(1)[1]); rest = s[m.end():]
            stop = re.search(r"<h[1-%d][^>]*>" % lvl, rest)
            return rest[:stop.start()] if stop else rest
    return ""

O = WORK / "onet" / "db_30_0_text"
occ = {r["O*NET-SOC Code"]: r for r in csv.DictReader(open(f"{O}/Occupation Data.txt", encoding="utf-8"), delimiter="\t")}
tasks = {}
for r in csv.DictReader(open(f"{O}/Task Statements.txt", encoding="utf-8"), delimiter="\t"):
    if r.get("Task Type") == "Core":
        tasks.setdefault(r["O*NET-SOC Code"], []).append(r["Task"])

out = []
for i, line in enumerate(open(HERE / "sources.tsv", encoding="utf-8"), 1):
    f, lvl, title, src, ref = (line.rstrip("\n").split("\t") + [""])[:5]
    row = {"title": title, "function": f, "level": lvl, "summary": "", "duties": [],
           "source": src, "url": "", "approximate": False}
    if src in ("avahr", "workable"):
        s = open(WORK / "pages" / f"{i}.html", encoding="utf-8", errors="ignore").read()
        s = re.sub(r"<script.*?</script>|<style.*?</style>", "", s, flags=re.S)
        if src == "avahr":
            summ = section(s, lambda h: h.lower().startswith("what is"))
            duty = section(s, lambda h: h.lower().startswith("duties and responsibilities"))
        else:
            summ = section(s, lambda h: h.lower() == "job brief")
            duty = section(s, lambda h: h.lower() == "responsibilities")
        row["summary"] = " ".join(t for t in (text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", summ, re.S)) if t)
        row["duties"] = [t for t in (text(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", duty, re.S)) if t]
        row["url"] = ref
        row["source"] = {"avahr": "AvaHR", "workable": "Workable"}[src]
    elif src == "onet":
        o = occ[ref]
        row.update(summary=o["Description"], duties=tasks.get(ref, []), approximate=True,
                   source=f"O*NET 30.0 occupation {ref} {o['Title']} (closest occupation, approximate)",
                   url=f"https://www.onetonline.org/link/summary/{ref}")
    else:
        row["source"] = "none: no published description found for this exact title"
    row["patterns"] = PAT["titles"][title]
    out.append(row)
OUT.parent.mkdir(parents=True, exist_ok=True)
json.dump({"about": "Role reference sheet for the buying committee. Descriptions are copied word for word "
                    "from the named source; O*NET rows are the closest occupation and marked approximate; "
                    "rows with source 'none' have no published description.",
           "fetched": str(date.today()), "exclude": PAT["exclude"], "titles": out},
          open(OUT, "w"), indent=1, ensure_ascii=False)
for r in out:
    print(f"{r['title']:36s} {r['source'][:22]:22s} summary {len(r['summary']):4d}  duties {len(r['duties'])}")
