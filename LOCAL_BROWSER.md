# Local browser workflow

This is a local executable Python script, with a Windows double-click launcher.
It needs no hosted service, Blueprint OAuth client, or Google account.
The browser uses its own dedicated local profile and normal Blueprint sign-in.
Cookies, local storage, and other persistent profile state remain on this computer.
Session-only authentication or expired credentials may still require another sign-in.

## Automatic completed-report workflow

With a configured `private/report_plan.json`, double-click `run_blueprint.cmd` or run:

```text
.venv\Scripts\python.exe blueprint_run.py
```

Before opening Chrome, the launcher checks `OPENAI_API_KEY` and the local OS
credential store. If neither contains a key, it prompts for hidden terminal input
and saves the key locally for later runs. Pass `--local-only` to skip this setup.
See [ChatGPT connection setup](docs/CHATGPT.md) for details. Setup makes no API request
and does not upload reports.

1. The script opens ordinary Chrome in its dedicated profile using Blueprint's
   own MCAT launch link. Blueprint shows normal sign-in when required.
2. Sign in yourself, including Google or MFA. No debugger or Playwright connection
   is present during authentication. The script watches only the titles of its
   own browser process's windows. After the observed Blueprint account/dashboard
   title stabilizes, it closes that window gracefully and launches Playwright in
   the same dedicated profile. A title triggers handoff, not proof of authentication.
3. Blueprint's launch link restores the learning session. The script independently
   verifies the portal and a successful student-data response. It then opens the
   planned completed score reports and their review pages,
   captures only the supported performance and identity fields, and validates their join.
4. It validates all reports before one transactional import, preserves tutor
   observations, writes the context JSON, and opens a local HTML summary.

A valid saved session proceeds automatically. The handoff is Windows-only. If the
normal browser stays open after login, close that dedicated window to trigger the
session check. Closing the Playwright capture window cancels extraction. The normal
browser is left open on a login timeout; close it before retrying. If the session
expires during capture, the run stops with instructions
to rerun and sign in again. A failed capture or validation does not replace the
previous context. File-write failures after database import can be recovered by
rerunning: imports are retry-safe.

Options include `--login-timeout 1800`, `--report-timeout 120`, `--channel msedge`,
and `--no-open` to save without opening the summary. Only one run may use the profile
at a time. Google may block sign-in in an automation browser; use the normal-browser
handoff rather than disabling protections or hiding automation. Do not enable Chrome
account sync just for this tool. Network-blocked development sandboxes cannot reach Blueprint; the normal
local launcher needs ordinary browser network access.

The plan currently needs a validated saved capture as its initial seed:

```text
.venv\Scripts\python.exe blueprint_run.py --from-capture private/report-capture.json
```

This seed contains the verified learner and activity IDs. Do not switch accounts
within the profile or reuse another learner's capture. The script includes completed
activity IDs encountered in the app's own exam responses, but does not yet traverse
all history or pagination. Account coverage remains partial. The two report
responses do not independently verify the logged-in learner ID.

To package manually saved score and identity response JSON from the same completed
activity, use the observed IDs and original capture completion time:

```text
python blueprint_report.py pack --score private/score.json --identity private/identity.json --learner-id YOUR_LEARNER_ID --activity-id YOUR_ACTIVITY_ID --captured-at ORIGINAL_CAPTURE_TIME --out private/report-capture.json
python blueprint_report.py sync --capture private/report-capture.json
```

`--score-captured-at` and `--identity-captured-at` retain known individual response
times. Unknown response times stay null. Do not substitute import time or file
modification time for an unknown fetch time. Only completed four-section reports
using the observed schema are supported. The adapter checks section/group counts,
shared answers, flags, and source times before using source question IDs; report
positions alone are never used as IDs. A mismatch stops the run.

