# Handoff: finish the Beehive Wire -> YouTube (Boring Money) setup

You are picking this up from a Sonnet 5.5 cloud session. The code is done and merged. What remains is the
one-time YouTube/Google credential setup, which needs a browser signed in as the owner of the Boring Money
channel. That session had no browser tools, so it could not do the clicking. If you have Chrome (the
Claude-in-Chrome extension) or a linked computer, do it; if you don't, say so to the user plainly and give
them the click list below rather than implying you can.

## Goal

Twice a day, right after each Beehive Wire edition prints (morning ~10:00 UTC, evening ~22:00 UTC), a
roughly ten-minute video of the front page uploads automatically to the user's **Boring Money** YouTube
channel. The user wants this fully hands-off: it must run on GitHub's servers, not depend on their laptop.

## State of play (verified against the repo, Oct 12 2026)

- Repo: `thomasqreason/beehive-wire`. PR #1 is **merged** into `main` (squash, commit `a80577e`).
- Code: `video.py` (due / --emit-payload / render / upload / whoami), `video_editorial.md` (Claude's
  narration rules), `youtube_auth.py` (one-time refresh-token minting), `.github/workflows/video.yml`,
  a `video:` block in `config.yaml`, README section "The YouTube video".
- Pipeline: story pick (14 stories) -> fetch article context -> Claude CLI writes `script.json` -> Pillow
  slides + edge-tts voice + ffmpeg -> upload via YouTube Data API v3 -> record in `data/video/published.json`.
  If Claude's script fails it reads the headlines straight, so a slot still gets a video.
- Triggers in `video.yml`: after the `build` workflow completes, cron shots at 11:10, 11:50, 12:30, 23:10,
  23:50, 00:30 UTC, and manual dispatch (`force`, `dry_run` inputs). `video.py due` dedupes by edition
  (date + slot) and skips an edition more than 10h old.
- **No video has ever been uploaded.** `data/video/published.json` does not exist on `main`. The one
  `video` run so far (run 38321822833, after the merge) was green in ~27s because `due` stood down: the
  YouTube secrets don't exist yet. A green tick there does NOT mean a video went up. The user believed
  "70 videos" had been made; that is not true, and they were told so.
- Tested here: slide rendering and ffmpeg assembly (with silent audio), script validation, the once-per-
  edition guard, the missing-credentials stand-down. **Not tested**: edge-tts voice (the sandbox blocked
  Microsoft's host), the article-context fetch against real sites (sandbox blocked them), the Claude CLI
  step in Actions, and the YouTube upload itself. Expect to debug the first real run.

## What is left (in order)

1. **Google Cloud (browser, signed in as the Boring Money owner):**
   - Create project `beehive-video` (https://console.cloud.google.com/projectcreate).
   - Enable YouTube Data API v3 (https://console.cloud.google.com/apis/library/youtube.googleapis.com).
   - OAuth consent screen (https://console.cloud.google.com/auth/overview): External, user's email, add the
     user's own email as a test user.
   - Create OAuth client, type **Desktop app** (https://console.cloud.google.com/auth/clients). Capture the
     client ID and secret. Do not paste them anywhere public.
2. **Mint the refresh token** on the user's machine: `python youtube_auth.py --client-id <ID> --client-secret <SECRET>`.
   It opens a browser; sign in and **pick the Boring Money channel** when Google asks which channel. Prints
   the refresh token. (Needs `pip install requests`.)
   - While the app is in "Testing" status, Google expires refresh tokens after 7 days. Publish the consent
     screen to "In production" (no verification needed for the user's own use of the sensitive scope, they
     will see an "unverified app" warning once) so the token does not lapse and silently stop the videos.
     Confirm this with the user; it is the likeliest reason the automation would quietly die a week in.
3. **Add repo secrets** at https://github.com/thomasqreason/beehive-wire/settings/secrets/actions:
   `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`. (`CLAUDE_CODE_OAUTH_TOKEN`
   already exists for `build.yml`.)
4. **Verify the target:** `YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... YOUTUBE_REFRESH_TOKEN=... python video.py whoami`
   must name Boring Money. `config.yaml` `video.expect_channel` ("boring money") also makes upload refuse
   a wrong channel.
5. **Dry run:** Actions -> `video` -> Run workflow with `dry_run` ticked. Watch the attached render. Check the
   voice (edge-tts may be refused from GitHub runners; the code falls back to espeak-ng, which is robotic),
   length (~10 min is the target; videos may run short where article context couldn't be fetched, notably
   Google News links), and that the script has no invented facts.
6. **Real run:** dispatch once without `dry_run`. Confirm the video appears on the channel, a row lands in
   `data/video/published.json`, and the next scheduled slot does not duplicate it.
7. Tell the user about the private-upload catch below.

## Gotchas to tell the user

- Google forces API uploads to **private** until the API project passes its compliance audit, regardless of
  `video.privacy`. Until then each video lands private and must be made public by hand. Suggest submitting
  the audit after the first few uploads.
- Custom thumbnails need a verified channel; a thumbnail failure is a warning, not a failure.
- Upload quota: 1,600 of 10,000 daily units each, so two a day is fine.
- The site's editorial slant is political (Buchanan-style lens in `editorial.md`). The narration prompt
  (`video_editorial.md`) only reads the headlines and sources and bars invention. If Boring Money is a
  neutral finance channel, the user may want the story mix tilted toward markets and household money.
  That is a question for them, not settled.
- Description says the voice is synthetic; `video.synthetic_disclosure: true` also sets YouTube's label.

## Rules for the user relationship

- The user is not technical and said they do not know what a PR is. Explain in plain words, one step at a
  time, and wait for "done" after each.
- They asked for "full access so it isn't held up when I'm not at my laptop". That is satisfied by the
  GitHub-hosted automation once the secrets exist. Never ask for or accept their Google password or login
  codes in chat.
- Report outcomes faithfully. Do not say a video was posted until `published.json` and the channel show it.
