"""The server's first query for each table (design §2): every table has a base even when the agent
stops early. Built from the fields discovery found in this model, never from fixed names."""

from app.agent.models import Aggregation
from app.reports.sales.discovery import SALES_MEASURES, ModelRoles
from app.reports.sales.lenses import Lens
from app.reports.sales.steps import LensStep


def seed_step(lens: Lens, roles: ModelRoles) -> LensStep | None:
    """None when this model has no fields for the table."""
    sales = [roles.measures[m] for m in SALES_MEASURES if m in roles.measures]
    columns, measures = roles.columns, roles.measures
    model_id = roles.model.dataset_id
    if roles.date_column is None:
        return None

    if lens == "lob_growth" and sales and "lob" in columns:
        return LensStep(
            model_id=model_id,
            measures=sales,  # value, weight, quantity: whichever the model has
            group_by=[columns["lob"]],
            periods=["day", "day_ly", "mtd", "mtd_ly", "fytd", "fytd_ly"],
            order="value_desc",
            why="Every LOB against last year",
        )
    if lens == "subcategory_loss" and sales and "subcategory" in columns:
        return LensStep(
            model_id=model_id,
            measures=sales[:1],
            group_by=[columns["subcategory"]],
            periods=["mtd", "mtd_ly", "fytd", "fytd_ly", "months_fy", "months_fy_ly"],
            why="Every sub-category against last year, month by month",
        )
    if lens == "stock_turn":
        group = [columns[c] for c in ("lob", "subcategory") if c in columns][:1]
        if "stock_turn" in measures:
            return LensStep(
                model_id=model_id,
                measures=[measures["stock_turn"]],
                group_by=group,
                periods=["fytd", "fytd_ly"],
                why="Stock turn this financial year against last year",
            )
        pair = _sales_and_stock(measures)
        if pair is not None:
            return LensStep(
                model_id=model_id,
                measures=list(pair),
                group_by=group,
                periods=["fytd", "month_ends_fy"],
                why="Sales this financial year and stock at each month end",
            )
        return None
    if lens == "price_range" and sales:
        if "price_band" in columns:
            return LensStep(
                model_id=model_id,
                measures=sales[:1],
                group_by=[columns["price_band"]],
                periods=["mtd", "mtd_ly", "fytd", "fytd_ly"],
                order="group_asc",
                why="Sales by price band against last year",
            )
        if "price" in columns:
            return LensStep(
                model_id=model_id,
                aggregations=[
                    Aggregation(column=columns["price"], function=f)
                    for f in ("min", "max", "average")
                ],
                periods=["fytd"],
                why="Lowest, highest and average price this financial year",
            )
    return None


def _sales_and_stock(measures: dict[str, str]) -> tuple[str, str] | None:
    """Sales and stock in the same unit (value with value, weight with weight)."""
    for sales, stock in (("sales_value", "stock_value"), ("sales_weight", "stock_weight")):
        if sales in measures and stock in measures:
            return measures[sales], measures[stock]
    sold = next((measures[m] for m in SALES_MEASURES if m in measures), None)
    held = next((measures[m] for m in ("stock_value", "stock_weight") if m in measures), None)
    return (sold, held) if sold and held else None
