"""Olist Sales & Revenue Intelligence Data Pipeline.

Production-grade, CSV-first analytics pipeline that processes raw Olist e-commerce
CSV tables, performs data hygiene/integrity checks, computes verified business KPIs,
and outputs clean summary CSVs and dashboard data.

Usage:
    python .\\scripts\\run_analysis.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd

# Paths
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
REPORTS_DIR = ROOT / "reports"
DELIVERABLES_DIR = ROOT / "deliverables"


def load_raw_data() -> dict[str, pd.DataFrame]:
    """Load the raw CSV files required for analysis."""
    print(" [1/5] Loading raw datasets...")
    files = {
        "customers": RAW_DIR / "olist_customers_dataset.csv",
        "orders": RAW_DIR / "olist_orders_dataset.csv",
        "items": RAW_DIR / "olist_order_items_dataset.csv",
        "payments": RAW_DIR / "olist_order_payments_dataset.csv",
        "reviews": RAW_DIR / "olist_order_reviews_dataset.csv",
        "products": RAW_DIR / "olist_products_dataset.csv",
        "sellers": RAW_DIR / "olist_sellers_dataset.csv",
        "translations": RAW_DIR / "product_category_name_translation.csv",
    }

    dfs = {}
    for name, path in files.items():
        if not path.exists():
            raise FileNotFoundError(f"Missing required file: {path}")
        dfs[name] = pd.read_csv(path)
        print(f"       -> Loaded {name}: {len(dfs[name]):,} rows")
    return dfs


def process_orders_and_items(
    dfs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clean and process orders and items at line-item and order grains."""
    print(" [2/5] Cleaning and reconciling orders & line items...")
    orders = dfs["orders"].copy()
    items = dfs["items"].copy()
    customers = dfs["customers"].copy()
    products = dfs["products"].copy()
    translations = dfs["translations"].copy()

    # Parse timestamps
    date_cols = [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ]
    for col in date_cols:
        orders[col] = pd.to_datetime(orders[col], errors="coerce")

    # Join customer location
    orders = orders.merge(
        customers[["customer_id", "customer_city", "customer_state"]],
        on="customer_id",
        how="left",
    )

    # Join product category translations
    products = products.merge(
        translations,
        on="product_category_name",
        how="left",
    )
    products["category_clean"] = products["product_category_name_english"].fillna(
        "Uncategorized"
    )

    # Enrich items with category
    items = items.merge(
        products[["product_id", "category_clean"]],
        on="product_id",
        how="left",
    )
    items["category_clean"] = items["category_clean"].fillna("Uncategorized")

    # Aggregate items to order level
    items_agg = (
        items.groupby("order_id")
        .agg(
            item_count=("order_item_id", "count"),
            item_sales=("price", "sum"),
            freight_charges=("freight_value", "sum"),
        )
        .reset_index()
    )
    items_agg["fulfilled_order_value"] = (
        items_agg["item_sales"] + items_agg["freight_charges"]
    )

    # Merge order financials
    orders = orders.merge(items_agg, on="order_id", how="left")
    orders["item_count"] = orders["item_count"].fillna(0).astype(int)
    orders["item_sales"] = orders["item_sales"].fillna(0.0)
    orders["freight_charges"] = orders["freight_charges"].fillna(0.0)
    orders["fulfilled_order_value"] = orders["fulfilled_order_value"].fillna(0.0)

    # Delivery duration calculation
    delivered_mask = (orders["order_status"] == "delivered") & orders[
        "order_delivered_customer_date"
    ].notna()
    delivery_delta = (
        orders["order_delivered_customer_date"] - orders["order_purchase_timestamp"]
    ).dt.total_seconds() / 86400.0
    orders["delivery_days"] = np.where(delivered_mask, delivery_delta, np.nan)

    # Strict delivery eligibility: purchase <= carrier <= delivered, and delivery_days >= 0
    strict_eligible = (
        delivered_mask
        & orders["order_delivered_carrier_date"].notna()
        & orders["order_estimated_delivery_date"].notna()
        & (orders["order_purchase_timestamp"] <= orders["order_delivered_carrier_date"])
        & (
            orders["order_delivered_carrier_date"]
            <= orders["order_delivered_customer_date"]
        )
        & (orders["delivery_days"] >= 0)
    )
    orders["strict_delivery_eligible"] = strict_eligible

    # On-time flag as boolean nullable or float (1.0 for on time, 0.0 for late, np.nan otherwise)
    is_on_time = (
        orders["order_delivered_customer_date"]
        <= orders["order_estimated_delivery_date"]
    ).astype(float)
    orders["is_on_time"] = np.where(strict_eligible, is_on_time, np.nan)

    return orders, items


