---
name: sales-csv-totals
description: Use when computing totals from our sales CSV files (units of the amount column, rounding, report format).
version: 1.0.0
---
# Sales CSV totals

Learned while reconciling the Q2 export with the user.

- The `amount` column in sales CSV exports is stored in cents.
- Sum the column, then divide by 100 to get dollars before reporting.
- Report the total as a plain number with two decimals, for example `12.34`.
