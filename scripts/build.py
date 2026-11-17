"""Combine raw chunks -> parquet, print/save profile, save 20k sample."""
import io, sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW, PROC, SAMPLE = ROOT / "data" / "raw", ROOT / "data" / "processed", ROOT / "data" / "sample"
pd.set_option("display.width", 200, "display.max_rows", 500, "display.max_columns", 50,
              "display.max_colwidth", 80)

files = sorted(RAW.glob("*.csv"))
parts = []
for f in files:
    d = pd.read_csv(f, dtype=str, keep_default_na=False, na_values=[""], engine="c")
    d["_source_file"] = f.name
    parts.append(d)
df = pd.concat(parts, ignore_index=True)
n_raw = len(df)

dupe_mask = df.duplicated("Complaint ID", keep="first")
n_dupes = int(dupe_mask.sum())
# check whether duplicate IDs are exact row duplicates or conflicting records
data_cols = [c for c in df.columns if c != "_source_file"]
dupe_ids = df.loc[dupe_mask, "Complaint ID"].unique()
conflicting = 0
if len(dupe_ids):
    sub = df[df["Complaint ID"].isin(dupe_ids)]
    conflicting = int((sub.groupby("Complaint ID")[data_cols].nunique(dropna=False) > 1).any(axis=1).sum())
dupe_files = df[df["Complaint ID"].isin(dupe_ids)]["_source_file"].value_counts().head(10)
df = df[~dupe_mask].drop(columns="_source_file").reset_index(drop=True)

# the API serialises nulls as the literal string "None" -> real missing values
none_counts = (df == "None").sum()
none_counts = none_counts[none_counts > 0]
df = df.mask(df == "None")

for c in ["Date received", "Date sent to company"]:
    # API returns UTC timestamps (e.g. 2025-01-01T00:21:12.000Z); keep time, store as tz-naive UTC
    df[c] = pd.to_datetime(df[c], errors="coerce", utc=True).dt.tz_localize(None)
df["Complaint ID"] = pd.to_numeric(df["Complaint ID"], errors="coerce").astype("Int64")

PROC.mkdir(parents=True, exist_ok=True)
df.to_parquet(PROC / "complaints_2020plus.parquet", index=False)

buf = io.StringIO()
with redirect_stdout(buf):
    N = len(df)
    print("=== CFPB complaints 2020+ : profile ===")
    print(f"raw files: {len(files)}; raw rows: {n_raw:,}; duplicate Complaint IDs dropped: {n_dupes:,} "
          f"(IDs whose duplicate rows differ in content: {conflicting})")
    if n_dupes:
        print("files involved in duplicates:\n" + dupe_files.to_string())
    print("literal 'None' strings converted to missing:\n" + none_counts.to_string())
    print(f"total rows: {N:,}")
    print(f"Date received: min {df['Date received'].min().date()}  max {df['Date received'].max().date()}")
    print(f"unparseable dates -> Date received: {df['Date received'].isna().sum()}, "
          f"Date sent to company: {df['Date sent to company'].isna().sum()}")

    print("\n=== rows by Product x year ===")
    ct = pd.crosstab(df["Product"], df["Date received"].dt.year, margins=True, margins_name="Total")
    print(ct.to_string())

    print("\n=== top 60 companies ===")
    vc = df["Company"].value_counts()
    top = pd.DataFrame({"count": vc, "share_%": (vc / N * 100).round(2)}).head(60)
    top["cum_share_%"] = top["share_%"].cumsum().round(2)
    top.index.name = "Company"
    print(top.reset_index().to_string(index=True))
    print(f"distinct companies: {vc.size:,}; top 60 cover {top['count'].sum()/N*100:.1f}% of rows")

    print("\n=== % with non-empty consumer narrative ===")
    if "Consumer complaint narrative" not in df.columns:
        print("NOT AVAILABLE: the API CSV export has no 'Consumer complaint narrative' column "
              "(nor 'Consumer consent provided?' / 'Consumer disputed?'). 0% of rows have a narrative.")
        nar = None
    else:
        nar = df["Consumer complaint narrative"].fillna("").str.strip().ne("")
    if nar is not None:
        print(f"overall: {nar.mean()*100:.1f}%  ({nar.sum():,} rows)")
        print((nar.groupby(df["Product"]).agg(["mean", "sum", "size"])
               .assign(pct=lambda t: (t["mean"] * 100).round(1))[["pct", "sum", "size"]]
               .rename(columns={"sum": "with_narrative", "size": "rows"})).to_string())
        print("\nby year:")
        print((nar.groupby(df["Date received"].dt.year).mean() * 100).round(1).to_string())

    print("\n=== columns: dtype, % missing ===")
    miss = pd.DataFrame({c: df[c].isna() | (df[c].astype("string").str.strip() == "").fillna(False)
                         for c in df.columns})
    print(pd.DataFrame({"dtype": df.dtypes.astype(str), "missing_%": (miss.mean() * 100).round(2)}).to_string())

    for c in ["Company response to consumer", "Timely response?", "Submitted via"]:
        print(f"\n=== {c} ===")
        v = df[c].value_counts(dropna=False)
        print(pd.DataFrame({"count": v, "pct": (v / N * 100).round(2)}).to_string())

text = buf.getvalue()
print(text)
SAMPLE.mkdir(parents=True, exist_ok=True)
(SAMPLE / "profile.txt").write_text(text, encoding="utf-8")

df.sample(n=20_000, random_state=42).to_csv(SAMPLE / "complaints_sample.csv", index=False)
print("saved parquet:", round((PROC / 'complaints_2020plus.parquet').stat().st_size / 1e6, 1), "MB;",
      "sample:", round((SAMPLE / 'complaints_sample.csv').stat().st_size / 1e6, 1), "MB")
