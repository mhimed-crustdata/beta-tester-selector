#!/usr/bin/env python3
"""Beta Tester Selector.

Takes the people who used your new feature (a PostHog export or a live PostHog query),
enriches each one with Crustdata, keeps only the ones whose role and company fit your ICP,
and drafts a short invite that names the feature they used and links to your booking page.
Data layer: Crustdata POST /person/enrich (https://docs.crustdata.com).

Shape:
  read_users() / fetch_posthog() -> who used the feature
  enrich()                        -> one documented Crustdata call, 25 profiles per request
  evaluate()                      -> deterministic ICP rules on one role (unit tested)
  invite_text()                   -> the invite (template, or Claude when configured)
  summary / slack_text            -> aggregate numbers and the alert
  only_new / save_state           -> invite each person once
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from crustdata_api import Crustdata, load_env
from notify import ask_claude, claude_available, send_slack

ENRICH_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.first_name",
    "basic_profile.current_title",
    "basic_profile.normalized_title",
    "experience.employment_details.current",
]

# Crustdata seniority_level values that are not a buyer or a champion.
DEFAULT_REJECT_SENIORITY = ["In Training", "Entry Level"]
DEFAULT_DEPARTMENTS = ["Product & Design", "Engineering & Technology", "Executive Leadership"]
DEFAULT_MIN_HEADCOUNT = 11
DEFAULT_MAX_HEADCOUNT = 1000


def default_icp() -> Dict[str, Any]:
    return {
        "departments": list(DEFAULT_DEPARTMENTS),
        "reject_seniority": list(DEFAULT_REJECT_SENIORITY),
        "min_headcount": DEFAULT_MIN_HEADCOUNT,
        "max_headcount": DEFAULT_MAX_HEADCOUNT,
    }


# ---- 1. who used the feature -------------------------------------------------------------

def norm_url(url: Optional[str]) -> str:
    """'https://www.example.com/in/Jo-Doe/' -> 'example.com/in/jo-doe'."""
    u = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith("www."):
        u = u[4:]
    return u.split("?")[0].split("#")[0].rstrip("/")


def read_users(path: str) -> List[Dict[str, Any]]:
    """CSV with a profile_url column (required) plus optional email and events."""
    users, seen = [], set()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("profile_url") or "").strip()
            key = norm_url(url)
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                events = int(row.get("events") or 0)
            except ValueError:
                events = 0
            users.append({"profile_url": url, "email": (row.get("email") or "").strip(), "events": events})
    return users


_IDENT = re.compile(r"^[A-Za-z0-9_$.\- ]{1,100}$")


def posthog_query(event: str, prop: str, days: int) -> str:
    """HogQL for 'people who fired `event` in the last N days'. Inputs are validated, not quoted blindly."""
    if not _IDENT.match(event) or not re.match(r"^[A-Za-z0-9_]{1,60}$", prop):
        raise ValueError("POSTHOG_EVENT or POSTHOG_PROFILE_PROPERTY contains unsupported characters")
    days = max(1, int(days))
    return (f"SELECT person.properties.{prop} AS profile_url, person.properties.email AS email, count() AS events "
            f"FROM events WHERE event = '{event}' AND timestamp > now() - INTERVAL {days} DAY "
            f"AND person.properties.{prop} IS NOT NULL GROUP BY profile_url, email ORDER BY events DESC LIMIT 500")


def fetch_posthog(days: int, opener=None) -> List[Dict[str, Any]]:
    """Active users from PostHog's query API (needs POSTHOG_PROJECT_ID and POSTHOG_PERSONAL_API_KEY)."""
    host = os.environ.get("POSTHOG_HOST", "https://us.posthog.com").rstrip("/")
    project = os.environ.get("POSTHOG_PROJECT_ID", "")
    key = os.environ.get("POSTHOG_PERSONAL_API_KEY", "")
    if not project or not key:
        raise SystemExit("Set POSTHOG_PROJECT_ID and POSTHOG_PERSONAL_API_KEY, or pass --users file.csv")
    query = posthog_query(os.environ.get("POSTHOG_EVENT", "beta_feature_used"),
                          os.environ.get("POSTHOG_PROFILE_PROPERTY", "profile_url"), days)
    url = f"{host}/api/projects/{urllib.parse.quote(project)}/query/"
    body = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "authorization": f"Bearer {key}", "content-type": "application/json"})
    with (opener or urllib.request.urlopen)(req, timeout=60) as resp:
        data = json.load(resp)
    users, seen = [], set()
    for profile_url, email, events in data.get("results") or []:
        k = norm_url(profile_url)
        if k and k not in seen:
            seen.add(k)
            users.append({"profile_url": profile_url, "email": email or "", "events": int(events or 0)})
    return users


