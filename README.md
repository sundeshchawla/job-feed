# job-feed

Daily job feed for Sundesh's Apply Desk.

Every morning (04:50 IST) a GitHub Action reads the public job boards of ~340 target employers
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Workday, Oracle HCM, Amazon,
Google, Uber, Atlassian, plus Hacker News "Who is hiring" posts tagged VISA), keeps senior
software roles in the 11 target countries, scores each against Sundesh's profile, and writes:

- `out/jobs.json` - matching jobs with full descriptions (the Apply Desk imports from here)
- `out/status.json` - which boards worked, per company
- `out/summary.md` - counts per country

Edit `feed/companies.json` to add or remove employers. Run by hand from the Actions tab
(**Fetch jobs > Run workflow**). Everything here is public job-posting data; no personal data.
