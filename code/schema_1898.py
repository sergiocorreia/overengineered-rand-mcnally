"""Schema for the 1898-1909 Rand McNally directory.

Changes vs. schema_1894:
- "Surplus" and "Undivided Profits" consolidated into a single "Surplus and Profits"
  column (stored in `surplus`). `undivided_profits` is therefore removed.
- Three new balance-sheet columns in the main table:
  - `deposits`
  - `loans` (header: "Loans and Discounts, Stocks and Securities")
  - `cash`  (header: "Cash and Exchanges")
- New symbols in the Name-of-Bank cell:
  - `§` state bank (`is_state`)
  - `†` private bank (`is_private`)
  - Established year at end of cell (`established_year`)
- New symbol in the Town cell: `▲` county seat (`is_county_seat`)

Reserve-city sub-tables (NYC commercial, Trust Cos, Savings banks, Private
bankers, Investment bankers, Foreign Banking Agencies, Stock Exchange Brokers)
use the shared `bank_type` field plus the optional fields documented in
prompt_1898.md. The NYC commercial sub-table adds four balance-sheet columns
modeled as optional fields (`due_to_banks`, `individual_deposits`, `bonds`,
`due_from_banks`).

This schema is used for directory editions 1898 through 1909 (ABA/state-banking
association symbols and charter numbers did not appear until 1910).
"""

from typing import Literal

from pydantic import BaseModel, Field

from schema_1911 import Correspondent


BankType = Literal[
    "commercial",
    "savings",
    "trust",
    "private",
    "investment",
    "broker",
    "foreign",
]


class Bank(BaseModel):
    state_abbreviation: str | None = Field(None, description="Two-digit state abbreviation detected from table header (e.g., RI)")

    # Location Info (Column 1)
    town_pop: int | None = Field(None, description="Population of the town (printed after the town name)")
    town_name: str | None = Field(None, description="Name of the town (for NYC-style sub-tables this column is a Street instead; see `address`)")
    county_name: str | None = Field(None, description="County name")
    is_county_seat: bool = Field(False, description="True if a triangle (▲) appears at the start of the town cell, indicating the town is the county seat")

    # Bank Identity (Column 2)
    bank_name: str = Field(..., description="Legal name of the bank")
    old_name: str | None = Field(None, description="Former name of the bank, e.g. 'Valley Bank' from '(Formerly Valley Bank)'")
    address: str | None = Field(None, description="Street address for NYC/reserve-city entries where the address is in its own column ('Location') or under the bank name on a second line")
    bank_type: BankType = Field("commercial", description="Sub-table the bank comes from: commercial (default), savings, trust, private, investment, broker, foreign")
    established_year: int | None = Field(None, description="Year established (YYYY or YY). A 2-digit value is preceded by an apostrophe (e.g. \"'89\") and should be stored as the 2-digit integer (not expanded).")
    classification: str | None = Field(None, description="Classification for private/investment banker rows (e.g. 'Investment Securities', 'Commercial Paper')")
    branch_of: str | None = Field(None, description="For Foreign Banking Agencies: the parent bank listed under 'Branch of'")

    # Boolean Flags (Symbols)
    ch_member: int = Field(0, description="1 for Member (*), 2 for Affiliate (+), 0 otherwise")
    is_state: bool = Field(False, description="True if § appears at the end of the Name-of-Bank cell (state bank)")
    is_private: bool = Field(False, description="True if † appears at the end of the Name-of-Bank cell (private bank)")

    # Officers (Columns 3-6)
    president: str | None = Field(None, description="Name of the President")
    vice_president: str | None = Field(None, description="Name of the Vice President")
    cashier: str | None = Field(None, description="Name of the Cashier")
    assistant_cashier: str | None = Field(None, description="Name of the Assistant Cashier")
    secretary: str | None = Field(None, description="Name of the Secretary (savings banks sub-table)")
    treasurer: str | None = Field(None, description="Name of the Treasurer (savings banks sub-table)")
    manager: str | None = Field(None, description="Manager or Agent (Foreign Banking Agencies sub-table)")

    # Bank Events
    event: str | None = Field(None, description="Event such as 'Liquidation', 'Merger with Bank XYZ', 'Closed for liquidation'")
    event_date: str | None = Field(None, description="Date of the event in YYYY-MM-DD format")

    # Liabilities & Assets (main table: 5 balance-sheet columns)
    is_branch: bool = Field(False, description="True if this is a branch of another bank")
    capital: float | None = Field(None, description="Paid-Up Capital")
    surplus: float | None = Field(None, description="Surplus and Profits")
    deposits: float | None = Field(None, description="Deposits (main table) or 'Deposits' column in Trust/Savings sub-tables")
    loans: float | None = Field(None, description="Loans and Discounts, Stocks and Securities (main table). In NYC commercial sub-table: Loans and Discounts only")
    cash: float | None = Field(None, description="Cash and Exchanges")

    # NYC commercial sub-table extra balance-sheet fields (optional)
    due_to_banks: float | None = Field(None, description="Due to Banks (NYC commercial sub-table)")
    individual_deposits: float | None = Field(None, description="Individual Deposits (NYC commercial sub-table)")
    bonds: float | None = Field(None, description="Bonds, Stocks, etc. (NYC commercial sub-table, when listed as a separate column)")
    due_from_banks: float | None = Field(None, description="Due from Banks (NYC commercial sub-table, separate from Cash and Exchanges)")

    # Correspondents (last column)
    correspondents: list[Correspondent] = Field(default_factory=list, description="List of principal correspondent banks")
    correspondents_raw: str | None = Field(None, description="Raw string underlying the list of principal correspondent banks")

    # Miscellaneous
    misc_notes: str | None = Field(None, description="Additional row text including map grid (e.g., 'A 1'), branch notes, or advertising.")


class Page(BaseModel):
    is_advertisment: bool = Field(..., description="Page only contains advertisements, maps, or other information")
    banks: list[Bank] = Field(..., description="List of banks found on the page")