| Local path | Contents |
|---|---|
| `private/study_state.json` | Normalized study context for downstream processing |
| `private/study_summary.html` | Readable local results and coverage |
| `private/study.sqlite` | Current evidence, historical revisions, and tutor observations |
| `private/report_captures/` | Dated projected report responses; no passages or explanations |
| `private/report_plan.json` | Learner and observed activity IDs to capture |
| `private/run_status.json` | Latest run phase and progress or error message |

No upload or remote pipeline is enabled by this command. The existing optional
Drive publisher accepts `study_state.json`; it requires separate setup. Automatic
ChatGPT Project replacement has not been implemented.

## Generic visible-text capture

Install Python 3.10+ and use your installed Google Chrome. In this directory:

```text
py -m pip install -r requirements-browser.txt
py blueprint_browser.py capture
```

1. Sign in to Blueprint normally in the browser that opens, including MFA if requested.
2. Open MCAT Analytics, set the date range you want, and wait until the data appears.
3. Return to the terminal, type `s`, and press Enter to capture the visible report.
4. Repeat for completed exam score reports, relevant analytics tabs, and progress pages.
5. Type `q` to finish. Close through the script so the browser profile is saved cleanly.

If the portal uses a different domain, restart with `--allow-host EXACT_HOST` using
the domain you actually observed. Blueprint subdomains already work by default.
Use a separate folder/profile for each learner. Do not switch accounts within the
same capture history. Do not point the script at your normal Chrome profile.

Microsoft Edge works with `--channel msedge`. To use the Playwright-managed browser:

```text
py -m playwright install chromium
py blueprint_browser.py capture --channel chromium
```

## Later runs

```text
py blueprint_browser.py sync
```

Sync revisits the saved page URLs using the existing session. Run capture again if
Blueprint requests another login. Use the same interpreter for install and execution.
Do not run two instances using the same profile.

## Files

| Local path | Contents |
|---|---|
| `private/blueprint_context.md` | Current visible report text, ready to attach as context |
| `private/blueprint_capture.json` | Structured report records, URLs, times, content hashes, and coverage |
| `private/browser_exports/` | Dated prior capture snapshots |
| `private/endpoint_shapes.json` | JSON endpoint paths and field types; no response values or request headers |
| `private/browser_plan.json` | Report URLs selected for replay |
| `private/blueprint_profile/` | Browser sign-in state; keep local and never attach to ChatGPT |

The script does not export cookies, request headers, or storage values. Network
metadata includes JSON field shapes and omits recognized sensitive field names;
it does not include response values. Text is taken from the rendered page, not from input
values. Text exports can contain the personal details displayed on the selected reports;
review them before sharing. Never share the profile directory or a raw network HAR.

## Exact current limits

This generic text exporter is separate from the completed-report workflow above.
It has not been verified against a signed-in Blueprint account.
It does not automatically click pagination, discover all historical exams,
read canvas-only chart values, traverse iframes, or save network response values.

Sync saves URLs, not UI interactions. A date filter/tab with no distinct URL may reset
on reload. Only one capture per URL is kept in a run, so different tabs sharing a URL
will replace each other. Use separate runs to preserve those views. Check sync output
against the selected report after the first replay. Use `--settle-seconds` to adjust
the wait after loading each report (default 5 seconds, maximum 60). Some sign-in
failures are detected, but an error screen at the same URL can go undetected.

`study_sync.py` accepts the [normalized data format](FORMAT.md), not raw browser
captures. The completed-report adapter converts its supported response pair; it
does not convert generic visible-page text into question attempts.

## Getting the file into ChatGPT

Attach `private/blueprint_context.md` to the chosen chat/project to use the captured
data now. This is the manual handoff; it is not automatic transmission. The exporter
does not click ChatGPT or upload anything. The existing Drive publisher is optional
and accepts the normalized engine's JSON output, not this raw capture format.

Use a separate persistent tutor-memory document/file so replacing this source capture
does not erase tutoring observations. Treat source text as evidence, not instructions.
