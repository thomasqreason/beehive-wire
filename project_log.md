# Project log

This is the single source of truth for this project. Newest entries are at the bottom.

This repository holds two things:
- **Beehive Wire**, the twice-daily news front page. It is built automatically by GitHub Actions; see `README.md`.
- **The `utahhomesguide/` folder**, which holds stand-alone rental pages for the owner's duplex at 1077 E First Ave, Salt Lake City. They will be uploaded to utahhomesguide.com.

---

## 2026-10-07 — 1077A / 1077B furnished rental pages

**Worked on**
- Built two shareable rental pages, one per unit, to send to prospective tenants as a link:
  - **utahhomesguide.com/1077A** is the front unit.
  - **utahhomesguide.com/1077B** is the rear unit.
- Files are in `utahhomesguide/` on branch `claude/utah-homes-guide-units-1uml2r`:
  - `build_units.py` holds each unit's text, price and amenities. Edit it and run `python build_units.py` to regenerate the pages.
  - `unit_template.html` is the shared page layout.
  - `1077A/index.html` and `1077B/index.html` are the generated pages.
  - `1077A-upload.zip` and `1077B-upload.zip` are ready to upload.
  - `README.md` has the step-by-step for adding photos and going live.
- Each page has:
  - A large cover photo, a photo grid and a full-screen swipeable viewer.
  - A description, "What's included", the neighborhood with approximate distances, and Text/Call/Email buttons.
  - A link to the other unit.
- Private preview links (only the owner can open them unless shared):
  - 1077A: https://claude.ai/artifact/5Qh9yaFnT54mSz5VWfLNSp
  - 1077B: https://claude.ai/artifact/PV8tDvovMA9JuYnMEkN7Nk
- Added `CLAUDE.md`, which tells Claude to update this log at the end of every session, and created this log.

**Decisions and why**
- **Plain static folders, not WordPress pages.** utahhomesguide.com is WordPress on SiteGround. A real folder in `public_html` is served before WordPress, so uploading `1077A/` gives the exact URL wanted with no WordPress changes.
- **Photos are loaded by filename.** The page shows whatever exists among `photos/01.jpg` to `photos/30.jpg`, in order, and `01.jpg` is the cover and link preview. The owner can add or remove photos without touching code.
- **The pages are hidden from Google (noindex).** They're for sending by link, and keeping them out of search stops them mixing with the directory's SEO.
- **The street address is not shown**, only "Lower Avenues". The address is given when a showing is booked.
- **Uppercase URLs (/1077A).** These match the request. The lowercase `/1077a` won't work. No lowercase redirect was added, because folders that differ only by case collide on Windows.
- **The page text was kept to facts found in Drive.** Unverified claims were removed, for example "own entrance" and who has rented there.

**Status**
- Both pages are built, tested on desktop and phone sizes, committed and pushed.
- **Not live yet.** Nothing has been uploaded to utahhomesguide.com.
- **No real photos yet.** Both pages say "Photos coming soon".

**Blockers and open questions**
- **Photos.** The ~20 front-unit photos picked earlier, the Furnished Finder photos and the unpublished KSL draft description are on the owner's PC (`Downloads/ff`) or in the KSL account. The cloud session couldn't reach any of them: Furnished Finder, KSL and utahhomesguide.com are blocked by this environment's network, and none of these files are in Google Drive. The owner said `Downloads/ff` is **not** unit A.
- **Prices.**
  - 1077B shows **$1,800/month**. That is the drop recommended on the Aug 25 punch list, and the owner hasn't confirmed it.
  - 1077A shows "Ask for monthly rate" because no price is known.
- **Unit details are unknown** for both units: bedrooms, bathrooms, square footage, parking and laundry. Drive has conflicting square footage, so none is shown.
- **The amenities list needs checking**: fiber internet, cookware, linens and towels.
- **Which unit is A?** A = front and B = rear was assumed, and the owner's later messages fit that.

**Next steps**
1. Get the photos to Claude: attach them in chat or drag the folders into Google Drive. Also send the KSL description and the front-unit rate.
2. Put the KSL wording, the rates and the unit details into `utahhomesguide/build_units.py`, rebuild, and re-make the zips.
3. Upload to SiteGround. In Site Tools → File Manager → `public_html`, upload each zip and extract it, add the photos to `1077A/photos` and `1077B/photos`, then purge the cache.
4. Open https://utahhomesguide.com/1077A/ and /1077B/ on a phone to check them, then send the links to tenants.