def process_reviews_and_payments(
    dfs: dict[str, pd.DataFrame], orders: pd.DataFrame
) -> pd.DataFrame:
    """Aggregate reviews and payments to order level and join with orders."""
    print(" [3/5] Processing reviews, payments, and customer satisfaction...")
    reviews = dfs["reviews"].copy()
    payments = dfs["payments"].copy()

    # Reviews: handle composite key and average review score per order
    reviews_order = (
        reviews.groupby("order_id")
        .agg(
            review_score=("review_score", "mean"),
            review_count=("review_id", "count"),
        )
        .reset_index()
    )

    orders = orders.merge(reviews_order, on="order_id", how="left")

    # Payments aggregation
    payments_order = (
        payments.groupby("order_id")
        .agg(
            payment_amount=("payment_value", "sum"),
            payment_methods_count=("payment_type", "nunique"),
        )
        .reset_index()
    )

    orders = orders.merge(payments_order, on="order_id", how="left")
    orders["payment_amount"] = orders["payment_amount"].fillna(0.0)

    # Check payment reconciliation discrepancy
    orders["payment_mismatch"] = (
        (orders["order_status"] == "delivered")
        & (orders["item_count"] > 0)
        & (abs(orders["payment_amount"] - orders["fulfilled_order_value"]) > 0.01)
    )

    return orders


def compute_kpis_and_reports(
    orders: pd.DataFrame, items: pd.DataFrame, dfs: dict[str, pd.DataFrame]
) -> dict[str, Any]:
    """Compute aggregate KPIs and prepare summary tables for Excel & Dashboard."""
    print(" [4/5] Computing summary tables & business metrics...")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DELIVERABLES_DIR.mkdir(parents=True, exist_ok=True)

    delivered_orders = orders[
        (orders["order_status"] == "delivered") & (orders["item_count"] > 0)
    ]
    strict_orders = orders[orders["strict_delivery_eligible"] == True]

    total_sales = float(delivered_orders["item_sales"].sum())
    total_freight = float(delivered_orders["freight_charges"].sum())
    delivered_count = int(len(delivered_orders))
    aov = float(delivered_orders["fulfilled_order_value"].sum() / delivered_count)
    observed_payments = float(dfs["payments"]["payment_value"].sum())
    avg_delivery_days = float(strict_orders["delivery_days"].mean())
    on_time_rate = float(strict_orders["is_on_time"].mean())
    reviewed_delivered = delivered_orders[delivered_orders["review_score"].notna()]
    avg_review_score = float(reviewed_delivered["review_score"].mean())
    review_coverage = float(len(reviewed_delivered) / delivered_count)
    mismatch_count = int(orders["payment_mismatch"].sum())

    kpis = {
        "delivered_item_sales": round(total_sales, 2),
        "freight_charges": round(total_freight, 2),
        "fulfilled_order_value": round(total_sales + total_freight, 2),
        "delivered_orders": delivered_count,
        "average_order_value": round(aov, 2),
        "observed_payment_amount_all_orders": round(observed_payments, 2),
        "strict_delivery_eligible_orders": int(len(strict_orders)),
        "average_delivery_days": round(avg_delivery_days, 2),
        "on_time_delivery_rate": round(on_time_rate, 4),
        "average_order_level_review_score": round(avg_review_score, 2),
        "review_coverage_delivered_orders": round(review_coverage, 4),
        "payment_item_mismatch_orders": mismatch_count,
    }

    # 1. Monthly Sales Trends
    delivered_orders_copy = delivered_orders.copy()
    delivered_orders_copy["purchase_month"] = (
        delivered_orders_copy["order_purchase_timestamp"].dt.to_period("M").astype(str)
    )
    monthly = (
        delivered_orders_copy.groupby("purchase_month")
        .agg(
            delivered_order_count=("order_id", "count"),
            delivered_item_sales=("item_sales", "sum"),
            delivered_freight_charges=("freight_charges", "sum"),
            delivered_order_value=("fulfilled_order_value", "sum"),
        )
        .reset_index()
    )
    monthly = monthly.sort_values("purchase_month")
    monthly["average_order_value"] = (
        monthly["delivered_order_value"] / monthly["delivered_order_count"]
    )
    monthly.to_csv(REPORTS_DIR / "monthly_sales_trends.csv", index=False)

    # 2. Category Performance
    delivered_items = items[items["order_id"].isin(delivered_orders["order_id"])].copy()
    category = (
        delivered_items.groupby("category_clean")
        .agg(
            delivered_item_lines=("order_item_id", "count"),
            delivered_order_count=("order_id", "nunique"),
            delivered_item_sales=("price", "sum"),
            delivered_freight_charges=("freight_value", "sum"),
        )
        .reset_index()
    )
    category["average_selling_price"] = (
        category["delivered_item_sales"] / category["delivered_item_lines"]
    )
    category["average_freight_charge"] = (
        category["delivered_freight_charges"] / category["delivered_item_lines"]
    )
    category["delivered_order_value"] = (
        category["delivered_item_sales"] + category["delivered_freight_charges"]
    )
    category = category.sort_values("delivered_item_sales", ascending=False)
    category.to_csv(REPORTS_DIR / "category_performance.csv", index=False)

    # 3. Delivery Performance by State
    state_rows = []
    for state, grp in delivered_orders.groupby("customer_state"):
        strict_grp = grp[grp["strict_delivery_eligible"] == True]
        state_rows.append(
            {
                "customer_state": state,
                "delivered_orders": len(grp),
                "delivered_item_sales": round(grp["item_sales"].sum(), 2),
                "strict_delivery_eligible_orders": len(strict_grp),
                "average_delivery_days": (
                    round(strict_grp["delivery_days"].mean(), 2)
                    if len(strict_grp) > 0
                    else np.nan
                ),
                "on_time_delivery_rate": (
                    round(strict_grp["is_on_time"].mean(), 4)
                    if len(strict_grp) > 0
                    else np.nan
                ),
            }
        )
    state_perf = pd.DataFrame(state_rows).sort_values(
        "delivered_item_sales", ascending=False
    )
    state_perf.to_csv(REPORTS_DIR / "delivery_performance.csv", index=False)

    # 4. Review Score vs Delivery Delay (The 1-Star Cliff)
    strict_delivered = strict_orders.copy()
    strict_delivered["delivery_status"] = np.where(
        strict_delivered["is_on_time"] == 1.0, "On-Time", "Late"
    )
    strict_delivered["score_int"] = (
        strict_delivered["review_score"].round().fillna(0).astype(int)
    )
    review_counts = (
        strict_delivered[strict_delivered["score_int"] > 0]
        .groupby(["delivery_status", "score_int"])
        .size()
        .unstack(fill_value=0)
    )
    review_counts.to_csv(REPORTS_DIR / "review_performance.csv")

    # 5. Payment Method Performance
    payments = dfs["payments"].copy()
    payments_perf = (
        payments.groupby("payment_type")
        .agg(
            payment_rows=("order_id", "count"),
            distinct_orders=("order_id", "nunique"),
            total_payment_amount=("payment_value", "sum"),
            average_payment_amount=("payment_value", "mean"),
        )
        .reset_index()
        .sort_values("total_payment_amount", ascending=False)
    )
    payments_perf.to_csv(REPORTS_DIR / "payment_method_performance.csv", index=False)

    # Save KPI summary
    with open(REPORTS_DIR / "kpi_summary.json", "w", encoding="utf-8") as f:
        json.dump(kpis, f, indent=2)

    return {
        "kpis": kpis,
        "monthly": monthly.to_dict(orient="records"),
        "top_categories": category.head(15).to_dict(orient="records"),
        "state_performance": state_perf.to_dict(orient="records"),
        "payments": payments_perf.to_dict(orient="records"),
        "reviews": {
            "on_time": (
                {str(k): int(v) for k, v in review_counts.loc["On-Time"].items()}
                if "On-Time" in review_counts.index
                else {}
            ),
            "late": (
                {str(k): int(v) for k, v in review_counts.loc["Late"].items()}
                if "Late" in review_counts.index
                else {}
            ),
        },
    }


