import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selector  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

ICP = selector.default_icp()


def person(title="Product Manager", seniority="Senior", department="Product & Design",
           headcount=120, extra_role=None, name="Pat Example"):
    current = [{"title": title, "seniority_level": seniority, "company_headcount_latest": headcount,
                "company_headcount_range": "51-200", "name": "Acme", "is_default": True}]
    if extra_role:
        current.append(extra_role)
    return {"crustdata_person_id": 1,
            "basic_profile": {"name": name, "first_name": name.split(" ")[0], "current_title": title,
                              "normalized_title": {"department": department}},
            "experience": {"employment_details": {"current": current}}}


def user(url="https://www.example.com/in/pat", events=5):
    return {"profile_url": url, "email": "pat@example.com", "events": events}


class EvaluateTests(unittest.TestCase):
    def test_fit(self):
        rec, reason = selector.evaluate(user(), person(), ICP)
        self.assertEqual(reason, "fit")
        self.assertEqual(rec["title"], "Product Manager")
        self.assertEqual(rec["headcount"], 120)

    def test_rejects_interns_and_entry_level(self):
        for level in ("In Training", "Entry Level"):
            self.assertEqual(selector.evaluate(user(), person(seniority=level), ICP)[1], "seniority")

    def test_rejects_other_departments(self):
        self.assertEqual(selector.evaluate(user(), person(department="Sales & Business Development"), ICP)[1],
                         "department")

    def test_rejects_company_too_large_or_too_small(self):
        self.assertEqual(selector.evaluate(user(), person(headcount=5000), ICP)[1], "company size")
        self.assertEqual(selector.evaluate(user(), person(headcount=3), ICP)[1], "company size")

    def test_rejects_unknown_company_size(self):
        self.assertEqual(selector.evaluate(user(), person(headcount=None), ICP)[1], "company size unknown")

    def test_unmatched_and_no_role(self):
        self.assertEqual(selector.evaluate(user(), None, ICP), (None, "not matched"))
        p = person()
        p["experience"]["employment_details"]["current"] = []
        self.assertEqual(selector.evaluate(user(), p, ICP), (None, "no current role"))

    def test_uses_only_the_primary_role(self):
        # A side role at a tiny company must not rescue (or sink) the primary role.
        side = {"title": "Advisor", "seniority_level": "Senior", "company_headcount_latest": 20, "is_default": False}
        rec, reason = selector.evaluate(user(), person(headcount=5000, extra_role=side), ICP)
        self.assertEqual(reason, "company size")
        self.assertEqual(rec["title"], "Product Manager")

    def test_custom_icp_keeps_all_departments(self):
        icp = dict(ICP, departments=[])
        self.assertEqual(selector.evaluate(user(), person(department="Operations & Logistics"), icp)[1], "fit")


class EnrichTests(unittest.TestCase):
    def make_client(self, calls, response):
        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            return response
        return Crustdata(api_key="test", transport=transport)

    def test_run_uses_documented_endpoint_and_filters(self):
        calls = []
        response = [
            {"matched_on": "https://www.example.com/in/pat", "match_status": "matched",
             "matches": [{"confidence_score": 1, "person_data": person()}]},
            {"matched_on": "https://www.example.com/in/kim", "match_status": "matched",
             "matches": [{"confidence_score": 1, "person_data": person(seniority="In Training", name="Kim Example")}]},
            {"matched_on": "https://www.example.com/in/zed", "match_status": "not_found", "matches": []},
        ]
        users = [user("https://www.example.com/in/pat", 9), user("https://www.example.com/in/kim"),
                 user("https://www.example.com/in/zed")]
        result = selector.run(self.make_client(calls, response), users, ICP, "Replay", "https://cal.com/x")
        self.assertTrue(calls[0][0].endswith("/person/enrich"))
        self.assertEqual(calls[0][2]["x-api-version"], "2025-11-01")
        self.assertEqual(len(calls[0][1]["professional_network_profile_urls"]), 3)
        self.assertIn("experience.employment_details.current", calls[0][1]["fields"])
        self.assertEqual(len(result["fits"]), 1)
        self.assertEqual(result["reasons"], {"fit": 1, "seniority": 1, "not matched": 1})
        s = selector.summary(result)
        self.assertEqual(s["fit_share_pct"], 33)
        self.assertEqual(s["fit_by_company_size"], {"51-200": 1})

    def test_matches_urls_despite_formatting(self):
        calls = []
        response = [{"matched_on": "https://example.com/in/Pat/", "match_status": "matched",
                     "matches": [{"person_data": person()}]}]
        result = selector.run(self.make_client(calls, response), [user("https://www.example.com/in/pat")],
                              ICP, "Replay", "https://cal.com/x")
        self.assertEqual(len(result["fits"]), 1)

    def test_batches_of_25(self):
        calls = []
        users = [user(f"https://www.example.com/in/u{i}") for i in range(30)]
        selector.run(self.make_client(calls, []), users, ICP, "Replay", "https://cal.com/x")
        self.assertEqual([len(c[1]["professional_network_profile_urls"]) for c in calls], [25, 5])

    def test_no_users_makes_no_call(self):
        calls = []
        selector.run(self.make_client(calls, []), [], ICP, "Replay", "https://cal.com/x")
        self.assertEqual(calls, [])


