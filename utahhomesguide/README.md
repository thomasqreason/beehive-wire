# utahhomesguide.com/1077A and /1077B

Two stand-alone pages for the furnished units at 1077 E First Ave, one link per unit.

- **1077A**: the front unit
- **1077B**: the rear unit

## Change the text or price
Edit `UNITS` in `build_units.py` (the rent, description and amenities), then run `python build_units.py`.
The layout is in `unit_template.html`.

## Add photos
Put the photos in `1077A/photos/` and `1077B/photos/`, named `01.jpg`, `02.jpg` … up to `30.jpg`.
`01.jpg` is the cover photo and link preview. The page shows every number that exists, in order,
so you don't need to change any code to add or remove a photo. Resize them to about 1600px wide first.

## Put it live (SiteGround)
Site Tools → Site → File Manager → `public_html`. Upload the `1077A` and `1077B` folders, including their `photos` folders.
Real folders take priority over WordPress, so the pages show up at https://utahhomesguide.com/1077A/ and /1077B/.
Then purge the SiteGround cache.

Both pages are marked `noindex`, so they don't appear in Google or mix with the directory. They're only for sharing by link.