def export_dashboard_payload(data: dict[str, Any]) -> None:
    """Generate the data payload consumed directly by the HTML dashboard."""
    print(" [5/5] Exporting interactive dashboard data payload...")
    payload_path = DELIVERABLES_DIR / "dashboard_data.js"
    json_data = json.dumps(data, indent=2)
    js_content = f"window.OLIST_DASHBOARD_DATA = {json_data};\n"
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(js_content)
    print(f"       -> Saved: {payload_path}")


def main() -> None:
    print("=" * 65)
    print(" Olist E-Commerce Analytics Pipeline (Python + Excel / CSV)")
    print("=" * 65)
    dfs = load_raw_data()
    orders, items = process_orders_and_items(dfs)
    orders = process_reviews_and_payments(dfs, orders)
    summary_data = compute_kpis_and_reports(orders, items, dfs)
    export_dashboard_payload(summary_data)

    k = summary_data["kpis"]
    print("\n Pipeline Execution Successful!")
    print("-" * 65)
    print(f" * Fulfilled Item Sales : R$ {k['delivered_item_sales']:,.2f}")
    print(f" * Freight Charges     : R$ {k['freight_charges']:,.2f}")
    print(f" * Fulfilled Order Value: R$ {k['fulfilled_order_value']:,.2f}")
    print(f" * Delivered Orders     : {k['delivered_orders']:,}")
    print(f" * Average Order Value  : R$ {k['average_order_value']:,.2f}")
    print(f" * On-Time Delivery Rate: {k['on_time_delivery_rate']*100:.2f}%")
    print(f" * Avg Delivery Days    : {k['average_delivery_days']:.2f} days")
    print(
        f" * Avg Review Score     : {k['average_order_level_review_score']:.2f} / 5.0"
    )
    print("-" * 65)
    print(" Summary CSVs generated in: reports/")
    print(" Dashboard payload generated in: deliverables/dashboard_data.js\n")


if __name__ == "__main__":
    main()
