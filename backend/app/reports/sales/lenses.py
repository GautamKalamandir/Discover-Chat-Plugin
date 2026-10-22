"""The four tables of a /sales-summary report (ADR 0016, design §2).

A lens is defined by what it shows and the fields it needs, never by a model or dashboard name: any
model whose schema has those fields can fill it.
"""

from typing import Literal, get_args

Lens = Literal["lob_growth", "subcategory_loss", "stock_turn", "price_range"]
LENSES: tuple[Lens, ...] = get_args(Lens)

TITLES: dict[Lens, str] = {
    "lob_growth": "LOB-wise growth and degrowth",
    "subcategory_loss": "Sub-category loss by store and month",
    "stock_turn": "Stock turn",
    "price_range": "Sales by price range",
}

# Short prefix of each lens in fact ids (e.g. "lob.2.r3.m1|mtd").
KEYS: dict[Lens, str] = {
    "lob_growth": "lob",
    "subcategory_loss": "sub",
    "stock_turn": "stock",
    "price_range": "price",
}
