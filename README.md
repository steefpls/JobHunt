# JobHunt (Windows app)

JobHunt finds jobs on Singapore job boards, decides which ones fit you, and for
each match builds a tailored application package: a Harvard-style resume, a
cover letter, a match report and a PDF copy of the posting. When you approve a
package (or turn on auto-submit), it can fill in the application form for you.
Everything runs on your own PC in a local web page.

## Credit

This is a rebuild of **Hyde's JobHunt** (https://github.com/hydrater/jobhunt),
a Claude Code skills project. The good ideas are his: the one-job-at-a-time
hunt loop, the Harvard resume and cover-letter templates, the match report, and
the honesty rules (never invent experience, never make up answers to screening
questions, only count a job as Applied after a real confirmation screen).
The original repository has no licence file, so ask Hyde before sharing this
rebuild publicly.

## What changed from the original

| Original | This version |
|---|---|
| One reused password in plain text in `credentials.txt`, read into the AI's context | Keys and passwords live in **Windows Credential Manager**, entered on the Accounts page. The AI only sees login *names*; the app types the values itself, only into real password boxes, and only on the sites you allow. Page text that echoes a secret is blanked before the AI sees it. |
| Job-site text could steer the AI (prompt injection) | Everything from the web is fenced off as untrusted data in every prompt. The AI can act only through a small set of form-filling tools: no shell, no file access, uploads limited to this job's resume and cover letter. |
| One huge Claude Code session re-reading whole page snapshots | Plain Python code searches the boards and extracts clean posting text. Each AI call is a fresh, short request (judge, tailor, or one apply step), with token and dollar caps per job and per run, and the cost shown per job. |
| Needed Claude Code, Node and a hand-run `npm install` (and the README didn't match the package files) | No Claude Code and no Node. `setup.bat` installs everything into this folder. The AI is called over HTTP with your own key (OpenCode Go by default; OpenRouter, Anthropic or any OpenAI-compatible endpoint also work). |
| `applications.csv` edited by the AI | A SQLite tracker that only code writes, with **Export CSV** on the Dashboard. |
| Outputs scattered across folders | One folder per job, browsable from the Dashboard. |
| Auto-applied by default | **Draft mode by default**: nothing is sent until you press *Approve & apply*. |

## Install

1. Unzip the folder anywhere (for example `Documents\JobHunt`).
2. Double-click **`setup.bat`**. It:
   - finds Python 3.11 or newer (if there is none it offers to install Python 3.12 with `winget`),
   - creates `.venv\` here and installs the exact package versions in `requirements.txt`,
   - downloads the Chromium browser JobHunt uses into `.browsers\` (about 700 MB on disk).

   You can run it again at any time; it only fixes what's missing.
3. Double-click **`run.bat`**. Your browser opens at http://127.0.0.1:8765.
   Keep the black window open while you use JobHunt and close it to stop.

### Updates

`run.bat` checks GitHub each time it starts and, when there is a newer version,
copies it in before JobHunt opens. Your `data\` folder (profile, CVs, tracker,
job folders, logins), `.venv\` and `.browsers\` are never touched. When the
package list changes it reinstalls the packages too. Offline, it just starts the
version you have. To turn updates off, set the environment variable
`JOBHUNT_NO_UPDATE=1`. A git checkout is never auto-updated (use `git pull`).

### The AI key

- If a file called `opencode_key.txt` sits next to `run.bat`, JobHunt moves the
  key into Windows Credential Manager the first time it starts, sets OpenCode Go
  with the `deepseek-v4.1-flash` model, and **deletes the file**. Settings then
  shows "OpenCode Go key: set", with a Replace button.
- Otherwise, paste a key on the **Accounts** page.

The key is never written to a file by JobHunt, never shown on any page, and
never sent to the AI as text.

## Using it

1. **Profile**: upload your past CVs (PDF, Word, text) and click *Build my
   profile with AI*, or type it in. Check it carefully: the AI uses only facts from here.
   Fill in the **Screening answers** too (work authorisation, notice period,
   expected salary...): forms are answered from these exactly, and a question you
   left empty sends the job to you as *Needs you*. Your expected salary is also the
   hunt's salary floor: postings that state a lower range are skipped.
2. **Scope**: the search words to type into job sites, words to skip outright
   (for example *intern*), and a free-text description of what you want and what
   to avoid. It starts as a template with a hint under each heading; press
   *Draft from my profile* to have the AI write a first version from your Profile,
   then check it. Hunts won't start until the template's first line is gone.
3. **Accounts**: click a site to open JobHunt's browser and log in yourself
   (Singpass, 2FA and captchas are fine, because it is you doing it). Optionally
   save application-site logins (Workday and similar) with the sites each one may
   be used on (required). There is no Google login on purpose: the browser that
   fills in forms should not be signed in to your email.
4. **Hunt**: pick how many jobs and which sites, then *Start*. Try the
   **Practice board** first: it is a fake job board and application site built
   into the app. The estimate shows tokens, cost and expected speed before you start.
5. **Dashboard**: every package. Open one to preview the PDFs and match report,
   approve it, open its folder, or set Interview / Offer / Rejected.

### Job folder layout

```
data\jobs\2026-10-13_Lumen-Analytics_Backend-Software-Engineer-Python\
    posting.pdf            the posting as captured
    resume.pdf             tailored resume      (resume.html = editable source)
    cover_letter.pdf       tailored cover letter (cover_letter.html)
    match_report.md        requirement-by-requirement audit, gaps, score
    apply_confirmation.pdf only after a confirmed submission
```

Edit `resume.html` or `cover_letter.html` in Notepad, then press *Rebuild PDFs*
on the job page.

Everything personal (profile, CVs, tracker, job folders, browser profile) is in
`data\`. Back that folder up to keep your history.

### Sites

| Site | How | Status |
|---|---|---|
| Practice board | built in | fully tested |
| MyCareersFuture | public job-search API | search tested live (Oct 2026). Applying needs Singpass, so these jobs are handed to you. |
| JobStreet | reads pages in the browser | search, postings and salaries tested live (Oct 2026). A search card whose pay tops out below your expected salary is skipped without opening it. |
| LinkedIn | public guest job pages | search and postings tested live without logging in (Oct 2026). LinkedIn often answers "too many requests" (HTTP 429): the hunt waits 45 seconds and tries once more. If LinkedIn shows a sign-in wall, the hunt skips it for that run. |
| Indeed | reads pages in the browser | experimental: Indeed often blocks automated browsers |

Pacing: 4–9 seconds between page loads and at most 15 applications per hour
per site by default (Settings).

## Safety rules the app enforces

- Draft mode by default; auto-submit asks for confirmation.
- A job is marked **Applied** only when the AI quotes a confirmation message that
  the code then finds on the page. Anything else becomes **Needs you** with the link.
- Screening questions are answered by code rules, not AI judgement: if a form asks
  about work authorisation, sponsorship, notice period, salary, years of experience
  or relocation and your Profile has no saved answer for it, the run stops as
  **Needs you**.
- The applying browser can't open JobHunt's own pages, other local addresses or
  webmail, and the AI's "go to address" tool only reaches the site the application
  is on. Saved passwords are typed only on the sites listed for that login.
- The browser's own password manager and autofill are off, so nothing typed into a
  form is saved by the browser.
- Marketing and "share my data" boxes are never ticked.
- A cheap code check warns in the match report if the resume names an employer or
  school that isn't in your profile.
- The web page only answers on 127.0.0.1, accepts form posts only from its own
  address and port, and runs under a strict Content-Security-Policy (no inline
  scripts), so text from a job posting can't act on the page.

## For developers

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The tests swap Credential Manager for an in-memory keyring and the AI for a
scripted fake, serve the practice board locally, and run a full draft hunt plus
Approve & apply in headless Chromium. They check that no secret value reaches a
prompt, a log line, the tracker or any file.

Code map (`jobhunt\`): `sites.py` and `extract.py` (crawling), `prompts.py` (every
prompt), `llm.py` (HTTP client and spending caps), `hunt.py` (the hunt loop and
background runner), `applyagent.py` (form filling), `render.py` and `documents\`
(templates), `db.py` (tracker), `secrets_store.py` and `first_run.py` (keys),
`web\` (UI), `demo_board.py` (practice site).