# ---- 2. enrich ---------------------------------------------------------------------------

def enrich(client: Crustdata, users: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return {normalized profile url: person_data} for the profiles Crustdata matched."""
    results = client.person_enrich([u["profile_url"] for u in users], ENRICH_FIELDS)
    out: Dict[str, Dict[str, Any]] = {}
    for res in results:
        if res.get("match_status") != "matched":
            continue
        matches = res.get("matches") or []
        if matches and matches[0].get("person_data"):
            out[norm_url(res.get("matched_on"))] = matches[0]["person_data"]
    return out


def primary_role(person: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The profile's main current role (is_default), else the first current role."""
    current = ((person.get("experience") or {}).get("employment_details") or {}).get("current") or []
    for role in current:
        if role.get("is_default"):
            return role
    return current[0] if current else None


# ---- 3. ICP rules ------------------------------------------------------------------------

def evaluate(user: Dict[str, Any], person: Optional[Dict[str, Any]],
             icp: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (record, reason). reason is 'fit' for a keeper, otherwise why the person was dropped.

    Every rule reads the same role (the primary one), so a title from one job is never
    combined with the company size of another.
    """
    if not person:
        return None, "not matched"
    role = primary_role(person)
    if not role:
        return None, "no current role"
    basic = person.get("basic_profile") or {}
    title = role.get("title") or basic.get("current_title") or ""
    seniority = role.get("seniority_level") or ""
    department = (basic.get("normalized_title") or {}).get("department") or ""
    headcount = role.get("company_headcount_latest")
    rec = {
        "id": norm_url(user["profile_url"]),
        "profile_url": user["profile_url"],
        "email": user.get("email", ""),
        "events": user.get("events", 0),
        "first_name": basic.get("first_name") or (basic.get("name") or "").split(" ")[0],
        "title": title,
        "seniority": seniority,
        "department": department,
        "company": role.get("name") or "",
        "headcount": headcount,
        "headcount_range": role.get("company_headcount_range") or "",
    }
    if seniority in icp["reject_seniority"]:
        return rec, "seniority"
    if icp["departments"] and department not in icp["departments"]:
        return rec, "department"
    if not isinstance(headcount, int):
        return rec, "company size unknown"
    if headcount < icp["min_headcount"] or headcount > icp["max_headcount"]:
        return rec, "company size"
    return rec, "fit"


# ---- 4. invite ---------------------------------------------------------------------------

def invite_text(rec: Dict[str, Any], feature: str, booking_url: str, use_claude: bool = False) -> str:
    """Short invite naming the feature the person used. Claude drafts it when asked, else a template."""
    name = rec.get("first_name") or "there"
    if use_claude:
        prompt = (
            "Write a 3 sentence email inviting a beta user to a 20 minute feedback call. "
            f"First name: {name}. Feature they used: {feature}. Their role: {rec['title']}. "
            f"Booking link: {booking_url}. Plain, warm, no hype, no exclamation marks. "
            "Return only the email body, starting with 'Hi'.")
        text = ask_claude(prompt, max_tokens=600)
        if text and booking_url in text:
            return text
    role = article(rec.get("title", ""))
    return (f"Hi {name}, you tried {feature} while it was in beta, and your take as {role} "
            f"would help us a lot. Could you spare 20 minutes for a feedback call? "
            f"Pick a time here: {booking_url}")


def article(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return "a user"
    return ("an " if t[:1].lower() in "aeiou" else "a ") + t


# ---- 5. run ------------------------------------------------------------------------------

def run(client: Crustdata, users: List[Dict[str, Any]], icp: Dict[str, Any], feature: str,
        booking_url: str, use_claude: bool = False) -> Dict[str, Any]:
    people = enrich(client, users) if users else {}
    fits, reasons = [], Counter()
    sample_funnel = []
    for u in users:
        rec, reason = evaluate(u, people.get(norm_url(u["profile_url"])), icp)
        reasons[reason] += 1
        if rec:
            sample_funnel.append(rec)
        if reason == "fit":
            rec["invite"] = invite_text(rec, feature, booking_url, use_claude)
            fits.append(rec)
    fits.sort(key=lambda r: -r["events"])
    return {"fits": fits, "reasons": dict(reasons), "total": len(users), "enriched": len(people),
            "all": sample_funnel}


def size_bucket(headcount: Any) -> str:
    if not isinstance(headcount, int):
        return "unknown"
    for limit, label in ((10, "1-10"), (50, "11-50"), (200, "51-200"), (1000, "201-1000")):
        if headcount <= limit:
            return label
    return "1001+"


def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    fits = result["fits"]
    total = result["total"]
    return {
        "users_checked": total,
        "matched_in_crustdata": result["enriched"],
        "icp_fit": len(fits),
        "fit_share_pct": round(100 * len(fits) / total) if total else 0,
        "dropped_by_reason": {k: v for k, v in sorted(result["reasons"].items()) if k != "fit"},
        "fit_by_company_size": dict(Counter(size_bucket(r["headcount"]) for r in fits)),
        "fit_by_department": dict(Counter(r["department"] for r in fits)),
    }


def slack_text(result: Dict[str, Any], feature: str, limit: int = 10) -> str:
    fits = result["fits"]
    lines = [f":test_tube: {len(fits)} of {result['total']} {feature} users fit the ICP and got a beta invite draft"]
    for r in fits[:limit]:
        lines.append(f"• {r['first_name'] or 'User'}, {r['title']} at {r['company']} "
                     f"({r['headcount']} people, {r['events']} uses)")
    return "\n".join(lines)


def load_state(state_path: str) -> set:
    if not os.path.exists(state_path):
        return set()
    with open(state_path) as fh:
        return set(json.load(fh))


def only_new(rows: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = load_state(state_path)
    return [r for r in rows if str(r["id"]) not in seen]


def save_state(rows: List[Dict[str, Any]], state_path: str) -> None:
    seen = load_state(state_path)
    seen.update(str(r["id"]) for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def write_outputs(result: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "results.json"), "w") as fh:
        json.dump({"summary": summary(result), "invites": result["fits"]}, fh, indent=1)
    cols = ["email", "first_name", "title", "company", "headcount", "events", "invite"]
    with open(os.path.join(out_dir, "invites.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(result["fits"])


def parse_list(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Pick beta testers that match your ICP (Crustdata data layer)")
    p.add_argument("--users", help="CSV with profile_url, email, events (default: query PostHog)")
    p.add_argument("--days", type=int, default=int(os.environ.get("SINCE_DAYS", "30")))
    p.add_argument("--feature", default=os.environ.get("FEATURE_NAME", "the new feature"))
    p.add_argument("--booking-url", default=os.environ.get("BOOKING_URL", "https://cal.com/your-team/feedback-call"))
    p.add_argument("--departments", default=",".join(DEFAULT_DEPARTMENTS),
                   help="comma list of Crustdata departments to keep ('' keeps all)")
    p.add_argument("--reject-seniority", default=",".join(DEFAULT_REJECT_SENIORITY))
    p.add_argument("--min-headcount", type=int, default=DEFAULT_MIN_HEADCOUNT)
    p.add_argument("--max-headcount", type=int, default=DEFAULT_MAX_HEADCOUNT)
    p.add_argument("--claude", action="store_true", help="draft invites with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="ignore the already-invited state")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    icp = {"departments": parse_list(args.departments), "reject_seniority": parse_list(args.reject_seniority),
           "min_headcount": args.min_headcount, "max_headcount": args.max_headcount}
    users = read_users(args.users) if args.users else fetch_posthog(args.days)
    if not args.all:  # skip people already invited, before spending enrich credits
        fresh = {r["id"] for r in only_new([{"id": norm_url(u["profile_url"])} for u in users], args.state)}
        users = [u for u in users if norm_url(u["profile_url"]) in fresh]
    use_claude = args.claude and claude_available()
    result = run(Crustdata(), users, icp, args.feature, args.booking_url, use_claude)
    write_outputs(result, args.out)
    print(json.dumps(summary(result), indent=1))
    if result["fits"]:
        print(send_slack(slack_text(result, args.feature), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(result["fits"], args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