class InviteTests(unittest.TestCase):
    def rec(self):
        return selector.evaluate(user(), person(title="Engineering Manager", name="Pat Example"), ICP)[0]

    def test_template_names_feature_and_link(self):
        text = selector.invite_text(self.rec(), "Session Replay Filters", "https://cal.com/x")
        self.assertIn("Hi Pat", text)
        self.assertIn("Session Replay Filters", text)
        self.assertIn("an Engineering Manager", text)
        self.assertTrue(text.endswith("https://cal.com/x"))

    def test_claude_text_used_only_when_it_keeps_the_link(self):
        import notify
        original = notify.ask_claude
        try:
            selector.ask_claude = lambda prompt, max_tokens=0: "Hi Pat, thanks for trying it. https://cal.com/x"
            self.assertIn("thanks for trying", selector.invite_text(self.rec(), "F", "https://cal.com/x", True))
            selector.ask_claude = lambda prompt, max_tokens=0: "Hi Pat, no link here."
            self.assertIn("you tried F", selector.invite_text(self.rec(), "F", "https://cal.com/x", True))
            selector.ask_claude = lambda prompt, max_tokens=0: None
            self.assertIn("you tried F", selector.invite_text(self.rec(), "F", "https://cal.com/x", True))
        finally:
            selector.ask_claude = original


class InputTests(unittest.TestCase):
    def test_read_users_dedupes_and_skips_blank(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["email", "profile_url", "events"])
            w.writerow(["a@x.com", "https://www.example.com/in/a", "4"])
            w.writerow(["a2@x.com", "https://example.com/in/A/", "x"])
            w.writerow(["b@x.com", "", "1"])
        try:
            users = selector.read_users(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["events"], 4)

    def test_posthog_query_rejects_injection(self):
        self.assertIn("event = 'beta_feature_used'", selector.posthog_query("beta_feature_used", "profile_url", 30))
        with self.assertRaises(ValueError):
            selector.posthog_query("x' OR 1=1 --", "profile_url", 30)
        with self.assertRaises(ValueError):
            selector.posthog_query("ok", "a; DROP", 30)

    def test_fetch_posthog_parses_rows(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"results": [["https://www.example.com/in/a", "a@x.com", 7],
                                               ["https://example.com/in/a/", "a@x.com", 2]]}).encode()

        seen = {}

        def opener(req, timeout=0):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return Resp()

        os.environ.update(POSTHOG_PROJECT_ID="12", POSTHOG_PERSONAL_API_KEY="k")
        try:
            users = selector.fetch_posthog(30, opener=opener)
        finally:
            os.environ.pop("POSTHOG_PROJECT_ID")
            os.environ.pop("POSTHOG_PERSONAL_API_KEY")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["events"], 7)
        self.assertTrue(seen["url"].endswith("/api/projects/12/query/"))


class StateAndSlackTests(unittest.TestCase):
    def test_only_new_and_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            rows = [{"id": "a"}, {"id": "b"}]
            self.assertEqual(len(selector.only_new(rows, path)), 2)
            selector.save_state(rows[:1], path)
            self.assertEqual([r["id"] for r in selector.only_new(rows, path)], ["b"])

    def test_slack_text(self):
        rec = selector.evaluate(user(), person(), ICP)[0]
        result = {"fits": [rec], "total": 4, "reasons": {"fit": 1}, "enriched": 4, "all": [rec]}
        text = selector.slack_text(result, "Replay")
        self.assertTrue(text.startswith(":test_tube: 1 of 4 Replay users fit the ICP"))
        self.assertIn("Product Manager at Acme", text)

    def test_size_bucket(self):
        self.assertEqual(selector.size_bucket(None), "unknown")
        self.assertEqual(selector.size_bucket(11), "11-50")
        self.assertEqual(selector.size_bucket(2000), "1001+")


if __name__ == "__main__":
    unittest.main()
