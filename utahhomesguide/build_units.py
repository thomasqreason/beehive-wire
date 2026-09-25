"""Build the two rental pages for utahhomesguide.com/1077A and /1077B.

Edit the UNITS dict below, then run:  python build_units.py
Output: 1077A/index.html and 1077B/index.html.
Upload those folders to the site's web root (public_html) on SiteGround.
Photos go in 1077A/photos/ and 1077B/photos/ named 01.jpg, 02.jpg, ... 30.jpg.
The page shows whichever numbers exist, in order; 01.jpg is the cover photo.
"""
import html
import json
import pathlib
from urllib.parse import quote

HERE = pathlib.Path(__file__).parent

CONTACT = {
    "phone": "8017479307",
    "phone_display": "(801) 747-9307",
    "email": "hello@utahhomesguide.com",
}

SHARED_AMENITIES = [
    "Fully furnished, move-in ready",
    "Fiber internet included",
    "Kitchen stocked with cookware and dishes",
    "Bed linens and towels provided",
    "Monthly stays, 30 days and up",
    "Walkable, tree-lined Avenues street",
]

NEARBY = [
    ("LDS Hospital", "about 1 mile"),
    ("University of Utah & University Hospital", "about 1.5 miles"),
    ("Downtown & City Creek Center", "about 1.5 miles"),
    ("Memory Grove & City Creek Canyon trails", "under 1 mile"),
    ("Salt Lake City International Airport", "about 15 minutes by car"),
]

UNITS = {
    "1077A": {
        "name": "The Front Unit",
        "tagline": "A furnished home in a century-old Avenues duplex",
        "rent": "",  # e.g. "$2,400 / month" — blank shows "Ask for monthly rate"
        "facts": ["Furnished", "Front unit", "Monthly stays"],
        "description": [
            "The front half of a 1910s duplex in the Lower Avenues, one of Salt Lake City's oldest and most walkable neighborhoods. "
            "It is the larger of the two units, set up for someone who wants a real home for a few months rather than a hotel room.",
            "Everything is here when you arrive: furniture, a working kitchen, linens and fast fiber internet. "
            "Bring your suitcase and your laptop. It suits travel nurses at the nearby hospitals, visiting faculty, "
            "people relocating to Salt Lake who want to learn the city before they buy, and anyone between homes.",
        ],
        "amenities": SHARED_AMENITIES,
        "other": ("1077B", "Looking for something smaller? See the rear unit"),
    },
    "1077B": {
        "name": "The Rear Unit",
        "tagline": "A private furnished retreat behind a century-old Avenues duplex",
        "rent": "$1,800 / month",
        "facts": ["Furnished", "Rear unit", "Monthly stays"],
        "description": [
            "The rear unit of a 1910s duplex in the Lower Avenues, tucked behind the main house and set back from the street. "
            "It is compact, quiet and private.",
            "It comes fully furnished with a working kitchen, linens and fast fiber internet, so you can move in with a suitcase. "
            "It has been rented as a furnished monthly rental since 2024.",
        ],
        "amenities": SHARED_AMENITIES,
        "other": ("1077A", "Need more room? See the front unit"),
    },
}

MAX_PHOTOS = 30


def render(slug, u):
    e = html.escape
    title = f"{u['name']} — Furnished Rental in the Salt Lake City Avenues"
    rent = u["rent"] or "Ask for monthly rate"
    sms_body = f"Hi, I'm interested in {slug} ({u['name']}). Is it available?"
    mail_subject = f"Inquiry: {slug} furnished rental"
    other_slug, other_text = u["other"]
    desc = " ".join(u["description"])[:155]
    fields = dict(
        title=e(title),
        meta_desc=e(desc),
        slug=slug,
        name=e(u["name"]),
        tagline=e(u["tagline"]),
        rent=e(rent),
        facts="".join(f"<li>{e(f)}</li>" for f in u["facts"]),
        description="".join(f"<p>{e(p)}</p>" for p in u["description"]),
        amenities="".join(f"<li>{e(a)}</li>" for a in u["amenities"]),
        nearby="".join(f"<li><span>{e(a)}</span><span>{e(b)}</span></li>" for a, b in NEARBY),
        phone=CONTACT["phone"],
        phone_display=CONTACT["phone_display"],
        email=CONTACT["email"],
        sms_body=quote(sms_body),
        mail_subject=quote(mail_subject),
        other_slug=other_slug,
        other_text=e(other_text),
        max_photos=MAX_PHOTOS,
        name_js=json.dumps(u["name"]),
    )
    page = TEMPLATE
    for key, value in fields.items():
        page = page.replace("{{" + key + "}}", str(value))
    return page


TEMPLATE = (HERE / "unit_template.html").read_text(encoding="utf-8")

if __name__ == "__main__":
    for slug, unit in UNITS.items():
        out = HERE / slug
        (out / "photos").mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(render(slug, unit), encoding="utf-8")
        print("wrote", out / "index.html")
