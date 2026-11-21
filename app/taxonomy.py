"""Synthetic attribute vocabulary.

content_category IDs are readable stand-ins modelled on the two-tier shape of the IAB Tech Lab
Content Taxonomy (tier-1 vertical + tier-2 sub-topic). They are NOT official IAB IDs.
device_type follows the spirit of the AdCOM device-type list, collapsed to four values.
"""

TIER1 = {
    "IAB-SPORTS": ["IAB-SPORTS-SOCCER", "IAB-SPORTS-CRICKET", "IAB-SPORTS-NFL"],
    "IAB-ENTERTAINMENT": ["IAB-ENT-MOVIES", "IAB-ENT-MUSIC", "IAB-ENT-TV"],
    "IAB-NEWS": ["IAB-NEWS-POLITICS", "IAB-NEWS-WORLD", "IAB-NEWS-LOCAL"],
    "IAB-TECH": ["IAB-TECH-MOBILE", "IAB-TECH-SOFTWARE"],
    "IAB-FINANCE": ["IAB-FIN-INVESTING", "IAB-FIN-BANKING"],
    "IAB-TRAVEL": ["IAB-TRAVEL-AIR", "IAB-TRAVEL-HOTELS"],
    "IAB-FOOD": ["IAB-FOOD-RECIPES", "IAB-FOOD-DINING"],
    "IAB-AUTOS": ["IAB-AUTOS-EV", "IAB-AUTOS-REVIEWS"],
    "IAB-HEALTH": ["IAB-HEALTH-FITNESS", "IAB-HEALTH-NUTRITION"],
    "IAB-GAMING": ["IAB-GAMING-MOBILE", "IAB-GAMING-CONSOLE"],
}
CATEGORY_LABELS = {k: k.removeprefix("IAB-").replace("-", " ").title() for k in TIER1}
for _children in TIER1.values():
    for _c in _children:
        CATEGORY_LABELS[_c] = _c.removeprefix("IAB-").replace("-", " ").title()

CATEGORIES = sorted(CATEGORY_LABELS)
COUNTRIES = ["US", "IN", "GB", "CA", "DE", "FR", "AU", "BR", "JP", "MX", "ES"]
DEVICE_TYPES = ["mobile", "desktop", "tablet", "ctv"]
PLACEMENT_TYPES = ["display", "native", "video"]
PUBLISHERS = [f"pub_{i}" for i in range(101, 141)]

ALLOWED_VALUES = {
    "content_category": set(CATEGORIES),
    "country": set(COUNTRIES),
    "device_type": set(DEVICE_TYPES),
    "placement_type": set(PLACEMENT_TYPES),
    "publisher_id": set(PUBLISHERS),
}
MULTI_VALUED = {"content_category"}
